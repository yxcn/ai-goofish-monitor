"""
关键词判断引擎：单组 OR 逻辑，命中任意关键词即推荐。
纯英数字关键词按完整词匹配，避免 Q1 误命中 Q1R5。
"""
import re
from typing import Any, Dict, Iterable, List


_ASCII_TOKEN_KEYWORD_PATTERN = re.compile(r"^[a-z0-9 ]+$")
_ASCII_TOKEN_BOUNDARY = r"[a-z0-9]"


def normalize_text(value: str) -> str:
    return " ".join((value or "").lower().split())


def _collect_text_fragments(value: Any, bucket: List[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            bucket.append(text)
        return
    if isinstance(value, (int, float, bool)):
        bucket.append(str(value))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_text_fragments(item, bucket)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text_fragments(item, bucket)


# 关键词规则只看"这件商品"自己的文本。
# 商品信息只取标题、描述、属性（名和值）、标签；售价、链接、发布时间这类字段不参与匹配。
# 卖家信息只取单值字段（昵称、个性签名、卖家标签、IP 属地……）；
# 卖家发布的商品列表 / 卖家收到的评价列表 是别的商品和别人写的评价，
# 搜 "iPhone 15" 配规则 "256G" 时，卖家另一件挂牌的标题或某条评价提到 256G 不能算本商品命中，
# 所以列表、字典一律不展平，这两项再按名字显式排除。
# 商品侧只取这些字段参与关键词 / 黑名单匹配（发货地区保留：黑名单规则可能按地区排除）
PRODUCT_TEXT_FIELDS = ("商品标题", "商品描述", "商品属性", "商品标签", "发货地区")
SELLER_EXCLUDED_FIELDS = frozenset({"卖家发布的商品列表", "卖家收到的评价列表"})


def _is_scalar_text(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def build_search_text(record: Dict[str, Any]) -> str:
    fragments: List[str] = []
    product_info = record.get("商品信息") if isinstance(record, dict) else None
    seller_info = record.get("卖家信息") if isinstance(record, dict) else None
    product_info = product_info if isinstance(product_info, dict) else {}
    seller_info = seller_info if isinstance(seller_info, dict) else {}

    for field in PRODUCT_TEXT_FIELDS:
        value = product_info.get(field)
        if field == "商品属性" and isinstance(value, dict):
            # 属性名也要能匹配（"成色"、"品牌"），_collect_text_fragments 对 dict 只取值
            for key, item in value.items():
                _collect_text_fragments(key, fragments)
                _collect_text_fragments(item, fragments)
            continue
        _collect_text_fragments(value, fragments)

    for field, value in seller_info.items():
        if field in SELLER_EXCLUDED_FIELDS or not _is_scalar_text(value):
            continue
        _collect_text_fragments(value, fragments)

    return normalize_text(" ".join(fragments))


def _normalize_keywords(values: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for raw in values or []:
        text = normalize_text(str(raw).strip())
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _uses_ascii_token_match(keyword: str) -> bool:
    return bool(keyword) and _ASCII_TOKEN_KEYWORD_PATTERN.fullmatch(keyword) is not None


def _keyword_matches(keyword: str, normalized_text: str) -> bool:
    if not _uses_ascii_token_match(keyword):
        return keyword in normalized_text
    pattern = rf"(?<!{_ASCII_TOKEN_BOUNDARY}){re.escape(keyword)}(?!{_ASCII_TOKEN_BOUNDARY})"
    return re.search(pattern, normalized_text) is not None


def evaluate_keyword_rules(keywords: List[str], search_text: str) -> Dict[str, Any]:
    normalized_text = normalize_text(search_text)
    normalized_keywords = _normalize_keywords(keywords)

    if not normalized_text:
        return {
            "analysis_source": "keyword",
            "is_recommended": False,
            "reason": "可匹配文本为空，关键词规则无法执行。",
            "matched_keywords": [],
            "keyword_hit_count": 0,
        }

    if not normalized_keywords:
        return {
            "analysis_source": "keyword",
            "is_recommended": False,
            "reason": "未配置关键词规则。",
            "matched_keywords": [],
            "keyword_hit_count": 0,
        }

    matched_keywords = [kw for kw in normalized_keywords if _keyword_matches(kw, normalized_text)]
    hit_count = len(matched_keywords)
    is_recommended = hit_count > 0

    if is_recommended:
        reason = f"命中 {hit_count} 个关键词：{', '.join(matched_keywords)}"
    else:
        reason = "未命中任何关键词。"

    return {
        "analysis_source": "keyword",
        "is_recommended": is_recommended,
        "reason": reason,
        "matched_keywords": matched_keywords,
        "keyword_hit_count": hit_count,
    }
