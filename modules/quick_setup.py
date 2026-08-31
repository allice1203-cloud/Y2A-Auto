#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""个人用户快速配置向导的纯状态与服务层。

本模块不读写配置文件，不保存凭据，也不依赖 Flask。路由层可以将
``load_config``、``update_config`` 以及现有平台检测函数作为适配器注入，从而在不复制
账号或 Token 的前提下提供四步向导。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "sg99.video-quick-setup.v1"

WIZARD_STEPS = (
    {
        "id": "goal",
        "index": 1,
        "title": "选择用途",
        "description": "选择一套个人使用预设，不会修改账号和密钥。",
    },
    {
        "id": "connections",
        "index": 2,
        "title": "连接账号",
        "description": "只检查当前用途必需的来源和发布账号。",
    },
    {
        "id": "processing",
        "index": 3,
        "title": "应用处理预设",
        "description": "预览变更后再应用，不覆盖 Cookies、Token 或 API Key。",
    },
    {
        "id": "verify",
        "index": 4,
        "title": "一键体检",
        "description": "聚合账号、AI、制作端、115 备份和通知状态。",
    },
)


HEALTH_COMPONENTS: dict[str, dict[str, str]] = {
    "bilibili_source": {
        "label": "B站来源账号",
        "action": "扫码登录并同步凭证",
        "group": "connection",
    },
    "bilibili_publish": {
        "label": "B站发布账号",
        "action": "扫码登录并同步凭证",
        "group": "connection",
    },
    "douyin_source": {
        "label": "抖音来源账号",
        "action": "打开专用登录窗口",
        "group": "connection",
    },
    "douyin_publish": {
        "label": "抖音发布账号",
        "action": "完成开放平台授权",
        "group": "connection",
    },
    "youtube_publish": {
        "label": "YouTube 发布频道",
        "action": "连接并验证 YouTube 频道",
        "group": "connection",
    },
    "ai": {
        "label": "AI 模型服务",
        "action": "检查接口、模型名与连通性",
        "group": "processing",
    },
    "money_printer": {
        "label": "二剪制作端",
        "action": "启动或修复本地制作端",
        "group": "processing",
    },
    "backup_115": {
        "label": "115 成片备份",
        "action": "检查 OpenList 和“视频搬运”目录",
        "group": "processing",
    },
    "telegram": {
        "label": "Telegram 通知",
        "action": "检查 Bot 和 Chat ID",
        "group": "optional",
    },
}


_COMMON_SAFE_CHANGES: dict[str, Any] = {
    "AUTO_MODE_ENABLED": False,
    "CONTENT_MODERATION_ENABLED": False,
    "VIDEO_ENCODER": "auto",
    "VIDEO_CUSTOM_PARAMS_ENABLED": False,
    "TRANSFER_115_BACKUP_ENABLED": True,
    "TRANSFER_MPT_WATCHDOG_ENABLED": True,
    "TRANSFER_TELEGRAM_INTAKE_ENABLED": True,
    "DOWNLOAD_CLEANUP_ENABLED": False,
    "YOUTUBE_DOWNLOAD_QUALITY_MODE": "manual",
    "YOUTUBE_DOWNLOAD_MAX_HEIGHT": "1080",
    "MAX_CONCURRENT_UPLOADS": 1,
    "SUBTITLE_EMBED_IN_VIDEO": True,
    "SUBTITLE_KEEP_ORIGINAL": True,
}


