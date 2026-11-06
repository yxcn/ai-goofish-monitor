import asyncio
import json

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.result_storage_service import (
    load_processed_link_keys,
    load_retryable_ai_error_count,
    mark_notification_status,
    save_result_record,
)


def _record(item_id: str, analysis: dict) -> dict:
    return {
        "搜索关键字": "thinkpad t14",
        "任务名称": "T14",
        "爬取时间": "2026-11-06 10:00:00",
        "商品信息": {
            "商品ID": item_id,
            "商品标题": f"ThinkPad T14 {item_id}",
            "当前售价": "¥1494",
            "商品链接": f"https://www.goofish.com/item?id={item_id}",
        },
        "ai_analysis": analysis,
    }


def _rows():
    with sqlite_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT item_id, is_recommended, raw_json FROM result_items ORDER BY item_id")]


def test_ai_error_records_are_not_processed_and_get_replaced_on_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    error_analysis = {"analysis_source": "ai", "is_recommended": False, "reason": "AI分析异常: 402", "error": "402 insufficient balance"}
    good_analysis = {"analysis_source": "ai", "is_recommended": True, "reason": "成色好价格低", "keyword_hit_count": 0}

    assert asyncio.run(save_result_record(_record("1", error_analysis), "thinkpad t14")) is True
    assert asyncio.run(save_result_record(_record("2", good_analysis), "thinkpad t14")) is True

    # AI 失败的商品不算已处理，下次运行会重新抓；正常商品照旧跳过
    assert len(load_processed_link_keys("thinkpad t14")) == 1
    assert load_retryable_ai_error_count("thinkpad t14") == 1

    # AI 恢复后重新分析：失败行被新结果覆盖，而不是 INSERT OR IGNORE 留着旧的失败记录
    assert asyncio.run(save_result_record(_record("1", good_analysis), "thinkpad t14")) is True
    rows = _rows()
    assert [r["item_id"] for r in rows] == ["1", "2"]
    assert rows[0]["is_recommended"] == 1
    assert "error" not in json.loads(rows[0]["raw_json"])["ai_analysis"]
    assert len(load_processed_link_keys("thinkpad t14")) == 2
    assert load_retryable_ai_error_count("thinkpad t14") == 0

    # 正常记录不会被后来的运行改写
    changed = _record("2", {**good_analysis, "reason": "改写后的理由"})
    asyncio.run(save_result_record(changed, "thinkpad t14"))
    assert json.loads(_rows()[1]["raw_json"])["ai_analysis"]["reason"] == "成色好价格低"


def test_mark_notification_status_writes_into_raw_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = _record("9", {"analysis_source": "ai", "is_recommended": True, "reason": "ok"})
    asyncio.run(save_result_record(record, "thinkpad t14"))

    status = {"成功": ["telegram"], "失败": {"wecom": "3 次尝试均失败: 500"}, "时间": "2026-11-06 10:01:00"}
    asyncio.run(mark_notification_status(record, "thinkpad t14", status))

    stored = json.loads(_rows()[0]["raw_json"])
    assert stored["通知状态"] == status
    assert stored["ai_analysis"]["is_recommended"] is True
