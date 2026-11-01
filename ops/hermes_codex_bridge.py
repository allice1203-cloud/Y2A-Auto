#!/usr/bin/env python3
"""Expose Hermes OpenAI-Codex OAuth as a narrow chat-completions endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
import time
from typing import Any
import uuid

from aiohttp import web


LOG = logging.getLogger("hermes_codex_bridge")
MAX_BODY_BYTES = 2_000_000
DEFAULT_MODEL = "gpt-5.6-luna"
_SECRET_RE = re.compile(r"(?:sk-|Bearer\s+)[A-Za-z0-9._-]{8,}", re.IGNORECASE)


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return _SECRET_RE.sub("<redacted>", text)[:600]


def _read_key(path: str) -> str:
    key = Path(path).read_text(encoding="utf-8").strip()
    if len(key) < 32:
        raise RuntimeError("bridge key is missing or too short")
    return key


def _authorized(request: web.Request, expected_key: str) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, expected_key)


def _tool_call_json(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None) or SimpleNamespace()
    return {
        "id": str(getattr(call, "id", "") or ""),
        "type": "function",
        "function": {
            "name": str(getattr(function, "name", "") or ""),
            "arguments": str(getattr(function, "arguments", "{}") or "{}"),
        },
    }


def _serialize_completion(result: Any, model: str) -> dict[str, Any]:
    choices = getattr(result, "choices", None) or []
    first = choices[0] if choices else SimpleNamespace()
    message = getattr(first, "message", None) or SimpleNamespace()
    tool_calls = getattr(message, "tool_calls", None) or []
    usage = getattr(result, "usage", None)

    response: dict[str, Any] = {
        "id": f"chatcmpl-hermes-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": getattr(message, "content", None),
                },
                "finish_reason": str(getattr(first, "finish_reason", "stop") or "stop"),
            }
        ],
    }
    if tool_calls:
        response["choices"][0]["message"]["tool_calls"] = [
            _tool_call_json(call) for call in tool_calls
        ]
    if usage is not None:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
        response["usage"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    return response


def _invoke_hermes(body: dict[str, Any], model: str, hermes_root: str) -> dict[str, Any]:
    import sys

    if hermes_root not in sys.path:
        sys.path.insert(0, hermes_root)
    from agent.auxiliary_client import resolve_provider_client

    client, resolved_model = resolve_provider_client(
        "openai-codex",
        model,
        api_mode="codex_responses",
    )
    if client is None:
        raise RuntimeError("Hermes openai-codex OAuth is unavailable")

    kwargs: dict[str, Any] = {
        "model": resolved_model or model,
        "messages": body["messages"],
        "timeout": 300,
    }
    if isinstance(body.get("tools"), list):
        kwargs["tools"] = body["tools"]
    if isinstance(body.get("extra_body"), dict):
        kwargs["extra_body"] = body["extra_body"]

    result = client.chat.completions.create(**kwargs)
    return _serialize_completion(result, resolved_model or model)


def create_app(*, key: str, model: str, hermes_root: str, max_concurrency: int) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "provider": "openai-codex", "model": model}
        )

    async def models(request: web.Request) -> web.Response:
        if not _authorized(request, key):
            return web.json_response({"error": {"message": "Unauthorized"}}, status=401)
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "hermes-openai-codex",
                    }
                ],
            }
        )

    async def chat_completions(request: web.Request) -> web.Response:
        if not _authorized(request, key):
            return web.json_response({"error": {"message": "Unauthorized"}}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return web.json_response(
                {"error": {"message": "messages must be an array"}}, status=400
            )
        requested_model = str(body.get("model") or model).strip()
        if requested_model != model:
            return web.json_response(
                {"error": {"message": f"Only {model} is enabled"}}, status=400
            )
        if body.get("stream") is True:
            return web.json_response(
                {"error": {"message": "Streaming is not enabled on this local bridge"}},
                status=400,
            )

        try:
            async with semaphore:
                payload = await asyncio.to_thread(
                    _invoke_hermes, body, model, hermes_root
                )
            return web.json_response(payload)
        except Exception as exc:
            LOG.error("Hermes Codex request failed: %s", _safe_error(exc))
            return web.json_response(
                {
                    "error": {
                        "message": "Hermes Codex request failed",
                        "type": "upstream_error",
                    }
                },
                status=502,
            )

    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="172.26.0.1")
    parser.add_argument("--port", type=int, default=18317)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--key-file", required=True)
    parser.add_argument(
        "--hermes-root",
        default=os.path.expanduser("~/.hermes/hermes-agent"),
    )
    parser.add_argument("--max-concurrency", type=int, default=2)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    key = _read_key(args.key_file)
    app = create_app(
        key=key,
        model=args.model,
        hermes_root=args.hermes_root,
        max_concurrency=args.max_concurrency,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
