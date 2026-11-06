import asyncio
import json
import sqlite3

import pytest

from src.infrastructure.persistence import sqlite_connection as sqlite_connection_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import run_with_lock_retry, sqlite_connection
from src.services import result_storage_service as storage
from src.services.result_storage_service import (
    delete_result_file_records,
    load_all_result_records,
    load_result_blacklist_keywords,
    load_result_summaries,
    load_result_summary,
    load_visible_result_item_ids,
    query_result_records,
    save_result_blacklist_keywords,
    save_result_record,
    update_item_status,
)

FILENAME = "macbook_air_m1_full_data.jsonl"
KEYWORD = "macbook air m1"


def _record(item_id: str, title: str, *, crawl_time: str, recommended: bool, source: str = "ai") -> dict:
    return {
        "搜索关键字": KEYWORD,
        "任务名称": "MBA",
        "爬取时间": crawl_time,
        "商品信息": {
            "商品ID": item_id,
            "商品标题": title,
            "当前售价": "¥4000",
            "商品链接": f"https://www.goofish.com/item?id={item_id}&spm=x",
        },
        "卖家信息": {"卖家昵称": "seller"},
        "ai_analysis": {"analysis_source": source, "is_recommended": recommended, "reason": "r"},
    }


RECORDS = [
    _record("1", "MacBook Air M1 8+256", crawl_time="2026-01-03T09:00:00", recommended=True),
    _record("2", "MacBook Air Intel i5", crawl_time="2026-01-03T09:05:00", recommended=False),
    _record("3", "MacBook Pro Intel 13", crawl_time="2026-01-03T09:10:00", recommended=True, source="keyword"),
    _record("4", "MacBook Air M1 16+512", crawl_time="2026-01-03T09:15:00", recommended=True),
]


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for record in RECORDS:
        assert asyncio.run(save_result_record(record, KEYWORD)) is True
    return tmp_path


def _hidden_flags() -> dict[str, int]:
    with sqlite_connection() as conn:
        rows = conn.execute("SELECT item_id, hidden_by_rule FROM result_items ORDER BY item_id").fetchall()
    return {row["item_id"]: row["hidden_by_rule"] for row in rows}


def _query(**overrides):
    params = dict(
        ai_recommended_only=False, keyword_recommended_only=False,
        sort_by="crawl_time", sort_order="desc", page=1, limit=20, include_hidden=False,
    )
    params.update(overrides)
    return asyncio.run(query_result_records(FILENAME, **params))


def test_hidden_by_rule_column_is_recomputed_when_rules_change(seeded):
    assert _hidden_flags() == {"1": 0, "2": 0, "3": 0, "4": 0}

    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    assert _hidden_flags() == {"1": 0, "2": 1, "3": 1, "4": 0}

    asyncio.run(save_result_blacklist_keywords(FILENAME, ["re:16\\+512"]))
    assert _hidden_flags() == {"1": 0, "2": 0, "3": 0, "4": 1}

    asyncio.run(save_result_blacklist_keywords(FILENAME, []))
    assert _hidden_flags() == {"1": 0, "2": 0, "3": 0, "4": 0}


def test_new_records_get_hidden_by_rule_from_existing_rules(seeded):
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    asyncio.run(save_result_record(
        _record("5", "MacBook Air Intel i7", crawl_time="2026-01-03T10:00:00", recommended=False), KEYWORD
    ))
    assert _hidden_flags()["5"] == 1
    assert load_visible_result_item_ids(FILENAME) == {"1", "4"}


def test_query_filters_and_paginates_in_sql_and_keeps_response_shape(seeded):
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    asyncio.run(update_item_status(FILENAME, "4", "expired"))

    total, items = _query()
    assert total == 1
    assert [item["商品信息"]["商品ID"] for item in items] == ["1"]
    assert items[0]["_status"] == "active"
    assert items[0]["_matched_blacklist_keywords"] == []
    assert items[0]["_hidden_reason"] is None
    assert items[0]["_effective_hidden"] is False

    total, items = _query(include_hidden=True, limit=2, page=1)
    assert total == 4
    # active 优先，然后按 crawl_time 倒序
    assert [item["商品信息"]["商品ID"] for item in items] == ["3", "2"]
    assert items[0]["_hidden_reason"] == "rule" and items[0]["_matched_blacklist_keywords"] == ["intel"]
    total, items = _query(include_hidden=True, limit=2, page=2)
    assert total == 4
    assert [item["商品信息"]["商品ID"] for item in items] == ["1", "4"]
    assert items[1]["_hidden_reason"] == "expired" and items[1]["_status"] == "expired"

    total, items = _query(include_hidden=True, ai_recommended_only=True)
    assert total == 2 and [item["商品信息"]["商品ID"] for item in items] == ["1", "4"]
    total, items = _query(include_hidden=True, keyword_recommended_only=True)
    assert total == 1 and items[0]["商品信息"]["商品ID"] == "3"

    exported = asyncio.run(load_all_result_records(
        FILENAME, ai_recommended_only=False, keyword_recommended_only=False,
        sort_by="price", sort_order="asc",
    ))
    assert [item["商品信息"]["商品ID"] for item in exported] == ["1"]


