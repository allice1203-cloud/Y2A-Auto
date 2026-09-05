#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Synchronize the app process with macOS' current loopback proxy."""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping
from urllib.parse import urlsplit
from urllib.request import getproxies


_PROXY_KEYS = {"http": "HTTP_PROXY", "https": "HTTPS_PROXY"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _macos_system_proxies() -> dict[str, str]:
    try:
        from _scproxy import _get_proxies

        raw = _get_proxies()
    except (ImportError, OSError):
        raw = getproxies()
    return {
        str(key).lower(): str(value).strip()
        for key, value in dict(raw or {}).items()
        if str(value or "").strip()
    }


def _safe_loopback_proxy(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or str(parsed.hostname or "").lower() not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or not port
    ):
        return ""
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def configure_local_system_proxy(
    environ: MutableMapping[str, str] | None = None,
    *,
    proxy_provider: Callable[[], dict[str, str]] = _macos_system_proxies,
) -> dict[str, object]:
    """Refresh stale loopback proxy variables from macOS System Settings.

    Explicit remote proxy variables are preserved. Only credential-free
    loopback proxy URLs discovered from the local OS may replace an existing
    loopback value, so this helper cannot redirect traffic to another host.
    """

    target = environ if environ is not None else os.environ
    discovered = proxy_provider()
    applied: dict[str, str] = {}
    for scheme, upper_key in _PROXY_KEYS.items():
        system_value = _safe_loopback_proxy(discovered.get(scheme, ""))
        if not system_value:
            continue
        current_value = str(target.get(upper_key) or target.get(upper_key.lower()) or "").strip()
        current_parsed = urlsplit(current_value) if current_value else None
        current_is_loopback = bool(
            current_parsed
            and str(current_parsed.hostname or "").lower() in _LOOPBACK_HOSTS
        )
        if current_value and not current_is_loopback:
            continue
        target[upper_key] = system_value
        target[upper_key.lower()] = system_value
        applied[scheme] = system_value

    no_proxy_entries = [
        item.strip()
        for item in str(target.get("NO_PROXY") or target.get("no_proxy") or "").split(",")
        if item.strip()
    ]
    for entry in ("127.0.0.1", "localhost", "::1"):
        if entry not in no_proxy_entries:
            no_proxy_entries.append(entry)
    no_proxy_value = ",".join(no_proxy_entries)
    target["NO_PROXY"] = no_proxy_value
    target["no_proxy"] = no_proxy_value
    return {
        "configured": bool(applied),
        "http": applied.get("http", ""),
        "https": applied.get("https", ""),
        "source": "macos_system_settings" if applied else "unchanged",
    }


__all__ = ["configure_local_system_proxy"]
