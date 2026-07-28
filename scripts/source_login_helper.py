#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.58,<2",
# ]
# ///

"""Mac-only local browser helper for source-platform cookie capture.

The helper opens an isolated Chrome profile on the platform's official login
page. It never asks for or records an account password. After the user finishes
login, only cookies belonging to the selected platform are written locally in
Netscape format for yt-dlp.
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import logging
import secrets
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.source_login import (  # noqa: E402
    PLATFORM_LOGIN_SPECS,
    has_authenticated_session,
    normalize_platform,
    validate_local_return_url,
    verify_login_authorization,
    write_netscape_cookie_file,
)


LOGGER = logging.getLogger("source_login_helper")
TERMINAL_STATES = {"success", "failed", "timeout"}
ACTIVE_STATES = {"opening", "waiting"}


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _read_secret(secret_file: Path) -> str:
    secret = secret_file.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise RuntimeError("本机登录助手密钥无效，请重新安装登录助手")
    return secret


def _status_page(session_id: str, status_token: str, return_url: str, platform: str) -> str:
    label = PLATFORM_LOGIN_SPECS[platform]["label"]
    js_session = json.dumps(session_id)
    js_token = json.dumps(status_token)
    safe_return = html.escape(return_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{label}登录 - 视频搬运通道</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #172033; }}
    main {{ width: min(560px, calc(100% - 32px)); margin: 10vh auto; }}
    .card {{ background: #fff; border: 1px solid #e3e8f1; border-radius: 20px; padding: 28px;
      box-shadow: 0 18px 48px rgba(35, 55, 90, .09); }}
    .eyebrow {{ color: #2563eb; font-size: 13px; font-weight: 700; letter-spacing: .08em; }}
    h1 {{ margin: 10px 0 12px; font-size: 28px; }}
    p {{ color: #5a6478; line-height: 1.75; }}
    .steps {{ background: #f7faff; border-radius: 14px; padding: 16px 18px; line-height: 1.9; }}
    .status {{ margin-top: 18px; border-radius: 12px; padding: 14px 16px; background: #fff7db; color: #815d00; }}
    .status.success {{ background: #e9f8ef; color: #166534; }}
    .status.error {{ background: #fff0f0; color: #b42318; }}
    a {{ color: #2563eb; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <section class="card">
      <div class="eyebrow">视频搬运通道 · 本机安全登录</div>
      <h1>正在连接{label}</h1>
      <p>系统已打开一个独立的 Chrome 登录窗口。请只在{label}官方页面扫码或确认登录。</p>
      <div class="steps">
        1. 在新打开的窗口完成扫码/登录<br>
        2. 不要把密码、验证码或 Cookie 发给任何人<br>
        3. 登录成功后，本页会自动返回搬运中心
      </div>
      <div class="status" id="status">正在打开官方登录页面…</div>
      <p><a href="{safe_return}">取消并返回搬运中心</a></p>
    </section>
  </main>
  <script>
    const sessionId = {js_session};
    const statusToken = {js_token};
    const statusBox = document.getElementById('status');
    let stopped = false;
    async function poll() {{
      if (stopped) return;
      try {{
        const query = new URLSearchParams({{session_id: sessionId, token: statusToken}});
        const response = await fetch('/status?' + query.toString(), {{cache: 'no-store'}});
        const data = await response.json();
        statusBox.textContent = data.message || '正在等待登录…';
        statusBox.className = 'status';
        if (data.status === 'success') {{
          stopped = true;
          statusBox.classList.add('success');
          setTimeout(() => window.location.replace(data.return_url), 900);
          return;
        }}
        if (data.status === 'failed' || data.status === 'timeout') {{
          stopped = true;
          statusBox.classList.add('error');
          return;
        }}
      }} catch (error) {{
        statusBox.textContent = '登录助手暂时无法响应，请保持本页打开后重试。';
        statusBox.className = 'status error';
      }}
      setTimeout(poll, 1500);
    }}
    poll();
  </script>
</body>
</html>"""