def test_summary_uses_sql_aggregates_over_visible_rows_only(seeded):
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    asyncio.run(update_item_status(FILENAME, "4", "hidden"))

    summary = asyncio.run(load_result_summary(FILENAME))
    assert summary["total_items"] == 1
    assert summary["recommended_items"] == 1
    assert summary["ai_recommended_items"] == 1
    assert summary["keyword_recommended_items"] == 0
    assert summary["latest_crawl_time"] == "2026-01-03T09:00:00"
    assert summary["latest_record"]["商品信息"]["商品ID"] == "1"
    assert summary["latest_recommendation"]["商品信息"]["商品ID"] == "1"

    asyncio.run(save_result_blacklist_keywords(FILENAME, []))
    asyncio.run(update_item_status(FILENAME, "4", "active"))
    summary = asyncio.run(load_result_summary(FILENAME))
    assert (summary["total_items"], summary["recommended_items"]) == (4, 3)
    assert (summary["ai_recommended_items"], summary["keyword_recommended_items"]) == (2, 1)
    assert summary["latest_crawl_time"] == "2026-01-03T09:15:00"
    assert summary["latest_record"]["商品信息"]["商品ID"] == "4"
    assert summary["latest_recommendation"]["商品信息"]["商品ID"] == "4"

    assert asyncio.run(load_result_summary("missing_full_data.jsonl")) is None


def test_summaries_for_all_files_match_per_file_summary(seeded):
    other = _record("9", "iPad Pro", crawl_time="2026-02-01T00:00:00", recommended=False)
    other["搜索关键字"] = "ipad pro"
    asyncio.run(save_result_record(other, "ipad pro"))
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))

    summaries = asyncio.run(load_result_summaries())
    assert set(summaries) == {FILENAME, "ipad_pro_full_data.jsonl"}
    assert summaries[FILENAME] == asyncio.run(load_result_summary(FILENAME))
    assert summaries["ipad_pro_full_data.jsonl"]["total_items"] == 1
    assert summaries["ipad_pro_full_data.jsonl"]["latest_recommendation"] is None


def test_deleting_result_file_also_drops_its_blacklist_rules(seeded):
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    assert asyncio.run(load_result_blacklist_keywords(FILENAME)) == ["intel"]

    deleted = asyncio.run(delete_result_file_records(FILENAME))

    assert deleted == 4
    assert asyncio.run(load_result_blacklist_keywords(FILENAME)) == []
    # 同关键词的新任务从零开始，不继承旧规则
    asyncio.run(save_result_record(RECORDS[1], KEYWORD))
    assert _hidden_flags() == {"2": 0}


def test_upgrade_adds_hidden_by_rule_column_and_backfills_from_saved_rules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "data" / "app.sqlite3"
    db_path.parent.mkdir()
    # 模拟上一版的库：没有 hidden_by_rule 列，但已经有记录和黑名单规则
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO app_metadata VALUES ('bootstrap:legacy_tasks','done'),
            ('bootstrap:legacy_results','done'), ('bootstrap:legacy_price_snapshots','done'),
            ('migration:result_items_status','done');
        CREATE TABLE result_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, result_filename TEXT NOT NULL, keyword TEXT NOT NULL,
            task_name TEXT NOT NULL, crawl_time TEXT NOT NULL, publish_time TEXT, price REAL,
            price_display TEXT, item_id TEXT, title TEXT, link TEXT, link_unique_key TEXT NOT NULL,
            seller_nickname TEXT, is_recommended INTEGER NOT NULL, analysis_source TEXT,
            keyword_hit_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active', raw_json TEXT NOT NULL,
            UNIQUE(result_filename, link_unique_key)
        );
        CREATE TABLE result_blacklist_rules (
            result_filename TEXT PRIMARY KEY, blacklist_keywords_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO result_blacklist_rules VALUES ('macbook_air_m1_full_data.jsonl', '["intel"]', 'now');
        """
    )
    for record in RECORDS:
        item = record["商品信息"]
        conn.execute(
            "INSERT INTO result_items (result_filename, keyword, task_name, crawl_time, item_id, title, link,"
            " link_unique_key, is_recommended, analysis_source, keyword_hit_count, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (FILENAME, KEYWORD, "MBA", record["爬取时间"], item["商品ID"], item["商品标题"], item["商品链接"],
             item["商品链接"].split("&")[0], int(record["ai_analysis"]["is_recommended"]),
             record["ai_analysis"]["analysis_source"], 0, json.dumps(record, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()

    bootstrap_sqlite_storage()

    assert _hidden_flags() == {"1": 0, "2": 1, "3": 1, "4": 0}
    assert _query()[0] == 2
    # 再跑一次是幂等的
    bootstrap_sqlite_storage()
    assert _hidden_flags() == {"1": 0, "2": 1, "3": 1, "4": 0}


def test_lock_retry_recovers_from_transient_lock_errors(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(sqlite_connection_module.time, "sleep", sleeps.append)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert run_with_lock_retry(flaky) == "ok"
    assert attempts["n"] == 3
    assert sleeps == [0.2, 0.5]


def test_lock_retry_gives_up_after_backoff_and_ignores_other_errors(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(sqlite_connection_module.time, "sleep", sleeps.append)

    def always_locked():
        raise sqlite3.OperationalError("database is busy")

    with pytest.raises(sqlite3.OperationalError):
        run_with_lock_retry(always_locked)
    assert sleeps == [0.2, 0.5, 1.0]

    calls = {"n": 0}

    def syntax_error():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        run_with_lock_retry(syntax_error)
    assert calls["n"] == 1


def test_save_result_record_retries_on_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sqlite_connection_module.time, "sleep", lambda _s: None)
    real_bootstrap = storage.bootstrap_sqlite_storage
    failures = {"left": 2}

    def flaky_bootstrap(*args, **kwargs):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_bootstrap(*args, **kwargs)

    monkeypatch.setattr(storage, "bootstrap_sqlite_storage", flaky_bootstrap)

    assert asyncio.run(save_result_record(RECORDS[0], KEYWORD)) is True
    assert failures["left"] == 0
    assert _query()[0] == 1
