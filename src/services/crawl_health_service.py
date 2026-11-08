"""
爬虫健康：每轮抓取的运行摘要、分类规则、存储和总览。

目的只有一个——让"闲鱼改接口 / 加风控 / 登录过期"这类需要人介入的情况
在界面和推送里第一时间说清楚是哪一类、该做什么，而不是等几天发现"怎么一直没推送"。

规则全部基于本轮计数和最近几轮的历史，不做任何网络请求。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Iterable, Optional

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import retry_on_lock, sqlite_connection

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
LEVEL_UNKNOWN = "unknown"

CATEGORY_OK = "ok"
CATEGORY_LOGIN_EXPIRED = "login_expired"
CATEGORY_RISK_CONTROL = "risk_control"
CATEGORY_API_DRIFT = "api_drift"
CATEGORY_ZERO_RESULTS = "zero_results"
CATEGORY_CONFIG_ERROR = "config_error"
CATEGORY_CRASH = "crash"
CATEGORY_PAUSED = "paused"
CATEGORY_NO_RUNS = "no_runs"

CATEGORY_TITLES = {
    CATEGORY_OK: "正常",
    CATEGORY_LOGIN_EXPIRED: "登录失效",
    CATEGORY_RISK_CONTROL: "触发风控",
    CATEGORY_API_DRIFT: "疑似接口变更",
    CATEGORY_ZERO_RESULTS: "搜索无结果",
    CATEGORY_CONFIG_ERROR: "配置错误",
    CATEGORY_CRASH: "运行异常",
    CATEGORY_PAUSED: "熔断暂停中",
    CATEGORY_NO_RUNS: "尚无运行记录",
}

CATEGORY_ACTIONS = {
    CATEGORY_OK: "",
    CATEGORY_LOGIN_EXPIRED: "到「账号管理」重新获取登录态（扫码或粘贴新的 state 文件）",
    CATEGORY_RISK_CONTROL: "暂停一段时间再跑；反复出现时设 RUN_HEADLESS=false 手动过验证，或降低抓取频率",
    CATEGORY_API_DRIFT: "闲鱼接口可能改版：对照一次真实响应检查 src/parsers.py 的字段映射和 src/scraper.py 的接口匹配",
    CATEGORY_ZERO_RESULTS: "先在闲鱼网页上确认该关键词仍有结果；有结果而这里为 0，多半是接口或筛选逻辑变了",
    CATEGORY_CONFIG_ERROR: "检查任务的登录态文件 / 代理配置",
    CATEGORY_CRASH: "查看该任务的运行日志里的异常堆栈",
    CATEGORY_PAUSED: "熔断到期会自动恢复；更新登录态文件后可立即恢复",
    CATEGORY_NO_RUNS: "",
}

# 阈值
PARSE_FAILURE_RATIO = 0.30       # 搜索结果解析失败比例 ≥ 30% 视为接口变更
PARSE_FAILURE_MIN_ITEMS = 5
DETAIL_FAILURE_RATIO = 0.50      # 详情接口不可用比例 ≥ 50% 视为接口变更
DETAIL_FAILURE_MIN_ATTEMPTS = 3
HISTORY_RUNS = 5                 # "此前几轮"的窗口
RUNS_RETENTION_PER_TASK = 200

COUNTER_NAMES = (
    "search_pages",
    "search_items_total",
    "search_parse_failed",
    "search_empty_pages",
    "price_filtered",
    "title_filtered",
    "already_processed",
    "detail_attempts",
    "detail_unavailable",
    "detail_timeouts",
    "detail_errors",
    "items_submitted",
    "risk_control_events",
    "login_expired_events",
)

_LOGIN_PATTERNS = ("login required", "登录失效", "登录态", "登录状态文件不存在", "passport", "mini_login", "login_expired", "token_expired", "session_expired")
_RISK_PATTERNS = ("fail_sys_user_validate", "baxia", "middleware", "风控", "验证码", "riskcontrol", "risk control", "滑块")
_CONFIG_PATTERNS = ("未找到可用的代理地址", "未找到可用的登录状态文件", "登录状态文件不存在", "prompt文件缺失", "no such file")


@dataclass
class RunStats:
    """一轮抓取的计数器；爬虫各处只管 add()，分类在结束时统一做。"""

    task_name: str
    keyword: str
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    counters: dict = field(default_factory=lambda: {name: 0 for name in COUNTER_NAMES})
    # 本任务的轮次编号（begin_task_run 发的，第 N 轮）；拿不到时为 None，不影响其它统计
    run_no: Optional[int] = None

    def add(self, name: str, amount: int = 1) -> None:
        if amount:
            self.counters[name] = int(self.counters.get(name, 0)) + int(amount)

    def get(self, name: str) -> int:
        return int(self.counters.get(name, 0))


def classify_error(reason: Optional[str]) -> str:
    """把任务级失败原因归到一个分类。"""
    text = str(reason or "").lower()
    if not text:
        return CATEGORY_OK
    if any(marker in text for marker in _CONFIG_PATTERNS):
        return CATEGORY_CONFIG_ERROR
    if any(marker in text for marker in _LOGIN_PATTERNS):
        return CATEGORY_LOGIN_EXPIRED
    if any(marker in text for marker in _RISK_PATTERNS):
        return CATEGORY_RISK_CONTROL
    return CATEGORY_CRASH


def assess_run(
    counters: dict,
    *,
    status: str,
    error_reason: Optional[str] = None,
    previous_runs: Iterable[dict] = (),
) -> dict:
    """根据本轮计数、失败原因和最近几轮历史给出 level / category / message。"""
    counters = {name: int((counters or {}).get(name, 0) or 0) for name in COUNTER_NAMES}
    previous = [run for run in previous_runs if isinstance(run, dict)]

    if status != "success":
        category = classify_error(error_reason)
        if category == CATEGORY_OK:
            category = CATEGORY_CRASH
        consecutive_failures = 1 + _leading_failures(previous)
        if category in (CATEGORY_LOGIN_EXPIRED, CATEGORY_CONFIG_ERROR):
            level = LEVEL_CRITICAL
        elif category == CATEGORY_RISK_CONTROL:
            level = LEVEL_CRITICAL if consecutive_failures >= 2 else LEVEL_WARNING
        else:
            level = LEVEL_CRITICAL if consecutive_failures >= 2 else LEVEL_WARNING
        reason_text = (str(error_reason or "未知错误").strip().splitlines() or ["未知错误"])[0][:200]
        suffix = f"（已连续 {consecutive_failures} 轮失败）" if consecutive_failures >= 2 else ""
        return _assessment(level, category, f"任务运行失败：{reason_text}{suffix}")

    total = counters["search_items_total"]
    failed = counters["search_parse_failed"]
    if total >= PARSE_FAILURE_MIN_ITEMS and failed / total >= PARSE_FAILURE_RATIO:
        return _assessment(
            LEVEL_CRITICAL,
            CATEGORY_API_DRIFT,
            f"最近一轮搜索返回 {total} 条，其中 {failed} 条解析失败（{failed / total:.0%}），字段结构很可能变了",
        )

    attempts = counters["detail_attempts"]
    unavailable = counters["detail_unavailable"] + counters["detail_errors"]
    if attempts >= DETAIL_FAILURE_MIN_ATTEMPTS and unavailable / attempts >= DETAIL_FAILURE_RATIO:
        return _assessment(
            LEVEL_CRITICAL,
            CATEGORY_API_DRIFT,
            f"最近一轮尝试抓取 {attempts} 个商品详情，{unavailable} 个没有返回商品数据（{unavailable / attempts:.0%}）",
        )

    if total == 0:
        history = [int(run.get("counters", {}).get("search_items_total", 0) or 0) for run in previous if run.get("status") == "success"]
        if history and median(history) > 0:
            return _assessment(
                LEVEL_WARNING,
                CATEGORY_ZERO_RESULTS,
                f"最近一轮搜索返回 0 条，此前 {len(history)} 轮中位数 {int(median(history))} 条",
            )
        return _assessment(LEVEL_WARNING, CATEGORY_ZERO_RESULTS, "最近一轮搜索返回 0 条")

    if counters["search_empty_pages"] and total == 0:
        return _assessment(LEVEL_WARNING, CATEGORY_API_DRIFT, "搜索接口返回了响应但没有商品列表字段")

    submitted = counters["items_submitted"]
    skipped = counters["already_processed"]
    message = f"最近一轮搜索 {total} 条，提交分析 {submitted} 条"
    if skipped:
        message += f"，{skipped} 条已处理过"
    if counters["detail_timeouts"]:
        message += f"，{counters['detail_timeouts']} 个详情超时"
    return _assessment(LEVEL_OK, CATEGORY_OK, message)


def _leading_failures(previous_runs: list[dict]) -> int:
    count = 0
    for run in previous_runs:
        if run.get("status") == "success":
            break
        count += 1
    return count


def _assessment(level: str, category: str, message: str) -> dict:
    return {
        "level": level,
        "category": category,
        "title": CATEGORY_TITLES.get(category, category),
        "message": message,
        "action": CATEGORY_ACTIONS.get(category, ""),
    }


# ---------------------------------------------------------------- 存储 ----------------------------------------------------------------

def _now_text(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def _run_counter_key(task_name: str) -> str:
    return f"run_no:{task_name}"


@retry_on_lock
def begin_task_run(task_name: str) -> int:
    """开跑时给本任务发一个轮次编号（第 N 轮），每个任务各自从 1 连续往上数。

    计数器放在 app_metadata 里，和 crawl_runs 的保留条数无关（那张表只留最近 200 轮）。
    第一次发号时从该任务已有 crawl_runs 行的最大 id 接着数，这样编号和之前健康面板里看到的轮次 id 不会倒退。
    同一事务里读、加、写，两个任务同时开跑也不会发重。
    """
    bootstrap_sqlite_storage()
    key = _run_counter_key(task_name)
    with sqlite_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
        if row is not None:
            try:
                current = int(row["value"])
            except (TypeError, ValueError):
                current = 0
        else:
            seed = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM crawl_runs WHERE task_name = ?", (task_name,)
            ).fetchone()[0]
            current = int(seed or 0)
        run_no = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (key, str(run_no)),
        )
        conn.commit()
    return run_no


@retry_on_lock
def record_crawl_run(
    stats: RunStats,
    *,
    status: str,
    error_reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """写入一轮运行摘要并返回评估结果（含 run_id）。"""
    bootstrap_sqlite_storage()
    previous = load_recent_runs(stats.task_name, limit=HISTORY_RUNS)
    assessment = assess_run(stats.counters, status=status, error_reason=error_reason, previous_runs=previous)
    finished_at = _now_text(now)
    with sqlite_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO crawl_runs (
                task_name, keyword, started_at, finished_at, status, error_reason,
                level, category, message, counters_json, notified, run_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                stats.task_name,
                stats.keyword,
                stats.started_at,
                finished_at,
                "success" if status == "success" else "failed",
                (str(error_reason)[:1000] if error_reason else None),
                assessment["level"],
                assessment["category"],
                assessment["message"],
                json.dumps(stats.counters, ensure_ascii=False),
                stats.run_no,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            """
            DELETE FROM crawl_runs
            WHERE task_name = ? AND id NOT IN (
                SELECT id FROM crawl_runs WHERE task_name = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (stats.task_name, stats.task_name, RUNS_RETENTION_PER_TASK),
        )
        conn.commit()
    return {**assessment, "run_id": run_id, "run_no": stats.run_no, "finished_at": finished_at, "status": status}


