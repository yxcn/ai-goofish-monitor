"""Webhook URL 拼接：原 URL 里的空值 / 重复参数不能丢，WEBHOOK_QUERY_PARAMETERS 追加在后面。"""
import asyncio
from urllib.parse import parse_qsl, urlsplit

from src.infrastructure.external.notification_clients.webhook_client import WebhookClient


PRODUCT = {
    "商品标题": "Sony A7M4",
    "当前售价": "¥9000",
    "商品链接": "https://www.goofish.com/item?id=1",
}


def _query_pairs(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def test_build_url_keeps_blank_and_duplicate_query_params():
    client = WebhookClient(
        webhook_url="https://hooks.example.com/send?token=&a=1&a=2",
        webhook_query_parameters='{"task":"{{title}}","empty":""}',
        pcurl_to_mobile=False,
    )
    message = client._build_message(PRODUCT, "价格合适")

    pairs = _query_pairs(client._build_url(message))

    assert pairs == [
        ("token", ""),
        ("a", "1"),
        ("a", "2"),
        ("task", message.notification_title),
        ("empty", ""),
    ]


def test_build_url_without_extra_params_preserves_original_query_exactly():
    client = WebhookClient(
        webhook_url="https://hooks.example.com/send?token=&a=1&a=2&b=x%20y",
        pcurl_to_mobile=False,
    )
    message = client._build_message(PRODUCT, "价格合适")

    url = client._build_url(message)

    assert url.startswith("https://hooks.example.com/send?")
    assert _query_pairs(url) == [("token", ""), ("a", "1"), ("a", "2"), ("b", "x y")]


def test_build_url_appends_params_with_same_name_instead_of_overwriting():
    client = WebhookClient(
        webhook_url="https://hooks.example.com/send?tag=fixed",
        webhook_query_parameters='{"tag":"{{price}}","flag":true,"n":3,"nil":null,"multi":["x","y"]}',
        pcurl_to_mobile=False,
    )
    message = client._build_message(PRODUCT, "价格合适")

    pairs = _query_pairs(client._build_url(message))

    assert pairs == [
        ("tag", "fixed"),
        ("tag", message.price),
        ("flag", "true"),
        ("n", "3"),
        ("nil", ""),
        ("multi", "x"),
        ("multi", "y"),
    ]


def test_get_request_uses_url_with_all_query_params(monkeypatch):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _Response()

    monkeypatch.setattr("requests.get", _fake_get)
    client = WebhookClient(
        webhook_url="https://hooks.example.com/send?token=&a=1&a=2",
        webhook_method="GET",
        webhook_query_parameters='{"task":"{{title}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send(PRODUCT, "价格合适"))

    pairs = _query_pairs(captured["url"])
    assert pairs[:3] == [("token", ""), ("a", "1"), ("a", "2")]
    assert pairs[3][0] == "task" and "Sony A7M4" in pairs[3][1]
