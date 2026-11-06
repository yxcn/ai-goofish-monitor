"""
价格历史记录与聚合服务
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Iterable, Optional

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.seller_profile import _is_similar, _keyword_tokens

PRICE_HISTORY_DIR = "price_history"
DEFAULT_HISTORY_WINDOW_DAYS = 30

# 成交参考价：闲鱼不公开成交价，用两类近似样本合成——
# 1. 两轮搜索之间消失的挂牌（最后挂牌价 ≈ 成交价，也可能是下架）
# 2. 已采集卖家商品列表里标为"已售"的同类商品
SOLD_REFERENCE_WINDOW_DAYS = 30
SOLD_REFERENCE_ABSENT_RUNS = 2      # 连续两轮没出现才算消失，避免搜索排序抖动误判
MIN_REFERENCE_SAMPLES = 3
PRICE_VERDICT_THRESHOLD = 0.10      # 比参考价低/高 10% 以上才下结论
# 配件不算同类成交样本（主板 600 会把笔记本的成交价拉低）
ACCESSORY_WORDS = ("主板", "屏幕", "屏总成", "键盘", "电池", "外壳", "充电器", "适配器", "内存条", "硬盘", "配件", "底座", "扩展坞", "风扇", "散热", "转接")


def normalize_keyword_slug(keyword: str) -> str:
    text = "".join(
        char for char in str(keyword or "").lower().replace(" ", "_")
        if char.isalnum() or char in "_-"
    ).rstrip("_")
    return text or "unknown"


def build_price_history_path(keyword: str) -> str:
    return os.path.join(
        PRICE_HISTORY_DIR,
        f"{normalize_keyword_slug(keyword)}_history.jsonl",
    )


def parse_price_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)

    text = str(value).strip().replace("¥", "").replace(",", "")
    if not text or text in {"价格异常", "暂无", "-", "N/A"}:
        return None
    if text.endswith("万"):
        text = str(float(text[:-1]) * 10000)
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return None


def _safe_iso_datetime(value: Optional[str]) -> str:
    if value:
        return value
    return datetime.now().isoformat()


def _to_day(iso_text: str) -> str:
    return iso_text[:10]


def _build_snapshot_record(
    *,
    keyword: str,
    task_name: str,
    item: dict,
    run_id: str,
    snapshot_time: str,
) -> Optional[dict]:
    item_id = str(item.get("商品ID") or "").strip()
    link = str(item.get("商品链接") or "").strip()
    unique_id = item_id or link
    price_value = parse_price_value(item.get("当前售价"))
    if not unique_id or price_value is None:
        return None

    return {
        "snapshot_time": snapshot_time,
        "snapshot_day": _to_day(snapshot_time),
        "run_id": run_id,
        "task_name": task_name,
        "keyword": keyword,
        "item_id": unique_id,
        "title": item.get("商品标题") or "",
        "price": price_value,
        "price_display": item.get("当前售价") or "",
        "tags": item.get("商品标签") or [],
        "region": item.get("发货地区") or "",
        "seller": item.get("卖家昵称") or "",
        "publish_time": item.get("发布时间") or "",
        "link": link,
    }


def record_market_snapshots(
    *,
    keyword: str,
    task_name: str,
    items: Iterable[dict],
    run_id: str,
    snapshot_time: Optional[str] = None,
    seen_item_ids: Optional[set[str]] = None,
) -> list[dict]:
    snapshot_time = _safe_iso_datetime(snapshot_time)
    seen = seen_item_ids if seen_item_ids is not None else set()
    records: list[dict] = []

    for item in items:
        record = _build_snapshot_record(
            keyword=keyword,
            task_name=task_name,
            item=item,
            run_id=run_id,
            snapshot_time=snapshot_time,
        )
        if record is None or record["item_id"] in seen:
            continue
        seen.add(record["item_id"])
        records.append(record)

    if not records:
        return []

    bootstrap_sqlite_storage()
    keyword_slug = normalize_keyword_slug(keyword)
    with sqlite_connection() as conn:
        for record in records:
            conn.execute(
                """
                INSERT OR IGNORE INTO price_snapshots (
                    keyword_slug, keyword, task_name, snapshot_time, snapshot_day,
                    run_id, item_id, title, price, price_display, tags_json, region,
                    seller, publish_time, link
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    keyword_slug,
                    record.get("keyword", keyword),
                    record.get("task_name", task_name),
                    record.get("snapshot_time", snapshot_time),
                    record.get("snapshot_day", _to_day(snapshot_time)),
                    record.get("run_id", run_id),
                    record.get("item_id", ""),
                    record.get("title", ""),
                    record.get("price"),
                    record.get("price_display", ""),
                    json.dumps(record.get("tags") or [], ensure_ascii=False),
                    record.get("region", ""),
                    record.get("seller", ""),
                    record.get("publish_time", ""),
                    record.get("link", ""),
                ),
            )
        conn.commit()
    return records


