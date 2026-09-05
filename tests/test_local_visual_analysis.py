import json
import logging
from pathlib import Path
import subprocess
import threading
import time

import pytest

from modules.config_manager import DEFAULT_CONFIG, _prune_unknown_config_keys
import modules.local_visual_analysis as visual
from modules.local_visual_analysis import (
    LocalVisualAnalyzer,
    analyze_video,
    local_visual_health,
    normalize_visual_result,
    select_frame_timestamps,
)


def test_local_visual_defaults_are_persisted_and_loopback_only():
    keys = {
        "TRANSFER_LOCAL_VISION_ENABLED",
        "TRANSFER_LOCAL_VISION_BASE_URL",
        "TRANSFER_LOCAL_VISION_QUICK_MAX_FRAMES",
        "TRANSFER_LOCAL_VISION_PROFESSIONAL_MAX_FRAMES",
    }

    pruned, removed = _prune_unknown_config_keys(
        {key: DEFAULT_CONFIG[key] for key in keys}
    )

    assert set(pruned) == keys
    assert removed == []
    assert DEFAULT_CONFIG["TRANSFER_LOCAL_VISION_ENABLED"] is True
    assert DEFAULT_CONFIG["TRANSFER_LOCAL_VISION_BASE_URL"].startswith(
        "http://127.0.0.1:"
    )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=-1):
        raw = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return raw if limit < 0 else raw[:limit]


class FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return FakeResponse(self.handler(request, timeout))


def _success_content(frame_count=3):
    return {
        "summary": "室内人物讲解，画面稳定。",
        "frames": [
            {
                "timestamp": index + 0.5,
                "description": f"人物讲解画面 {index + 1}",
                "shot_type": "medium",
                "motion": "static",
                "text_present": index == 0,
                "quality_score": 0.8,
                "issues": [],
            }
            for index in range(frame_count)
        ],
        "suggested_segments": [
            {"start": 1, "end": 4, "reason": "主体清晰", "score": 0.9}
        ],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:49821/v1",
        "https://example.com:49821/v1",
        "http://user:secret@127.0.0.1:49821/v1",
        "ftp://127.0.0.1:49821/v1",
        "http://127.0.0.1/v1",
        "http://localhost:49821/v1?redirect=evil",
        "http://localhost:49821/v1#fragment",
    ],
)
def test_endpoint_rejects_every_non_loopback_or_ambiguous_form(url):
    with pytest.raises(ValueError, match="loopback|端口"):
        LocalVisualAnalyzer({"TRANSFER_LOCAL_VISION_BASE_URL": url})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:49821/v1",
        "http://localhost:49821/v1/",
        "http://[::1]:49821/v1",
    ],
)
def test_endpoint_accepts_only_explicit_loopback_hosts(url):
    analyzer = LocalVisualAnalyzer({"TRANSFER_LOCAL_VISION_BASE_URL": url})
    assert url.rstrip("/") == analyzer.base_url
    assert any(
        isinstance(handler, visual._NoRedirectHandler)
        for handler in analyzer._opener.handlers
    )


def test_disabled_and_direct_modes_do_not_touch_video_or_endpoint(monkeypatch):
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("analyzer must not be constructed")

    monkeypatch.setattr(visual, "LocalVisualAnalyzer", fail_if_constructed)

    disabled = analyze_video(
        "/path/does/not/exist.mp4",
        "professional",
        config={"TRANSFER_LOCAL_VISION_ENABLED": False},
    )
    direct = analyze_video(
        "/path/does/not/exist.mp4",
        "direct",
        config={"TRANSFER_LOCAL_VISION_ENABLED": True},
    )

    assert disabled["status"] == "skipped"
    assert direct["status"] == "skipped"
    assert disabled["frame_count"] == direct["frame_count"] == 0