class SourceLoginApplication:
    def __init__(
        self,
        *,
        cookie_dir: Path,
        profile_dir: Path,
        secret_file: Path,
        chrome_path: Path,
        timeout_seconds: int = 600,
        success_url: str = "",
    ):
        self.cookie_dir = cookie_dir.resolve()
        self.profile_dir = profile_dir.resolve()
        self.secret_file = secret_file.resolve()
        self.chrome_path = chrome_path.resolve()
        self.timeout_seconds = max(120, min(int(timeout_seconds), 1200))
        self.success_url = str(success_url or "").strip()
        if self.success_url:
            parsed_success = urlparse(self.success_url)
            if (
                parsed_success.scheme != "https"
                or parsed_success.hostname != "transfer.sg99.online"
                or parsed_success.username
                or parsed_success.password
            ):
                raise RuntimeError("登录成功返回地址无效")
        self.secret = _read_secret(self.secret_file)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def check_runtime(self) -> None:
        if not self.chrome_path.is_file():
            raise RuntimeError("未找到 Google Chrome，无法打开官方登录窗口")
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def _update(self, session_id: str, **changes: Any) -> None:
        with self._lock:
            item = self._sessions.get(session_id)
            if item:
                item.update(changes)
                item["updated_at"] = time.time()

    def start_session(self, platform: str, return_url: str) -> dict[str, Any]:
        normalized = normalize_platform(platform)
        safe_return = validate_local_return_url(return_url)
        with self._lock:
            for item in self._sessions.values():
                if item.get("status") in ACTIVE_STATES:
                    raise RuntimeError("已有一个登录窗口正在等待，请先完成或关闭它")
            session_id = str(uuid.uuid4())
            item = {
                "id": session_id,
                "status_token": secrets.token_urlsafe(32),
                "platform": normalized,
                "status": "opening",
                "message": "正在打开官方登录页面…",
                "return_url": self.success_url or safe_return,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self._sessions[session_id] = item
        worker = threading.Thread(
            target=self._capture_login,
            args=(session_id,),
            name=f"source-login-{normalized}",
            daemon=True,
        )
        worker.start()
        return dict(item)

    def get_status(self, session_id: str, status_token: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._sessions.get(session_id)
            if not item or not hmac.compare_digest(
                str(item.get("status_token") or ""),
                str(status_token or ""),
            ):
                return None
            return {
                "status": item["status"],
                "message": item["message"],
                "return_url": item["return_url"],
            }

    def _capture_login(self, session_id: str) -> None:
        with self._lock:
            item = dict(self._sessions[session_id])
        platform = item["platform"]
        spec = PLATFORM_LOGIN_SPECS[platform]
        profile_path = self.profile_dir / platform
        cookie_path = self.cookie_dir / spec["cookie_filename"]
        context = None
        try:
            from playwright.sync_api import sync_playwright

            self._update(session_id, message=f"正在打开{spec['label']}官方登录页面…")
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_path),
                    executable_path=str(self.chrome_path),
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                    viewport={"width": 1120, "height": 820},
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(spec["login_url"], wait_until="domcontentloaded", timeout=60000)
                self._update(
                    session_id,
                    status="waiting",
                    message=f"请在新窗口完成{spec['label']}扫码或登录确认…",
                )
                deadline = time.monotonic() + self.timeout_seconds
                while time.monotonic() < deadline:
                    cookies = context.cookies()
                    if has_authenticated_session(cookies, platform):
                        count = write_netscape_cookie_file(cookies, platform, cookie_path)
                        LOGGER.info("%s登录成功，已保存%d条平台Cookie", spec["label"], count)
                        self._update(
                            session_id,
                            status="success",
                            message=f"{spec['label']}登录成功，Cookie 已安全保存到本机",
                        )
                        time.sleep(1)
                        return
                    time.sleep(2)
                self._update(
                    session_id,
                    status="timeout",
                    message="登录等待已超时，请返回搬运中心后重新发起",
                )
        except Exception as exc:
            LOGGER.exception("%s本机登录失败", spec["label"])
            self._update(
                session_id,
                status="failed",
                message=f"未能完成{spec['label']}登录：{str(exc)[:180]}",
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


class SourceLoginRequestHandler(BaseHTTPRequestHandler):
    server_version = "VideoTransferLoginHelper/1.0"

    @property
    def application(self) -> SourceLoginApplication:
        return self.server.application  # type: ignore[attr-defined]

    def log_message(self, format_text: str, *args: Any) -> None:
        del format_text, args
        LOGGER.info("%s %s", self.command, urlparse(self.path).path)

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, "application/json; charset=utf-8", _json_bytes(payload))

    def _send_error_page(self, status: HTTPStatus, message: str) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'><title>登录助手</title>"
            "<style>body{font-family:-apple-system,sans-serif;padding:40px;background:#f7f8fb}"
            ".box{max-width:560px;margin:auto;background:white;padding:24px;border-radius:16px}</style>"
            f"<div class='box'><h1>无法开始登录</h1><p>{html.escape(message)}</p>"
            "<p><a href='http://127.0.0.1:5188/transfer-center'>返回搬运中心</a></p></div>"
        ).encode("utf-8")
        self._send(status, "text/html; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"ready": True, "service": "source-login-helper"})
            return
        if parsed.path == "/status":
            item = self.application.get_status(
                str((query.get("session_id") or [""])[0]),
                str((query.get("token") or [""])[0]),
            )
            if not item:
                self._send_json(HTTPStatus.NOT_FOUND, {"message": "登录会话不存在或已过期"})
                return
            self._send_json(HTTPStatus.OK, item)
            return
        if parsed.path != "/connect":
            self._send_error_page(HTTPStatus.NOT_FOUND, "页面不存在")
            return

        try:
            platform, return_url = verify_login_authorization(
                self.application.secret,
                {
                    key: str((query.get(key) or [""])[0])
                    for key in ("platform", "return_url", "issued_at", "nonce", "signature")
                },
            )
            item = self.application.start_session(platform, return_url)
        except ValueError as exc:
            self._send_error_page(HTTPStatus.FORBIDDEN, str(exc))
            return
        except RuntimeError as exc:
            self._send_error_page(HTTPStatus.CONFLICT, str(exc))
            return
        body = _status_page(
            item["id"],
            item["status_token"],
            item["return_url"],
            item["platform"],
        ).encode("utf-8")
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="视频搬运通道本机来源登录助手")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5191)
    parser.add_argument("--cookie-dir", type=Path, default=REPO_ROOT / "cookies")
    parser.add_argument("--profile-dir", type=Path, default=REPO_ROOT / "config" / "source-login-profiles")
    parser.add_argument("--secret-file", type=Path, default=REPO_ROOT / "config" / "source_login_helper_secret")
    parser.add_argument(
        "--chrome-path",
        type=Path,
        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--success-url", default="")
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    application = SourceLoginApplication(
        cookie_dir=args.cookie_dir,
        profile_dir=args.profile_dir,
        secret_file=args.secret_file,
        chrome_path=args.chrome_path,
        timeout_seconds=args.timeout_seconds,
        success_url=args.success_url,
    )
    application.check_runtime()
    if args.check:
        print("source-login-helper: ready")
        return 0

    server = ThreadingHTTPServer((args.host, args.port), SourceLoginRequestHandler)
    server.application = application  # type: ignore[attr-defined]
    LOGGER.info("本机来源登录助手启动：http://%s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