def load_recent_runs(task_name: str, *, limit: int = HISTORY_RUNS) -> list[dict]:
    """最近 limit 轮，最新在前。"""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM crawl_runs WHERE task_name = ? ORDER BY id DESC LIMIT ?",
            (task_name, int(limit)),
        ).fetchall()
    return [_row_to_run(row) for row in rows]


def _row_to_run(row) -> dict:
    try:
        counters = json.loads(row["counters_json"] or "{}")
    except (TypeError, ValueError):
        counters = {}
    keys = row.keys()
    run_no = row["run_no"] if "run_no" in keys else None
    return {
        "id": int(row["id"]),
        "run_no": int(run_no) if run_no is not None else None,
        "task_name": row["task_name"],
        "keyword": row["keyword"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "error_reason": row["error_reason"],
        "level": row["level"],
        "category": row["category"],
        "message": row["message"],
        "counters": counters,
        "notified": bool(row["notified"]),
    }


def should_notify_health(task_name: str, category: str, *, now: Optional[datetime] = None) -> bool:
    """同一任务同一分类每天最多提醒一次。"""
    bootstrap_sqlite_storage()
    day = _now_text(now)[:10]
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM crawl_runs
            WHERE task_name = ? AND category = ? AND notified = 1 AND substr(finished_at, 1, 10) = ?
            LIMIT 1
            """,
            (task_name, category, day),
        ).fetchone()
    return row is None


@retry_on_lock
def mark_run_notified(run_id: int) -> None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute("UPDATE crawl_runs SET notified = 1 WHERE id = ?", (int(run_id),))
        conn.commit()


# ---------------------------------------------------------------- 总览 ----------------------------------------------------------------

_LEVEL_ORDER = {LEVEL_OK: 0, LEVEL_UNKNOWN: 0, LEVEL_WARNING: 1, LEVEL_CRITICAL: 2}


def build_crawler_health(tasks: Iterable, guard_tasks: Optional[dict] = None, *, now: Optional[datetime] = None) -> dict:
    """给界面用的总览：每个任务最近一轮的评估 + 熔断器状态。

    tasks 是任务对象或 dict（需要 task_name / keyword / id），guard_tasks 是 FailureGuard.snapshot()。
    """
    current = now or datetime.now()
    guard_tasks = guard_tasks or {}
    entries = []
    for task in tasks:
        task_name = str(_task_attr(task, "task_name") or "")
        keyword = str(_task_attr(task, "keyword") or "")
        task_id = _task_attr(task, "id")
        runs = load_recent_runs(task_name, limit=1) if task_name else []
        latest = runs[0] if runs else None
        guard = guard_tasks.get(task_name) if isinstance(guard_tasks.get(task_name), dict) else {}
        consecutive = int(guard.get("consecutive_failures") or 0)
        paused_until = guard.get("paused_until")
        paused_dt = _parse_dt(paused_until)
        is_paused = bool(paused_dt and paused_dt > current)

        if latest is None:
            entry = {
                "level": LEVEL_UNKNOWN,
                "category": CATEGORY_NO_RUNS,
                "title": CATEGORY_TITLES[CATEGORY_NO_RUNS],
                "message": "还没有运行过",
                "action": "",
            }
        else:
            entry = {key: latest[key] for key in ("level", "category", "message")}
            entry["title"] = CATEGORY_TITLES.get(entry["category"], entry["category"])
            entry["action"] = CATEGORY_ACTIONS.get(entry["category"], "")

        if is_paused:
            # 熔断中：底层原因（登录失效等）决定级别，分类标成 paused 让界面说明"到期自动恢复"
            underlying = classify_error(guard.get("last_failure_reason"))
            level = LEVEL_CRITICAL if underlying in (CATEGORY_LOGIN_EXPIRED, CATEGORY_CONFIG_ERROR) else LEVEL_WARNING
            entry = {
                "level": max(level, entry["level"], key=lambda item: _LEVEL_ORDER.get(item, 0)),
                "category": CATEGORY_PAUSED,
                "title": CATEGORY_TITLES[CATEGORY_PAUSED],
                "message": f"连续失败 {consecutive} 次后已暂停到 {paused_until}；最近原因：{CATEGORY_TITLES.get(underlying, underlying)}",
                "action": CATEGORY_ACTIONS[CATEGORY_PAUSED] + (f"；{CATEGORY_ACTIONS[underlying]}" if CATEGORY_ACTIONS.get(underlying) else ""),
            }

        entries.append(
            {
                "task_id": task_id,
                "task_name": task_name,
                "keyword": keyword,
                **entry,
                "last_run_at": latest["finished_at"] if latest else None,
                "last_run_status": latest["status"] if latest else None,
                "consecutive_failures": consecutive,
                "paused_until": paused_until if is_paused else None,
                "counters": latest["counters"] if latest else {},
            }
        )

    overall = LEVEL_OK
    for entry in entries:
        if _LEVEL_ORDER.get(entry["level"], 0) > _LEVEL_ORDER.get(overall, 0):
            overall = entry["level"]
    return {"overall_level": overall, "generated_at": _now_text(current), "tasks": entries}


def _task_attr(task, name: str):
    if isinstance(task, dict):
        return task.get(name)
    return getattr(task, name, None)


def _parse_dt(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def format_alert_text(assessment: dict) -> str:
    """推送里用的一段话：分类 + 细节 + 建议动作。"""
    title = assessment.get("title") or CATEGORY_TITLES.get(assessment.get("category", ""), "")
    parts = [f"【{title}】{assessment.get('message', '')}".strip()]
    action = assessment.get("action")
    if action:
        parts.append(f"建议：{action}")
    return "\n".join(parts)


def sanitize_reason(reason: Optional[str]) -> str:
    """失败原因里去掉太长的堆栈和 URL 参数。"""
    text = str(reason or "").strip()
    text = re.sub(r"\?[^\s]{60,}", "?…", text)
    return text[:500]