def test_frame_sampling_is_uniform_and_has_mode_specific_hard_caps():
    quick = select_frame_timestamps(
        100,
        "quick",
        {"TRANSFER_LOCAL_VISION_QUICK_MAX_FRAMES": 999},
    )
    professional = select_frame_timestamps(
        100,
        "professional",
        {"TRANSFER_LOCAL_VISION_PROFESSIONAL_MAX_FRAMES": 999},
    )

    assert len(quick) == visual.QUICK_HARD_MAX_FRAMES
    assert len(professional) == visual.PROFESSIONAL_HARD_MAX_FRAMES
    assert quick == sorted(quick)
    assert professional == sorted(professional)
    assert quick[0] > 0 and quick[-1] < 100
    assert len({round(b - a, 3) for a, b in zip(quick, quick[1:])}) == 1
    assert select_frame_timestamps(2.1, "quick") == [0.35, 1.05, 1.75]
    assert select_frame_timestamps(0, "professional") == []


def test_model_json_is_strictly_normalized_and_bounded():
    raw = {
        "summary": " S  " * 500,
        "frames": [
            {
                "timestamp": -100,
                "description": "D" * 500,
                "shot_type": "unknown-shot",
                "motion": "PAN",
                "text_present": "yes",
                "quality_score": 9,
                "issues": ["I" * 200] * 10,
            },
            {
                "timestamp": 999,
                "description": "second",
                "shot_type": "close-up",
                "motion": "unknown motion",
                "text_present": 0,
                "quality_score": -1,
                "issues": "not-a-list",
            },
            {"timestamp": 5},
        ],
        "suggested_segments": [
            {"start": -4, "end": 99, "reason": "R" * 400, "score": 2},
            {"start": 9, "end": 2, "reason": "invalid", "score": 0.5},
        ]
        + [
            {"start": i / 10, "end": i / 10 + 0.1, "reason": str(i), "score": 0.5}
            for i in range(20)
        ],
        "warnings": ["W" * 300] * 20,
        "ignored": "/private/secret/path",
    }

    result = normalize_visual_result(
        raw,
        duration=10,
        sampled_timestamps=[1, 9],
        elapsed_seconds=99999,
    )

    assert set(result) == {
        "status",
        "summary",
        "frames",
        "suggested_segments",
        "warnings",
        "frame_count",
        "elapsed_seconds",
    }
    assert result["status"] == "ok"
    assert len(result["summary"]) <= 600
    assert len(result["frames"]) == 2
    assert result["frames"][0]["timestamp"] == 1
    assert result["frames"][1]["timestamp"] == 9
    assert result["frames"][0]["shot_type"] == "other"
    assert result["frames"][1]["shot_type"] == "close_up"
    assert result["frames"][0]["motion"] == "pan"
    assert result["frames"][1]["motion"] == "other"
    assert result["frames"][0]["text_present"] is True
    assert result["frames"][1]["text_present"] is False
    assert result["frames"][0]["quality_score"] == 1
    assert result["frames"][1]["quality_score"] == 0
    assert len(result["frames"][0]["issues"]) == 5
    assert all(len(item) <= 120 for item in result["frames"][0]["issues"])
    assert len(result["suggested_segments"]) == 8
    assert all(0 <= item["start"] < item["end"] <= 10 for item in result["suggested_segments"])
    assert all(0 <= item["score"] <= 1 for item in result["suggested_segments"])
    assert len(result["warnings"]) == 8
    assert all(len(item) <= 200 for item in result["warnings"])
    assert result["frame_count"] == 2
    assert result["elapsed_seconds"] == 3600
    assert "/private/secret/path" not in json.dumps(result)


