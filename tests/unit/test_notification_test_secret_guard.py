import pytest

from src.services.notification_config_service import (
    NotificationSettingsValidationError,
    _build_notification_settings_model,
    prepare_notification_test_settings,
)


def _stored():
    return _build_notification_settings_model({
        "telegram_bot_token": "123456:stored-token",
        "telegram_chat_id": "42",
        "telegram_api_base_url": "https://api.telegram.org",
        "gotify_url": "https://gotify.example",
        "gotify_token": "gotify-stored",
        "wx_bot_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stored",
        "pcurl_to_mobile": True,
    })


def test_changing_telegram_base_url_without_token_is_rejected():
    with pytest.raises(NotificationSettingsValidationError, match="TELEGRAM_BOT_TOKEN"):
        prepare_notification_test_settings(
            {"TELEGRAM_API_BASE_URL": "https://attacker.example"}, _stored(), channel="telegram"
        )
    # 不指定渠道的整体测试同样受保护
    with pytest.raises(NotificationSettingsValidationError, match="TELEGRAM_BOT_TOKEN"):
        prepare_notification_test_settings({"TELEGRAM_API_BASE_URL": "https://attacker.example"}, _stored())


def test_same_or_omitted_url_keeps_using_stored_token():
    merged = prepare_notification_test_settings({"TELEGRAM_CHAT_ID": "7"}, _stored(), channel="telegram")
    assert merged.telegram_bot_token == "123456:stored-token"
    assert merged.telegram_chat_id == "7"

    merged = prepare_notification_test_settings(
        {"TELEGRAM_API_BASE_URL": "HTTPS://api.telegram.org/"}, _stored(), channel="telegram"
    )
    assert merged.telegram_bot_token == "123456:stored-token"


def test_new_url_with_new_secret_is_allowed():
    merged = prepare_notification_test_settings(
        {"TELEGRAM_API_BASE_URL": "https://proxy.example", "TELEGRAM_BOT_TOKEN": "999:new"},
        _stored(),
        channel="telegram",
    )
    assert merged.telegram_bot_token == "999:new"
    assert merged.telegram_api_base_url == "https://proxy.example"

    with pytest.raises(NotificationSettingsValidationError, match="GOTIFY_TOKEN"):
        prepare_notification_test_settings({"GOTIFY_URL": "https://evil.example"}, _stored(), channel="gotify")
    merged = prepare_notification_test_settings(
        {"GOTIFY_URL": "https://evil.example", "GOTIFY_TOKEN": "t2"}, _stored(), channel="gotify"
    )
    assert merged.gotify_token == "t2"


def test_url_that_is_itself_the_secret_can_be_replaced_directly():
    # 企业微信的 key 就在 URL 里，换 URL 等于用户自己提供了新密钥，不需要再要别的字段
    merged = prepare_notification_test_settings(
        {"WX_BOT_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new"}, _stored(), channel="wecom"
    )
    assert merged.wx_bot_url.endswith("key=new")
