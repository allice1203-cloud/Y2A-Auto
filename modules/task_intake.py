#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""任务快速入口的输入规范化。"""

from __future__ import annotations

import re
from urllib.parse import urlparse


PROCESSING_PRESETS = {
    "direct": "仅下载 / 原片分发",
    "quick": "快速二剪",
    "professional": "质量优先",
}


def normalize_processing_preset(value: str) -> str:
    normalized = str(value or "professional").strip().lower()
    if normalized not in PROCESSING_PRESETS:
        raise ValueError("请选择有效的处理配方")
    return normalized


def parse_source_url_batch(value: str, *, limit: int = 20) -> list[str]:
    """解析一行一个的公开视频链接，去重并限制单次任务量。"""

    normalized_limit = max(1, min(int(limit), 100))
    raw_lines = re.split(r"[\r\n]+", str(value or ""))
    urls: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_lines:
        candidate = raw_line.strip()
        if not candidate:
            continue
        if not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
            candidate = f"https://{candidate.lstrip('/')}"
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"无法识别视频链接：{raw_line.strip()[:80]}")
        canonical = candidate.rstrip("/")
        if canonical in seen:
            continue
        seen.add(canonical)
        urls.append(candidate)
        if len(urls) > normalized_limit:
            raise ValueError(f"单次最多创建 {normalized_limit} 条任务")
    if not urls:
        raise ValueError("请至少粘贴一条视频链接")
    return urls