def test_success_path_extracts_temporary_jpegs_and_sends_data_urls(
    tmp_path, monkeypatch
):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video-placeholder")
    temp_directories = set()
    captured_payloads = []

    monkeypatch.setattr(visual, "get_ffmpeg_path", lambda **kwargs: "ffmpeg")

    def fake_run(command, **kwargs):
        output = Path(command[-1])
        temp_directories.add(output.parent)
        output.write_bytes(b"\xff\xd8local-jpeg\xff\xd9")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(visual.subprocess, "run", fake_run)

    def handle_request(request, timeout):
        assert request.full_url.endswith("/v1/chat/completions")
        payload = json.loads(request.data.decode("utf-8"))
        captured_payloads.append(payload)
        image_parts = [
            item
            for item in payload["messages"][1]["content"]
            if item.get("type") == "image_url"
        ]
        assert len(image_parts) == 3
        assert all(
            item["image_url"]["url"].startswith("data:image/jpeg;base64,")
            for item in image_parts
        )
        assert all(directory.exists() for directory in temp_directories)
        return {
            "choices": [
                {"message": {"content": json.dumps(_success_content(3))}}
            ]
        }

    opener = FakeOpener(handle_request)
    analyzer = LocalVisualAnalyzer(
        {
            "TRANSFER_LOCAL_VISION_BASE_URL": "http://127.0.0.1:49821/v1",
            "TRANSFER_LOCAL_VISION_MODEL_NAME": "/private/models/Qwen3-VL-2B",
            "TRANSFER_LOCAL_VISION_QUICK_MAX_FRAMES": 3,
            "TRANSFER_LOCAL_VISION_FRAME_WIDTH": 640,
        },
        opener=opener,
    )

    result = analyzer.analyze_video(video_path, "quick", {"duration": 30})

    assert result["status"] == "ok"
    assert result["frame_count"] == 3
    assert len(result["frames"]) == 3
    assert captured_payloads[0]["model"] == "/private/models/Qwen3-VL-2B"
    assert captured_payloads[0]["response_format"]["type"] == "json_schema"
    assert captured_payloads[0]["repetition_penalty"] == 1.15
    assert captured_payloads[0]["enable_thinking"] is False
    assert not any(directory.exists() for directory in temp_directories)
    assert "/private/models" not in json.dumps(result)


def test_missing_duration_is_probed_without_exposing_source_path(tmp_path, monkeypatch):
    video_path = tmp_path / "private-source.mp4"
    video_path.write_bytes(b"video-placeholder")
    monkeypatch.setattr(visual, "get_ffprobe_path", lambda **kwargs: "ffprobe")
    monkeypatch.setattr(visual, "get_ffmpeg_path", lambda **kwargs: "ffmpeg")

    def fake_run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, "4.0\n", "")
        Path(command[-1]).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(visual.subprocess, "run", fake_run)
    opener = FakeOpener(
        lambda request, timeout: {
            "choices": [
                {"message": {"content": json.dumps(_success_content(4))}}
            ]
        }
    )
    analyzer = LocalVisualAnalyzer(
        {"TRANSFER_LOCAL_VISION_MODEL_NAME": "local-model"}, opener=opener
    )

    result = analyzer.analyze_video(video_path, "quick")

    assert result["status"] == "ok"
    assert result["frame_count"] == 4
    assert str(video_path) not in json.dumps(result)


def test_malformed_model_output_fails_open_cleans_frames_and_never_logs_raw_data(
    tmp_path, monkeypatch, caplog
):
    video_path = tmp_path / "sensitive-source.mp4"
    video_path.write_bytes(b"video-placeholder")
    temp_directories = set()
    monkeypatch.setattr(visual, "get_ffmpeg_path", lambda **kwargs: "ffmpeg")

    def fake_run(command, **kwargs):
        output = Path(command[-1])
        temp_directories.add(output.parent)
        output.write_bytes(b"secret-image-bytes")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(visual.subprocess, "run", fake_run)
    opener = FakeOpener(
        lambda request, timeout: {
            "choices": [
                {
                    "message": {
                        "content": "raw-model-secret that is deliberately not json"
                    }
                }
            ]
        }
    )
    analyzer = LocalVisualAnalyzer(
        {"TRANSFER_LOCAL_VISION_MODEL_NAME": "/private/model/path"}, opener=opener
    )

    with caplog.at_level(logging.WARNING, logger="local_visual_analysis"):
        result = analyzer.analyze_video(video_path, "quick", {"duration": 1})

    assert result["status"] == "failed"
    assert "不阻断" in result["summary"]
    assert result["frames"] == []
    assert result["frame_count"] == 1
    assert not any(directory.exists() for directory in temp_directories)
    logged = caplog.text
    assert "raw-model-secret" not in logged
    assert "secret-image-bytes" not in logged
    assert str(video_path) not in logged
    assert "/private/model/path" not in logged


