import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.utils import log_time, random_sleep

NEXT_PAGE_SELECTOR = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
SEARCH_RESULTS_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
# 点击"下一页"后首轮等待搜索接口响应的时间
PAGE_REQUEST_TIMEOUT_MS = 20_000
# 首轮超时后不重复点击、只继续等待的时间（接口慢时第一次点击其实已经生效）
PAGE_REQUEST_EXTENDED_TIMEOUT_MS = 40_000
# 重新点击一次之后再等多久
PAGE_RECLICK_REQUEST_TIMEOUT_MS = 20_000
PAGE_CLICK_TIMEOUT_MS = 10_000
# 一页最多点几次"下一页"：首次 + 最多一次补点
PAGE_MAX_CLICKS = 2
PAGE_CLICK_SLEEP_MIN_SECONDS = 2
PAGE_CLICK_SLEEP_MAX_SECONDS = 5
# 给 Playwright 自己的等待超时留出的余量，保证分段等待先于它触发
_WAITER_TIMEOUT_MARGIN_MS = 5_000

# 分页组件上"当前页"的候选选择器：闲鱼的 class 带 hash，用 contains 匹配；最后兜底 ARIA / antd
ACTIVE_PAGE_SELECTORS = (
    "[class*='search-pagination'] [class*='active']",
    "[class*='search-pagination'] [aria-current='page']",
    ".ant-pagination-item-active",
)


@dataclass(frozen=True)
class PageAdvanceResult:
    advanced: bool
    response: Optional[Any] = None
    stop_reason: Optional[str] = None


def is_search_results_response(
    response: Any,
    api_url_fragment: str = SEARCH_RESULTS_API_FRAGMENT,
) -> bool:
    request = getattr(response, "request", None)
    request_method = getattr(request, "method", None)
    response_url = getattr(response, "url", "")
    return api_url_fragment in response_url and request_method == "POST"


async def read_active_page_number(page: Any) -> Optional[int]:
    """尽力从分页组件读出当前高亮的页码；读不到就返回 None，调用方按保守策略处理。"""
    for selector in ACTIVE_PAGE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            text = await locator.inner_text(timeout=1_000)
        except Exception:
            continue
        match = re.search(r"\d+", text or "")
        if match:
            return int(match.group())
    return None


def _start_response_waiter(page: Any, *, timeout_ms: float) -> "asyncio.Task":
    """提前挂一个搜索接口响应的监听，覆盖"点击 -> 首轮等待 -> 延长等待"整个窗口。

    之前每次重试都重新 ``expect_response`` 并再点一次按钮：首次点击其实成功、只是接口慢，
    第二次点击就把页面推到 N+2，中间一页的商品静默丢失。监听只挂一次就没有这个问题。
    """
    return asyncio.ensure_future(
        page.wait_for_event(
            "response",
            predicate=is_search_results_response,
            timeout=timeout_ms,
        )
    )


def _discard_waiter(waiter: "asyncio.Task") -> None:
    if not waiter.done():
        waiter.cancel()
        return
    # 已结束但没人取过结果的异常会在回收时打 "exception was never retrieved"
    if not waiter.cancelled():
        waiter.exception()


async def _wait_for_response_in_stages(
    waiter: "asyncio.Task",
    stage_timeouts_ms: list,
    *,
    page_num: int,
    logger: Callable[[str], None],
) -> Optional[Any]:
    """按阶段等待同一个监听：阶段之间不做任何页面操作，只记日志。"""
    for stage_index, stage_ms in enumerate(stage_timeouts_ms):
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=stage_ms / 1000)
        except asyncio.TimeoutError:
            remaining = stage_timeouts_ms[stage_index + 1 :]
            if remaining:
                logger(
                    f"点击'下一页'后 {stage_ms / 1000:.0f} 秒内未收到第 {page_num} 页搜索响应，"
                    f"不重复点击，再等待 {sum(remaining) / 1000:.0f} 秒..."
                )
        except PlaywrightTimeoutError:
            # Playwright 自己的总超时先到了（正常不会发生），等同于本阶段超时
            return None
    return None


