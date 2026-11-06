"""
结果记录富化与文件名校验服务
"""

from src.infrastructure.persistence.storage_names import normalize_keyword_from_filename
from src.services.price_history_service import (
    build_item_price_context,
    build_sold_reference,
    load_price_snapshots,
    load_sold_listings,
    parse_price_value,
)
from src.services.result_storage_service import load_visible_result_item_ids


def validate_result_filename(filename: str) -> None:
    if not filename.endswith(".jsonl") or "/" in filename or ".." in filename:
        raise ValueError("无效的文件名")


def enrich_records_with_price_insight(records: list[dict], filename: str) -> list[dict]:
    """给没有 price_insight 的老记录补一份价格位置。

    新记录在抓取时已经算好并落库（推送和 AI 用的就是那份），读时不再重算，
    否则结果页和推送里的"贵 / 便宜"会因为样本集不同而对不上。
    成交参考价对整批记录只算一次。
    """
    if not any(not record.get("price_insight") for record in records):
        return records
    keyword = normalize_keyword_from_filename(filename)
    snapshots = load_price_snapshots(keyword)
    if not snapshots:
        return records

    visible_item_ids = load_visible_result_item_ids(filename)
    sold_reference = build_sold_reference(snapshots, load_sold_listings(keyword))
    visible_snapshots = [
        snapshot
        for snapshot in snapshots
        if str(snapshot.get("item_id") or "") in visible_item_ids
    ]
    enriched = []
    for record in records:
        if record.get("price_insight"):
            enriched.append(record)
            continue
        info = record.get("商品信息", {}) or {}
        clone = dict(record)
        clone["price_insight"] = build_item_price_context(
            snapshots,
            item_id=str(info.get("商品ID") or ""),
            current_price=parse_price_value(info.get("当前售价")),
            market_snapshots=visible_snapshots,
            sold_reference=sold_reference,
        )
        enriched.append(clone)
    return enriched
