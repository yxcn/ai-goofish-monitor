from datetime import datetime, timedelta

from src.services.crawl_health_service import (
    CATEGORY_API_DRIFT,
    CATEGORY_CONFIG_ERROR,
    CATEGORY_CRASH,
    CATEGORY_LOGIN_EXPIRED,
    CATEGORY_NO_RUNS,
    CATEGORY_OK,
    CATEGORY_PAUSED,
    CATEGORY_RISK_CONTROL,
    CATEGORY_ZERO_RESULTS,
    LEVEL_CRITICAL,
    LEVEL_OK,
    LEVEL_UNKNOWN,
    LEVEL_WARNING,
    RunStats,
    assess_run,
    build_crawler_health,
    classify_error,
    format_alert_text,
    load_recent_runs,
    mark_run_notified,
    record_crawl_run,
    should_notify_health,
)


def _counters(**overrides):
    base = {"search_items_total": 30, "search_parse_failed": 0, "detail_attempts": 10, "detail_unavailable": 0,
            "detail_errors": 0, "items_submitted": 8, "already_processed": 20, "detail_timeouts": 0, "search_empty_pages": 0}
    base.update(overrides)
    return base


def test_classify_error_recognizes_the_three_human_actionable_causes():
    assert classify_error("Login required: redirected to https://passport.goofish.com/mini_login") == CATEGORY_LOGIN_EXPIRED
    assert classify_error("详情接口返回登录态失效: ['FAIL_SYS_TOKEN_EXPIRED::令牌过期']") == CATEGORY_LOGIN_EXPIRED
    assert classify_error("FAIL_SYS_USER_VALIDATE") == CATEGORY_RISK_CONTROL
    assert classify_error("baxia-dialog") == CATEGORY_RISK_CONTROL
    assert classify_error("未找到可用的登录状态文件，无法继续执行任务。") == CATEGORY_CONFIG_ERROR
    assert classify_error("TargetClosedError: browser closed") == CATEGORY_CRASH
    assert classify_error("") == CATEGORY_OK


def test_failed_runs_are_classified_and_escalate_when_repeated():
    login = assess_run(_counters(), status="failed", error_reason="Login required: passport")
    assert (login["level"], login["category"]) == (LEVEL_CRITICAL, CATEGORY_LOGIN_EXPIRED)
    assert login["action"].startswith("到「账号管理」")

    once = assess_run(_counters(), status="failed", error_reason="FAIL_SYS_USER_VALIDATE")
    assert (once["level"], once["category"]) == (LEVEL_WARNING, CATEGORY_RISK_CONTROL)
    twice = assess_run(_counters(), status="failed", error_reason="FAIL_SYS_USER_VALIDATE",
                       previous_runs=[{"status": "failed"}, {"status": "success"}])
    assert twice["level"] == LEVEL_CRITICAL and "连续 2 轮" in twice["message"]

    crash = assess_run(_counters(), status="failed", error_reason="RuntimeError: boom\nTraceback...")
    assert (crash["level"], crash["category"]) == (LEVEL_WARNING, CATEGORY_CRASH)
    assert crash["message"] == "任务运行失败：RuntimeError: boom"


def test_silent_api_drift_is_detected_from_parse_and_detail_failure_ratios():
    drift = assess_run(_counters(search_items_total=30, search_parse_failed=12), status="success")
    assert (drift["level"], drift["category"]) == (LEVEL_CRITICAL, CATEGORY_API_DRIFT)
    assert "12 条解析失败（40%）" in drift["message"]

    # 样本太少不下结论
    assert assess_run(_counters(search_items_total=3, search_parse_failed=2), status="success")["category"] == CATEGORY_OK

    detail = assess_run(_counters(detail_attempts=6, detail_unavailable=2, detail_errors=1), status="success")
    assert (detail["level"], detail["category"]) == (LEVEL_CRITICAL, CATEGORY_API_DRIFT)
    assert "3 个没有返回商品数据（50%）" in detail["message"]

    # 超时是网络问题，不算接口变更
    assert assess_run(_counters(detail_attempts=6, detail_timeouts=5), status="success")["category"] == CATEGORY_OK


def test_zero_results_warn_and_mention_history():
    history = [{"status": "success", "counters": {"search_items_total": n}} for n in (30, 28, 31)]
    zero = assess_run(_counters(search_items_total=0, detail_attempts=0, items_submitted=0), status="success", previous_runs=history)
    assert (zero["level"], zero["category"]) == (LEVEL_WARNING, CATEGORY_ZERO_RESULTS)
    assert "此前 3 轮中位数 30 条" in zero["message"]

    first = assess_run(_counters(search_items_total=0, detail_attempts=0, items_submitted=0), status="success")
    assert first["category"] == CATEGORY_ZERO_RESULTS and first["message"] == "最近一轮搜索返回 0 条"


def test_healthy_run_message_summarizes_counts():
    ok = assess_run(_counters(detail_timeouts=1), status="success")
    assert (ok["level"], ok["category"]) == (LEVEL_OK, CATEGORY_OK)
    assert ok["message"] == "最近一轮搜索 30 条，提交分析 8 条，20 条已处理过，1 个详情超时"
    assert ok["action"] == ""


