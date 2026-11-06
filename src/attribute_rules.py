"""
商品属性硬规则。

闲鱼详情接口会给出结构化属性（分类、品牌、内存容量、硬盘容量、拆修、成色……），
这些比标题可靠得多。任务可以配置几条确定性规则，在采集卖家资料和调用 AI 之前
把明显不合格的商品挡掉，既省钱也省时间。

规则格式：每行一条 ``属性名 运算符 值``，例如::

    分类 = 笔记本电脑
    内存容量 >= 16GB
    硬盘容量 >= 256GB
    拆修 不包含 维修

运算符：``=`` / ``!=``（整体相等，忽略大小写和空格）、``包含`` / ``不包含``（子串）、
``>=`` ``<=`` ``>`` ``<``（数值比较，支持 GB/TB 等容量单位，1T 按 1024GB 算）。

商品没有这个属性、或属性值解析不出数字时，这条规则跳过，交给后面的 AI 判断；
只有属性明确存在且不满足时才判定不通过。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

_RULE_PATTERN = re.compile(
    r"^\s*(?P<attr>[^<>=!]+?)\s*(?P<op>>=|<=|!=|==|=|>|<|不包含|包含)\s*(?P<value>.+?)\s*$"
)
_NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")
_UNIT_MULTIPLIERS = {
    "": 1.0,
    "g": 1.0,
    "gb": 1.0,
    "t": 1024.0,
    "tb": 1024.0,
    "m": 1.0 / 1024,
    "mb": 1.0 / 1024,
}
_NUMERIC_OPERATORS = {">=", "<=", ">", "<"}


@dataclass(frozen=True)
class AttributeRule:
    attribute: str
    operator: str
    value: str
    raw: str

    def describe(self) -> str:
        return f"{self.attribute} {self.operator} {self.value}"


def parse_attribute_rule(text: str) -> AttributeRule:
    """解析单条规则，格式不对时抛 ValueError（带上原文，方便在界面上提示）。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("规则不能为空")
    match = _RULE_PATTERN.match(raw)
    if not match:
        raise ValueError(f"无法解析属性规则「{raw}」，格式应为：属性名 运算符 值，例如：内存容量 >= 16GB")
    attribute = match.group("attr").strip()
    operator = "=" if match.group("op") == "==" else match.group("op")
    value = match.group("value").strip()
    if not attribute or not value:
        raise ValueError(f"无法解析属性规则「{raw}」：属性名和值都不能为空")
    if operator in _NUMERIC_OPERATORS and _parse_number(value) is None:
        raise ValueError(f"属性规则「{raw}」使用了数值比较，但「{value}」不是数字")
    return AttributeRule(attribute=attribute, operator=operator, value=value, raw=raw)


def normalize_attribute_rules(values: Iterable[str] | str | None) -> List[str]:
    """拆行、去空、去重并校验格式，返回规范化后的规则文本列表。"""
    if values is None:
        return []
    raw_lines: List[str] = []
    if isinstance(values, str):
        raw_lines.extend(values.splitlines())
    else:
        for value in values:
            raw_lines.extend(str(value).splitlines())

    normalized: List[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        if not line.strip():
            continue
        rule = parse_attribute_rule(line)
        text = rule.describe()
        key = _normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def parse_attribute_rules(values: Iterable[str] | str | None) -> List[AttributeRule]:
    return [parse_attribute_rule(text) for text in normalize_attribute_rules(values)]


def evaluate_attribute_rules(attributes: dict | None, rules: Iterable[AttributeRule]) -> List[str]:
    """返回未通过的规则说明；空列表表示全部通过（或无法判断）。"""
    attributes = attributes or {}
    failures: List[str] = []
    for rule in rules:
        actual = attributes.get(rule.attribute)
        if actual is None or not str(actual).strip():
            continue  # 商品没写这个属性，交给 AI
        actual_text = _normalize_text(str(actual))
        expected_text = _normalize_text(rule.value)

        if rule.operator in _NUMERIC_OPERATORS:
            actual_number = _parse_number(actual_text)
            expected_number = _parse_number(expected_text)
            if actual_number is None or expected_number is None:
                continue  # 属性值不是数字（如“待确认”），无法判断
            passed = _compare(actual_number, expected_number, rule.operator)
        elif rule.operator == "=":
            passed = actual_text == expected_text
        elif rule.operator == "!=":
            passed = actual_text != expected_text
        elif rule.operator == "包含":
            passed = expected_text in actual_text
        elif rule.operator == "不包含":
            passed = expected_text not in actual_text
        else:  # pragma: no cover - parse_attribute_rule 已限制运算符
            continue

        if not passed:
            failures.append(f"{rule.describe()}（实际：{actual}）")
    return failures


def _normalize_text(text: str) -> str:
    return "".join(str(text or "").lower().split())


def _parse_number(text: str) -> Optional[float]:
    match = _NUMBER_PATTERN.search(str(text or ""))
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = _UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        multiplier = 1.0  # 未知单位（如英寸）按原数值比较
    return number * multiplier


def _compare(actual: float, expected: float, operator: str) -> bool:
    if operator == ">=":
        return actual >= expected
    if operator == "<=":
        return actual <= expected
    if operator == ">":
        return actual > expected
    return actual < expected
