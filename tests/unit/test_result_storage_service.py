import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from src.infrastructure.persistence import sqlite_connection as sqlite_connection_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    init_schema,
    run_with_lock_retry,
    sqlite_connection,
)
from src.services import result_storage_service as storage
from src.services.result_storage_service import (
    delete_result_file_records,
    load_all_result_records,
    load_inbox_counts,
    load_result_blacklist_keywords,
    load_result_summaries,
    load_result_summary,
    load_review_summary,
    load_visible_result_item_ids,
    query_result_records,
    save_result_blacklist_keywords,
    save_result_record,
    update_item_review_status,
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


# ---------------------------------------------------------------------------
# 结果收件箱：review_status / reviewed_at
# ---------------------------------------------------------------------------

OTHER_FILENAME = "ipad_pro_full_data.jsonl"
EMPTY_REVIEW_SUMMARY = {
    "inbox": 0, "not_recommended": 0, "contacted": 0, "excluded": 0, "bought": 0, "total": 0,
}


def _review_rows(filename: str = FILENAME) -> dict[str, tuple[str, str | None]]:
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT item_id, review_status, reviewed_at FROM result_items WHERE result_filename = ?"
            " ORDER BY item_id",
            (filename,),
        ).fetchall()
    return {row["item_id"]: (row["review_status"], row["reviewed_at"]) for row in rows}


def _ids(items: list[dict]) -> list[str]:
    return [item["商品信息"]["商品ID"] for item in items]


def _other_record(item_id: str, title: str, *, recommended: bool) -> dict:
    record = _record(item_id, title, crawl_time=f"2026-02-01T00:0{item_id[-1]}:00", recommended=recommended)
    record["搜索关键字"] = "ipad pro"
    return record


