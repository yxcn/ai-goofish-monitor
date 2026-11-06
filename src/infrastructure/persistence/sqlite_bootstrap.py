"""
SQLite 启动初始化与旧文件迁移。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from src.domain.models.task import Task
from src.infrastructure.persistence.result_visibility import backfill_hidden_by_rule
from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection
from src.infrastructure.persistence.storage_names import (
    build_result_filename,
    normalize_keyword_from_filename,
    normalize_keyword_slug,
)


BOOTSTRAP_LOCK = threading.Lock()
LEGACY_CONFIG_FILE = "config.json"
LEGACY_RESULT_DIR = "jsonl"
LEGACY_PRICE_HISTORY_DIR = "price_history"
TASKS_BOOTSTRAP_KEY = "bootstrap:legacy_tasks"
RESULTS_BOOTSTRAP_KEY = "bootstrap:legacy_results"
SNAPSHOTS_BOOTSTRAP_KEY = "bootstrap:legacy_price_snapshots"
HIDDEN_BY_RULE_BACKFILL_KEY = "migration:result_items_hidden_by_rule_backfill"


def bootstrap_sqlite_storage(
    db_path: str | None = None,
    *,
    legacy_config_file: str | None = LEGACY_CONFIG_FILE,
    legacy_result_dir: str = LEGACY_RESULT_DIR,
    legacy_price_history_dir: str = LEGACY_PRICE_HISTORY_DIR,
) -> None:
    with BOOTSTRAP_LOCK:
        with sqlite_connection(db_path) as conn:
            init_schema(conn)
            _import_tasks_if_needed(conn, legacy_config_file)
            _import_results_if_needed(conn, legacy_result_dir)
            _import_price_snapshots_if_needed(conn, legacy_price_history_dir)
            _backfill_hidden_by_rule_if_needed(conn)


def _table_is_empty(conn, table_name: str) -> bool:
    row = conn.execute(f"SELECT COUNT(1) AS total FROM {table_name}").fetchone()
    return row is None or int(row["total"]) == 0


def _load_json_file(path: Path):
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return None
    return json.loads(content)


def _import_tasks_if_needed(conn, legacy_config_file: str | None) -> None:
    if _bootstrap_completed(conn, TASKS_BOOTSTRAP_KEY):
        return
    if not _table_is_empty(conn, "tasks"):
        _mark_bootstrap_completed(conn, TASKS_BOOTSTRAP_KEY)
        conn.commit()
        return
    if legacy_config_file is None:
        _mark_bootstrap_completed(conn, TASKS_BOOTSTRAP_KEY)
        conn.commit()
        return
    path = Path(legacy_config_file)
    tasks = _load_json_file(path)
    if not isinstance(tasks, list):
        _mark_bootstrap_completed(conn, TASKS_BOOTSTRAP_KEY)
        conn.commit()
        return

    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            print(f"[bootstrap] 跳过 {legacy_config_file} 第 {index} 条：不是对象")
            continue
        task = _validate_legacy_task(raw_task, index=index, source=str(legacy_config_file))
        if task is None:
            continue
        conn.execute(
            """
            INSERT INTO tasks (
                id, task_name, enabled, keyword, description, analyze_images,
                max_pages, personal_only, min_price, max_price, cron,
                ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                account_strategy, free_shipping, new_publish_option, region,
                decision_mode, keyword_rules_json, title_include_keywords_json,
                extra_keywords_json, title_exclude_keywords_json, attribute_rules_json,
                is_running
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index,
                task.task_name,
                int(task.enabled),
                task.keyword,
                task.description or "",
                int(task.analyze_images),
                int(task.max_pages),
                int(task.personal_only),
                task.min_price,
                task.max_price,
                task.cron,
                task.ai_prompt_base_file,
                task.ai_prompt_criteria_file,
                task.account_state_file,
                task.account_strategy,
                int(task.free_shipping),
                task.new_publish_option,
                task.region,
                task.decision_mode,
                json.dumps(task.keyword_rules or [], ensure_ascii=False),
                json.dumps(task.title_include_keywords or [], ensure_ascii=False),
                json.dumps(task.extra_keywords or [], ensure_ascii=False),
                json.dumps(task.title_exclude_keywords or [], ensure_ascii=False),
                json.dumps(task.attribute_rules or [], ensure_ascii=False),
                0,
            ),
        )
    _mark_bootstrap_completed(conn, TASKS_BOOTSTRAP_KEY)
    conn.commit()


def _validate_legacy_task(raw_task: dict, *, index: int, source: str) -> Task | None:
    """旧 config.json 的任务逐条过 Task 模型校验；坏数据打印并跳过，而不是让 find_all 永久抛错。"""
    payload = dict(raw_task)
    payload.setdefault("ai_prompt_base_file", "prompts/base_prompt.txt")
    payload.setdefault("ai_prompt_criteria_file", "")
    payload.setdefault("max_pages", 1)
    payload.setdefault("personal_only", False)
    payload.setdefault("enabled", True)
    payload["id"] = index
    payload["is_running"] = False
    try:
        return Task(**payload)
    except Exception as exc:
        name = raw_task.get("task_name") or f"#{index}"
        print(f"[bootstrap] 跳过 {source} 中无法解析的任务 '{name}'：{exc}")
        return None


