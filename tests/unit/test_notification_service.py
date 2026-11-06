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
            "reason": "有条件推荐：配置和价格符合要求",
            "value_score": "58",
            "value_summary": "1494元比当前搜索样本均价低约7%",
            "criteria_analysis": {
                "price": {"status": "PASS"},
                "memory": {"status": "pass"},
                "battery_health": {"status": "NEEDS_MANUAL_CHECK"},
                "seller_type": {"status": "PASS", "analysis_details": {}},
                "note": "not a criterion",
            },
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
    assert message.verdict == "有条件推荐"
    assert message.match_score == 58
    assert message.criteria_summary == "3 项通过，1 项待确认"
    assert message.notification_title == "🚨 有条件推荐! ThinkPad <T14> & 配件"
    assert message.content == (
        "结论: 有条件推荐（AI 匹配度 58%）\n"
        "标准: 3 项通过，1 项待确认\n"
        "价格: 1850\n"
        "原因: 有条件推荐：配置和价格符合要求\n"
        "性价比: 1494元比当前搜索样本均价低约7%\n"
        "注意: 图片显示为T14s，并非标题所写T14\n"
        "聊卖家前需确认:\n"
        "1. 电池健康度和循环次数\n"
        "2. 维修、拆换件及进水历史\n"
        "链接: https://www.goofish.com/item/123"
    )


def test_build_message_without_analysis_keeps_the_old_layout():
    message = _OkClient(enabled=True, pcurl_to_mobile=False)._build_message(_product(), "价格合适")
    assert message.confirm_items == () and message.risk_notes == ()
    assert message.verdict is None and message.match_score is None and message.criteria_summary is None
    assert message.notification_title == "🚨 新推荐! Sony A7M4"
    assert message.content == "价格: 9999\n原因: 价格合适\n链接: https://www.goofish.com/item/123"


def test_verdict_follows_the_result_card_rules():
    from src.infrastructure.external.notification_clients.base import (
        match_score,
        recommendation_verdict,
        summarize_criteria,
    )

    assert recommendation_verdict({"is_recommended": True, "reason": "配置很好"}) == "强烈推荐"
    assert recommendation_verdict({"is_recommended": True, "reason": "  有条件推荐，需确认电池"}) == "有条件推荐"
    assert recommendation_verdict({"is_recommended": False, "reason": "有条件推荐"}) == "不建议购买"
    assert recommendation_verdict({"reason": "还没分析"}) is None
    assert recommendation_verdict(None) is None

    assert match_score({"value_score": 82.6}) == 83
    assert match_score({"value_score": "120"}) == 100
    assert match_score({"value_score": "high"}) is None
    assert match_score({"value_score": True}) is None
    assert match_score({}) is None

    assert summarize_criteria({"criteria_analysis": {"a": {"status": "FAIL"}, "b": {"status": "needs_manual_check"}}}) == (0, 1, 1)
    assert summarize_criteria({}) == (0, 0, 0)


def test_strong_recommendation_title_and_content():
    product = {**_product(), "ai_analysis": {"is_recommended": True, "reason": "配置很好", "value_score": 90}}
    message = _OkClient(enabled=True, pcurl_to_mobile=False)._build_message(product, "配置很好")
    assert message.notification_title == "🚨 强烈推荐! Sony A7M4"
    assert message.content.startswith("结论: 强烈推荐（AI 匹配度 90%）\n价格: 9999\n原因: 配置很好\n")


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
    assert "⭐ 结论: <b>有条件推荐</b> · AI 匹配度 58%\n📋 标准: 3 项通过，1 项待确认\n💰 价格: <b>1850</b>\n📝 原因" in text
    assert "📝 原因: 有条件推荐：&lt;配置&gt;符合\n💹 性价比: 1494元比当前搜索样本均价低约7%" in text
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


def test_telegram_client_redacts_bot_token_from_network_errors(monkeypatch):
    import requests

    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient

    def _boom(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr("requests.post", _boom)
    client = TelegramClient("123:secret-token", "4567", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(client.send(_product(), "价格合适"))
    assert "secret-token" not in str(excinfo.value)
    assert "/bot***/sendMessage" in str(excinfo.value)


def _listing_with_facts():
    return {
        **_product(),
        "当前售价": "¥1494",
        "商品原价": "¥2300",
        "商品标签": ["包邮", "验货宝", "包邮", ""],
        "“想要”人数": 8,
        "浏览量": "208",
        "发布时间": "2026-11-05 12:29",
        "发货地区": "山东",
    }


def test_build_message_includes_listing_facts():
    message = _OkClient(enabled=True, pcurl_to_mobile=False)._build_message(_listing_with_facts(), "价格合适")
    assert message.original_price == "¥2300"
    assert message.tags == ("包邮", "验货宝")
    assert (message.wants_count, message.view_count) == (8, 208)
    assert message.content.startswith(
        "价格: ¥1494（原价 ¥2300） 包邮 · 验货宝\n"
        "热度: 8 人想要 · 208 次浏览 · 11-05 12:29 发布 · 山东\n"
        "原因: 价格合适\n"
    )


def test_listing_facts_skip_placeholders_and_same_original_price():
    from src.infrastructure.external.notification_clients.base import listing_facts

    facts = listing_facts(
        {"当前售价": "¥1494", "商品原价": "1494", "商品标签": [], "“想要”人数": "NaN", "浏览量": "-", "发布时间": "", "发货地区": "暂无"}
    )
    assert facts == {
        "original_price": None, "tags": (), "wants_count": None, "view_count": None, "publish_time": None, "region": None,
    }


def test_telegram_client_renders_listing_facts(monkeypatch):
    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    monkeypatch.setattr("requests.post", lambda url, json=None, headers=None, timeout=None: captured.update(json=json) or _FakeResponse())
    client = TelegramClient("123:token", "4567", pcurl_to_mobile=False)

    asyncio.run(client.send(_listing_with_facts(), "价格合适"))

    text = captured["json"]["text"]
    assert "💰 价格: <b>¥1494</b> <s>¥2300</s> · 包邮 · 验货宝\n👀 8 人想要 · 208 次浏览 · 11-05 12:29 发布 · 山东\n📝 原因: 价格合适" in text
