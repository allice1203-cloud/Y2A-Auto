from modules.media_preflight import assess_x_compatibility


def _base_info():
    return {
        "duration": 60,
        "size_bytes": 20 * 1024 * 1024,
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 720,
        "height": 1280,
        "fps": 30,
        "pix_fmt": "yuv420p",
        "audio_channels": 2,
        "has_audio": True,
    }


def test_x_compatible_portrait_video_passes():
    result = assess_x_compatibility(_base_info())

    assert result["compatible"] is True
    assert result["blockers"] == []
    assert result["transcode_reasons"] == []


def test_x_long_video_requires_editorial_cut_instead_of_silent_trim():
    info = _base_info()
    info["duration"] = 180

    result = assess_x_compatibility(info)

    assert result["compatible"] is False
    assert any("原创剪辑" in item for item in result["blockers"])


def test_x_nonstandard_media_is_marked_for_transcode():
    info = _base_info()
    info.update(
        {
            "video_codec": "hevc",
            "audio_codec": "opus",
            "pix_fmt": "yuv444p",
            "fps": 120,
        }
    )

    result = assess_x_compatibility(info)

    assert result["blockers"] == []
    assert len(result["transcode_reasons"]) == 4
