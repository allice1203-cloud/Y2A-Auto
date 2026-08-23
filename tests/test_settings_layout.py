from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_page_uses_readable_summary_cards_and_form_text():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert ".settings-summary-grid {\n    grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert ".settings-summary-label {\n    font-size: 12px;" in css
    assert ".settings-summary-value {\n    margin-bottom: 8px;\n    font-size: 15px;" in css
    assert ".settings-summary-card .settings-status-badge" in css
    assert "font-size: 11px;" in css
    assert ".settings-card-body .form-label" in css
    assert ".settings-card-body .help-text" in css


def test_settings_page_keeps_responsive_single_column_mobile_layout():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

    mobile = css[css.rfind("@media (max-width: 767.98px)") :]
    assert ".settings-summary-grid" in css
    assert "grid-template-columns: 1fr;" in css
    assert ".settings-page-header h2" in css
    assert "font-size: 30px;" in css


def test_settings_page_distinguishes_telegram_bot_token_from_internal_access_token():
    template = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

    assert 'name="NOTIFY_TELEGRAM_ENABLED"' in template
    assert 'name="NOTIFY_TELEGRAM_BOT_TOKEN"' in template
    assert 'type="password" id="notify-telegram-bot-token"' in template
    assert 'name="NOTIFY_TELEGRAM_CHAT_ID"' in template
    assert 'id="telegram-detect-chat-btn"' in template
    assert 'data-channel="telegram"' in template
    assert "它不是 BotFather Bot Token" in template


def test_notification_settings_prioritize_telegram_and_collapse_optional_channels():
    template = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    notifications = template.split('id="vtab-notifications"', 1)[1].split(
        'id="vtab-ops"', 1
    )[0]

    assert 'class="notification-settings-layout"' in notifications
    assert 'class="settings-card notification-policy-card"' in notifications
    assert 'class="settings-card notification-primary-card"' in notifications
    assert 'class="settings-card notification-secondary-card"' in notifications
    assert notifications.count('class="notification-channel-disclosure"') == 3
    assert 'settings-card h-100' not in notifications
    assert 'col-xl-5' not in notifications


def test_notification_settings_have_desktop_hierarchy_and_mobile_fallback():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert ".notification-settings-layout {\n    display: grid;" in css
    assert ".notification-primary-body {\n    display: grid;" in css
    assert ".notification-channel-disclosure > summary" in css
    assert ".notification-channel-panel {\n    display: grid;" in css

    tablet = css[css.index("@media (max-width: 991.98px)") :]
    assert ".notification-primary-body" in tablet
    assert "grid-template-columns: 1fr;" in tablet
    assert ".notification-event-groups" in tablet
