import asyncio
import json
import os
import sys
import random
import re
import tempfile
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import (
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.ai_handler import (
    download_all_images,
    get_ai_analysis,
    send_ntfy_notification,
    cleanup_task_images,
)
from src.config import (
    AI_DEBUG_MODE,
    DETAIL_API_URL_PATTERN,
    LOGIN_IS_EDGE,
    RUN_HEADLESS,
    RUNNING_IN_DOCKER,
    SKIP_AI_ANALYSIS,
    STATE_FILE,
)
from src.parsers import (
    _parse_search_results_json,
    _parse_user_items_data,
    calculate_reputation_from_ratings,
    parse_item_detail_extras,
    parse_item_detail_facts,
    parse_ratings_data,
    parse_seller_detail,
    parse_user_head_data,
)
from src.utils import (
    format_registration_days,
    get_link_unique_key,
    log_time,
    random_sleep,
    safe_get,
    save_to_jsonl,
)
from src.rotation import RotationPool, load_state_files, parse_proxy_pool, RotationItem
from src.failure_guard import FailureGuard
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.item_analysis_dispatcher import (
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)
from src.services.price_history_service import (
    build_market_reference,
    load_price_snapshots,
    load_sold_listings,
    parse_price_value,
    record_market_snapshots,
)
from src.services.result_storage_service import load_processed_link_keys, mark_notification_status
from src.services.crawl_health_service import (
    LEVEL_OK,
    RunStats,
    begin_task_run,
    classify_error,
    format_alert_text,
    mark_run_notified,
    record_crawl_run,
    should_notify_health,
    CATEGORY_TITLES,
    CATEGORY_ACTIONS,
)
from src.services.seller_profile_cache import SellerProfileCache
from src.attribute_rules import normalize_attribute_rules
from src.title_filter import (
    filter_items_by_title,
    normalize_title_keywords,
    title_filter_reason,
)
from src.services.search_pagination import (
    advance_search_page,
    is_search_results_response,
)


class RiskControlError(Exception):
    pass


class LoginRequiredError(Exception):
    """Raised when Goofish redirects to the passport/mini_login flow."""


class ItemDetailUnavailable(Exception):
    """详情接口没有返回商品数据（ret 非 SUCCESS 或 itemDO 为空）。

    这种商品本次不落库、不标记已处理，下次运行会重新抓；
    否则登录态过期时整批商品会以空数据落库并永久被"已存在，跳过"。
    """


LOGIN_EXPIRED_RET_MARKERS = (
    "FAIL_SYS_TOKEN",
    "FAIL_SYS_SESSION_EXPIRED",
    "NEED_LOGIN",
    "LOGIN_REQUIRED",
    "FAIL_SYS_ILLEGAL_ACCESS",
)


def classify_detail_ret(ret_values) -> str:
    """把 mtop 的 ret 列表归为 ok / risk_control / login_expired / failed。"""
    if ret_values is None:
        return "failed"
    values = ret_values if isinstance(ret_values, (list, tuple)) else [ret_values]
    texts = [str(v) for v in values if v is not None]
    if any(t.startswith("SUCCESS") for t in texts):
        return "ok"
    joined = " ".join(texts)
    if "FAIL_SYS_USER_VALIDATE" in joined:
        return "risk_control"
    if any(marker in joined for marker in LOGIN_EXPIRED_RET_MARKERS):
        return "login_expired"
    return "failed"


FAILURE_GUARD = FailureGuard()
EDGE_DOCKER_WARNING_PRINTED = False


def _is_login_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return "passport.goofish.com" in lowered or "mini_login" in lowered


def _resolve_browser_channel() -> str:
    global EDGE_DOCKER_WARNING_PRINTED
    if RUNNING_IN_DOCKER:
        if LOGIN_IS_EDGE and not EDGE_DOCKER_WARNING_PRINTED:
            print(
                "检测到 LOGIN_IS_EDGE=true，但 Docker 镜像未内置 Edge，"
                "任务运行时将改用 Chromium。"
            )
            EDGE_DOCKER_WARNING_PRINTED = True
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def _should_analyze_images(task_config: dict) -> bool:
    raw_value = task_config.get("analyze_images", True)
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() not in {"false", "0", "no", "off"}


# 搜索页区域弹窗的 class 带 CSS Module hash，线上同时存在多套 hash。
# 只匹配前缀，避免写死 provItem--QAdOx8nD / searchBtn--Ic6RKcAb 后失效。
REGION_AREA_WRAP_SELECTOR = "[class*='areaWrap']:visible"
REGION_COLUMN_SELECTOR = ":scope > div"
REGION_OPTION_SELECTOR = "[class*='provItem']:visible"
REGION_SUBMIT_SELECTOR = "[class*='searchBtn']:visible"
REGION_SUBMIT_TEXT = re.compile(r"查看(?:\d+|999\+)件宝贝")
CHROMIUM_CRASH_DUMPS_DIRNAME = "chromium-crashpad"


def build_chromium_launch_args(crash_dumps_dir: str) -> list[str]:
    """Chromium 启动参数。

    Docker 里的 Playwright Chromium 会拉起 chrome_crashpad_handler。数据库路径一旦落到
    ``/``，handler 会自监控重启并占住大量内存。这里禁用 crash reporter，同时把 dump
    目录固定到可写临时目录。
    """
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-web-security",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-features=IsolateOrigins,site-per-process,Crashpad",
        f"--crash-dumps-dir={crash_dumps_dir}",
    ]


def prepare_chromium_launch_args() -> list[str]:
    crash_dumps_dir = os.path.join(tempfile.gettempdir(), CHROMIUM_CRASH_DUMPS_DIRNAME)
    os.makedirs(crash_dumps_dir, exist_ok=True)
    return build_chromium_launch_args(crash_dumps_dir)


