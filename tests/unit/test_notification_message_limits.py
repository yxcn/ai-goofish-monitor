"""通知正文长度：Telegram 4096 字符、企业微信 4096 字节，超长先砍 AI 理由，保留标题/价格/链接。"""
import asyncio

from src.infrastructure.external.notification_clients.base import (
    TELEGRAM_TEXT_LIMIT,
    WECOM_MARKDOWN_LIMIT_BYTES,
    NotificationMessage,
    render_within_limit,
    truncate_message,
    utf8_length,
    utf16_length,
)
from src.infrastructure.external.notification_clients.telegram_client import TelegramClient
from src.infrastructure.external.notification_clients.wecom_bot_client import WeComBotClient

LINK = "https://www.goofish.com/item/123"


def _message(**overrides) -> NotificationMessage:
    base = dict(
        title="Sony A7M4 全套",
        price="¥9999",
        reason="理由",
        desktop_link=LINK,
        mobile_link=None,
        notification_title="🚨 新推荐! Sony A7M4 全套",
        content="",
        image_url=None,
    )
    base.update(overrides)
    return NotificationMessage(**base)


# -- truncate_message --


def test_truncate_message_by_characters():
    assert truncate_message("abcdefghij", 5) == "abcd…"
    assert truncate_message("short", 10) == "short"
    assert truncate_message("abc", 0) == ""
    assert truncate_message("abc", 1) == "…"
    assert truncate_message("ab cd ef", 6) == "ab cd…"  # 省略号前的空白去掉


def test_truncate_message_by_utf8_bytes_never_splits_a_character():
    text = "中" * 10  # 30 字节
    result = truncate_message(text, 10, measure=utf8_length)
    assert result == "中中…"
    assert utf8_length(result) <= 10
    assert truncate_message(text, 30, measure=utf8_length) == text


def test_utf16_length_counts_emoji_as_two_units():
    assert utf16_length("🚨") == 2
    assert len("🚨") == 1
    assert utf16_length("abc中") == 4


# -- render_within_limit --


def _render(message: NotificationMessage) -> str:
    lines = [message.title, f"价格: {message.price}", f"原因: {message.reason}"]
    if message.value_summary:
        lines.append(f"性价比: {message.value_summary}")
    if message.seller_signals:
        lines.append("信号: " + " · ".join(message.seller_signals))
    lines.append(message.desktop_link)
    return "\n".join(lines)


def test_render_within_limit_returns_unchanged_when_it_fits():
    message = _message(reason="短理由")
    assert render_within_limit(message, _render, 500) == _render(message)


def test_render_within_limit_cuts_reason_first_and_keeps_other_fields():
    message = _message(reason="理" * 500, value_summary="值" * 40, seller_signals=("信号一", "信号二"))

    result = render_within_limit(message, _render, 200)

    assert len(result) <= 200
    assert "原因: 理" in result and "…" in result
    assert "性价比: " + "值" * 40 in result  # 理由足够长，砍理由就够了
    assert "信号: 信号一 · 信号二" in result
    assert result.endswith(LINK)
    assert result.startswith("Sony A7M4 全套")


def test_render_within_limit_drops_optional_fields_when_reason_is_short():
    message = _message(
        reason="短理由",
        value_summary="值" * 300,
        seller_signals=tuple(f"信号{i}" for i in range(40)),
    )

    result = render_within_limit(message, _render, 120)

    assert len(result) <= 120
    assert "原因: 短理由" in result  # 理由本来就短，不动
    assert "性价比" not in result    # 性价比被整段去掉
    assert result.endswith(LINK)
    assert result.startswith("Sony A7M4 全套")


def test_render_within_limit_keeps_a_stub_of_reason_instead_of_dropping_it():
    message = _message(reason="理" * 300)

    result = render_within_limit(message, _render, 100)

    assert len(result) <= 100
    assert "原因: 理" in result and "原因: None" not in result
    assert "理…" in result  # 砍到最短也留个带省略号的残句
    assert result.endswith(LINK)


def test_render_within_limit_hard_truncates_as_last_resort():
    message = _message(title="标" * 300, reason="x")
    result = render_within_limit(message, _render, 100)
    assert len(result) <= 100 and result.endswith("…")


def test_render_within_limit_measures_in_bytes_when_asked():
    message = _message(reason="理" * 2000)
    result = render_within_limit(message, _render, 300, measure=utf8_length)
    assert utf8_length(result) <= 300
    assert result.endswith(LINK)


# -- 渠道 --


def _long_product():
    return {
        "商品标题": "Sony A7M4 + 24-70 F2.8 GM II 全套",
        "当前售价": "¥15999",
        "商品链接": LINK,
        "ai_analysis": {
            "is_recommended": True,
            "reason": "理由" * 3000,
            "value_summary": "性价比说明" * 200,
            "risk_tags": ["需确认 电池循环次数", "成色描述与图片不符"],
            "criteria_analysis": {"seller_type": {"status": "PASS"}},
        },
        "卖家画像": {"结论": "个人卖家", "信号": [f"信号{i}" for i in range(50)]},
    }


def test_telegram_text_is_capped_at_4096_utf16_units_and_keeps_links(monkeypatch):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Response()

    monkeypatch.setattr("requests.post", _fake_post)
    client = TelegramClient(bot_token="1:x", chat_id="1", pcurl_to_mobile=False)

    asyncio.run(client.send(_long_product(), "理由" * 3000))

    text = captured["json"]["text"]
    assert utf16_length(text) <= TELEGRAM_TEXT_LIMIT
    assert f"<a href='{LINK}'>电脑端链接</a>" in text
    assert "💰 价格: <b>¥15999</b>" in text
    assert "📝 原因: 理由" in text and "…" in text
    # 没有被硬截断：HTML 标签完好
    assert text.count("<a ") == text.count("</a>")


def test_wecom_markdown_is_capped_at_4096_bytes_and_keeps_links(monkeypatch):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Response()

    monkeypatch.setattr("requests.post", _fake_post)
    client = WeComBotClient(bot_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", pcurl_to_mobile=False)

    asyncio.run(client.send(_long_product(), "理由" * 3000))

    content = captured["json"]["markdown"]["content"]
    assert utf8_length(content) <= WECOM_MARKDOWN_LIMIT_BYTES
    assert content.startswith("## 🚨 强烈推荐!")
    assert f"- 电脑端链接: [{LINK}]({LINK})" in content
    assert "- 价格: ¥15999" in content
    assert "- 原因: 理由" in content and "…" in content


def test_short_messages_are_not_touched(monkeypatch):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0, "ok": True}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Response()

    monkeypatch.setattr("requests.post", _fake_post)
    product = {"商品标题": "T14", "当前售价": "¥1000", "商品链接": LINK}

    asyncio.run(WeComBotClient(bot_url="https://example.test/hook?key=k", pcurl_to_mobile=False).send(product, "价格合适"))
    assert "…" not in captured["json"]["markdown"]["content"]

    asyncio.run(TelegramClient(bot_token="1:x", chat_id="1", pcurl_to_mobile=False).send(product, "价格合适"))
    assert "…" not in captured["json"]["text"]