def test_record_load_retention_and_daily_notification_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stats = RunStats(task_name="T14", keyword="ThinkPad T14")
    stats.add("search_items_total", 30)
    stats.add("search_parse_failed", 15)
    now = datetime(2026, 11, 6, 20, 0, 0)

    first = record_crawl_run(stats, status="success", now=now)
    assert first["category"] == CATEGORY_API_DRIFT and first["run_id"] >= 1
    assert should_notify_health("T14", CATEGORY_API_DRIFT, now=now) is True
    mark_run_notified(first["run_id"])
    assert should_notify_health("T14", CATEGORY_API_DRIFT, now=now) is False
    assert should_notify_health("T14", CATEGORY_API_DRIFT, now=now + timedelta(days=1)) is True
    assert should_notify_health("T14", CATEGORY_ZERO_RESULTS, now=now) is True

    runs = load_recent_runs("T14")
    assert len(runs) == 1 and runs[0]["notified"] is True and runs[0]["counters"]["search_parse_failed"] == 15

    # 失败轮的连续计数来自历史
    failed = record_crawl_run(RunStats(task_name="T14", keyword="k"), status="failed", error_reason="FAIL_SYS_USER_VALIDATE", now=now)
    failed2 = record_crawl_run(RunStats(task_name="T14", keyword="k"), status="failed", error_reason="FAIL_SYS_USER_VALIDATE", now=now)
    assert failed["level"] == LEVEL_WARNING and failed2["level"] == LEVEL_CRITICAL

    # 只保留最近 200 轮
    for _ in range(205):
        record_crawl_run(RunStats(task_name="T14", keyword="k"), status="success", now=now)
    assert len(load_recent_runs("T14", limit=500)) == 200


def test_overview_reports_unknown_latest_and_paused_states(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 6, 20, 0, 0)
    ok_stats = RunStats(task_name="A", keyword="a")
    ok_stats.add("search_items_total", 10)
    ok_stats.add("items_submitted", 2)
    record_crawl_run(ok_stats, status="success", now=now)
    record_crawl_run(RunStats(task_name="B", keyword="b"), status="failed", error_reason="Login required", now=now)

    tasks = [{"id": 1, "task_name": "A", "keyword": "a"}, {"id": 2, "task_name": "B", "keyword": "b"}, {"id": 3, "task_name": "C", "keyword": "c"}]
    guard = {"B": {"consecutive_failures": 3, "paused_until": (now + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"), "last_failure_reason": "Login required"}}
    overview = build_crawler_health(tasks, guard, now=now)

    by_name = {entry["task_name"]: entry for entry in overview["tasks"]}
    assert overview["overall_level"] == LEVEL_CRITICAL
    assert by_name["A"]["level"] == LEVEL_OK and by_name["A"]["category"] == CATEGORY_OK and by_name["A"]["task_id"] == 1
    assert by_name["C"]["level"] == LEVEL_UNKNOWN and by_name["C"]["category"] == CATEGORY_NO_RUNS and by_name["C"]["last_run_at"] is None
    paused = by_name["B"]
    assert paused["category"] == CATEGORY_PAUSED and paused["level"] == LEVEL_CRITICAL
    assert paused["consecutive_failures"] == 3 and paused["paused_until"]
    assert "登录失效" in paused["message"] and "账号管理" in paused["action"]

    # 暂停已过期就不再标 paused
    expired_guard = {"B": {**guard["B"], "paused_until": (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")}}
    later = build_crawler_health(tasks, expired_guard, now=now)
    assert {e["task_name"]: e["category"] for e in later["tasks"]}["B"] == CATEGORY_LOGIN_EXPIRED


def test_alert_text_has_title_detail_and_action():
    text = format_alert_text({"title": "疑似接口变更", "message": "12 条解析失败", "action": "检查 parsers"})
    assert text == "【疑似接口变更】12 条解析失败\n建议：检查 parsers"
    assert format_alert_text({"category": CATEGORY_OK, "message": "一切正常", "action": ""}) == "【正常】一切正常"


def test_begin_task_run_numbers_each_task_separately_and_continues_from_old_run_ids(tmp_path, monkeypatch):
    """轮次编号：每个任务各自连续；第一次发号从该任务已有的 crawl_runs id 接着数；结束时写进运行摘要。"""
    from src.services.crawl_health_service import begin_task_run

    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 8, 23, 0, 0)
    # 升级前已经跑过的几轮：只有 id，没有 run_no
    for _ in range(3):
        record_crawl_run(RunStats(task_name="T14", keyword="k"), status="success", now=now)
    assert load_recent_runs("T14")[0]["run_no"] is None

    assert begin_task_run("T14") == 4
    assert begin_task_run("T14") == 5
    assert begin_task_run("另一个任务") == 1
    assert begin_task_run("另一个任务") == 2

    stats = RunStats(task_name="T14", keyword="k", run_no=begin_task_run("T14"))
    assert stats.run_no == 6
    summary = record_crawl_run(stats, status="success", now=now)
    assert summary["run_no"] == 6
    assert load_recent_runs("T14")[0]["run_no"] == 6