def _backfill_hidden_by_rule_if_needed(conn) -> None:
    """升级旧库：按已保存的黑名单规则回填 result_items.hidden_by_rule（只跑一次）。"""
    if _bootstrap_completed(conn, HIDDEN_BY_RULE_BACKFILL_KEY):
        return
    changed = backfill_hidden_by_rule(conn)
    if changed:
        print(f"[bootstrap] 已为 {changed} 条结果记录回填黑名单命中状态")
    _mark_bootstrap_completed(conn, HIDDEN_BY_RULE_BACKFILL_KEY)
    conn.commit()


def _import_results_if_needed(conn, legacy_result_dir: str) -> None:
    if _bootstrap_completed(conn, RESULTS_BOOTSTRAP_KEY):
        return
    if not _table_is_empty(conn, "result_items"):
        _mark_bootstrap_completed(conn, RESULTS_BOOTSTRAP_KEY)
        conn.commit()
        return
    result_dir = Path(legacy_result_dir)
    if not result_dir.exists():
        _mark_bootstrap_completed(conn, RESULTS_BOOTSTRAP_KEY)
        conn.commit()
        return

    for path in sorted(result_dir.glob("*.jsonl")):
        filename = path.name
        keyword = normalize_keyword_from_filename(filename)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                _insert_result_record(conn, record, keyword=keyword, filename=filename)
    _mark_bootstrap_completed(conn, RESULTS_BOOTSTRAP_KEY)
    conn.commit()


def _import_price_snapshots_if_needed(conn, legacy_price_history_dir: str) -> None:
    if _bootstrap_completed(conn, SNAPSHOTS_BOOTSTRAP_KEY):
        return
    if not _table_is_empty(conn, "price_snapshots"):
        _mark_bootstrap_completed(conn, SNAPSHOTS_BOOTSTRAP_KEY)
        conn.commit()
        return
    history_dir = Path(legacy_price_history_dir)
    if not history_dir.exists():
        _mark_bootstrap_completed(conn, SNAPSHOTS_BOOTSTRAP_KEY)
        conn.commit()
        return

    for path in sorted(history_dir.glob("*_history.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                _insert_price_snapshot(conn, record)
    _mark_bootstrap_completed(conn, SNAPSHOTS_BOOTSTRAP_KEY)
    conn.commit()


def _insert_result_record(conn, record: dict, *, keyword: str, filename: str) -> None:
    item = record.get("商品信息", {}) or {}
    analysis = record.get("ai_analysis", {}) or {}
    link = str(item.get("商品链接") or "")
    if link:
        link_unique_key = link.split("&", 1)[0]
    else:
        item_id = str(item.get("商品ID") or "").strip()
        if item_id:
            link_unique_key = f"item:{item_id}"
        else:
            link_unique_key = "hash:" + hashlib.sha1(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
    final_keyword = str(record.get("搜索关键字") or keyword)
    result_filename = filename or build_result_filename(final_keyword)
    keyword_hit_count = analysis.get("keyword_hit_count", 0)
    try:
        keyword_hit_count = int(keyword_hit_count)
    except (TypeError, ValueError):
        keyword_hit_count = 0

    conn.execute(
        """
        INSERT OR IGNORE INTO result_items (
            result_filename, keyword, task_name, crawl_time, publish_time, price,
            price_display, item_id, title, link, link_unique_key, seller_nickname,
            is_recommended, analysis_source, keyword_hit_count, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result_filename,
            final_keyword,
            record.get("任务名称", ""),
            record.get("爬取时间", ""),
            item.get("发布时间"),
            _parse_price(item.get("当前售价")),
            item.get("当前售价"),
            item.get("商品ID"),
            item.get("商品标题"),
            link,
            link_unique_key,
            (record.get("卖家信息", {}) or {}).get("卖家昵称") or item.get("卖家昵称"),
            _as_int(analysis.get("is_recommended", False)),
            analysis.get("analysis_source"),
            keyword_hit_count,
            json.dumps(record, ensure_ascii=False),
        ),
    )


def _insert_price_snapshot(conn, record: dict) -> None:
    keyword = str(record.get("keyword") or "")
    slug = str(record.get("keyword_slug") or normalize_keyword_slug(keyword))
    conn.execute(
        """
        INSERT OR IGNORE INTO price_snapshots (
            keyword_slug, keyword, task_name, snapshot_time, snapshot_day, run_id,
            item_id, title, price, price_display, tags_json, region, seller,
            publish_time, link
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            keyword,
            record.get("task_name", ""),
            record.get("snapshot_time", ""),
            record.get("snapshot_day", ""),
            record.get("run_id", ""),
            record.get("item_id", ""),
            record.get("title", ""),
            _parse_price(record.get("price")),
            record.get("price_display"),
            json.dumps(record.get("tags") or [], ensure_ascii=False),
            record.get("region"),
            record.get("seller"),
            record.get("publish_time"),
            record.get("link"),
        ),
    )


def _as_int(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None:
        return 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _parse_price(value):
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


def _bootstrap_completed(conn, key: str) -> bool:
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return row is not None


def _mark_bootstrap_completed(conn, key: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO app_metadata(key, value)
        VALUES (?, 'done')
        """,
        (key,),
    )
