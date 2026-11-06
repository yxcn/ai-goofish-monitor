import asyncio
import base64

from fastapi.testclient import TestClient

import src.app as app_module  # 切目录前导入：StaticFiles 挂载要求仓库里的 static/ 存在
import src.scraper as scraper
from src.api import auth as auth_module
from src.parsers import _parse_search_results_json
from src.services.crawl_health_service import RunStats


def _search_item(item_id: str, bad: bool = False):
    main = {
        "exContent": {"title": f"ThinkPad T14 #{item_id}", "price": [{"text": "¥"}, {"text": "1494"}], "area": "上海", "userNickName": "u"},
        "clickParam": {"args": {"publishTime": "1762400000000"} if not bad else {"publishTime": 5}},
        "targetUrl": None if bad else f"https://www.goofish.com/item?id={item_id}",
    }
    return {"data": {"item": {"main": main}}}


def test_search_parser_counts_items_and_failures_into_run_stats():
    stats = RunStats(task_name="T", keyword="k")
    payload = {"data": {"resultList": [_search_item("1"), _search_item("2", bad=True), _search_item("3")]}}
    items = asyncio.run(_parse_search_results_json(payload, "第 1 页", stats=stats))
    assert len(items) == 2
    assert stats.get("search_pages") == 1 and stats.get("search_items_total") == 3 and stats.get("search_parse_failed") == 1

    asyncio.run(_parse_search_results_json({"data": {}}, "第 2 页", stats=stats))
    assert stats.get("search_pages") == 2 and stats.get("search_empty_pages") == 1
    # 不传 stats 也照常工作
    assert len(asyncio.run(_parse_search_results_json(payload, "x"))) == 2


def test_health_alert_is_rate_limited_and_marks_the_run(monkeypatch):
    sent = []
    marked = []
    gate = {"allow": True}
    monkeypatch.setattr(scraper, "should_notify_health", lambda task, category, **kw: gate["allow"])
    monkeypatch.setattr(scraper, "mark_run_notified", lambda run_id: marked.append(run_id))

    async def fake_send(product_data, reason):
        sent.append((product_data, reason))
        return {"telegram": {"success": True}}

    monkeypatch.setattr(scraper, "send_ntfy_notification", fake_send)
    assessment = {"run_id": 7, "category": "api_drift", "title": "疑似接口变更", "message": "12 条解析失败", "action": "检查 parsers", "level": "critical"}

    asyncio.run(scraper._notify_health_alert({"task_name": "T14", "keyword": "ThinkPad T14"}, assessment))
    assert len(sent) == 1 and marked == [7]
    assert sent[0][0]["商品标题"] == "[爬虫健康] T14：疑似接口变更"
    assert "【疑似接口变更】12 条解析失败" in sent[0][1] and "建议：检查 parsers" in sent[0][1]

    gate["allow"] = False
    asyncio.run(scraper._notify_health_alert({"task_name": "T14"}, assessment))
    assert len(sent) == 1


def test_task_failure_push_leads_with_category_and_action(monkeypatch):
    sent = []

    async def fake_send(product_data, reason):
        sent.append(reason)

    monkeypatch.setattr(scraper, "send_ntfy_notification", fake_send)
    monkeypatch.setattr(
        scraper.FAILURE_GUARD,
        "record_failure",
        lambda *a, **k: {"should_notify": True, "consecutive_failures": 3, "paused_until": None, "opened_circuit": True},
    )
    asyncio.run(
        scraper._notify_task_failure(
            {"task_name": "T14", "keyword": "ThinkPad T14"},
            "Login required: redirected to https://passport.goofish.com/mini_login",
            cookie_path=None,
        )
    )
    assert sent and sent[0].startswith("【登录失效】建议：到「账号管理」")
    assert "任务运行失败(已连续 3/" in sent[0]


def test_record_run_summary_never_raises(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(scraper, "record_crawl_run", boom)
    assert scraper._record_run_summary(RunStats(task_name="T", keyword="k"), "") is None
    assert "写入运行摘要失败" in capsys.readouterr().out


def test_health_endpoint_requires_auth_and_returns_overview(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app_module.app)
    assert client.get("/api/health/crawler").status_code == 401

    username, password = auth_module._credentials()
    headers = {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}
    response = client.get("/api/health/crawler", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["overall_level"] == "ok" and body["tasks"] == [] and body["generated_at"]

    runs = client.get("/api/health/crawler/runs/T14", headers=headers)
    assert runs.status_code == 200 and runs.json() == {"task_name": "T14", "runs": []}