def _format_failure_reason(reason: str, limit: int = 500) -> str:
    if not reason:
        return "未知错误"
    cleaned = " ".join(str(reason).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _record_run_summary(run_stats: "RunStats", last_error: str) -> Optional[dict]:
    """把本轮计数写进 crawl_runs 并返回评估；写库失败只打日志，不影响任务结果。"""
    try:
        assessment = record_crawl_run(
            run_stats,
            status="failed" if last_error else "success",
            error_reason=last_error or None,
        )
    except Exception as exc:
        print(f"[健康] 写入运行摘要失败（忽略）: {exc}")
        return None
    print(
        f"[健康] 本轮评估：{assessment.get('title')}（{assessment.get('level')}）— {assessment.get('message')}"
    )
    return assessment


async def _notify_health_alert(task_config: dict, assessment: dict) -> None:
    """任务没报错但健康评估异常时推送一条；同一任务同一分类每天最多一次。"""
    task_name = task_config.get("task_name", "未命名任务")
    category = str(assessment.get("category") or "")
    try:
        if not should_notify_health(task_name, category):
            print(f"[健康] 今天已提醒过「{assessment.get('title')}」，本次不再推送。")
            return
    except Exception as exc:
        print(f"[健康] 检查提醒记录失败（忽略）: {exc}")
        return
    product_data = {
        "商品标题": f"[爬虫健康] {task_name}：{assessment.get('title')}",
        "当前售价": "N/A",
        "商品链接": "#",
    }
    notify_reason = format_alert_text(assessment) + f"\n任务: {task_name}\n关键词: {task_config.get('keyword', '') or 'N/A'}"
    try:
        await send_ntfy_notification(product_data, notify_reason)
        if assessment.get("run_id"):
            mark_run_notified(int(assessment["run_id"]))
    except Exception as exc:
        print(f"[健康] 推送健康提醒失败: {exc}")


async def _notify_task_failure(
    task_config: dict, reason: str, *, cookie_path: Optional[str], assessment: Optional[dict] = None
) -> None:
    task_name = task_config.get("task_name", "未命名任务")
    keyword = task_config.get("keyword", "")
    formatted_reason = _format_failure_reason(reason)
    # 推送里先说是哪一类、该做什么，再贴原始错误
    category = (assessment or {}).get("category") or classify_error(formatted_reason)
    category_title = CATEGORY_TITLES.get(category, category)
    category_action = CATEGORY_ACTIONS.get(category, "")

    # Some failures are deterministic misconfiguration and should pause/notify immediately.
    pause_immediately = any(
        marker in formatted_reason
        for marker in (
            "未找到可用的代理地址",
            "未找到可用的登录状态文件",
        )
    )

    guard_result = FAILURE_GUARD.record_failure(
        task_name,
        formatted_reason,
        cookie_path=cookie_path,
        min_failures_to_pause=1 if pause_immediately else None,
    )

    if not guard_result.get("should_notify"):
        print(
            f"[FailureGuard] 任务 '{task_name}' 失败计数 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold}，暂不通知。"
        )
        return

    paused_until = guard_result.get("paused_until")
    paused_until_str = (
        paused_until.strftime("%Y-%m-%d %H:%M:%S") if paused_until else "N/A"
    )

    product_data = {
        "商品标题": f"[任务异常] {task_name}",
        "当前售价": "N/A",
        "商品链接": "#",
    }
    notify_reason = (
        f"【{category_title}】" + (f"建议：{category_action}\n" if category_action else "")
        + f"任务运行失败(已连续 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold} 次): {formatted_reason}"
        f"\n任务: {task_name}"
        f"\n关键词: {keyword or 'N/A'}"
        f"\n已自动暂停重试，暂停到: {paused_until_str}"
        f"\n修复后(更新登录态/cookies文件)将自动恢复。"
    )

    try:
        await send_ntfy_notification(product_data, notify_reason)
    except Exception as e:
        print(f"发送任务异常通知失败: {e}")


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_rotation_settings(task_config: dict) -> dict:
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}

    account_enabled = _as_bool(
        account_cfg.get("enabled"),
        _as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"), False),
    )
    account_mode = (
        account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")
    ).lower()
    account_state_dir = account_cfg.get("state_dir") or os.getenv(
        "ACCOUNT_STATE_DIR", "state"
    )
    account_retry_limit = _as_int(
        account_cfg.get("retry_limit"),
        _as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2),
    )
    account_blacklist_ttl = _as_int(
        account_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300),
    )

    proxy_enabled = _as_bool(
        proxy_cfg.get("enabled"), _as_bool(os.getenv("PROXY_ROTATION_ENABLED"), False)
    )
    proxy_mode = (
        proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")
    ).lower()
    proxy_pool = proxy_cfg.get("proxy_pool") or os.getenv("PROXY_POOL", "")
    proxy_retry_limit = _as_int(
        proxy_cfg.get("retry_limit"),
        _as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2),
    )
    proxy_blacklist_ttl = _as_int(
        proxy_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300),
    )

    return {
        "account_enabled": account_enabled,
        "account_mode": account_mode,
        "account_state_dir": account_state_dir,
        "account_retry_limit": max(1, account_retry_limit),
        "account_blacklist_ttl": max(0, account_blacklist_ttl),
        "proxy_enabled": proxy_enabled,
        "proxy_mode": proxy_mode,
        "proxy_pool": proxy_pool,
        "proxy_retry_limit": max(1, proxy_retry_limit),
        "proxy_blacklist_ttl": max(0, proxy_blacklist_ttl),
    }


def _get_ai_analysis_concurrency(task_config: dict) -> int:
    configured = task_config.get("ai_analysis_concurrency")
    default = _as_int(os.getenv("AI_ANALYSIS_CONCURRENCY"), 2)
    return max(1, _as_int(configured, default))


def _get_seller_profile_cache_ttl(task_config: dict) -> int:
    configured = task_config.get("seller_profile_cache_ttl")
    default = _as_int(os.getenv("SELLER_PROFILE_CACHE_TTL"), 1800)
    return max(0, _as_int(configured, default))


def _default_context_options() -> dict:
    return {
        "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
        "color_scheme": "light",
    }


def _clean_kwargs(options: dict) -> dict:
    return {k: v for k, v in options.items() if v is not None}


def filter_items_by_price(items, min_price, max_price, price_key: str = "当前售价") -> list:
    """本地价格过滤：只保留售价落在 [min_price, max_price] 内的商品。

    站内价格筛选可能没生效（价格输入框没找到、请求超时、筛选响应异常后回退到未筛选结果），
    这里在逐商品处理前再过滤一次，免得超出价位的商品被抓详情、调 AI、落库、推送。
    价格解析不出来的商品（空、"价格异常"）保留，交给后续环节判断；上下限写反时自动交换。
    """
    item_list = list(items or [])
    low = parse_price_value(min_price)
    high = parse_price_value(max_price)
    if low is None and high is None:
        return item_list
    if low is not None and high is not None and low > high:
        low, high = high, low
    kept = []
    for item in item_list:
        price = parse_price_value(item.get(price_key)) if isinstance(item, dict) else None
        if price is None:
            kept.append(item)
        elif low is not None and price < low:
            continue
        elif high is not None and price > high:
            continue
        else:
            kept.append(item)
    return kept


def build_stealth_init_script(context_options: dict) -> str:
    """按 BrowserContext 的 is_mobile / has_touch 生成反检测脚本，避免指纹自相矛盾。

    - 移动端 UA 下 navigator.plugins 应为空；桌面快照（has_touch=False）下 maxTouchPoints 应为 0。
      之前写死 plugins=[1..5]、maxTouchPoints=5，和 context 的设备参数对不上。
    - 不再伪造 window.chrome.loadTimes：新版 Chrome 已移除该方法，有它反而像伪装；
      真浏览器自带 window.chrome 时也不覆盖。
    """
    is_mobile = bool(context_options.get("is_mobile"))
    has_touch = bool(context_options.get("has_touch"))
    plugins_js = "[]" if is_mobile else "[1, 2, 3, 4, 5]"
    max_touch_points = 5 if has_touch else 0
    return f"""
        // 移除webdriver标识
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});

        // navigator 属性和 context 的设备参数保持一致（移动端没有插件）
        Object.defineProperty(navigator, 'plugins', {{get: () => {plugins_js}}});
        Object.defineProperty(navigator, 'languages', {{get: () => ['zh-CN', 'zh', 'en-US', 'en']}});

        // 真浏览器自带 window.chrome 就不动；补的时候只给 runtime / csi，不伪造已被新版 Chrome 移除的方法
        if (!window.chrome) {{
            window.chrome = {{runtime: {{}}, csi: function() {{}}}};
        }}

        // 触摸点数和 has_touch 一致
        Object.defineProperty(navigator, 'maxTouchPoints', {{get: () => {max_touch_points}}});

        // 覆盖permissions查询（避免暴露自动化）
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({{state: Notification.permission}}) :
                originalQuery(parameters)
        );
    """


def _looks_like_mobile(ua: str) -> Optional[bool]:
    if not ua:
        return None
    ua_lower = ua.lower()
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        return True
    if "windows" in ua_lower or "macintosh" in ua_lower:
        return False
    return None


