#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
from typing import Dict, Iterable, Optional, Tuple

from .bili_sdk import login_v2
from .bili_sdk.exceptions import ArgsException
from .bili_sdk.utils.network import Credential, HEADERS, get_client

from .bilibili_runtime import configure_bilibili_runtime


logger = logging.getLogger("bilibili_auth")


def _run_async(coro):
    """Run async coroutine in sync context."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # 已有事件循环时（如嵌套调用），在新线程中运行
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


def _parse_cookies_text(content: str) -> Dict[str, str]:
    content = (content or "").strip()
    if not content:
        return {}

    cookies: Dict[str, str] = {}

    # Netscape format
    if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = str(parts[5]).strip()
                value = str(parts[6]).strip()
                if name:
                    cookies[name] = value
        return cookies

    # JSON format
    data = json.loads(content)
    if isinstance(data, dict):
        # 兼容 {"cookies": [{"name":"...","value":"..."}]} 结构
        cookies_list = data.get("cookies")
        if isinstance(cookies_list, list):
            for item in cookies_list:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name is None or value is None:
                    continue
                cookies[str(name)] = str(value)
            if cookies:
                return cookies
        for k, v in data.items():
            if isinstance(v, (str, int, float)):
                cookies[str(k)] = str(v)
        return cookies

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None or value is None:
                continue
            cookies[str(name)] = str(value)
        return cookies

    return cookies


def load_cookie_dict(cookie_file: str) -> Dict[str, str]:
    if not cookie_file or not os.path.exists(cookie_file):
        raise FileNotFoundError(f"Cookie 文件不存在: {cookie_file}")
    with open(cookie_file, "r", encoding="utf-8") as f:
        content = f.read()
    cookies = _parse_cookies_text(content)
    if not cookies:
        raise ValueError("Cookie 文件为空或格式不正确")
    return cookies


def build_credential(cookies: Dict[str, str]) -> Credential:
    configure_bilibili_runtime()
    cookies = cookies or {}
    return Credential(
        sessdata=cookies.get("SESSDATA") or cookies.get("sessdata"),
        bili_jct=cookies.get("bili_jct") or cookies.get("biliJct"),
        dedeuserid=cookies.get("DedeUserID") or cookies.get("dedeuserid"),
        buvid3=cookies.get("buvid3"),
        buvid4=cookies.get("buvid4"),
        ac_time_value=cookies.get("ac_time_value"),
    )


def validate_credential(credential: Credential) -> Tuple[bool, str]:
    missing = []
    try:
        if not credential.has_sessdata():
            missing.append("SESSDATA")
    except Exception:
        missing.append("SESSDATA")
    try:
        if not credential.has_bili_jct():
            missing.append("bili_jct")
    except Exception:
        missing.append("bili_jct")
    try:
        if not credential.has_dedeuserid():
            missing.append("DedeUserID")
    except Exception:
        missing.append("DedeUserID")

    if missing:
        return False, f"缺少必要 Cookie 字段: {', '.join(missing)}"
    return True, "credential 有效"


async def _check_credential_remote(credential: Credential) -> bool:
    cookies = await credential.get_buvid_cookies()
    resp = await get_client().request(
        method="GET",
        url="https://api.bilibili.com/x/web-interface/nav",
        headers=HEADERS.copy(),
        cookies=cookies,
    )
    if resp.code != 200:
        return False
    data = resp.json()
    body = data.get("data") or {}
    return data.get("code") == 0 and bool(body.get("isLogin"))


def validate_credential_remote(credential: Credential) -> Tuple[bool, str]:
    ok, msg = validate_credential(credential)
    if not ok:
        return ok, msg

    try:
        if _run_async(_check_credential_remote(credential)):
            return True, "credential 已通过 Bilibili 登录态校验"
    except Exception as exc:
        logger.warning("Bilibili 登录态远程校验失败: %s", exc)
        return False, f"Bilibili 登录态校验失败: {exc}"
    return False, "Bilibili 登录态校验未通过，请重新扫码登录"


def load_credential_from_file(cookie_file: str) -> Credential:
    cookies = load_cookie_dict(cookie_file)
    credential = build_credential(cookies)
    ok, msg = validate_credential(credential)
    if not ok:
        raise ValueError(msg)
    return credential


def save_credential_to_file(credential: Credential, cookie_file: str) -> bool:
    if not cookie_file:
        return False

    try:
        cookies = credential.get_cookies()
    except Exception as exc:
        logger.error("读取 Bilibili 登录 Cookie 失败: %s", exc)
        return False

    if not isinstance(cookies, dict):
        logger.error("读取 Bilibili 登录 Cookie 失败: 返回格式无效")
        return False

    required_keys = ["SESSDATA", "bili_jct", "DedeUserID"]
    missing_keys = [key for key in required_keys if not str(cookies.get(key) or "").strip()]
    if missing_keys:
        logger.error("Bilibili 登录 Cookie 缺少必要字段: %s", ", ".join(missing_keys))
        return False

    ordered_keys = [
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "buvid3",
        "buvid4",
        "ac_time_value",
    ]
    cookie_items = []
    seen_keys = set()
    for key in ordered_keys:
        value = cookies.get(key)
        if value is None or str(value) == "":
            continue
        seen_keys.add(key)
        cookie_items.append(
            {
                "name": key,
                "value": str(value),
                "domain": ".bilibili.com",
                "path": "/",
            }
        )

    for key, value in cookies.items():
        if key in seen_keys or value is None or str(value) == "":
            continue
        if key.startswith("_") or key == "proxy":
            continue
        cookie_items.append(
            {
                "name": str(key),
                "value": str(value),
                "domain": ".bilibili.com",
                "path": "/",
            }
        )

    try:
        os.makedirs(os.path.dirname(cookie_file) or ".", exist_ok=True)
        if str(cookie_file).lower().endswith(".txt"):
            from .source_login import write_netscape_cookie_file

            netscape_items = [
                {
                    **item,
                    "secure": True,
                    "httpOnly": item["name"] == "SESSDATA",
                    "expires": 0,
                }
                for item in cookie_items
            ]
            write_netscape_cookie_file(netscape_items, "bilibili", cookie_file)
        else:
            target_dir = os.path.dirname(cookie_file) or "."
            fd, temp_path = tempfile.mkstemp(
                prefix=".bilibili-cookies-",
                suffix=".tmp",
                dir=target_dir,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(cookie_items, f, ensure_ascii=False, indent=2)
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, cookie_file)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
    except (OSError, TypeError, ValueError) as exc:
        logger.error("保存 Bilibili 登录 Cookie 失败: %s", exc)
        return False
    return True


class BilibiliQrLoginSession:
    """In-memory Bilibili QR login session wrapper."""

    def __init__(self):
        configure_bilibili_runtime()
        self.created_at = int(time.time())
        self.qr = login_v2.QrCodeLogin()
        self.generated = False
        self.last_state = None

    def generate(self) -> Dict[str, str]:
        try:
            _run_async(self.qr.generate_qrcode())
        except ArgsException as e:
            raise RuntimeError(str(e))
        self.generated = True
        pic = self.qr.get_qrcode_picture()
        raw = pic.content if pic else b""
        image_b64 = base64.b64encode(raw).decode("ascii") if raw else ""
        return {
            "image_base64": image_b64,
            "mime_type": "image/png",
        }

    def check_status(
        self,
        cookie_file: Optional[str] = None,
        cookie_files: Optional[Iterable[str]] = None,
    ) -> Dict[str, object]:
        if not self.generated:
            return {"status": "not_started"}

        try:
            event = _run_async(self.qr.check_state())
        except ArgsException as e:
            raise RuntimeError(str(e))

        status = getattr(event, "value", str(event))
        self.last_state = status
        payload: Dict[str, object] = {"status": status}

        if status == login_v2.QrCodeLoginEvents.DONE.value:
            credential = self.qr.get_credential()
            ok, msg = validate_credential_remote(credential)
            if not ok:
                payload["status"] = "failed"
                payload["message"] = msg
                return payload

            target_files = []
            for target_file in [cookie_file, *(cookie_files or [])]:
                normalized_path = str(target_file or "").strip()
                if normalized_path and normalized_path not in target_files:
                    target_files.append(normalized_path)

            if not target_files:
                payload["status"] = "failed"
                payload["message"] = "Bilibili 登录成功，但未配置 Cookie 保存路径"
                payload["cookies_saved"] = False
                return payload

            saved_paths = [
                target_file
                for target_file in target_files
                if save_credential_to_file(credential, target_file)
            ]
            payload["cookies_saved"] = len(saved_paths) == len(target_files)
            payload["cookies_paths"] = saved_paths
            if len(saved_paths) != len(target_files):
                payload["status"] = "failed"
                payload["message"] = "Bilibili 登录成功，但下载或投稿凭证同步保存失败"
                return payload

            payload["cookies_path"] = target_files[0]
            payload["cookies_synced"] = len(saved_paths) > 1
            payload["credential_ok"] = True

        return payload