def test_health_discovers_model_from_models_endpoint_and_redacts_path():
    def handle_request(request, timeout):
        if request.full_url.endswith("/health"):
            return {"status": "ok"}
        if request.full_url.endswith("/v1/models"):
            return {
                "object": "list",
                "data": [{"id": "/private/models/Qwen3-VL-2B-Instruct-4bit"}],
            }
        raise AssertionError(request.full_url)

    opener = FakeOpener(handle_request)
    analyzer = LocalVisualAnalyzer({}, opener=opener)

    result = analyzer.health()

    assert result == {
        "status": "available",
        "enabled": True,
        "available": True,
        "model": "Qwen3-VL-2B-Instruct-4bit",
        "message": "本地视觉服务可用",
    }
    assert "/private/models" not in json.dumps(result)
    assert [item[0].full_url for item in opener.requests] == [
        "http://127.0.0.1:49821/health",
        "http://127.0.0.1:49821/v1/models",
    ]


def test_health_prefers_the_actually_loaded_model_over_cached_model_order():
    def handle_request(request, timeout):
        if request.full_url.endswith("/health"):
            return {
                "status": "healthy",
                "loaded_model": "/private/models/Qwen3-VL-2B-Instruct-4bit",
            }
        raise AssertionError("cached model listing must not be consulted")

    opener = FakeOpener(handle_request)
    result = LocalVisualAnalyzer({}, opener=opener).health()

    assert result["available"] is True
    assert result["model"] == "Qwen3-VL-2B-Instruct-4bit"
    assert [item[0].full_url for item in opener.requests] == [
        "http://127.0.0.1:49821/health"
    ]


def test_public_health_handles_disabled_and_invalid_configuration_without_raising():
    disabled = local_visual_health({"TRANSFER_LOCAL_VISION_ENABLED": False})
    invalid = local_visual_health(
        {
            "TRANSFER_LOCAL_VISION_ENABLED": True,
            "TRANSFER_LOCAL_VISION_BASE_URL": "http://example.com:49821/v1",
        }
    )

    assert disabled["status"] == "disabled"
    assert disabled["enabled"] is False
    assert invalid["status"] == "unavailable"
    assert invalid["available"] is False


def test_global_analysis_lock_keeps_local_inference_single_slot(tmp_path, monkeypatch):
    videos = []
    for index in range(2):
        path = tmp_path / f"video-{index}.mp4"
        path.write_bytes(b"video")
        videos.append(path)

    monkeypatch.setattr(
        LocalVisualAnalyzer,
        "_extract_frames",
        lambda self, video_path, timestamps, target_dir, **kwargs: [],
    )
    monkeypatch.setattr(
        LocalVisualAnalyzer, "_resolve_model", lambda self, **kwargs: "model"
    )
    monkeypatch.setattr(
        LocalVisualAnalyzer,
        "_build_payload",
        lambda self, frames, timestamps, duration, model: {},
    )

    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()

    def fake_request(self, url, *, payload=None, timeout=None):
        with state_lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.04)
        with state_lock:
            state["active"] -= 1
        return {
            "choices": [
                {"message": {"content": json.dumps(_success_content(1))}}
            ]
        }

    monkeypatch.setattr(LocalVisualAnalyzer, "_request_json", fake_request)
    analyzers = [
        LocalVisualAnalyzer({"TRANSFER_LOCAL_VISION_MODEL_NAME": "model"})
        for _ in range(2)
    ]
    results = []

    threads = [
        threading.Thread(
            target=lambda analyzer=analyzer, path=path: results.append(
                analyzer.analyze_video(path, "quick", {"duration": 1})
            )
        )
        for analyzer, path in zip(analyzers, videos)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert state["maximum"] == 1
    assert [result["status"] for result in results] == ["ok", "ok"]


def test_lock_wait_uses_the_single_end_to_end_timeout_budget(tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    analyzer = LocalVisualAnalyzer(
        {"TRANSFER_LOCAL_VISION_MODEL_NAME": "model"}
    )
    analyzer.timeout = 0.08

    assert visual._INFERENCE_LOCK.acquire(timeout=0.1)
    started_at = time.monotonic()
    try:
        result = analyzer.analyze_video(video_path, "quick", {"duration": 1})
    finally:
        visual._INFERENCE_LOCK.release()

    assert result["status"] == "failed"
    assert time.monotonic() - started_at < 0.4
