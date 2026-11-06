import asyncio

import pytest

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.pushplus_client import PushPlusClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}","link":"{{desktop_link}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert "task=%F0%9F%9A%A8+%E6%96%B0%E6%8E%A8%E8%8D%90%21+Sony+A7M4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["data"] is None


def _product():
    return {
        "商品标题": "Sony A7M4",
        "当前售价": "9999",
        "商品链接": "https://www.goofish.com/item/123",
    }


def test_pushplus_client_sends_text_template_and_requires_business_code(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)
    client = PushPlusClient("test-token", "group-code", pcurl_to_mobile=False)

    asyncio.run(client.send(_product(), "价格合适"))

    assert captured["url"] == "https://www.pushplus.plus/send"
    assert captured["json"]["token"] == "test-token"
    assert captured["json"]["topic"] == "group-code"
    assert captured["json"]["template"] == "txt"
    assert captured["json"]["content"].startswith("价格: 9999\n原因: 价格合适")


def test_pushplus_client_rejects_business_error_with_http_200(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 903, "msg": "无效的用户token"}

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: _FakeResponse(),
    )
    client = PushPlusClient("bad-token", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError, match="无效的用户token"):
        asyncio.run(client.send(_product(), "价格合适"))


def _recommended_product():
    return {
        "商品标题": "ThinkPad <T14> & 配件",
        "当前售价": "1850",
        "商品链接": "https://www.goofish.com/item/123",
        "ai_analysis": {
            "is_recommended": True,
            "risk_tags": [
                "需确认：电池健康度和循环次数",
                "需确认: 维修、拆换件及进水历史",
                "图片显示为T14s，并非标题所写T14",
                "需确认：电池健康度和循环次数",
                "",
                "需确认：",
            ],
        },
    }


def test_build_message_splits_risk_tags_into_checklist_and_notes():
    message = _OkClient(enabled=True, pcurl_to_mobile=False)._build_message(
        _recommended_product(), "有条件推荐：配置和价格符合要求"
    )

    assert message.confirm_items == ("电池健康度和循环次数", "维修、拆换件及进水历史")
    assert message.risk_notes == ("图片显示为T14s，并非标题所写T14",)
    assert message.content == (
        "价格: 1850\n"
        "原因: 有条件推荐：配置和价格符合要求\n"
        "注意: 图片显示为T14s，并非标题所写T14\n"
        "聊卖家前需确认:\n"
        "1. 电池健康度和循环次数\n"
        "2. 维修、拆换件及进水历史\n"
        "链接: https://www.goofish.com/item/123"
    )


def test_build_message_without_analysis_keeps_the_old_layout():
    message = _OkClient(enabled=True, pcurl_to_mobile=False)._build_message(_product(), "价格合适")
    assert message.confirm_items == () and message.risk_notes == ()
    assert message.content == "价格: 9999\n原因: 价格合适\n链接: https://www.goofish.com/item/123"


def test_telegram_client_renders_checklist_and_escapes_html(monkeypatch):
    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)
    client = TelegramClient("123:token", "4567", pcurl_to_mobile=False)

    asyncio.run(client.send(_recommended_product(), "有条件推荐：<配置>符合"))

    assert captured["url"] == "https://api.telegram.org/bot123:token/sendMessage"
    assert captured["json"]["chat_id"] == "4567"
    assert captured["json"]["parse_mode"] == "HTML"
    text = captured["json"]["text"]
    assert "<b>ThinkPad &lt;T14&gt; &amp; 配件</b>" in text
    assert "📝 原因: 有条件推荐：&lt;配置&gt;符合" in text
    assert "⚠️ 注意:\n• 图片显示为T14s，并非标题所写T14" in text
    assert "❓ 聊卖家前需确认:\n1. 电池健康度和循环次数\n2. 维修、拆换件及进水历史" in text
    assert text.endswith("💻 <a href='https://www.goofish.com/item/123'>电脑端链接</a>")


def test_telegram_client_raises_on_api_error(monkeypatch):
    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "description": "Bad Request: chat not found"}

    monkeypatch.setattr("requests.post", lambda *args, **kwargs: _FakeResponse())
    client = TelegramClient("123:token", "4567", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError, match="chat not found"):
        asyncio.run(client.send(_product(), "价格合适"))
