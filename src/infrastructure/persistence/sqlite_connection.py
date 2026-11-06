"""
SQLite 连接与 schema 初始化。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from src.infrastructure.persistence.storage_names import DEFAULT_DATABASE_PATH


BUSY_TIMEOUT_MS = 5000
# busy_timeout 到期后 SQLite 仍可能抛 "database is locked"/"busy"（WAL checkpoint、另一进程长写），
# 写操作在放弃前再做几次短退避重试
LOCK_RETRY_DELAYS = (0.2, 0.5, 1.0)
_LOCK_ERROR_MARKERS = ("locked", "busy")

T = TypeVar("T")


def is_sqlite_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in _LOCK_ERROR_MARKERS
    )


def run_with_lock_retry(
    operation: Callable[[], T],
    *,
    delays: tuple[float, ...] = LOCK_RETRY_DELAYS,
    label: str = "sqlite write",
) -> T:
    """执行一个同步 SQLite 操作；遇到锁冲突按 delays 退避重试，其他错误直接抛出。"""
    for attempt, delay in enumerate(delays, start=1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc):
                raise
            print(f"[sqlite] {label} 遇到锁冲突 ({exc})，{delay}s 后第 {attempt}/{len(delays)} 次重试")
            time.sleep(delay)
    return operation()


def retry_on_lock(func: Callable[..., T]) -> Callable[..., T]:
    """装饰同步写函数：锁冲突时按 LOCK_RETRY_DELAYS 重试。"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        return run_with_lock_retry(lambda: func(*args, **kwargs), label=func.__name__)

    return wrapper

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        task_name TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        keyword TEXT NOT NULL,
        description TEXT,
        analyze_images INTEGER NOT NULL,
        max_pages INTEGER NOT NULL,
        personal_only INTEGER NOT NULL,
        min_price TEXT,
        max_price TEXT,
        cron TEXT,
        ai_prompt_base_file TEXT NOT NULL,
        ai_prompt_criteria_file TEXT NOT NULL,
        account_state_file TEXT,
        account_strategy TEXT NOT NULL,
        free_shipping INTEGER NOT NULL,
        new_publish_option TEXT,
        region TEXT,
        decision_mode TEXT NOT NULL,
        keyword_rules_json TEXT NOT NULL,
        title_include_keywords_json TEXT NOT NULL DEFAULT '[]',
        extra_keywords_json TEXT NOT NULL DEFAULT '[]',
        title_exclude_keywords_json TEXT NOT NULL DEFAULT '[]',
        attribute_rules_json TEXT NOT NULL DEFAULT '[]',
        is_running INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_filename TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        crawl_time TEXT NOT NULL,
        publish_time TEXT,
        price REAL,
        price_display TEXT,
        item_id TEXT,
        title TEXT,
        link TEXT,
        link_unique_key TEXT NOT NULL,
        seller_nickname TEXT,
        is_recommended INTEGER NOT NULL,
        analysis_source TEXT,
        keyword_hit_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        hidden_by_rule INTEGER NOT NULL DEFAULT 0,
        raw_json TEXT NOT NULL,
        UNIQUE(result_filename, link_unique_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword_slug TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        snapshot_time TEXT NOT NULL,
        snapshot_day TEXT NOT NULL,
        run_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        title TEXT,
        price REAL NOT NULL,
        price_display TEXT,
        tags_json TEXT NOT NULL,
        region TEXT,
        seller TEXT,
        publish_time TEXT,
        link TEXT,
        UNIQUE(keyword_slug, run_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_blacklist_rules (
        result_filename TEXT PRIMARY KEY,
        blacklist_keywords_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_name ON tasks(task_name)",
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_crawl
    ON result_items(result_filename, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_publish
    ON result_items(result_filename, publish_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_price
    ON result_items(result_filename, price DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_recommended
    ON result_items(result_filename, is_recommended, analysis_source, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_time
    ON price_snapshots(keyword_slug, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_item_time
    ON price_snapshots(keyword_slug, item_id, snapshot_time DESC)
    """,
)


def get_database_path() -> str:
    return os.getenv("APP_DATABASE_FILE", DEFAULT_DATABASE_PATH)


def _prepare_database_file(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    _migrate_result_items_status(conn)
    _migrate_result_items_hidden_by_rule(conn)
    _migrate_tasks_json_list_columns(conn)
    conn.commit()


def _migrate_result_items_hidden_by_rule(conn: sqlite3.Connection) -> None:
    """result_items 补 hidden_by_rule 列（黑名单命中落列，查询/计数走 WHERE 而不是解析 raw_json）。幂等。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()}
    if "hidden_by_rule" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN hidden_by_rule INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_filename_visibility_crawl"
        " ON result_items(result_filename, status, hidden_by_rule, crawl_time DESC)"
    )


# 后加的任务列表型字段，旧库缺哪列补哪列（都是 JSON 数组文本，默认空列表）
TASK_JSON_LIST_COLUMNS = (
    "title_include_keywords_json",
    "extra_keywords_json",
    "title_exclude_keywords_json",
    "attribute_rules_json",
)


def _migrate_tasks_json_list_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for column in TASK_JSON_LIST_COLUMNS:
        if column not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'")


def _migrate_result_items_status(conn: sqlite3.Connection) -> None:
    """为 result_items 表添加 status 列（仅执行一次）。"""
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = 'migration:result_items_status'"
    ).fetchone()
    if row is not None:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()]
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('migration:result_items_status', 'done')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_filename_status_crawl"
        " ON result_items(result_filename, status, crawl_time DESC)"
    )


@contextmanager
def sqlite_connection(
    db_path: str | None = None,
) -> Iterator[sqlite3.Connection]:
    path = db_path or get_database_path()
    _prepare_database_file(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()