def test_upgrade_adds_review_status_columns_and_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "data" / "app.sqlite3"
    db_path.parent.mkdir()
    # 模拟上一版的库：有 status / hidden_by_rule，但没有 review_status / reviewed_at
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO app_metadata VALUES ('bootstrap:legacy_tasks','done'),
            ('bootstrap:legacy_results','done'), ('bootstrap:legacy_price_snapshots','done'),
            ('migration:result_items_status','done'),
            ('migration:result_items_hidden_by_rule_backfill','done');
        CREATE TABLE result_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, result_filename TEXT NOT NULL, keyword TEXT NOT NULL,
            task_name TEXT NOT NULL, crawl_time TEXT NOT NULL, publish_time TEXT, price REAL,
            price_display TEXT, item_id TEXT, title TEXT, link TEXT, link_unique_key TEXT NOT NULL,
            seller_nickname TEXT, is_recommended INTEGER NOT NULL, analysis_source TEXT,
            keyword_hit_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            hidden_by_rule INTEGER NOT NULL DEFAULT 0, raw_json TEXT NOT NULL,
            UNIQUE(result_filename, link_unique_key)
        );
        """
    )
    record = RECORDS[0]
    item = record["商品信息"]
    conn.execute(
        "INSERT INTO result_items (result_filename, keyword, task_name, crawl_time, item_id, title, link,"
        " link_unique_key, is_recommended, analysis_source, keyword_hit_count, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (FILENAME, KEYWORD, "MBA", record["爬取时间"], item["商品ID"], item["商品标题"], item["商品链接"],
         item["商品链接"].split("&")[0], 1, "ai", 0, json.dumps(record, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()

    def _columns() -> dict[str, sqlite3.Row]:
        with sqlite_connection(str(db_path)) as c:
            return {r["name"]: r for r in c.execute("PRAGMA table_info(result_items)").fetchall()}

    def _indexes() -> set[str]:
        with sqlite_connection(str(db_path)) as c:
            return {r["name"] for r in c.execute("PRAGMA index_list(result_items)").fetchall()}

    assert "review_status" not in _columns() and "reviewed_at" not in _columns()
    assert "idx_results_filename_review" not in _indexes()

    with sqlite_connection(str(db_path)) as c:
        init_schema(c)

    cols = _columns()
    assert cols["review_status"]["notnull"] == 1
    assert cols["review_status"]["dflt_value"] == "'new'"
    assert "reviewed_at" in cols
    assert "idx_results_filename_review" in _indexes()
    # 老数据拿到默认值
    with sqlite_connection(str(db_path)) as c:
        row = c.execute("SELECT review_status, reviewed_at FROM result_items").fetchone()
        assert (row["review_status"], row["reviewed_at"]) == ("new", None)
        # 再跑一次是幂等的
        init_schema(c)
    assert "review_status" in _columns()

    # 走正常入口也能读到默认处理状态
    total, items = _query()
    assert total == 1
    assert items[0]["_review_status"] == "new" and items[0]["_reviewed_at"] is None


def test_listed_items_default_to_new_review_status(seeded):
    total, items = _query()
    assert total == 4
    assert {item["_review_status"] for item in items} == {"new"}
    assert all(item["_reviewed_at"] is None for item in items)

    exported = asyncio.run(load_all_result_records(
        FILENAME, ai_recommended_only=False, keyword_recommended_only=False,
        sort_by="crawl_time", sort_order="desc",
    ))
    assert all(item["_review_status"] == "new" for item in exported)

    summary = asyncio.run(load_result_summary(FILENAME))
    assert summary["latest_record"]["_review_status"] == "new"
    assert summary["latest_recommendation"]["_reviewed_at"] is None


def test_update_review_status_filters_listing_and_export(seeded):
    result = asyncio.run(update_item_review_status(FILENAME, "1", "contacted"))
    assert result["review_status"] == "contacted"
    assert isinstance(result["reviewed_at"], str)
    assert datetime.fromisoformat(result["reviewed_at"]).isoformat(timespec="seconds") == result["reviewed_at"]

    total, items = _query(review_statuses=["contacted"])
    assert total == 1 and _ids(items) == ["1"]
    assert items[0]["_review_status"] == "contacted"
    assert items[0]["_reviewed_at"] == result["reviewed_at"]

    # 收件箱 = 推荐且未处理：已联系的 1 不在，未推荐的 2 也不在
    total, items = _query(review_statuses=["new"], recommended=True)
    assert total == 2 and _ids(items) == ["4", "3"]
    total, items = _query(recommended=False)
    assert total == 1 and _ids(items) == ["2"]
    total, items = _query(review_statuses=["contacted", "bought"])
    assert total == 1 and _ids(items) == ["1"]
    # 空列表 / None 不过滤
    total, _ = _query(review_statuses=[])
    assert total == 4

    exported = asyncio.run(load_all_result_records(
        FILENAME, ai_recommended_only=False, keyword_recommended_only=False,
        sort_by="crawl_time", sort_order="desc", review_statuses=["new"], recommended=True,
    ))
    assert _ids(exported) == ["4", "3"]

    # 与现有筛选叠加：AI 推荐 + 未处理
    total, items = _query(ai_recommended_only=True, review_statuses=["new"])
    assert total == 1 and _ids(items) == ["4"]

    # 被隐藏的行默认不出现，include_hidden 时回来
    asyncio.run(update_item_status(FILENAME, "1", "hidden"))
    total, _ = _query(review_statuses=["contacted"])
    assert total == 0
    total, items = _query(review_statuses=["contacted"], include_hidden=True)
    assert total == 1 and items[0]["_review_status"] == "contacted"


def test_setting_review_status_back_to_new_clears_reviewed_at(seeded):
    asyncio.run(update_item_review_status(FILENAME, "1", "bought"))
    status, reviewed_at = _review_rows()["1"]
    assert status == "bought" and reviewed_at

    result = asyncio.run(update_item_review_status(FILENAME, "1", "new"))
    assert result == {"review_status": "new", "reviewed_at": None}
    assert _review_rows()["1"] == ("new", None)
    _, items = _query(review_statuses=["new"], recommended=True)
    assert "1" in _ids(items)


def test_update_review_status_rejects_unknown_status_and_missing_item(seeded):
    with pytest.raises(ValueError):
        asyncio.run(update_item_review_status(FILENAME, "1", "maybe"))
    with pytest.raises(ValueError):
        asyncio.run(update_item_review_status(FILENAME, "1", ""))
    assert _review_rows()["1"] == ("new", None)

    assert asyncio.run(update_item_review_status(FILENAME, "404", "bought")) is None
    # 同一商品 ID 在别的文件里不受影响
    assert asyncio.run(update_item_review_status(OTHER_FILENAME, "1", "bought")) is None
    assert _review_rows()["1"] == ("new", None)


def test_review_summary_counts_visible_rows_only(seeded):
    assert asyncio.run(load_review_summary(FILENAME)) == {
        **EMPTY_REVIEW_SUMMARY, "inbox": 3, "not_recommended": 1, "total": 4,
    }

    asyncio.run(update_item_review_status(FILENAME, "1", "contacted"))
    asyncio.run(update_item_review_status(FILENAME, "3", "excluded"))
    asyncio.run(update_item_review_status(FILENAME, "2", "bought"))
    assert asyncio.run(load_review_summary(FILENAME)) == {
        "inbox": 1, "not_recommended": 0, "contacted": 1, "excluded": 1, "bought": 1, "total": 4,
    }

    # 手动隐藏的 4、黑名单命中的 2 和 3 都不计入
    asyncio.run(update_item_status(FILENAME, "4", "hidden"))
    asyncio.run(save_result_blacklist_keywords(FILENAME, ["intel"]))
    assert asyncio.run(load_review_summary(FILENAME)) == {**EMPTY_REVIEW_SUMMARY, "contacted": 1, "total": 1}

    assert asyncio.run(load_review_summary("missing_full_data.jsonl")) == EMPTY_REVIEW_SUMMARY


def test_inbox_counts_span_all_files_and_skip_hidden_rows(seeded):
    asyncio.run(save_result_record(_other_record("9", "iPad Pro", recommended=True), "ipad pro"))
    asyncio.run(save_result_record(_other_record("8", "iPad Air", recommended=True), "ipad pro"))
    asyncio.run(save_result_record(_other_record("7", "iPad mini", recommended=False), "ipad pro"))
    asyncio.run(update_item_status(OTHER_FILENAME, "8", "hidden"))

    assert asyncio.run(load_inbox_counts()) == {"count": 4, "files": {FILENAME: 3, OTHER_FILENAME: 1}}

    asyncio.run(update_item_review_status(FILENAME, "1", "contacted"))
    asyncio.run(update_item_review_status(OTHER_FILENAME, "9", "bought"))
    # 没有收件箱记录的文件不再出现
    assert asyncio.run(load_inbox_counts()) == {"count": 2, "files": {FILENAME: 2}}

    asyncio.run(save_result_blacklist_keywords(FILENAME, ["macbook"]))
    assert asyncio.run(load_inbox_counts()) == {"count": 0, "files": {}}


def test_inbox_counts_on_empty_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(load_inbox_counts()) == {"count": 0, "files": {}}


def test_resaving_record_keeps_review_state(seeded):
    asyncio.run(update_item_review_status(FILENAME, "1", "contacted"))
    before = _review_rows()["1"]
    # 正常记录重复落库被忽略，处理状态不变
    assert asyncio.run(save_result_record(RECORDS[0], KEYWORD)) is True
    assert _review_rows()["1"] == before

    # AI 失败占位记录被重新分析结果覆盖时，upsert 不碰处理状态
    error_record = _record("7", "MacBook Air M1 待分析", crawl_time="2026-01-03T11:00:00", recommended=False)
    error_record["ai_analysis"]["error"] = "402 insufficient balance"
    asyncio.run(save_result_record(error_record, KEYWORD))
    asyncio.run(update_item_review_status(FILENAME, "7", "excluded"))
    excluded_state = _review_rows()["7"]
    assert excluded_state[0] == "excluded"

    good_record = _record("7", "MacBook Air M1 待分析", crawl_time="2026-01-03T12:00:00", recommended=True)
    asyncio.run(save_result_record(good_record, KEYWORD))
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT is_recommended, review_status, reviewed_at FROM result_items WHERE item_id = '7'"
        ).fetchone()
    assert row["is_recommended"] == 1
    assert (row["review_status"], row["reviewed_at"]) == excluded_state
