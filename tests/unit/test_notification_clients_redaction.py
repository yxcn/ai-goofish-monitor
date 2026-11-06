"""通知客户端：requests 报错里的 URL 密钥不能原样进日志 / 返回前端。"""
import asyncio

import pytest
import requests

from src.infrastructure.external.notification_clients.bark_client import BarkClient
from src.infrastructure.external.notification_clients.base import (
    REDACTED,
    rebuild_exception,
    redact_secrets,
    url_path_segments,
    url_query_values,
)
from src.infrastructure.external.notification_clients.gotify_client import GotifyClient
from src.infrastructure.external.notification_clients.ntfy_client import NtfyClient
from src.infrastructure.external.notification_clients.pushplus_client import PushPlusClient
from src.infrastructure.external.notification_clients.telegram_client import TelegramClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.infrastructure.external.notification_clients.wecom_bot_client import WeComBotClient

# 下面都是随手编的假密钥，只用于断言"不出现在报错里"
WECOM_KEY = "693a0a2b-1234-4cde-9fab-0123456789ab"
BARK_KEY = "AbCdEfGhIjKlMnOpQrStUv"
GOTIFY_TOKEN = "AqZ3kLmN9pQ2rStU"
NTFY_TOPIC = "my-private-topic-x7y8z9"
TG_TOKEN = "123456789:AAFakeTelegramBotTokenValue-xyz"
WEBHOOK_URL = "https://hooks.example.com/services/T000/B000/XXXXsecretpathXXXX"
PUSHPLUS_TOKEN = "pushplus-token-0123456789abcdef"

PRODUCT = {
    "商品标题": "Sony A7M4",
    "当前售价": "9999",
    "商品链接": "https://www.goofish.com/item/123",
}


def _http_error_for(url: str):
    def _fake(*_args, **_kwargs):
        raise requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")

    return _fake


def _connection_error_for(path: str):
    def _fake(*_args, **_kwargs):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='example.com', port=443): Max retries exceeded "
            f"with url: {path} (Caused by NewConnectionError('boom'))"
        )

    return _fake


# -- 纯函数 --


def test_redact_secrets_masks_query_values_userinfo_and_known_secrets():
    text = f"400 Client Error for url: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}&debug=1"
    out = redact_secrets(text)
    assert WECOM_KEY not in out
    assert f"key={REDACTED}" in out
    assert "debug=1" in out

    bare_path = f"Max retries exceeded with url: /message?token={GOTIFY_TOKEN} (Caused by ...)"
    out = redact_secrets(bare_path)
    assert GOTIFY_TOKEN not in out and f"token={REDACTED}" in out

    assert redact_secrets("https://user:pass@ntfy.example/topic") == f"https://{REDACTED}@ntfy.example/topic"

    out = redact_secrets(f"for url: https://api.day.app/{BARK_KEY}/", secrets=(BARK_KEY,))
    assert out == f"for url: https://api.day.app/{REDACTED}/"

    # Webhook：整个 URL 打码，其他文字保留
    assert redact_secrets("boom for url: https://hooks.example.com/abc?x=1 done", redact_all_urls=True) == (
        f"boom for url: {REDACTED} done"
    )

    # 太短的"密钥"不做全文替换，None / 空串直接忽略
    assert redact_secrets("ok 1 a", secrets=("1", None, "")) == "ok 1 a"


def test_redact_secrets_handles_url_encoded_secret():
    secret = "ab/cd+ef=="
    text = f"for url: https://x.example/send?note=1&tok={secret} and path /p/ab%2Fcd%2Bef%3D%3D"
    out = redact_secrets(text, secrets=(secret,))
    assert secret not in out
    assert "ab%2Fcd%2Bef%3D%3D" not in out


def test_url_helpers_extract_path_segments_and_sensitive_query_values():
    assert url_path_segments(f"https://api.day.app/{BARK_KEY}/") == (BARK_KEY,)
    assert url_path_segments(None) == ()
    assert url_query_values(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}&debug=1") == (WECOM_KEY,)
    assert url_query_values("https://x.example/?a=1") == ()