def _build_context_overrides(snapshot: dict) -> dict:
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    navigator = env.get("navigator") or {}
    screen = env.get("screen") or {}
    intl = env.get("intl") or {}

    overrides = {}

    ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if ua:
        overrides["user_agent"] = ua

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    locale = None
    if accept_language:
        locale = accept_language.split(",")[0].strip()
    elif navigator.get("language"):
        locale = navigator["language"]
    if locale:
        overrides["locale"] = locale

    tz = intl.get("timeZone")
    if tz:
        overrides["timezone_id"] = tz

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)):
        overrides["device_scale_factor"] = float(dpr)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    mobile_flag = _looks_like_mobile(ua or "")
    if mobile_flag is not None:
        overrides["is_mobile"] = mobile_flag

    return _clean_kwargs(overrides)


# 这些头由浏览器按请求类型生成。扩展快照里常见的是某次 XHR 的值
# （Sec-Fetch-Dest: empty / Sec-Fetch-Mode: cors / Accept: */*）。
# 如果原样套到 BrowserContext，文档和脚本请求会被改成 CORS 空请求，搜索页只剩白屏。
_BROWSER_MANAGED_EXTRA_HEADERS = {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "user-agent",
}


def _build_extra_headers(raw_headers: Optional[dict]) -> dict:
    if not raw_headers:
        return {}
    headers = {}
    for key, value in raw_headers.items():
        if not key or key.lower() in _BROWSER_MANAGED_EXTRA_HEADERS or value is None:
            continue
        headers[key] = value
    return headers


# 卖家主页滚动加载：每次滚动后最多等这么久"新一页到了"；新页一到就立刻继续滚
PROFILE_SCROLL_PAGE_TIMEOUT_SECONDS = 8
# 风控弹窗检测总等待；广告弹窗"出现就点"的最长等待
RISK_CONTROL_OVERLAY_TIMEOUT_MS = 2000
AD_POPUP_CLOSE_TIMEOUT_MS = 1500