def load_price_snapshots(keyword: str) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM price_snapshots
            WHERE keyword_slug = ?
            ORDER BY snapshot_time ASC, id ASC
            """,
            (normalize_keyword_slug(keyword),),
        ).fetchall()
    snapshots: list[dict] = []
    for row in rows:
        snapshots.append(
            {
                "snapshot_time": row["snapshot_time"],
                "snapshot_day": row["snapshot_day"],
                "run_id": row["run_id"],
                "task_name": row["task_name"],
                "keyword": row["keyword"],
                "item_id": row["item_id"],
                "title": row["title"],
                "price": row["price"],
                "price_display": row["price_display"],
                "tags": json.loads(row["tags_json"] or "[]"),
                "region": row["region"],
                "seller": row["seller"],
                "publish_time": row["publish_time"],
                "link": row["link"],
            }
        )
    return snapshots


def delete_price_snapshots(keyword: str) -> int:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM price_snapshots WHERE keyword_slug = ?",
            (normalize_keyword_slug(keyword),),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


def _dedupe_latest(records: Iterable[dict], group_key: str) -> list[dict]:
    latest_by_key: dict[str, dict] = {}
    for record in records:
        key = str(record.get(group_key) or "").strip()
        if not key:
            continue
        latest_by_key[key] = record
    return list(latest_by_key.values())


def _summarize_prices(records: Iterable[dict]) -> dict:
    entries = [record for record in records if parse_price_value(record.get("price")) is not None]
    prices = [float(record["price"]) for record in entries]
    if not prices:
        return {
            "sample_count": 0,
            "avg_price": None,
            "median_price": None,
            "min_price": None,
            "max_price": None,
        }

    return {
        "sample_count": len(prices),
        "avg_price": round(sum(prices) / len(prices), 2),
        "median_price": round(float(median(prices)), 2),
        "min_price": round(min(prices), 2),
        "max_price": round(max(prices), 2),
    }


def _build_daily_trend(snapshots: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[str(snapshot.get("snapshot_day") or "")].append(snapshot)

    points: list[dict] = []
    for day in sorted(grouped.keys()):
        day_records = _dedupe_latest(grouped[day], "item_id")
        summary = _summarize_prices(day_records)
        summary["day"] = day
        points.append(summary)
    return points


def _recent_window_snapshots(snapshots: list[dict], window_days: int) -> list[dict]:
    if not snapshots:
        return []
    latest_time = max(str(record.get("snapshot_time") or "") for record in snapshots)
    latest_dt = datetime.fromisoformat(latest_time)
    filtered = []
    for record in snapshots:
        current_time = datetime.fromisoformat(str(record.get("snapshot_time") or latest_time))
        if (latest_dt - current_time).days <= max(0, window_days):
            filtered.append(record)
    return filtered


def find_vanished_listings(
    snapshots: list[dict],
    *,
    window_days: int = SOLD_REFERENCE_WINDOW_DAYS,
    absent_runs: int = SOLD_REFERENCE_ABSENT_RUNS,
) -> list[dict]:
    """两轮之间消失的挂牌：最后一次出现时的价格。

    只取最近 window_days 内最后出现、且最近 absent_runs 轮都没再出现的商品；
    不足 absent_runs + 1 轮时按现有轮数放宽，只有一轮时没有"消失"可言。
    """
    if not snapshots:
        return []
    ordered = sorted(snapshots, key=lambda record: (str(record.get("snapshot_time") or ""), str(record.get("run_id") or "")))
    run_order: list[str] = []
    for record in ordered:
        run_id = str(record.get("run_id") or "")
        if run_id and run_id not in run_order:
            run_order.append(run_id)
    if len(run_order) < 2:
        return []
    recent_runs = set(run_order[-min(absent_runs, len(run_order) - 1):])
    latest_time = datetime.fromisoformat(str(ordered[-1].get("snapshot_time")))

    last_seen: dict[str, dict] = {}
    for record in ordered:
        item_id = str(record.get("item_id") or "")
        if item_id:
            last_seen[item_id] = record

    vanished = []
    for item_id, record in last_seen.items():
        if str(record.get("run_id") or "") in recent_runs:
            continue
        price = parse_price_value(record.get("price"))
        if price is None:
            continue
        seen_at = datetime.fromisoformat(str(record.get("snapshot_time") or latest_time.isoformat()))
        if (latest_time - seen_at).days > max(0, window_days):
            continue
        vanished.append(
            {
                "item_id": item_id,
                "title": record.get("title"),
                "price": price,
                "last_seen_at": record.get("snapshot_time"),
                "source": "vanished",
            }
        )
    return vanished


def select_sold_listings(records: Iterable[dict], keyword: str) -> list[dict]:
    """从结果记录里挑出各卖家"已售"的同类商品（品类相同或标题命中关键词，排除配件），按商品 ID 去重。"""
    tokens = _keyword_tokens(keyword)
    picked: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        item = record.get("商品信息") if isinstance(record.get("商品信息"), dict) else {}
        seller = record.get("卖家信息") if isinstance(record.get("卖家信息"), dict) else {}
        attributes = item.get("商品属性") if isinstance(item.get("商品属性"), dict) else {}
        category_label = str(attributes.get("分类") or "").strip()
        record_tokens = tokens + _keyword_tokens(record.get("搜索关键字"), record.get("实际搜索词"))
        for listing in seller.get("卖家发布的商品列表") or []:
            if not isinstance(listing, dict) or listing.get("商品状态") != "已售":
                continue
            title = str(listing.get("商品标题") or "")
            if any(word in title for word in ACCESSORY_WORDS):
                continue
            if not _is_similar(listing, item.get("品类ID"), record_tokens, category_label):
                continue
            price = parse_price_value(listing.get("商品价格"))
            listing_id = str(listing.get("商品ID") or "")
            if price is None or not listing_id or listing_id in picked:
                continue
            picked[listing_id] = {"item_id": listing_id, "title": title, "price": price, "source": "sold"}
    return list(picked.values())


def load_sold_listings(keyword: str) -> list[dict]:
    """从结果库里所有同关键词记录的卖家资料中收集"已售"同类商品。"""
    bootstrap_sqlite_storage()
    slug = normalize_keyword_slug(keyword)
    records = []
    with sqlite_connection() as conn:
        rows = conn.execute("SELECT keyword, raw_json FROM result_items").fetchall()
    for row in rows:
        if normalize_keyword_slug(row["keyword"]) != slug:
            continue
        try:
            records.append(json.loads(row["raw_json"]))
        except (TypeError, ValueError):
            continue
    return select_sold_listings(records, keyword)


def build_sold_reference(
    snapshots: list[dict],
    sold_listings: Optional[list[dict]] = None,
    *,
    window_days: int = SOLD_REFERENCE_WINDOW_DAYS,
    min_samples: int = MIN_REFERENCE_SAMPLES,
) -> dict:
    """合成成交参考价：消失挂牌 + 卖家已售同类，取中位数；样本少于 min_samples 时标记不可靠。"""
    vanished = find_vanished_listings(snapshots, window_days=window_days)
    sold = [entry for entry in (sold_listings or []) if parse_price_value(entry.get("price")) is not None]
    samples = [{"price": entry["price"]} for entry in vanished] + [{"price": parse_price_value(entry["price"])} for entry in sold]
    summary = _summarize_prices(samples)
    return {
        **summary,
        "vanished_count": len(vanished),
        "sold_listing_count": len(sold),
        "window_days": window_days,
        "reliable": summary["sample_count"] >= min_samples,
    }


def build_price_verdict(
    price_now: Optional[float],
    *,
    market_median: Optional[float],
    market_sample_count: int,
    sold_reference: Optional[dict],
    min_samples: int = MIN_REFERENCE_SAMPLES,
    threshold: float = PRICE_VERDICT_THRESHOLD,
) -> dict:
    """先比成交参考价（样本够），否则比挂牌中位价（样本够），都不够就写样本不足。"""
    sold_reference = sold_reference or {}
    basis = None
    if sold_reference.get("reliable") and sold_reference.get("median_price"):
        basis = ("sold", "成交参考价", float(sold_reference["median_price"]), int(sold_reference["sample_count"]))
    elif market_median and market_sample_count >= min_samples:
        basis = ("listing", "挂牌中位价", float(market_median), int(market_sample_count))

    result = {
        "price_verdict": "样本不足",
        "price_verdict_key": "insufficient",
        "price_verdict_basis": None,
        "reference_price": None,
        "reference_sample_count": 0,
        "price_diff_percent": None,
        "price_verdict_text": "样本不足，暂不判断贵贱",
    }
    if price_now is None or basis is None:
        return result
    basis_key, basis_name, reference, sample_count = basis
    diff = (price_now - reference) / reference
    if diff <= -threshold:
        verdict, key = "性价比高", "good_deal"
    elif diff >= threshold:
        verdict, key = "偏贵", "pricey"
    else:
        verdict, key = "价格正常", "fair"
    if abs(diff) < 0.01:
        comparison = f"与{basis_name}持平"
    else:
        comparison = f"比{basis_name}{'低' if diff < 0 else '高'} {abs(diff):.0%}"
    result.update(
        {
            "price_verdict": verdict,
            "price_verdict_key": key,
            "price_verdict_basis": basis_key,
            "reference_price": round(reference, 2),
            "reference_sample_count": sample_count,
            "price_diff_percent": round(diff * 100, 1),
            "price_verdict_text": f"{comparison}（{basis_name} ¥{reference:.0f}，{sample_count} 条）→ {verdict}",
        }
    )
    return result


def _percent_vs(price_now: Optional[float], reference: Optional[float]) -> Optional[float]:
    if price_now is None or not reference:
        return None
    return round((price_now - reference) / reference * 100, 1)


def _resolve_deal_label(score: int) -> str:
    if score >= 65:
        return "高性价比"
    if score >= 50:
        return "值得关注"
    if score >= 40:
        return "价格正常"
    return "价格偏高"


def build_item_price_context(
    snapshots: list[dict],
    *,
    item_id: str,
    current_price: Optional[float],
    market_snapshots: Optional[list[dict]] = None,
    sold_listings: Optional[list[dict]] = None,
) -> dict:
    if not item_id:
        return {"observation_count": 0, "deal_score": None, "deal_label": "暂无数据"}

    item_snapshots = [record for record in snapshots if str(record.get("item_id")) == str(item_id)]
    if not item_snapshots:
        return {"observation_count": 0, "deal_score": None, "deal_label": "暂无数据"}

    latest_item_snapshot = item_snapshots[-1]
    price_now = current_price if current_price is not None else parse_price_value(latest_item_snapshot.get("price"))
    historical_prices = [float(record["price"]) for record in item_snapshots if parse_price_value(record.get("price")) is not None]
    source_snapshots = market_snapshots if market_snapshots is not None else snapshots
    latest_run_id = str(source_snapshots[-1].get("run_id") or "") if source_snapshots else ""
    latest_market = _dedupe_latest(
        [record for record in source_snapshots if str(record.get("run_id") or "") == latest_run_id],
        "item_id",
    )
    market_summary = _summarize_prices(latest_market)
    market_avg = market_summary.get("avg_price")
    market_median = market_summary.get("median_price")
    # 消失挂牌从全部快照里找（它们多半不在可见结果里），已售同类来自已采集的卖家资料
    sold_reference = build_sold_reference(snapshots, sold_listings)
    verdict = build_price_verdict(
        price_now,
        market_median=market_median,
        market_sample_count=int(market_summary.get("sample_count") or 0),
        sold_reference=sold_reference,
    )

    score = 50
    if price_now is not None and market_avg:
        score += int(((market_avg - price_now) / market_avg) * 60)
    if price_now is not None and historical_prices:
        historical_max = max(historical_prices)
        if historical_max > 0:
            score += int(((historical_max - price_now) / historical_max) * 20)
        if math.isclose(price_now, min(historical_prices), rel_tol=0.001):
            score += 8
    score = max(0, min(100, score))

    previous_price = historical_prices[-2] if len(historical_prices) >= 2 else None
    change_amount = None if previous_price is None or price_now is None else round(price_now - previous_price, 2)
    change_percent = None
    if change_amount is not None and previous_price:
        change_percent = round(change_amount / previous_price * 100, 2)

    return {
        "observation_count": len(historical_prices),
        "current_price": price_now,
        "avg_price": round(sum(historical_prices) / len(historical_prices), 2),
        "median_price": round(float(median(historical_prices)), 2),
        "min_price": round(min(historical_prices), 2),
        "max_price": round(max(historical_prices), 2),
        "first_seen_at": item_snapshots[0].get("snapshot_time"),
        "last_seen_at": latest_item_snapshot.get("snapshot_time"),
        "market_avg_price": market_avg,
        "market_median_price": market_median,
        "market_sample_count": int(market_summary.get("sample_count") or 0),
        "sold_reference": sold_reference,
        "vs_listing_median_percent": _percent_vs(price_now, market_median),
        "vs_sold_reference_percent": _percent_vs(price_now, sold_reference.get("median_price")),
        **verdict,
        "price_change_amount": change_amount,
        "price_change_percent": change_percent,
        "deal_score": score,
        "deal_label": _resolve_deal_label(score),
    }


def build_market_reference(
    *,
    keyword: str,
    item: dict,
    current_market_items: list[dict],
    historical_snapshots: list[dict],
    sold_listings: Optional[list[dict]] = None,
) -> dict:
    current_market_records = []
    for market_item in current_market_items:
        price = parse_price_value(market_item.get("当前售价"))
        if price is None:
            continue
        current_market_records.append({"price": price})

    market_snapshot = _summarize_prices(current_market_records)
    history_summary = _summarize_prices(_dedupe_latest(historical_snapshots, "item_id"))
    item_context = build_item_price_context(
        historical_snapshots,
        item_id=str(item.get("商品ID") or ""),
        current_price=parse_price_value(item.get("当前售价")),
        sold_listings=sold_listings,
    )
    return {
        "当前搜索样本": market_snapshot,
        "历史价格概览": history_summary,
        "本商品价格位置": item_context,
        "关键词": keyword,
    }


def build_price_history_insights(
    keyword: str,
    *,
    window_days: int = DEFAULT_HISTORY_WINDOW_DAYS,
    visible_item_ids: Optional[set[str]] = None,
) -> dict:
    snapshots = load_price_snapshots(keyword)
    # 成交参考用全部快照：消失的挂牌本来就不在可见结果里
    sold_reference = build_sold_reference(snapshots, load_sold_listings(keyword)) if snapshots else build_sold_reference([])
    if visible_item_ids is not None:
        snapshots = [
            snapshot
            for snapshot in snapshots
            if str(snapshot.get("item_id") or "") in visible_item_ids
        ]
    if not snapshots:
        return {
            "market_summary": _summarize_prices([]),
            "history_summary": {"unique_items": 0, **_summarize_prices([])},
            "daily_trend": [],
            "latest_snapshot_at": None,
            "sold_reference": sold_reference,
        }

    recent_snapshots = _recent_window_snapshots(snapshots, window_days)
    latest_run_id = str(snapshots[-1].get("run_id") or "")
    latest_run_snapshots = _dedupe_latest(
        [record for record in snapshots if str(record.get("run_id") or "") == latest_run_id],
        "item_id",
    )
    latest_records_by_item = _dedupe_latest(recent_snapshots, "item_id")

    return {
        "market_summary": {
            **_summarize_prices(latest_run_snapshots),
            "snapshot_time": snapshots[-1].get("snapshot_time"),
        },
        "history_summary": {
            "unique_items": len(latest_records_by_item),
            **_summarize_prices(latest_records_by_item),
        },
        "daily_trend": _build_daily_trend(recent_snapshots),
        "latest_snapshot_at": snapshots[-1].get("snapshot_time"),
        "sold_reference": sold_reference,
    }