def _preset(
    preset_id: str,
    name: str,
    summary: str,
    processing_mode: str,
    targets: tuple[str, ...],
    required_checks: tuple[str, ...],
    optional_checks: tuple[str, ...],
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(_COMMON_SAFE_CHANGES)
    merged.update(dict(changes))
    return {
        "id": preset_id,
        "name": name,
        "summary": summary,
        "processing_mode": processing_mode,
        "target_platforms": list(targets),
        "required_checks": list(required_checks),
        "optional_checks": list(optional_checks),
        "changes": merged,
    }


PRESETS: dict[str, dict[str, Any]] = {
    "personal_stable": _preset(
        "personal_stable",
        "个人稳妥",
        "B站为主，人工审核，1080p 处理并自动备份成片。",
        "professional",
        ("bilibili",),
        ("bilibili_source", "bilibili_publish", "ai", "money_printer", "backup_115"),
        ("telegram",),
        {
            "UPLOAD_TARGET_DEFAULT": "bilibili",
            "TRANSLATE_TITLE": True,
            "TRANSLATE_DESCRIPTION": True,
            "GENERATE_TAGS": True,
            "RECOMMEND_PARTITION": True,
            "YOUTUBE_DOWNLOAD_THREADS": 4,
            "MAX_CONCURRENT_TASKS": 1,
            "VIDEO_CPU_PRESET": "medium",
            "VIDEO_CPU_PRESET_HD": "veryfast",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE": "professional",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_TARGETS": "bilibili",
        },
    ),
    "trend_fast": _preset(
        "trend_fast",
        "热点快速",
        "快速二剪，优先输出 X、B站和抖音手工发布素材。",
        "quick",
        ("x", "bilibili", "douyin"),
        ("bilibili_publish", "douyin_source", "money_printer", "backup_115"),
        ("ai", "douyin_publish", "telegram"),
        {
            "UPLOAD_TARGET_DEFAULT": "bilibili",
            "TRANSLATE_TITLE": True,
            "TRANSLATE_DESCRIPTION": False,
            "GENERATE_TAGS": True,
            "RECOMMEND_PARTITION": True,
            "YOUTUBE_DOWNLOAD_THREADS": 6,
            "MAX_CONCURRENT_TASKS": 2,
            "VIDEO_CPU_PRESET": "veryfast",
            "VIDEO_CPU_PRESET_HD": "veryfast",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE": "quick",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_TARGETS": "x,bilibili,douyin",
        },
    ),
    "multi_platform_growth": _preset(
        "multi_platform_growth",
        "多平台增长",
        "标准二剪与分平台文案，覆盖 X、YouTube、B站和抖音。",
        "professional",
        ("x", "youtube", "bilibili", "douyin"),
        (
            "bilibili_publish",
            "youtube_publish",
            "ai",
            "money_printer",
            "backup_115",
        ),
        ("douyin_source", "douyin_publish", "telegram"),
        {
            "UPLOAD_TARGET_DEFAULT": "bilibili",
            "TRANSLATE_TITLE": True,
            "TRANSLATE_DESCRIPTION": True,
            "GENERATE_TAGS": True,
            "RECOMMEND_PARTITION": True,
            "RECOMMEND_PARTITION_WITH_COVER": True,
            "YOUTUBE_DOWNLOAD_THREADS": 4,
            "MAX_CONCURRENT_TASKS": 2,
            "VIDEO_CPU_PRESET": "medium",
            "VIDEO_CPU_PRESET_HD": "veryfast",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE": "professional",
            "TRANSFER_TELEGRAM_INTAKE_DEFAULT_TARGETS": "x,youtube,bilibili,douyin",
        },
    ),
}


_CONFIG_RULES: dict[str, dict[str, Any]] = {
    "AUTO_MODE_ENABLED": {"type": bool},
    "CONTENT_MODERATION_ENABLED": {"type": bool},
    "VIDEO_ENCODER": {"type": str, "choices": {"auto", "cpu", "nvidia", "intel", "amd"}},
    "VIDEO_CUSTOM_PARAMS_ENABLED": {"type": bool},
    "TRANSFER_115_BACKUP_ENABLED": {"type": bool},
    "TRANSFER_MPT_WATCHDOG_ENABLED": {"type": bool},
    "TRANSFER_TELEGRAM_INTAKE_ENABLED": {"type": bool},
    "TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE": {
        "type": str,
        "choices": {"direct", "quick", "professional"},
    },
    "TRANSFER_TELEGRAM_INTAKE_DEFAULT_TARGETS": {
        "type": str,
        "choices": {
            "bilibili",
            "x,bilibili,douyin",
            "x,youtube,bilibili,douyin",
        },
    },
    "DOWNLOAD_CLEANUP_ENABLED": {"type": bool},
    "YOUTUBE_DOWNLOAD_QUALITY_MODE": {"type": str, "choices": {"highest", "manual"}},
    "YOUTUBE_DOWNLOAD_MAX_HEIGHT": {
        "type": str,
        "choices": {"2160", "1440", "1080", "720", "480", "360"},
    },
    "MAX_CONCURRENT_UPLOADS": {"type": int, "min": 1, "max": 8},
    "SUBTITLE_EMBED_IN_VIDEO": {"type": bool},
    "SUBTITLE_KEEP_ORIGINAL": {"type": bool},
    "UPLOAD_TARGET_DEFAULT": {"type": str, "choices": {"acfun", "bilibili", "both"}},
    "TRANSLATE_TITLE": {"type": bool},
    "TRANSLATE_DESCRIPTION": {"type": bool},
    "GENERATE_TAGS": {"type": bool},
    "RECOMMEND_PARTITION": {"type": bool},
    "RECOMMEND_PARTITION_WITH_COVER": {"type": bool},
    "YOUTUBE_DOWNLOAD_THREADS": {"type": int, "min": 1, "max": 16},
    "MAX_CONCURRENT_TASKS": {"type": int, "min": 1, "max": 8},
    "VIDEO_CPU_PRESET": {
        "type": str,
        "choices": {
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        },
    },
    "VIDEO_CPU_PRESET_HD": {
        "type": str,
        "choices": {
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        },
    },
}

_CONFIG_LABELS = {
    "AUTO_MODE_ENABLED": "无人值守自动发布",
    "CONTENT_MODERATION_ENABLED": "阿里云内容审核",
    "VIDEO_ENCODER": "视频编码器",
    "VIDEO_CUSTOM_PARAMS_ENABLED": "自定义转码参数",
    "TRANSFER_115_BACKUP_ENABLED": "115 自动备份",
    "TRANSFER_MPT_WATCHDOG_ENABLED": "本地制作端自检修复",
    "TRANSFER_TELEGRAM_INTAKE_ENABLED": "Telegram 快速创建任务",
    "TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE": "Telegram 默认处理模式",
    "TRANSFER_TELEGRAM_INTAKE_DEFAULT_TARGETS": "Telegram 默认目标平台",
    "DOWNLOAD_CLEANUP_ENABLED": "下载素材自动清理",
    "YOUTUBE_DOWNLOAD_QUALITY_MODE": "下载画质模式",
    "YOUTUBE_DOWNLOAD_MAX_HEIGHT": "下载分辨率上限",
    "MAX_CONCURRENT_UPLOADS": "发布并发数",
    "SUBTITLE_EMBED_IN_VIDEO": "字幕烧录",
    "SUBTITLE_KEEP_ORIGINAL": "保留原字幕",
    "UPLOAD_TARGET_DEFAULT": "默认投稿平台",
    "TRANSLATE_TITLE": "自动翻译标题",
    "TRANSLATE_DESCRIPTION": "自动翻译简介",
    "GENERATE_TAGS": "自动生成标签",
    "RECOMMEND_PARTITION": "自动推荐分区",
    "RECOMMEND_PARTITION_WITH_COVER": "分区推荐使用封面",
    "YOUTUBE_DOWNLOAD_THREADS": "下载线程数",
    "MAX_CONCURRENT_TASKS": "处理并发数",
    "VIDEO_CPU_PRESET": "常规转码速度",
    "VIDEO_CPU_PRESET_HD": "高清长视频转码速度",
}

_SENSITIVE_MARKERS = (
    "password", "secret", "token", "cookie", "api_key", "access_key", "credential",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def list_presets() -> list[dict[str, Any]]:
    """返回可直接渲染的预设副本，调用方不会修改模块常量。"""
    return [deepcopy(item) for item in PRESETS.values()]


def get_preset(preset_id: str | None) -> dict[str, Any] | None:
    preset = PRESETS.get(str(preset_id or "").strip())
    return deepcopy(preset) if preset else None


def preset_changes(preset_id: str) -> dict[str, Any]:
    preset = get_preset(preset_id)
    if not preset:
        raise ValueError("未知的快速配置预设")
    changes = dict(preset["changes"])
    if any(_is_sensitive_key(key) for key in changes):
        raise ValueError("快速配置预设不得包含凭据字段")
    return changes


def _normalize_value(value: Any, rule: Mapping[str, Any]) -> Any:
    expected = rule["type"]
    if expected is bool:
        if isinstance(value, bool):
            normalized = value
        elif isinstance(value, (int, float)) and value in (0, 1):
            normalized = bool(value)
        elif str(value).strip().lower() in {"1", "true", "on", "yes"}:
            normalized = True
        elif str(value).strip().lower() in {"0", "false", "off", "no"}:
            normalized = False
        else:
            raise ValueError("应为开关值")
    elif expected is int:
        if isinstance(value, bool):
            raise ValueError("应为整数")
        normalized = int(value)
    else:
        normalized = str(value).strip()

    choices = rule.get("choices")
    if choices is not None and normalized not in choices:
        raise ValueError("不在允许范围内")
    if isinstance(normalized, int):
        if "min" in rule and normalized < int(rule["min"]):
            raise ValueError(f"不能小于 {rule['min']}")
        if "max" in rule and normalized > int(rule["max"]):
            raise ValueError(f"不能大于 {rule['max']}")
    return normalized


def validate_changes(changes: Mapping[str, Any] | None) -> dict[str, Any]:
    """检查快速配置变更；敏感字段与未登记字段均拒绝。"""
    normalized: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for raw_key, raw_value in dict(changes or {}).items():
        key = str(raw_key or "").strip()
        if _is_sensitive_key(key):
            errors.append({"key": key, "message": "快速配置不允许修改凭据"})
            continue
        rule = _CONFIG_RULES.get(key)
        if not rule:
            errors.append({"key": key, "message": "不支持通过快速配置修改"})
            continue
        try:
            normalized[key] = _normalize_value(raw_value, rule)
        except (TypeError, ValueError) as exc:
            errors.append({"key": key, "message": str(exc)})

    warnings: list[dict[str, str]] = []
    if normalized.get("AUTO_MODE_ENABLED"):
        warnings.append({
            "key": "AUTO_MODE_ENABLED",
            "message": "无人值守发布建议在真实小样验收后再单独开启。",
        })
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized_changes": normalized,
    }


def _display_value(value: Any, key: str) -> Any:
    if _is_sensitive_key(key):
        return "已配置" if value not in (None, "", False) else "未配置"
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if value in (None, ""):
        return "未设置"
    return value


def preview_changes(
    current_config: Mapping[str, Any] | None,
    proposed_changes: Mapping[str, Any] | None,
) -> dict[str, Any]:
    validation = validate_changes(proposed_changes)
    current = dict(current_config or {})
    items: list[dict[str, Any]] = []
    for key, new_value in validation["normalized_changes"].items():
        old_value = current.get(key)
        rule = _CONFIG_RULES.get(key, {})
        try:
            comparable_old_value = _normalize_value(old_value, rule)
        except (TypeError, ValueError):
            comparable_old_value = old_value
        if comparable_old_value == new_value:
            continue
        items.append({
            "key": key,
            "label": _CONFIG_LABELS.get(key, key),
            "before": _display_value(comparable_old_value, key),
            "after": _display_value(new_value, key),
            "category": (
                "workflow" if key in {"AUTO_MODE_ENABLED", "UPLOAD_TARGET_DEFAULT"}
                else "media" if key.startswith("VIDEO_") or key.startswith("YOUTUBE_DOWNLOAD_")
                else "automation"
            ),
        })
    return {
        **validation,
        "items": items,
        "changed_count": len(items),
    }


def _status_from_mapping(payload: Mapping[str, Any]) -> str:
    if "ready" in payload:
        return "ready" if bool(payload.get("ready")) else "error"
    if "connected" in payload:
        return "ready" if bool(payload.get("connected")) else "error"
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in {"ok", "pass", "passed", "success", "ready", "connected", "healthy", "work"}:
        return "ready"
    if raw_status in {"warning", "partial", "optional", "skipped", "not_required"}:
        return "warning"
    if raw_status in {"error", "failed", "failure", "unavailable", "disconnected", "blocked"}:
        return "error"
    return "unknown"


def normalize_health_result(component_id: str, raw_result: Any) -> dict[str, Any]:
    spec = HEALTH_COMPONENTS.get(component_id, {})
    if isinstance(raw_result, Mapping):
        status = _status_from_mapping(raw_result)
        message = str(raw_result.get("message") or "").strip()
        details = {
            key: value
            for key, value in raw_result.items()
            if key not in {"status", "ready", "connected", "message"}
            and not _is_sensitive_key(str(key))
        }
    else:
        status = "ready" if raw_result is True else ("error" if raw_result is False else "unknown")
        message = ""
        details = {}
    return {
        "id": component_id,
        "label": spec.get("label", component_id),
        "group": spec.get("group", "other"),
        "status": status,
        "ready": status == "ready",
        "message": message or (
            "检查通过" if status == "ready"
            else "尚未检查" if status == "unknown"
            else "需要处理"
        ),
        "next_action": spec.get("action", "检查配置"),
        "details": details,
    }


def aggregate_health_results(
    results: Mapping[str, Any] | None,
    required_checks: Iterable[str],
    optional_checks: Iterable[str] = (),
) -> dict[str, Any]:
    required = list(dict.fromkeys(str(item) for item in required_checks))
    optional = [
        item for item in dict.fromkeys(str(item) for item in optional_checks)
        if item not in required
    ]
    normalized_results = dict(results or {})
    items: list[dict[str, Any]] = []
    for component_id in required + optional:
        item = normalize_health_result(component_id, normalized_results.get(component_id))
        item["required"] = component_id in required
        items.append(item)

    required_items = [item for item in items if item["required"]]
    ready_count = sum(1 for item in required_items if item["ready"])
    error_count = sum(1 for item in required_items if item["status"] == "error")
    unknown_count = sum(1 for item in required_items if item["status"] == "unknown")
    total = len(required_items)
    ready = total > 0 and ready_count == total
    if ready:
        status = "ready"
    elif error_count:
        status = "blocked"
    else:
        status = "pending"
    return {
        "schema": SCHEMA_VERSION,
        "status": status,
        "ready": ready,
        "score": round(100 * ready_count / total) if total else 0,
        "required_total": total,
        "required_ready": ready_count,
        "required_error": error_count,
        "required_unknown": unknown_count,
        "items": items,
    }


@dataclass(frozen=True)
class HealthCheck:
    id: str
    runner: Callable[[], Any]


class QuickSetupHealthService:
    """执行路由层注入的只读检查，并将异常收敛为用户可读状态。"""

    def __init__(self, checks: Mapping[str, Callable[[], Any]] | None = None):
        self._checks = dict(checks or {})

    def run(
        self,
        required_checks: Iterable[str],
        optional_checks: Iterable[str] = (),
    ) -> dict[str, Any]:
        requested = list(dict.fromkeys([
            *(str(item) for item in required_checks),
            *(str(item) for item in optional_checks),
        ]))
        results: dict[str, Any] = {}
        for component_id in requested:
            runner = self._checks.get(component_id)
            if not runner:
                results[component_id] = {
                    "status": "unknown",
                    "message": "尚未接入该检查项",
                }
                continue
            try:
                results[component_id] = runner()
            except Exception:
                results[component_id] = {
                    "status": "error",
                    "message": "检查失败，请查看本地服务日志",
                }
        return aggregate_health_results(results, required_checks, optional_checks)

    def run_for_preset(self, preset_id: str) -> dict[str, Any]:
        preset = get_preset(preset_id)
        if not preset:
            raise ValueError("未知的快速配置预设")
        return self.run(preset["required_checks"], preset["optional_checks"])


def _preset_is_applied(config: Mapping[str, Any], preset: Mapping[str, Any]) -> bool:
    validation = validate_changes(preset.get("changes") or {})
    if not validation["valid"]:
        return False
    return all(config.get(key) == value for key, value in validation["normalized_changes"].items())


def build_wizard_state(
    current_config: Mapping[str, Any] | None,
    preset_id: str | None = None,
    health_results: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(current_config or {})
    preset = get_preset(preset_id)
    health = aggregate_health_results(
        health_results,
        preset["required_checks"] if preset else (),
        preset["optional_checks"] if preset else (),
    )
    connection_items = [
        item for item in health["items"]
        if item["required"] and item["group"] == "connection"
    ]
    connections_ready = bool(connection_items) and all(item["ready"] for item in connection_items)
    applied = bool(preset) and _preset_is_applied(config, preset)
    verified = bool(applied and health["ready"])

    completed = {
        "goal": bool(preset),
        "connections": connections_ready,
        "processing": applied,
        "verify": verified,
    }
    current_step = "verify" if verified else "goal"
    if preset and not connections_ready:
        current_step = "connections"
    elif preset and connections_ready and not applied:
        current_step = "processing"
    elif preset and applied:
        current_step = "verify"

    steps = []
    current_found = False
    for step in WIZARD_STEPS:
        step_id = step["id"]
        if completed[step_id]:
            status = "complete"
        elif step_id == current_step:
            status = "current"
            current_found = True
        else:
            status = "pending" if current_found or current_step != step_id else "current"
        steps.append({**step, "status": status, "complete": completed[step_id]})

    preview = preview_changes(config, preset["changes"] if preset else {})
    return {
        "schema": SCHEMA_VERSION,
        "preset": preset,
        "steps": steps,
        "current_step": current_step,
        "complete": verified,
        "connections_ready": connections_ready,
        "preset_applied": applied,
        "preview": preview,
        "health": health,
    }


def build_quick_setup_context(
    current_config: Mapping[str, Any] | None,
    preset_id: str | None = None,
    health_results: Mapping[str, Any] | None = None,
    actions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """生成 ``quick_setup.html`` 需要的完整上下文。"""
    wizard = build_wizard_state(current_config, preset_id, health_results)
    return {
        "wizard": wizard,
        "presets": list_presets(),
        "preview": wizard["preview"],
        "health_summary": wizard["health"],
        "quick_setup_actions": dict(actions or {}),
    }


__all__ = [
    "HEALTH_COMPONENTS",
    "PRESETS",
    "QuickSetupHealthService",
    "WIZARD_STEPS",
    "aggregate_health_results",
    "build_quick_setup_context",
    "build_wizard_state",
    "get_preset",
    "list_presets",
    "normalize_health_result",
    "preset_changes",
    "preview_changes",
    "validate_changes",
]
