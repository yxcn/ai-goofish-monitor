"""
商品标题预筛选。

闲鱼搜索是模糊匹配，搜 "ThinkPad T14" 也会夹带手机等无关商品。
任务可以配置 ``title_include_keywords``：标题里不含任一关键词的商品，
在打开详情页和调用 AI 之前就直接跳过，省时间也省 token。
"""
from __future__ import annotations

import re
from typing import Iterable, List

_SPLIT_PATTERN = re.compile(r"[\n,，]+")


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def normalize_title_keywords(values: Iterable[str] | str | None) -> List[str]:
    """拆分、去空、去重（忽略大小写），保留用户原始写法。"""
    if values is None:
        return []
    raw_values: List[str] = []
    if isinstance(values, str):
        raw_values.extend(_SPLIT_PATTERN.split(values))
    else:
        for value in values:
            raw_values.extend(_SPLIT_PATTERN.split(str(value)))

    normalized: List[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        text = str(raw).strip()
        if not text:
            continue
        key = _normalize(text)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def title_filter_reason(
    title: str,
    include_keywords: Iterable[str] | str | None,
    exclude_keywords: Iterable[str] | str | None = None,
) -> str | None:
    """标题没通过预筛选时返回原因，通过时返回 None。

    先看排除词（标题含任一排除词即跳过，用来挡主板、键盘、配件这类），
    再看必须包含的词。
    """
    excluded = normalize_title_keywords(exclude_keywords)
    if excluded:
        haystack = _normalize(title)
        for keyword in excluded:
            if _normalize(keyword) in haystack:
                return f"标题含排除词 {keyword}"
    included = normalize_title_keywords(include_keywords)
    if included and not title_matches_keywords(title, included):
        return f"标题不含 {'/'.join(included)}"
    return None


def filter_items_by_title(
    items: Iterable[dict],
    keywords: Iterable[str] | str | None,
    exclude_keywords: Iterable[str] | str | None = None,
    title_key: str = "商品标题",
) -> List[dict]:
    """只保留标题通过预筛选的商品；没有配置任何关键词时原样返回。

    价格快照和市场参考也要走这一步，否则闲鱼在搜不到结果时塞进来的
    推荐商品（往往是手机）会把均价、中位数和性价比评分全部带偏。
    """
    item_list = list(items)
    if not normalize_title_keywords(keywords) and not normalize_title_keywords(exclude_keywords):
        return item_list
    return [
        item
        for item in item_list
        if title_filter_reason(str(item.get(title_key) or ""), keywords, exclude_keywords) is None
    ]


def title_matches_keywords(title: str, keywords: Iterable[str] | str | None) -> bool:
    """标题包含任一关键词即通过；没有配置关键词时一律通过。

    匹配不区分大小写，按子串匹配：填 ``T14`` 时 ``thinkpad t14 gen2``、
    ``T14s`` 都算包含，更细的判断交给后续的 AI 或关键词规则。
    """
    normalized_keywords = normalize_title_keywords(keywords)
    if not normalized_keywords:
        return True
    haystack = _normalize(title)
    if not haystack:
        return False
    return any(_normalize(keyword) in haystack for keyword in normalized_keywords)