async def advance_search_page(
    *,
    page: Any,
    page_num: int,
    logger: Callable[[str], None] = log_time,
    wait_after_click: Callable[[float, float], Awaitable[None]] = random_sleep,
    read_active_page: Callable[[Any], Awaitable[Optional[int]]] = read_active_page_number,
    request_timeout_ms: float = PAGE_REQUEST_TIMEOUT_MS,
    extended_timeout_ms: float = PAGE_REQUEST_EXTENDED_TIMEOUT_MS,
    reclick_timeout_ms: float = PAGE_RECLICK_REQUEST_TIMEOUT_MS,
    max_clicks: int = PAGE_MAX_CLICKS,
) -> PageAdvanceResult:
    next_button = page.locator(NEXT_PAGE_SELECTOR).first
    if not await next_button.count():
        logger("已到达最后一页，未找到可用的'下一页'按钮，停止翻页。")
        return PageAdvanceResult(advanced=False, stop_reason="no_next_button")

    clicks = 0
    while True:
        clicks += 1
        # 首次点击：首轮 + 延长等待；补点之后：只等一轮
        stage_timeouts_ms = (
            [request_timeout_ms, extended_timeout_ms]
            if clicks == 1
            else [reclick_timeout_ms]
        )
        waiter = _start_response_waiter(
            page, timeout_ms=sum(stage_timeouts_ms) + _WAITER_TIMEOUT_MARGIN_MS
        )
        # 让监听任务先跑到注册监听那一步，再去点击
        await asyncio.sleep(0)

        try:
            await next_button.scroll_into_view_if_needed()
            await next_button.click(timeout=PAGE_CLICK_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            _discard_waiter(waiter)
            logger(f"第 {page_num} 页下一页按钮点击超时，停止翻页。")
            return PageAdvanceResult(advanced=False, stop_reason="click_timeout")
        except BaseException:
            _discard_waiter(waiter)
            raise

        try:
            response = await _wait_for_response_in_stages(
                waiter, stage_timeouts_ms, page_num=page_num, logger=logger
            )
        except asyncio.CancelledError:
            _discard_waiter(waiter)
            raise
        except Exception as exc:
            # 页面被关闭 / 崩溃等：监听被 Playwright 拒绝，翻页没法继续
            _discard_waiter(waiter)
            logger(f"等待第 {page_num} 页搜索响应时出错，停止翻页: {exc}")
            return PageAdvanceResult(advanced=False, stop_reason="response_error")

        if response is not None:
            await wait_after_click(
                PAGE_CLICK_SLEEP_MIN_SECONDS,
                PAGE_CLICK_SLEEP_MAX_SECONDS,
            )
            return PageAdvanceResult(advanced=True, response=response)

        _discard_waiter(waiter)

        if clicks >= max_clicks:
            logger(
                f"等待第 {page_num} 页搜索响应超时（已点击'下一页' {clicks} 次），停止翻页。"
            )
            return PageAdvanceResult(advanced=False, stop_reason="response_timeout")

        # 决定是否补点一次：先核对分页组件上的页码，已经在目标页就绝不能再点（会跳页）
        active_page = await read_active_page(page)
        if active_page is not None and active_page >= page_num:
            logger(
                f"分页组件显示已在第 {active_page} 页，但未捕获到搜索响应；"
                "为避免跳页不再重复点击，停止翻页。"
            )
            return PageAdvanceResult(advanced=False, stop_reason="response_missed")
        if active_page is None:
            logger(
                f"无法读取当前页码，按保守策略只补点一次'下一页'（第 {page_num} 页）..."
            )
        else:
            logger(
                f"分页组件仍显示第 {active_page} 页，首次点击可能未生效，补点一次'下一页'..."
            )
        if not await next_button.count():
            logger("'下一页'按钮已不可用，停止翻页。")
            return PageAdvanceResult(advanced=False, stop_reason="no_next_button")
