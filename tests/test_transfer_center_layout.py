from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_transfer_center_uses_scoped_wide_layout_and_current_stylesheet():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    page = (ROOT / "templates" / "transfer_center.html").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert "?v=0.16.4" in base
    assert "{% block body_class %}" in base
    assert "{% block body_class %}transfer-center-body{% endblock %}" in page
    assert ".transfer-center-body .app-content-inner" in css
    assert "width: min(100%, 1760px);" in css


def test_transfer_center_grid_and_font_contracts_are_responsive():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert "--studio-font-sans:" in css
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert ".transfer-page .workspace-panel-body" in css


def test_standard_remix_is_the_default_review_experience():
    review = (ROOT / "templates" / "transfer_review.html").read_text(
        encoding="utf-8"
    )
    center = (ROOT / "templates" / "transfer_center.html").read_text(
        encoding="utf-8"
    )

    assert "job.processing_mode or 'professional'" in review
    assert "标准二剪 / AI 重制" in review
    assert "AI 一键生成二剪成片" in review
    assert "分平台二剪版本" in review
    assert "仅提示，不阻塞" in review
    assert "来源跟踪列表" in center
    assert "value=\"unconfirmed\" selected" in center
