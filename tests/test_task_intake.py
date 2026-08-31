import pytest

from modules.task_intake import normalize_processing_preset, parse_source_url_batch


def test_parse_source_url_batch_normalizes_deduplicates_and_keeps_order():
    urls = parse_source_url_batch(
        "x.com/a/status/1\nhttps://youtu.be/demo\nhttps://youtu.be/demo/\n"
    )

    assert urls == [
        "https://x.com/a/status/1",
        "https://youtu.be/demo",
    ]


def test_parse_source_url_batch_rejects_empty_and_large_batches():
    with pytest.raises(ValueError, match="至少粘贴"):
        parse_source_url_batch("\n")
    with pytest.raises(ValueError, match="最多创建 2 条"):
        parse_source_url_batch("https://a.test/1\nhttps://a.test/2\nhttps://a.test/3", limit=2)


def test_processing_preset_is_explicit():
    assert normalize_processing_preset("quick") == "quick"
    assert normalize_processing_preset("") == "professional"
    assert normalize_processing_preset("direct") == "direct"
    with pytest.raises(ValueError, match="有效的处理配方"):
        normalize_processing_preset("fastest")
