"""
结果记录可见性：黑名单命中状态落在 result_items.hidden_by_rule 列上。

写入记录和保存黑名单规则时计算一次，查询/计数只走 WHERE，不再把整文件 raw_json 解析一遍。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from src.services.result_blacklist_service import (
    match_blacklist_keywords,
    normalize_blacklist_keywords,
)

# "可见" = 未被手动隐藏/过期，且没有命中黑名单规则
VISIBLE_ROW_SQL = "status = 'active' AND hidden_by_rule = 0"


def load_blacklist_keywords(conn: sqlite3.Connection, filename: str) -> list[str]:
    row = conn.execute(
        "SELECT blacklist_keywords_json FROM result_blacklist_rules WHERE result_filename = ?",
        (filename,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["blacklist_keywords_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_blacklist_keywords(payload)


def compute_hidden_by_rule(record: dict, blacklist_keywords: Iterable[str] | None) -> int:
    keywords = list(blacklist_keywords or [])
    if not keywords:
        return 0
    return 1 if match_blacklist_keywords(record, keywords) else 0


def recompute_hidden_by_rule(
    conn: sqlite3.Connection,
    filename: str,
    blacklist_keywords: Iterable[str] | None,
) -> int:
    """按给定规则重算某个结果文件所有行的 hidden_by_rule，返回改动的行数。不提交事务。"""
    keywords = list(blacklist_keywords or [])
    if not keywords:
        cursor = conn.execute(
            "UPDATE result_items SET hidden_by_rule = 0 WHERE result_filename = ? AND hidden_by_rule != 0",
            (filename,),
        )
        return int(cursor.rowcount or 0)

    rows = conn.execute(
        "SELECT id, hidden_by_rule, raw_json FROM result_items WHERE result_filename = ?",
        (filename,),
    ).fetchall()
    changes: list[tuple[int, int]] = []
    for row in rows:
        try:
            record = json.loads(row["raw_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        hidden = compute_hidden_by_rule(record, keywords)
        if hidden != int(row["hidden_by_rule"] or 0):
            changes.append((hidden, int(row["id"])))
    if changes:
        conn.executemany(
            "UPDATE result_items SET hidden_by_rule = ? WHERE id = ?",
            changes,
        )
    return len(changes)


def backfill_hidden_by_rule(conn: sqlite3.Connection) -> int:
    """给所有已配置黑名单规则的结果文件回填 hidden_by_rule（用于升级旧库）。"""
    rows = conn.execute(
        "SELECT result_filename, blacklist_keywords_json FROM result_blacklist_rules"
    ).fetchall()
    changed = 0
    for row in rows:
        try:
            payload = json.loads(row["blacklist_keywords_json"] or "[]")
        except json.JSONDecodeError:
            payload = []
        changed += recompute_hidden_by_rule(
            conn,
            str(row["result_filename"]),
            normalize_blacklist_keywords(payload),
        )
    return changed