async def _scroll_until_exhausted(
    page,
    *,
    stop_event: asyncio.Event,
    page_arrived: asyncio.Event,
    label: str,
    timeout_seconds: Optional[float] = None,
) -> None:
    """滚到底部触发下一页，等到"新一页响应到了"就马上再滚。

    之前等的是 ``stop_event``（没有下一页才会 set），``nextPage=true`` 时每页都要等满超时，
    多页卖家一次采集要一两分钟。现在只在真正没有下一页或超时没新页时退出。
    """
    if timeout_seconds is None:
        timeout_seconds = PROFILE_SCROLL_PAGE_TIMEOUT_SECONDS
    while not stop_event.is_set():
        page_arrived.clear()
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            await asyncio.wait_for(page_arrived.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            print(f"      [滚动超时] {label}可能已加载完毕。")
            break


async def _detect_risk_control_overlay(page) -> Optional[str]:
    """两种风控弹窗合并成一个 locator 检测，总等待不超过 2 秒；返回命中的标识或 None。"""
    baxia_dialog = page.locator("div.baxia-dialog-mask")
    middleware_widget = page.locator("div.J_MIDDLEWARE_FRAME_WIDGET")
    block_overlay = baxia_dialog.or_(middleware_widget).filter(visible=True).first
    try:
        await block_overlay.wait_for(
            state="visible", timeout=RISK_CONTROL_OVERLAY_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        return None
    try:
        if await baxia_dialog.first.is_visible():
            return "baxia-dialog"
    except Exception:
        pass
    return "J_MIDDLEWARE_FRAME_WIDGET"


def _print_risk_control_banner(marker: str, keyword: str) -> None:
    print("\n==================== CRITICAL BLOCK DETECTED ====================")
    print(f"检测到闲鱼反爬虫验证弹窗 ({marker})，无法继续操作。")
    print("这通常是因为操作过于频繁或被识别为机器人。")
    print("建议：")
    print("1. 停止脚本一段时间再试。")
    print("2. (推荐) 在 .env 文件中设置 RUN_HEADLESS=false，以非无头模式运行，这有助于绕过检测。")
    print("3. 更新登录状态文件，确保登录状态有效；并降低任务执行频率。")
    print(f"任务 '{keyword}' 将在此处中止。")
    print("===================================================================")


async def _close_ad_popup(page) -> bool:
    """广告弹窗的关闭按钮出现就点，最多等 1.5 秒；没有就直接继续。"""
    close_icon = page.locator("div[class*='closeIconBg']").first
    try:
        await close_icon.wait_for(state="visible", timeout=AD_POPUP_CLOSE_TIMEOUT_MS)
        await close_icon.click(timeout=AD_POPUP_CLOSE_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        print("LOG: 未检测到广告弹窗。")
        return False
    except Exception as exc:
        print(f"LOG: 关闭广告弹窗失败（忽略）: {exc}")
        return False
    print("LOG: 已关闭广告弹窗。")
    return True


async def _join_analysis_dispatcher(dispatcher, *, max_rounds: int) -> list:
    """等后台分析任务全部结束；某个任务抛异常时只记日志，继续等其余任务。

    ``dispatcher.join()`` 内部是 ``gather(*tasks)``：任一任务抛异常就整体抛出，其余任务还在跑，
    异常再从 finally 冒出去会跳过 ``browser.close()``。这里每抓到一个异常就再 join 一轮
    （抛过的任务已从在途集合里移除），轮数以已提交的任务数为上限。
    """
    errors: list = []
    for _ in range(max(1, max_rounds)):
        try:
            await dispatcher.join()
            return errors
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)
            log_time(f"[后台分析] 一个分析任务异常退出（已记录，继续等待其余任务）: {exc!r}")
    log_time(f"[后台分析] 已有 {len(errors)} 个任务异常，达到上限，不再等待剩余任务。")
    return errors


async def _cancel_analysis_dispatcher(dispatcher) -> int:
    """任务被取消时不再等全部分析完成，直接取消在途任务。"""
    try:
        cancelled = await dispatcher.cancel_all()
    except Exception as exc:
        log_time(f"取消后台分析任务时出错（忽略）: {exc!r}")
        return 0
    if cancelled:
        log_time(f"已取消 {cancelled} 个在途的后台分析任务。")
    return cancelled


async def scrape_user_profile(context, user_id: str) -> dict:
    """
    【新版】访问指定用户的个人主页，按顺序采集其摘要信息、完整的商品列表和完整的评价列表。
    """
    print(f"   -> 开始采集用户ID: {user_id} 的完整信息...")
    profile_data = {}
    page = await context.new_page()

    # 为各项异步任务准备Future和数据容器
    head_api_future = asyncio.get_event_loop().create_future()

    all_items, all_ratings = [], []
    stop_item_scrolling, stop_rating_scrolling = asyncio.Event(), asyncio.Event()
    # 每收到一页列表响应就 set 一次：滚动后只等"新一页到了"，不用每页都把超时等满。
    # 这些 Event 和 page.on 回调都在当前运行的 loop 上创建/触发。
    item_page_arrived, rating_page_arrived = asyncio.Event(), asyncio.Event()

    async def handle_response(response: Response):
        # 捕获头部摘要API
        if (
            "mtop.idle.web.user.page.head" in response.url
            and not head_api_future.done()
        ):
            try:
                head_api_future.set_result(await response.json())
                print(f"      [API捕获] 用户头部信息... 成功")
            except Exception as e:
                if not head_api_future.done():
                    head_api_future.set_exception(e)

        # 捕获商品列表API
        elif "mtop.idle.web.xyh.item.list" in response.url:
            try:
                data = await response.json()
                all_items.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 商品列表... 当前已捕获 {len(all_items)} 件")
                if not data.get("data", {}).get("nextPage", True):
                    stop_item_scrolling.set()
            except Exception as e:
                stop_item_scrolling.set()
            finally:
                item_page_arrived.set()

        # 捕获评价列表API
        elif "mtop.idle.web.trade.rate.list" in response.url:
            try:
                data = await response.json()
                all_ratings.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 评价列表... 当前已捕获 {len(all_ratings)} 条")
                if not data.get("data", {}).get("nextPage", True):
                    stop_rating_scrolling.set()
            except Exception as e:
                stop_rating_scrolling.set()
            finally:
                rating_page_arrived.set()

    page.on("response", handle_response)

    try:
        # --- 任务1: 导航并采集头部信息 ---
        await page.goto(
            f"https://www.goofish.com/personal?userId={user_id}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        head_data = await asyncio.wait_for(head_api_future, timeout=15)
        profile_data = await parse_user_head_data(head_data)

        # --- 任务2: 滚动加载所有商品 (默认页面) ---
        print("      [采集阶段] 开始采集该用户的商品列表...")
        await random_sleep(2, 4)  # 等待第一页商品API完成
        await _scroll_until_exhausted(
            page,
            stop_event=stop_item_scrolling,
            page_arrived=item_page_arrived,
            label="商品列表",
        )
        profile_data["卖家发布的商品列表"] = await _parse_user_items_data(all_items)

        # --- 任务3: 点击并采集所有评价 ---
        print("      [采集阶段] 开始采集该用户的评价列表...")
        rating_tab_locator = page.locator("//div[text()='信用及评价']/ancestor::li")
        if await rating_tab_locator.count() > 0:
            await rating_tab_locator.click()
            await random_sleep(3, 5)  # 等待第一页评价API完成

            await _scroll_until_exhausted(
                page,
                stop_event=stop_rating_scrolling,
                page_arrived=rating_page_arrived,
                label="评价列表",
            )

            profile_data["卖家收到的评价列表"] = await parse_ratings_data(all_ratings)
            reputation_stats = await calculate_reputation_from_ratings(all_ratings)
            profile_data.update(reputation_stats)
        else:
            print("      [警告] 未找到评价选项卡，跳过评价采集。")

    except Exception as e:
        print(f"   [错误] 采集用户 {user_id} 信息时发生错误: {e}")
    finally:
        page.remove_listener("response", handle_response)
        await page.close()
        print(f"   -> 用户 {user_id} 信息采集完成。")

    return profile_data


async def scrape_xianyu(task_config: dict, debug_limit: int = 0):
    """
    【核心执行器】
    根据单个任务配置，异步爬取闲鱼商品数据，并对每个新发现的商品进行实时的、独立的AI分析和通知。
    """
    keyword = task_config["keyword"]
    max_pages = task_config.get("max_pages", 1)
    personal_only = task_config.get("personal_only", False)
    min_price = task_config.get("min_price")
    max_price = task_config.get("max_price")
    ai_prompt_text = task_config.get("ai_prompt_text", "")
    analyze_images = _should_analyze_images(task_config)
    decision_mode = str(task_config.get("decision_mode", "ai")).strip().lower()
    if decision_mode not in {"ai", "keyword"}:
        decision_mode = "ai"
    keyword_rules = task_config.get("keyword_rules") or []
    title_include_keywords = normalize_title_keywords(task_config.get("title_include_keywords"))
    title_exclude_keywords = normalize_title_keywords(task_config.get("title_exclude_keywords"))
    # 附加搜索词：和主关键词依次搜索，共用去重、结果集和判断标准
    extra_keywords = [
        extra
        for extra in normalize_title_keywords(task_config.get("extra_keywords"))
        if extra.strip().lower() != str(keyword).strip().lower()
    ]
    search_keywords = [keyword] + extra_keywords
    # 运行摘要：各处只 add 计数，结束时统一分类并写库，界面和推送据此判断"爬虫是否需要人介入"
    run_stats = RunStats(task_name=str(task_config.get("task_name", "未命名任务")), keyword=str(keyword))
    # 轮次编号：开跑就发号，写进每条商品记录（结果页显示「第 N 轮」）和结束时的运行摘要；发号失败不影响抓取
    try:
        run_stats.run_no = begin_task_run(run_stats.task_name)
        print(f"LOG: 本次是任务「{run_stats.task_name}」的第 {run_stats.run_no} 轮。")
    except Exception as exc:
        print(f"警告: 轮次编号分配失败，本轮商品记录不带轮次号: {exc}")
    try:
        attribute_rules = normalize_attribute_rules(task_config.get("attribute_rules"))
    except ValueError as exc:
        print(f"警告: 属性硬规则无法解析，本次运行忽略属性规则: {exc}")
        attribute_rules = []
    free_shipping = task_config.get("free_shipping", False)
    raw_new_publish = task_config.get("new_publish_option") or ""
    new_publish_option = raw_new_publish.strip()
    if new_publish_option == "__none__":
        new_publish_option = ""
    region_filter = (task_config.get("region") or "").strip()

    processed_links = set()
    history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    history_seen_item_ids: set[str] = set()
    historical_snapshots = load_price_snapshots(keyword)
    # 已采集卖家的"已售"同类商品，和消失的挂牌一起合成成交参考价
    sold_listings = load_sold_listings(keyword)
    result_filename = build_result_filename(keyword)
    processed_links = load_processed_link_keys(keyword)
    if processed_links:
        print(f"LOG: 发现已存在结果集 {result_filename}，已加载 {len(processed_links)} 个历史商品用于去重。")
    else:
        print(f"LOG: 结果集 {result_filename} 当前为空，将写入新记录。")

    rotation_settings = _get_rotation_settings(task_config)
    account_items = load_state_files(rotation_settings["account_state_dir"])
    runtime_plan = resolve_account_runtime_plan(
        strategy=task_config.get("account_strategy"),
        account_state_file=task_config.get("account_state_file"),
        has_root_state_file=os.path.exists(STATE_FILE),
        available_account_files=account_items,
    )
    forced_account = runtime_plan["forced_account"]
    if runtime_plan["prefer_root_state"]:
        account_items = [STATE_FILE]
        rotation_settings["account_enabled"] = False
    elif runtime_plan["use_account_pool"]:
        rotation_settings["account_enabled"] = True
    else:
        rotation_settings["account_enabled"] = False

    account_pool = RotationPool(
        account_items, rotation_settings["account_blacklist_ttl"], "account"
    )
    proxy_pool = RotationPool(
        parse_proxy_pool(rotation_settings["proxy_pool"]),
        rotation_settings["proxy_blacklist_ttl"],
        "proxy",
    )

    selected_account: Optional[RotationItem] = None
    selected_proxy: Optional[RotationItem] = None

    def _select_account(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_account
        if forced_account:
            return RotationItem(value=forced_account)
        if not rotation_settings["account_enabled"]:
            if os.path.exists(STATE_FILE):
                return RotationItem(value=STATE_FILE)
            return None
        if (
            rotation_settings["account_mode"] == "per_task"
            and selected_account
            and not force_new
        ):
            return selected_account
        picked = account_pool.pick_random()
        return picked or selected_account

    def _select_proxy(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_proxy
        if not rotation_settings["proxy_enabled"]:
            return None
        if (
            rotation_settings["proxy_mode"] == "per_task"
            and selected_proxy
            and not force_new
        ):
            return selected_proxy
        picked = proxy_pool.pick_random()
        return picked or selected_proxy

    async def _run_scrape_attempt(
        state_file: str, proxy_server: Optional[str], search_keyword: str
    ) -> int:
        processed_item_count = 0
        title_filtered_count = 0
        stop_scraping = False

        if not os.path.exists(state_file):
            raise FileNotFoundError(f"登录状态文件不存在: {state_file}")

        snapshot_data = None
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                snapshot_data = json.load(f)
        except Exception as e:
            print(f"警告：读取登录状态文件失败，将直接按路径使用: {e}")

        async with async_playwright() as p:
            launch_args = prepare_chromium_launch_args()

            launch_kwargs = {"headless": RUN_HEADLESS, "args": launch_args}
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}

            launch_kwargs["channel"] = _resolve_browser_channel()

            browser = await p.chromium.launch(**launch_kwargs)

            context_kwargs = _default_context_options()
            storage_state_arg = state_file
            analysis_dispatcher: Optional[ItemAnalysisDispatcher] = None

            if isinstance(snapshot_data, dict):
                # 新版扩展导出的增强快照，包含环境和Header
                if any(
                    key in snapshot_data
                    for key in ("env", "headers", "page", "storage")
                ):
                    print(f"检测到增强浏览器快照，应用环境参数: {state_file}")
                    storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
                    context_kwargs.update(_build_context_overrides(snapshot_data))
                    extra_headers = _build_extra_headers(snapshot_data.get("headers"))
                    if extra_headers:
                        context_kwargs["extra_http_headers"] = extra_headers
                else:
                    storage_state_arg = snapshot_data

            context_kwargs = _clean_kwargs(context_kwargs)
            context = await browser.new_context(
                storage_state=storage_state_arg, **context_kwargs
            )
            seller_profile_cache = SellerProfileCache(
                ttl_seconds=_get_seller_profile_cache_ttl(task_config)
            )
            analysis_dispatcher = ItemAnalysisDispatcher(
                concurrency=_get_ai_analysis_concurrency(task_config),
                skip_ai_analysis=SKIP_AI_ANALYSIS,
                seller_loader=lambda user_id: seller_profile_cache.get_or_load(
                    str(user_id),
                    lambda seller_key: scrape_user_profile(context, seller_key),
                ),
                image_downloader=download_all_images,
                ai_analyzer=get_ai_analysis,
                notifier=send_ntfy_notification,
                saver=save_to_jsonl,
                notification_status_writer=mark_notification_status,
            )

            # 增强反检测脚本：按 context 的 is_mobile / has_touch 生成，和设备参数保持一致
            await context.add_init_script(build_stealth_init_script(context_kwargs))

            page = await context.new_page()
            cancelled = False

            try:
                # 步骤 0 - 模拟真实用户：先访问首页（重要的反检测措施）
                log_time("步骤 0 - 模拟真实用户访问首页...")
                await page.goto(
                    "https://www.goofish.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                log_time("[反爬] 在首页停留，模拟浏览...")
                await random_sleep(1, 2)

                # 模拟随机滚动（移动设备的触摸滚动）
                await page.evaluate("window.scrollBy(0, Math.random() * 500 + 200)")
                await random_sleep(1, 2)

                log_time("步骤 1 - 导航到搜索结果页...")
                # 使用 'q' 参数构建正确的搜索URL，并进行URL编码
                params = {"q": search_keyword}
                search_url = f"https://www.goofish.com/search?{urlencode(params)}"
                log_time(f"目标URL: {search_url}")

                # 先监听搜索接口响应，再执行导航，避免错过首次请求
                async with page.expect_response(
                    is_search_results_response, timeout=30000
                ) as initial_response_info:
                    await page.goto(
                        search_url, wait_until="domcontentloaded", timeout=60000
                    )
                if _is_login_url(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url} (cookies/state likely expired)"
                    )

                # 捕获初始搜索的API数据
                initial_response = await initial_response_info.value

                # 等待页面加载出关键筛选元素，以确认已成功进入搜索结果页
                try:
                    await page.wait_for_selector("text=新发布", timeout=15000)
                except PlaywrightTimeoutError as e:
                    if _is_login_url(page.url):
                        raise LoginRequiredError(
                            f"Login required: redirected to {page.url} (cookies/state likely expired)"
                        ) from e
                    raise

                # 模拟真实用户行为：页面加载后的初始停留和浏览
                log_time("[反爬] 模拟用户查看页面...")
                await random_sleep(1, 3)

                # --- 检查是否存在验证弹窗：baxia 验证框和 middleware 框合并检测，总等待不超过 2 秒 ---
                risk_marker = await _detect_risk_control_overlay(page)
                if risk_marker:
                    _print_risk_control_banner(risk_marker, keyword)
                    raise RiskControlError(risk_marker)

                # 广告弹窗：关闭按钮出现就点，最多等 1.5 秒
                await _close_ad_popup(page)

                final_response = None
                log_time("步骤 2 - 应用筛选条件...")
                if new_publish_option:
                    try:
                        await page.click("text=新发布")
                        await random_sleep(1, 2)  # 原来是 (1.5, 2.5)
                        async with page.expect_response(
                            is_search_results_response, timeout=20000
                        ) as response_info:
                            await page.click(f"text={new_publish_option}")
                            # --- 修改: 增加排序后的等待时间 ---
                            await random_sleep(2, 4)  # 原来是 (3, 5)
                        final_response = await response_info.value
                    except PlaywrightTimeoutError:
                        log_time(
                            f"[警告] 新发布筛选 '{new_publish_option}' 请求超时，该筛选未生效，继续执行。"
                        )
                    except Exception as e:
                        log_time(f"[警告] 应用新发布筛选失败，该筛选未生效: {e}")

                if personal_only:
                    async with page.expect_response(
                        is_search_results_response, timeout=20000
                    ) as response_info:
                        await page.click("text=个人闲置")
                        # --- 修改: 将固定等待改为随机等待，并加长 ---
                        await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                    final_response = await response_info.value

                if free_shipping:
                    try:
                        async with page.expect_response(
                            is_search_results_response, timeout=20000
                        ) as response_info:
                            await page.click("text=包邮")
                            await random_sleep(2, 4)
                        final_response = await response_info.value
                    except PlaywrightTimeoutError:
                        log_time("[警告] 包邮筛选请求超时，该筛选未生效，继续执行。")
                    except Exception as e:
                        log_time(f"[警告] 应用包邮筛选失败，该筛选未生效: {e}")

                if region_filter:
                    try:
                        area_trigger = page.get_by_text("区域", exact=True)
                        if await area_trigger.count():
                            await area_trigger.first.click()
                            await random_sleep(1.5, 2)
                            popover_candidates = page.locator("div.ant-popover")
                            popover = popover_candidates.filter(
                                has=page.locator(
                                    ".areaWrap--FaZHsn8E, [class*='areaWrap']"
                                )
                            ).last
                            if not await popover.count():
                                popover = popover_candidates.filter(
                                    has=page.get_by_text("重新定位")
                                ).last
                            if not await popover.count():
                                popover = popover_candidates.filter(
                                    has=page.get_by_text("查看")
                                ).last
                            if not await popover.count():
                                print("LOG: 未找到区域弹窗，跳过区域筛选。")
                                raise PlaywrightTimeoutError("region-popover-not-found")
                            await popover.wait_for(state="visible", timeout=5000)

                            # 列表容器：第一层 children 即省/市/区三列，不再强依赖具体类名，提升鲁棒性
                            area_wrap = popover.locator(
                                REGION_AREA_WRAP_SELECTOR
                            ).first
                            await area_wrap.wait_for(state="visible", timeout=3000)
                            columns = area_wrap.locator(REGION_COLUMN_SELECTOR)
                            col_prov = columns.nth(0)
                            col_city = columns.nth(1)
                            col_dist = columns.nth(2)

                            region_parts = [
                                p.strip() for p in region_filter.split("/") if p.strip()
                            ]

                            async def _click_in_column(
                                column_locator, text_value: str, desc: str
                            ) -> None:
                                # 闲鱼 CSS Module 的 hash 会变，不能写死 provItem--XXXX。
                                option = column_locator.locator(
                                    REGION_OPTION_SELECTOR
                                ).get_by_text(text_value, exact=True).first
                                if await option.count():
                                    await option.click()
                                    await random_sleep(1.5, 2)
                                    try:
                                        await option.wait_for(
                                            state="attached", timeout=1500
                                        )
                                        await option.wait_for(
                                            state="visible", timeout=1500
                                        )
                                    except PlaywrightTimeoutError:
                                        pass
                                else:
                                    print(f"LOG: 未找到{desc} '{text_value}'，跳过。")

                            if len(region_parts) >= 1:
                                await _click_in_column(
                                    col_prov, region_parts[0], "省份"
                                )
                                await random_sleep(1, 2)
                            if len(region_parts) >= 2:
                                await _click_in_column(
                                    col_city, region_parts[1], "城市"
                                )
                                await random_sleep(1, 2)
                            if len(region_parts) >= 3:
                                await _click_in_column(
                                    col_dist, region_parts[2], "区/县"
                                )
                                await random_sleep(1, 2)

                            search_btn = popover.locator(
                                REGION_SUBMIT_SELECTOR
                            ).filter(has_text=REGION_SUBMIT_TEXT).last
                            if await search_btn.count():
                                try:
                                    async with page.expect_response(
                                        is_search_results_response,
                                        timeout=20000,
                                    ) as response_info:
                                        await search_btn.click()
                                        await random_sleep(2, 3)
                                    final_response = await response_info.value
                                except PlaywrightTimeoutError:
                                    log_time("[警告] 区域筛选提交超时，该筛选未生效，继续执行。")
                            else:
                                print(
                                    "LOG: 未找到区域弹窗的“查看XX件宝贝”按钮，跳过提交。"
                                )
                        else:
                            print("LOG: 未找到区域筛选触发器。")
                    except PlaywrightTimeoutError:
                        log_time(f"[警告] 区域筛选 '{region_filter}' 请求超时，该筛选未生效，继续执行。")
                    except Exception as e:
                        log_time(f"[警告] 应用区域筛选 '{region_filter}' 失败，该筛选未生效: {e}")

                # 站内价格筛选是否真的生效；没生效时下面用 filter_items_by_price 本地兜底
                price_filter_applied = False
                if min_price or max_price:
                    try:
                        price_container = page.locator(
                            'div[class*="search-price-input-container"]'
                        ).first
                        if await price_container.is_visible():
                            if min_price:
                                await price_container.get_by_placeholder("¥").first.fill(
                                    str(min_price)
                                )
                                # --- 修改: 将固定等待改为随机等待 ---
                                await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)
                            if max_price:
                                await (
                                    price_container.get_by_placeholder("¥")
                                    .nth(1)
                                    .fill(str(max_price))
                                )
                                # --- 修改: 将固定等待改为随机等待 ---
                                await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)

                            async with page.expect_response(
                                is_search_results_response, timeout=20000
                            ) as response_info:
                                await page.keyboard.press("Tab")
                                # --- 修改: 增加确认价格后的等待时间 ---
                                await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                            final_response = await response_info.value
                            price_filter_applied = bool(final_response and final_response.ok)
                        else:
                            log_time("[警告] 未找到价格输入容器，站内价格筛选未生效。")
                    except PlaywrightTimeoutError as e:
                        if _is_login_url(page.url):
                            raise LoginRequiredError(
                                f"Login required: redirected to {page.url} (cookies/state likely expired)"
                            ) from e
                        log_time("[警告] 价格筛选请求超时，站内价格筛选未生效。")

                log_time("所有筛选已完成，开始处理商品列表...")

                # 筛选失败不能再静默回退：以前 final_response 为 None 或 !ok 时直接用 initial_response，
                # 处理的是未过滤的全量结果，超出价位的商品照样抓详情、调 AI、落库、推送
                filters_requested = bool(
                    new_publish_option
                    or personal_only
                    or free_shipping
                    or region_filter
                    or min_price
                    or max_price
                )
                if final_response is not None and not final_response.ok:
                    log_time(
                        f"[警告] 筛选后的搜索响应异常 (HTTP {final_response.status})，回退到未筛选的初始结果。"
                    )
                elif filters_requested and final_response is None:
                    log_time("[警告] 所有筛选都未拿到有效响应，回退到未筛选的初始结果。")
                if (min_price or max_price) and not price_filter_applied:
                    log_time(
                        f"[警告] 价格筛选未生效，已改用本地过滤：只处理售价在 "
                        f"{min_price or '不限'} ~ {max_price or '不限'} 内的商品。"
                    )

                current_response = (
                    final_response
                    if final_response and final_response.ok
                    else initial_response
                )
                for page_num in range(1, max_pages + 1):
                    if stop_scraping:
                        break
                    log_time(f"开始处理第 {page_num}/{max_pages} 页 ...")

                    if page_num > 1:
                        page_advance_result = await advance_search_page(
                            page=page,
                            page_num=page_num,
                        )
                        if not page_advance_result.advanced:
                            break
                        current_response = page_advance_result.response

                    if not (current_response and current_response.ok):
                        log_time(f"第 {page_num} 页响应无效，跳过。")
                        continue

                    basic_items = await _parse_search_results_json(
                        await current_response.json(), f"第 {page_num} 页", stats=run_stats
                    )
                    if not basic_items:
                        break
                    # 本地价格过滤：不管站内价格筛选有没有生效都过一遍，
                    # 超出价位的商品不再抓详情、调 AI、落库、推送，也不计入价格统计
                    if min_price or max_price:
                        in_range_items = filter_items_by_price(basic_items, min_price, max_price)
                        dropped_by_price = len(basic_items) - len(in_range_items)
                        run_stats.add("price_filtered", dropped_by_price)
                        if dropped_by_price:
                            log_time(
                                f"第 {page_num} 页 {len(basic_items)} 个商品中有 {dropped_by_price} 个售价不在 "
                                f"{min_price or '不限'} ~ {max_price or '不限'} 区间内，已本地过滤。"
                            )
                        basic_items = in_range_items
                    # 价格快照和市场参考只统计通过标题预筛选的商品，
                    # 否则搜不到结果时闲鱼塞进来的推荐手机会把均价和性价比评分带偏
                    market_items = filter_items_by_title(
                        basic_items, title_include_keywords, title_exclude_keywords
                    )
                    if len(market_items) != len(basic_items):
                        log_time(
                            f"第 {page_num} 页 {len(basic_items)} 个商品中有 {len(market_items)} 个"
                            "通过标题预筛选，只有这些计入价格统计。"
                        )
                    historical_snapshots.extend(
                        record_market_snapshots(
                            keyword=keyword,
                            task_name=task_config.get("task_name", "Untitled Task"),
                            items=market_items,
                            run_id=history_run_id,
                            snapshot_time=datetime.now().isoformat(),
                            seen_item_ids=history_seen_item_ids,
                        )
                    )

                    total_items_on_page = len(basic_items)
                    for i, item_data in enumerate(basic_items, 1):
                        if debug_limit > 0 and processed_item_count >= debug_limit:
                            log_time(
                                f"已达到调试上限 ({debug_limit})，停止获取新商品。"
                            )
                            stop_scraping = True
                            break

                        unique_key = get_link_unique_key(item_data["商品链接"])
                        if unique_key in processed_links:
                            run_stats.add("already_processed")
                            log_time(
                                f"[页内进度 {i}/{total_items_on_page}] 商品 '{item_data['商品标题'][:20]}...' 已存在，跳过。"
                            )
                            continue

                        # 标题预筛选：闲鱼搜索会夹带无关商品，标题不含必需词或含排除词的不再抓详情、不调 AI
                        skip_reason = title_filter_reason(
                            item_data.get("商品标题", ""),
                            title_include_keywords,
                            title_exclude_keywords,
                        )
                        if skip_reason:
                            title_filtered_count += 1
                            run_stats.add("title_filtered")
                            log_time(
                                f"[页内进度 {i}/{total_items_on_page}] 商品 '{item_data['商品标题'][:20]}...' "
                                f"{skip_reason}，跳过。"
                            )
                            continue

                        log_time(
                            f"[页内进度 {i}/{total_items_on_page}] 发现新商品，获取详情: {item_data['商品标题'][:30]}..."
                        )
                        # --- 修改: 访问详情页前的等待时间，模拟用户在列表页上看了一会儿 ---
                        await random_sleep(2, 4)  # 原来是 (2, 4)

                        detail_page = await context.new_page()
                        try:
                            run_stats.add("detail_attempts")
                            async with detail_page.expect_response(
                                lambda r: DETAIL_API_URL_PATTERN in r.url, timeout=25000
                            ) as detail_info:
                                await detail_page.goto(
                                    item_data["商品链接"],
                                    wait_until="domcontentloaded",
                                    timeout=25000,
                                )

                            detail_response = await detail_info.value
                            if detail_response.ok:
                                detail_json = await detail_response.json()

                                ret_string = str(
                                    await safe_get(detail_json, "ret", default=[])
                                )
                                if "FAIL_SYS_USER_VALIDATE" in ret_string:
                                    print(
                                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                                    )
                                    print(
                                        "检测到闲鱼反爬虫验证 (FAIL_SYS_USER_VALIDATE)，程序将终止。"
                                    )
                                    long_sleep_duration = random.randint(3, 60)
                                    print(
                                        f"为避免账户风险，将执行一次长时间休眠 ({long_sleep_duration} 秒) 后再退出..."
                                    )
                                    await asyncio.sleep(long_sleep_duration)
                                    print("长时间休眠结束，现在将安全退出。")
                                    print(
                                        "==================================================================="
                                    )
                                    raise RiskControlError("FAIL_SYS_USER_VALIDATE")

                                # 解析商品详情数据并更新 item_data
                                item_do = await safe_get(
                                    detail_json, "data", "itemDO", default={}
                                )
                                seller_do = await safe_get(
                                    detail_json, "data", "sellerDO", default={}
                                )
                                ret_status = classify_detail_ret(
                                    await safe_get(detail_json, "ret", default=[])
                                )
                                if ret_status == "login_expired":
                                    raise LoginRequiredError(
                                        f"详情接口返回登录态失效: {ret_string}"
                                    )
                                if ret_status != "ok" or not item_do:
                                    raise ItemDetailUnavailable(
                                        f"详情接口未返回商品数据 (ret={ret_string or '空'})，本次不记录，下次运行重新抓取"
                                    )

                                reg_days_raw = await safe_get(
                                    seller_do, "userRegDay", default=0
                                )
                                registration_duration_text = format_registration_days(
                                    reg_days_raw
                                )

                                # --- START: 新增代码块 ---

                                # 1. 提取卖家的芝麻信用信息
                                zhima_credit_text = await safe_get(
                                    seller_do, "zhimaLevelInfo", "levelName"
                                )

                                # 2. 提取该商品的完整图片列表
                                image_infos = await safe_get(
                                    item_do, "imageInfos", default=[]
                                )
                                if image_infos:
                                    # 使用列表推导式获取所有有效的图片URL
                                    all_image_urls = [
                                        img.get("url")
                                        for img in image_infos
                                        if img.get("url")
                                    ]
                                    if all_image_urls:
                                        # 用新的字段存储图片列表，替换掉旧的单个链接
                                        item_data["商品图片列表"] = all_image_urls
                                        # (可选) 仍然保留主图链接，以防万一
                                        item_data["商品主图链接"] = all_image_urls[0]

                                # --- END: 新增代码块 ---

                                # 3. 提取商品描述正文和成色/品牌等属性，供 AI 判断电池、维修史等
                                item_data.update(parse_item_detail_extras(item_do))
                                # 4. 收藏数、卖家城市、最近活跃、回复率：商品页上有、搜索结果里没有
                                item_data.update(parse_item_detail_facts(item_do, seller_do))

                                item_data["“想要”人数"] = await safe_get(
                                    item_do,
                                    "wantCnt",
                                    default=item_data.get("“想要”人数", "NaN"),
                                )
                                item_data["浏览量"] = await safe_get(
                                    item_do, "browseCnt", default="-"
                                )
                                # ...[此处可添加更多从详情页解析出的商品信息]...

                                user_id = await safe_get(seller_do, "sellerId")

                                # 构建基础记录
                                final_record = {
                                    "爬取时间": datetime.now().isoformat(),
                                    "搜索关键字": keyword,
                                    "实际搜索词": search_keyword,
                                    "任务名称": task_config.get(
                                        "task_name", "Untitled Task"
                                    ),
                                    "轮次": run_stats.run_no,
                                    "商品信息": item_data,
                                    "卖家信息": {},
                                }
                                price_reference = build_market_reference(
                                    keyword=keyword,
                                    item=item_data,
                                    current_market_items=market_items,
                                    historical_snapshots=historical_snapshots,
                                    sold_listings=sold_listings,
                                )
                                final_record["价格参考"] = price_reference
                                final_record["price_insight"] = price_reference.get(
                                    "本商品价格位置", {}
                                )

                                analysis_dispatcher.submit(
                                    ItemAnalysisJob(
                                        keyword=keyword,
                                        task_name=task_config.get(
                                            "task_name", "Untitled Task"
                                        ),
                                        decision_mode=decision_mode,
                                        analyze_images=analyze_images,
                                        prompt_text=ai_prompt_text,
                                        keyword_rules=tuple(keyword_rules or []),
                                        attribute_rules=tuple(attribute_rules),
                                        final_record=final_record,
                                        seller_id=str(user_id) if user_id else None,
                                        zhima_credit_text=zhima_credit_text,
                                        registration_duration_text=registration_duration_text,
                                        seller_detail=parse_seller_detail(seller_do),
                                    )
                                )

                                processed_links.add(unique_key)
                                run_stats.add("items_submitted")
                                processed_item_count += 1
                                log_time(
                                    f"商品已提交后台分析。累计处理 {processed_item_count} 个新商品。"
                                )

                                # --- 修改: 增加单个商品处理后的主要延迟 ---
                                log_time(
                                    "[反爬] 执行一次主要的随机延迟以模拟用户浏览间隔..."
                                )
                                await random_sleep(5, 10)
                            else:
                                print(
                                    f"   错误: 获取商品详情API响应失败，状态码: {detail_response.status}"
                                )
                                if AI_DEBUG_MODE:
                                    print(
                                        f"--- [DETAIL DEBUG] FAILED RESPONSE from {item_data['商品链接']} ---"
                                    )
                                    try:
                                        print(await detail_response.text())
                                    except Exception as e:
                                        print(f"无法读取响应内容: {e}")
                                    print(
                                        "----------------------------------------------------"
                                    )

                        except PlaywrightTimeoutError:
                            run_stats.add("detail_timeouts")
                            print(f"   错误: 访问商品详情页或等待API响应超时。")
                        except ItemDetailUnavailable as e:
                            run_stats.add("detail_unavailable")
                            print(f"   跳过: {e}")
                        except (RiskControlError, LoginRequiredError):
                            # 风控 / 登录失效必须终止整个任务，不能被下面的兜底 except 吞掉继续下一个商品
                            raise
                        except Exception as e:
                            run_stats.add("detail_errors")
                            print(f"   错误: 处理商品详情时发生未知错误: {e}")
                        finally:
                            await detail_page.close()
                            # --- 修改: 增加关闭页面后的短暂整理时间 ---
                            await random_sleep(2, 4)  # 原来是 (1, 2.5)

                    # --- 新增: 在处理完一页所有商品后，翻页前，增加一个更长的“休息”时间 ---
                    if not stop_scraping and page_num < max_pages:
                        print(
                            f"--- 第 {page_num} 页处理完毕，准备翻页。执行一次页面间的长时休息... ---"
                        )
                        await random_sleep(10, 15)

            except PlaywrightTimeoutError as e:
                if _is_login_url(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url} (cookies/state likely expired)"
                    ) from e
                print(f"\n操作超时错误: 页面元素或网络响应未在规定时间内出现。\n{e}")
                raise
            except asyncio.CancelledError:
                cancelled = True
                log_time("收到取消信号，正在终止当前爬虫任务...")
                raise
            except (RiskControlError, LoginRequiredError):
                raise
            except Exception as e:
                if type(e).__name__ == "TargetClosedError":
                    log_time("浏览器已关闭，忽略后续异常（可能是任务被停止）。")
                    return processed_item_count
                if "passport.goofish.com" in str(e):
                    raise LoginRequiredError(
                        f"Login required: redirected to passport flow ({e})"
                    ) from e
                print(f"\n爬取过程中发生未知错误: {e}")
                raise
            finally:
                try:
                    if analysis_dispatcher is not None:
                        if cancelled:
                            # 被取消时不再等全部 AI 分析跑完，直接取消在途任务
                            await _cancel_analysis_dispatcher(analysis_dispatcher)
                        else:
                            log_time("等待后台分析任务完成...")
                            await _join_analysis_dispatcher(
                                analysis_dispatcher,
                                max_rounds=processed_item_count + 1,
                            )
                except asyncio.CancelledError:
                    # 等待期间才收到取消：取消在途分析后继续往下关浏览器，取消信号随后继续上抛
                    if analysis_dispatcher is not None:
                        await _cancel_analysis_dispatcher(analysis_dispatcher)
                    raise
                finally:
                    if title_filtered_count:
                        log_time(f"标题预筛选跳过了 {title_filtered_count} 个商品。")
                    if debug_limit:
                        log_time("调试模式：任务执行完毕，浏览器将在5秒后自动关闭...")
                        await asyncio.sleep(5)
                        if sys.stdin.isatty():
                            input("按回车键关闭浏览器...")
                    else:
                        log_time("任务执行完毕，正在关闭浏览器...")
                    try:
                        await browser.close()
                    except Exception as exc:
                        log_time(f"关闭浏览器时出错（忽略）: {exc}")

        return processed_item_count

    processed_item_count = 0
    attempt_limit = max(
        rotation_settings["account_retry_limit"],
        rotation_settings["proxy_retry_limit"],
        1,
    )
    last_error = ""
    last_state_path: Optional[str] = None

    # If this task is already in a paused state, skip immediately.
    task_name_for_guard = task_config.get("task_name", "未命名任务")
    pause_cookie_path = None
    if (
        isinstance(task_config.get("account_state_file"), str)
        and task_config.get("account_state_file").strip()
    ):
        pause_cookie_path = task_config.get("account_state_file").strip()
    elif os.path.exists(STATE_FILE):
        pause_cookie_path = STATE_FILE

    decision = FAILURE_GUARD.should_skip_start(
        task_name_for_guard, cookie_path=pause_cookie_path
    )
    if decision.skip:
        print(
            f"[FailureGuard] 任务 '{task_name_for_guard}' 已暂停重试 (连续失败 {decision.consecutive_failures}/{FAILURE_GUARD.threshold})"
        )
        if decision.should_notify:
            try:
                await send_ntfy_notification(
                    {
                        "商品标题": f"[任务暂停] {task_name_for_guard}",
                        "当前售价": "N/A",
                        "商品链接": "#",
                    },
                    "任务处于暂停状态，将跳过执行。\n"
                    f"原因: {decision.reason}\n"
                    f"连续失败: {decision.consecutive_failures}/{FAILURE_GUARD.threshold}\n"
                    f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}\n"
                    "修复方法: 更新登录态/cookies文件后会自动恢复。",
                )
            except Exception as e:
                print(f"发送任务暂停通知失败: {e}")

        cleanup_task_images(task_config.get("task_name", "default"))
        return 0

    for attempt in range(1, attempt_limit + 1):
        if attempt == 1:
            selected_account = _select_account()
            selected_proxy = _select_proxy()
        else:
            if (
                rotation_settings["account_enabled"]
                and rotation_settings["account_mode"] == "on_failure"
            ):
                account_pool.mark_bad(selected_account, last_error)
                selected_account = _select_account(force_new=True)
            if (
                rotation_settings["proxy_enabled"]
                and rotation_settings["proxy_mode"] == "on_failure"
            ):
                proxy_pool.mark_bad(selected_proxy, last_error)
                selected_proxy = _select_proxy(force_new=True)

        if rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if not rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if rotation_settings["proxy_enabled"] and not selected_proxy:
            last_error = "未找到可用的代理地址，无法继续执行任务。"
            print(last_error)
            break

        state_path = selected_account.value if selected_account else STATE_FILE
        last_state_path = state_path
        proxy_server = selected_proxy.value if selected_proxy else None
        if rotation_settings["account_enabled"]:
            print(f"账号轮换：使用登录状态 {state_path}")
        if rotation_settings["proxy_enabled"] and proxy_server:
            print(f"IP 轮换：使用代理 {proxy_server}")

        try:
            for keyword_index, search_keyword in enumerate(search_keywords, 1):
                if len(search_keywords) > 1:
                    print(
                        f"\n===== 搜索词 {keyword_index}/{len(search_keywords)}: {search_keyword} ====="
                    )
                processed_item_count += await _run_scrape_attempt(
                    state_path, proxy_server, search_keyword
                )
            last_error = ""
            FAILURE_GUARD.record_success(task_name_for_guard)
            break
        except LoginRequiredError as e:
            last_error = str(e)
            run_stats.add("login_expired_events")
            print(f"检测到登录失效/重定向: {e}")
            break
        except RiskControlError as e:
            last_error = str(e)
            run_stats.add("risk_control_events")
            print(f"检测到风控或验证触发: {e}")
            # 风控验证通常不是简单轮换能解决的，避免无意义重试。
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"本次尝试失败: {last_error}")
            if attempt < attempt_limit:
                print("将尝试轮换账号/IP 后重试...")

    assessment = _record_run_summary(run_stats, last_error)

    if last_error:
        await _notify_task_failure(task_config, last_error, cookie_path=last_state_path, assessment=assessment)
    elif assessment and assessment.get("level") != LEVEL_OK:
        # 任务"成功"但健康评估不正常（解析失败率高、0 结果）：这是接口变更最常见的样子，单独提醒
        await _notify_health_alert(task_config, assessment)

    # 清理任务图片目录
    cleanup_task_images(task_config.get("task_name", "default"))

    return processed_item_count
