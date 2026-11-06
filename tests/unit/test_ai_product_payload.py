"""送给模型的商品 JSON 精简：去图片 URL、截短链接、卖家商品 / 评价列表限 30 条、紧凑序列化。"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import src.ai_handler as ai_handler
import src.config as app_config
from src.infrastructure.external.ai_client import (
    AI_PAYLOAD_MAX_RATINGS,
    AI_PAYLOAD_MAX_SELLER_ITEMS,
    AI_PAYLOAD_RATING_TEXT_CHARS,
    AIClient,
    build_ai_product_payload,
    dump_ai_product_json,
)


IMAGE_URL = "https://img.example/pic/{n}.jpg?x-oss-process=image/resize,w_800"
LONG_LINK = (
    "https://www.goofish.com/item?id=123456789&spm=a21ybx.search.searchFeedList.1"
    "&categoryId=50023914&trackParams=" + "t" * 300
)


def _build_record() -> dict:
    return {
        "爬取时间": "2026-01-01T10:00:00",
        "搜索关键字": "sony a7m4",
        "任务名称": "Sony A7M4",
        "商品信息": {
            "商品ID": "123456789",
            "商品标题": "Sony A7M4 国行",
            "当前售价": "¥9000",
            "商品链接": LONG_LINK,
            "商品图片列表": [IMAGE_URL.format(n=i) for i in range(10)],
            "商品主图链接": IMAGE_URL.format(n=0),
            "商品描述": "自用一年，快门 1 万，无磕碰。",
            "商品属性": {"成色": "99新", "品牌": "索尼"},
        },
        "卖家信息": {
            "卖家昵称": "小明",
            "卖家头像链接": "https://img.example/avatar.jpg",
            "卖家信用等级": "优秀",
            "作为卖家的好评数": 12,
            "作为买家的好评数": 30,
            "卖家收到的评价总数": 50,
            "卖家发布的商品列表": [
                {
                    "商品ID": f"item-{i}",
                    "商品标题": f"在售商品 {i}",
                    "商品价格": str(100 + i),
                    "商品主图": IMAGE_URL.format(n=100 + i),
                    "商品状态": "在售" if i % 2 else "已售",
                    "商品标签": ["包邮", "验货宝"],
                    "发布标签": "48小时内发布",
                    "想要人数": i,
                    "品类ID": 50023914,
                }
                for i in range(40)
            ],
            "卖家收到的评价列表": [
                {
                    "评价ID": f"rate-{i}",
                    "评价内容": f"第{i}条评价：" + "非常好的买家，沟通顺畅，发货迅速，期待下次合作。" * 8,
                    "评价类型": "好评",
                    "评价来源角色": "卖家" if i % 3 else "买家",
                    "该用户在此交易中的角色": "买家" if i % 3 else "卖家",
                    "评价者昵称": f"用户{i}",
                    "评价时间": f"2025-0{(i % 9) + 1}-01",
                    "评价图片": [IMAGE_URL.format(n=200 + i + k) for k in range(3)],
                    "评价者IP属地": "中国湖北省",
                }
                for i in range(50)
            ],
            "卖家芝麻信用": "优秀",
            "卖家注册时长": "来闲鱼6年",
        },
        "卖家画像": {"摘要": "个人玩家", "指标": {"在售": 20}},
        "价格参考": {"本商品价格位置": {"price_verdict_text": "偏低"}},
    }


def test_build_ai_product_payload_shrinks_json_and_keeps_key_fields():
    record = _build_record()
    before = copy.deepcopy(record)

    payload = build_ai_product_payload(record)
    compact = dump_ai_product_json(record)
    original = json.dumps(record, ensure_ascii=False, indent=2)

    # 入参不被改动
    assert record == before
    # 明显变小
    assert len(compact) < len(original) // 3

    item = payload["商品信息"]
    assert "商品图片列表" not in item
    assert "商品主图链接" not in item
    assert item["商品链接"] == "https://www.goofish.com/item?id=123456789"
    assert item["商品ID"] == "123456789"
    assert item["商品描述"] == record["商品信息"]["商品描述"]
    assert item["商品属性"] == {"成色": "99新", "品牌": "索尼"}

    seller = payload["卖家信息"]
    assert "卖家头像链接" not in seller
    assert seller["作为卖家的好评数"] == 12
    assert seller["作为买家的好评数"] == 30
    assert seller["卖家收到的评价总数"] == 50
    assert seller["卖家芝麻信用"] == "优秀"

    items = seller["卖家发布的商品列表"]
    assert len(items) == AI_PAYLOAD_MAX_SELLER_ITEMS == 30
    assert items[0] == {
        "商品标题": "在售商品 0",
        "商品价格": "100",
        "商品状态": "已售",
        "发布标签": "48小时内发布",
    }
    for entry in items:
        assert set(entry) <= {"商品标题", "商品价格", "商品状态", "发布标签"}

    ratings = seller["卖家收到的评价列表"]
    assert len(ratings) == AI_PAYLOAD_MAX_RATINGS == 30
    for entry in ratings:
        assert set(entry) == {"评价类型", "评价时间", "评价来源角色", "该用户在此交易中的角色", "评价内容"}
        assert len(entry["评价内容"]) <= AI_PAYLOAD_RATING_TEXT_CHARS
    assert ratings[0]["评价内容"].startswith("第0条评价：")
    assert ratings[0]["评价内容"].endswith("…")
    assert ratings[1]["评价来源角色"] == "卖家"
    assert ratings[1]["该用户在此交易中的角色"] == "买家"

    # 其他字段原样
    assert payload["卖家画像"] == record["卖家画像"]
    assert payload["价格参考"] == record["价格参考"]
    assert payload["任务名称"] == "Sony A7M4"

    # 紧凑序列化：不缩进、不转义中文、图片 URL 完全不出现
    assert "\n" not in compact
    assert '": ' not in compact and '", "' not in compact
    assert "Sony A7M4 国行" in compact
    assert "img.example" not in compact
    assert "trackParams" not in compact


def test_build_ai_product_payload_tolerates_missing_or_malformed_sections():
    assert build_ai_product_payload({}) == {}
    payload = build_ai_product_payload(
        {
            "商品信息": {"商品标题": "x", "商品链接": "https://www.goofish.com/item?spm=1"},
            "卖家信息": {"卖家发布的商品列表": "not-a-list", "卖家收到的评价列表": [None, {"评价内容": None}]},
        }
    )
    assert payload["商品信息"]["商品链接"] == "https://www.goofish.com/item"
    assert payload["卖家信息"]["卖家发布的商品列表"] == "not-a-list"
    assert payload["卖家信息"]["卖家收到的评价列表"] == [{"评价内容": None}]


def test_ai_client_build_messages_uses_compact_payload():
    client = AIClient.__new__(AIClient)

    messages = client._build_messages(_build_record(), [], "判断是否推荐。")

    content = messages[0]["content"]
    assert isinstance(content, str)
    assert "img.example" not in content
    assert "trackParams" not in content
    assert "https://www.goofish.com/item?id=123456789" in content
    assert "作为卖家的好评数" in content
    assert content.count("评价来源角色") == 30
    assert content.count("在售商品 ") == 30
    assert '"商品标题": "' not in content  # 没有 indent 风格的 ": "


def _build_fake_client(create_impl):
    responses = SimpleNamespace(create=create_impl)
    chat = SimpleNamespace(completions=SimpleNamespace(create=create_impl))
    return SimpleNamespace(responses=responses, chat=chat)


@pytest.fixture(autouse=True)
def _fresh_compat_profile(monkeypatch):
    ai_handler.reset_compat_profile()
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (0, 0, 0))
    yield
    ai_handler.reset_compat_profile()


def test_ai_handler_get_ai_analysis_sends_compact_payload(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sent = {}

    async def fake_create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(
            output_text=(
                '{"prompt_version":"v1","is_recommended":true,'
                '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
            )
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", False)

    result = asyncio.run(
        ai_handler.get_ai_analysis(_build_record(), image_paths=[], prompt_text="请输出 JSON")
    )

    assert result["is_recommended"] is True
    wire = json.dumps(sent, ensure_ascii=False, default=str)
    assert "img.example" not in wire
    assert "trackParams" not in wire
    assert "作为卖家的好评数" in wire
    assert "Sony A7M4 国行" in wire
