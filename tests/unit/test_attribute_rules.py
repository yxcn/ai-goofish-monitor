import pytest

from src.attribute_rules import (
    evaluate_attribute_rules,
    normalize_attribute_rules,
    parse_attribute_rule,
    parse_attribute_rules,
)

LAPTOP_RULES = """
分类 = 笔记本电脑
内存容量 >= 16GB
硬盘容量 >= 256GB
拆修 包含 无
"""


def test_parse_rule_splits_attribute_operator_and_value():
    rule = parse_attribute_rule("  内存容量>=16GB ")
    assert (rule.attribute, rule.operator, rule.value) == ("内存容量", ">=", "16GB")
    assert parse_attribute_rule("分类 == 笔记本电脑").operator == "="
    assert parse_attribute_rule("拆修 不包含 维修").operator == "不包含"


@pytest.mark.parametrize(
    "bad",
    ["内存容量", "内存容量 >=", ">= 16GB", "内存容量 >= 很大", "   "],
)
def test_parse_rule_rejects_malformed_lines(bad):
    with pytest.raises(ValueError):
        parse_attribute_rule(bad)


def test_normalize_splits_lines_dedupes_and_canonicalizes_spacing():
    assert normalize_attribute_rules(LAPTOP_RULES) == [
        "分类 = 笔记本电脑",
        "内存容量 >= 16GB",
        "硬盘容量 >= 256GB",
        "拆修 包含 无",
    ]
    assert normalize_attribute_rules(["内存容量>=16GB", "内存容量 >= 16gb", ""]) == ["内存容量 >= 16GB"]
    assert normalize_attribute_rules(None) == []


def test_laptop_rules_reject_phones_and_8gb_machines():
    rules = parse_attribute_rules(LAPTOP_RULES)

    phone = {"分类": "手机", "运行内存": "12GB", "存储容量": "256GB"}
    assert evaluate_attribute_rules(phone, rules) == ["分类 = 笔记本电脑（实际：手机）"]

    small = {"分类": "笔记本电脑", "内存容量": "8GB", "硬盘容量": "512GB", "拆修": "无任何拆修"}
    assert evaluate_attribute_rules(small, rules) == ["内存容量 >= 16GB（实际：8GB）"]

    repaired = {"分类": "笔记本电脑", "内存容量": "16GB", "硬盘容量": "256GB", "拆修": "有维修"}
    assert evaluate_attribute_rules(repaired, rules) == ["拆修 包含 无（实际：有维修）"]

    # 闲鱼的拆修属性值有「无任何拆修」「无第三方维修」「有维修」：按子串排除“维修”会把“无第三方维修”也否掉
    good = {"分类": "笔记本电脑", "内存容量": "16GB", "硬盘容量": "1T", "拆修": "无第三方维修"}
    assert evaluate_attribute_rules(good, rules) == []
    untouched = {"分类": "笔记本电脑", "内存容量": "16GB", "硬盘容量": "512GB", "拆修": "无任何拆修"}
    assert evaluate_attribute_rules(untouched, rules) == []


def test_missing_or_unparseable_attributes_are_left_to_the_ai():
    rules = parse_attribute_rules(LAPTOP_RULES)
    assert evaluate_attribute_rules({}, rules) == []
    assert evaluate_attribute_rules({"分类": "笔记本电脑", "内存容量": "待确认"}, rules) == []
    assert evaluate_attribute_rules(None, rules) == []


def test_capacity_units_and_text_operators():
    assert evaluate_attribute_rules({"硬盘容量": "1T"}, parse_attribute_rules("硬盘容量 >= 512GB")) == []
    assert evaluate_attribute_rules({"硬盘容量": "500MB"}, parse_attribute_rules("硬盘容量 >= 1GB")) != []
    assert evaluate_attribute_rules({"屏幕尺寸": "14-14.9英寸"}, parse_attribute_rules("屏幕尺寸 >= 14")) == []
    assert evaluate_attribute_rules({"屏幕尺寸": "14-14.9英寸"}, parse_attribute_rules("屏幕尺寸 >= 15")) != []
    assert evaluate_attribute_rules({"品牌": "Lenovo/联想"}, parse_attribute_rules("品牌 包含 联想")) == []
    assert evaluate_attribute_rules({"品牌": "Lenovo/联想"}, parse_attribute_rules("品牌 = 联想")) != []
    assert evaluate_attribute_rules({"成色": "几乎全新"}, parse_attribute_rules("成色 != 明显使用痕迹")) == []
    assert evaluate_attribute_rules({"分类": "笔记本 电脑"}, parse_attribute_rules("分类 = 笔记本电脑")) == []
