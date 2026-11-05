from src.keyword_rule_engine import build_search_text, evaluate_keyword_rules
from src.parsers import (
    ITEM_DESCRIPTION_TRUNCATED_SUFFIX,
    MAX_ITEM_DESCRIPTION_CHARS,
    parse_item_detail_extras,
)


def _sample_item_do():
    return {
        "title": "ThinkPad X1 Carbon",
        "desc": "  自用 X1 Carbon Gen10，电池健康 92%，无拆修，保修到明年三月。\n  ",
        "itemLabelExtList": [
            {"propertyText": "成色", "text": "几乎全新"},
            {"propertyText": "品牌", "text": "联想"},
            {"propertyText": "分类", "text": "笔记本电脑"},
        ],
    }


def test_extracts_description_and_properties():
    extras = parse_item_detail_extras(_sample_item_do())

    assert extras["商品描述"] == "自用 X1 Carbon Gen10，电池健康 92%，无拆修，保修到明年三月。"
    assert extras["商品属性"] == {
        "成色": "几乎全新",
        "品牌": "联想",
        "分类": "笔记本电脑",
    }


def test_missing_or_empty_fields_are_omitted():
    assert parse_item_detail_extras({}) == {}
    assert parse_item_detail_extras(None) == {}
    assert parse_item_detail_extras({"desc": "   ", "itemLabelExtList": []}) == {}
    assert parse_item_detail_extras({"desc": None, "itemLabelExtList": None}) == {}


def test_malformed_labels_are_skipped_and_first_value_wins():
    extras = parse_item_detail_extras(
        {
            "itemLabelExtList": [
                "not-a-dict",
                {"propertyText": "成色"},
                {"text": "缺少属性名"},
                {"propertyText": "成色", "text": "轻微使用痕迹"},
                {"propertyText": "成色", "text": "重复的成色"},
            ]
        }
    )

    assert "商品描述" not in extras
    assert extras["商品属性"] == {"成色": "轻微使用痕迹"}


def test_long_description_is_truncated():
    long_desc = "电" * (MAX_ITEM_DESCRIPTION_CHARS + 500)

    description = parse_item_detail_extras({"desc": long_desc})["商品描述"]

    assert description.endswith(ITEM_DESCRIPTION_TRUNCATED_SUFFIX)
    assert len(description) == MAX_ITEM_DESCRIPTION_CHARS + len(ITEM_DESCRIPTION_TRUNCATED_SUFFIX)


def test_keyword_rules_can_match_description_text():
    item_data = {"商品标题": "ThinkPad X1 Carbon 笔记本"}
    item_data.update(parse_item_detail_extras(_sample_item_do()))
    record = {"商品信息": item_data, "卖家信息": {}}

    result = evaluate_keyword_rules(["无拆修"], build_search_text(record))

    assert result["is_recommended"] is True
    assert result["matched_keywords"] == ["无拆修"]