def test_rebuild_exception_keeps_type_and_drops_response_object():
    response = requests.Response()
    response.status_code = 401
    response.url = f"https://x.example/?key={WECOM_KEY}"
    original = requests.HTTPError("boom", response=response)

    rebuilt = rebuild_exception(original, "clean")

    assert type(rebuilt) is requests.HTTPError
    assert str(rebuilt) == "clean"
    assert rebuilt.response is None

    class _NeedsTwoArgs(Exception):
        def __init__(self, a, b):
            super().__init__(a, b)

    fallback = rebuild_exception(_NeedsTwoArgs(1, 2), "clean")
    assert type(fallback) is RuntimeError and str(fallback) == "clean"


# -- 各渠道 --


def _assert_sanitized(exc_info, expected_type, *secrets):
    exc = exc_info.value
    assert type(exc) is expected_type
    text = str(exc)
    for secret in secrets:
        assert secret not in text, text
    assert REDACTED in text
    # 不能通过异常链把原始异常（带完整 URL）带出去
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_wecom_client_redacts_key_in_requests_error(monkeypatch):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"
    monkeypatch.setattr("requests.post", _http_error_for(url))
    client = WeComBotClient(bot_url=url, pcurl_to_mobile=False)

    with pytest.raises(requests.HTTPError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.HTTPError, WECOM_KEY)
    assert "qyapi.weixin.qq.com" in str(exc_info.value)  # 主机名保留，方便排错


def test_bark_client_redacts_device_key_in_path(monkeypatch):
    url = f"https://api.day.app/{BARK_KEY}/"
    monkeypatch.setattr("requests.post", _http_error_for(url))
    client = BarkClient(bark_url=url, pcurl_to_mobile=False)

    with pytest.raises(requests.HTTPError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.HTTPError, BARK_KEY)


def test_gotify_client_redacts_token_query(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        _http_error_for(f"https://gotify.example/message?token={GOTIFY_TOKEN}"),
    )
    client = GotifyClient(gotify_url="https://gotify.example", gotify_token=GOTIFY_TOKEN, pcurl_to_mobile=False)

    with pytest.raises(requests.HTTPError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.HTTPError, GOTIFY_TOKEN)


def test_ntfy_client_redacts_topic_and_connection_errors(monkeypatch):
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    monkeypatch.setattr("requests.post", _connection_error_for(f"/{NTFY_TOPIC}"))
    client = NtfyClient(topic_url=url, pcurl_to_mobile=False)

    with pytest.raises(requests.ConnectionError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.ConnectionError, NTFY_TOPIC)


def test_telegram_client_redacts_bot_token_and_keeps_exception_type(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        _connection_error_for(f"/bot{TG_TOKEN}/sendMessage"),
    )
    client = TelegramClient(bot_token=TG_TOKEN, chat_id="42", pcurl_to_mobile=False)

    with pytest.raises(requests.ConnectionError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.ConnectionError, TG_TOKEN)


def test_telegram_client_redacts_token_in_api_error_description(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "description": f"Unauthorized for bot{TG_TOKEN}"}

    monkeypatch.setattr("requests.post", lambda *a, **k: _Response())
    client = TelegramClient(bot_token=TG_TOKEN, chat_id="42", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    assert TG_TOKEN not in str(exc_info.value)


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_webhook_client_redacts_whole_url(monkeypatch, method):
    rendered_url = f"{WEBHOOK_URL}?task=Sony+A7M4"
    monkeypatch.setattr("requests.post", _http_error_for(rendered_url))
    monkeypatch.setattr("requests.get", _http_error_for(rendered_url))
    client = WebhookClient(
        webhook_url=WEBHOOK_URL,
        webhook_method=method,
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}"}',
        pcurl_to_mobile=False,
    )

    with pytest.raises(requests.HTTPError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    _assert_sanitized(exc_info, requests.HTTPError, WEBHOOK_URL, "hooks.example.com")


def test_pushplus_client_wraps_requests_errors_with_same_type(monkeypatch):
    monkeypatch.setattr("requests.post", _http_error_for("https://www.pushplus.plus/send"))
    client = PushPlusClient(token=PUSHPLUS_TOKEN, pcurl_to_mobile=False)

    with pytest.raises(requests.HTTPError) as exc_info:
        asyncio.run(client.send(PRODUCT, "价格合适"))

    assert type(exc_info.value) is requests.HTTPError
    assert PUSHPLUS_TOKEN not in str(exc_info.value)
