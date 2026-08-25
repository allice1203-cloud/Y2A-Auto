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
    assert 'name="completion_rate"' in center
    assert 'name="revenue_cny"' in center
    assert 'name="local_visual_ratio"' in center
    assert "下一批{% if not performance.strategy.sample_size %}" in center


def test_performance_panel_exposes_one_click_sync_route():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    center = (ROOT / "templates" / "transfer_center.html").read_text(
        encoding="utf-8"
    )

    assert "@app.route('/transfer-center/performance/sync', methods=['POST'])" in app_source
    assert "def transfer_center_sync_performance():" in app_source
    assert "url_for('transfer_center_sync_performance')" in center
    assert "不覆盖已手工填写" in center
    assert "transfer-center-performance-sync" in (
        ROOT / "modules" / "transfer_center.py"
    ).read_text(encoding="utf-8")
    assert "发布后 24 小时、72 小时和 7 天自动同步" in center
    assert "选题动作：{{ performance.strategy.topic_guidance }}" in center
    assert "不补造数据" in center
