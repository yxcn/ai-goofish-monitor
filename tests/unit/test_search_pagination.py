import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.services.search_pagination import (
    advance_search_page,
    is_search_results_response,
    read_active_page_number,
)

# 测试用的分段超时（毫秒）：首轮 / 不重点击继续等 / 补点后再等
FAST_TIMEOUTS = dict(request_timeout_ms=50, extended_timeout_ms=200, reclick_timeout_ms=50)


class FakeRequest:
    def __init__(self, method: str = "POST"):
        self.method = method


class FakeResponse:
    def __init__(self, url: str, ok: bool = True, method: str = "POST"):
        self.url = url
        self.ok = ok
        self.request = FakeRequest(method)


class FakeLocator:
    def __init__(self, count: int, click_error: Exception | None = None):
        self._count = count
        self.clicks = 0
        self.scrolls = 0
        self.click_timeout = None
        self._click_error = click_error

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolls += 1

    async def click(self, timeout: int | None = None) -> None:
        self.clicks += 1
        self.click_timeout = timeout
        if self._click_error is not None:
            raise self._click_error


class FakePage:
    """每次点击"下一页"对应 outcomes 里的一项：

    - FakeResponse            → 搜索响应立刻到达
    - ("after", 秒, FakeResponse) → 延迟一段时间后到达（模拟接口慢）
    - "never"                 → 永不到达（等着被取消）
    - Exception               → 监听被拒绝（页面关闭等）
    """

    def __init__(
        self,
        next_button_count: int,
        outcomes: list[object],
        click_error: Exception | None = None,
    ):
        self.locator_stub = FakeLocator(next_button_count, click_error=click_error)
        self._outcomes = list(outcomes)
        self.waiters_started = 0
        self.waiter_timeouts: list[float] = []
        self.cancelled_waiters = 0

    def locator(self, _selector: str) -> FakeLocator:
        return self.locator_stub

    async def wait_for_event(self, event: str, predicate=None, timeout: float | None = None):
        assert event == "response"
        assert predicate is is_search_results_response
        self.waiters_started += 1
        self.waiter_timeouts.append(timeout)
        if not self._outcomes:
            raise AssertionError("missing fake response outcome")
        outcome = self._outcomes.pop(0)
        try:
            if outcome == "never":
                await asyncio.Event().wait()
            if isinstance(outcome, tuple):
                _, delay, response = outcome
                await asyncio.sleep(delay)
                return response
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        except asyncio.CancelledError:
            self.cancelled_waiters += 1
            raise


async def _noop_random_sleep(_min_seconds: float, _max_seconds: float) -> None:
    return None


def _active_page_reader(value):
    async def _read(_page):
        return value

    return _read


def _run(page, *, page_num=2, logs=None, active_page="unset", **overrides):
    kwargs = dict(FAST_TIMEOUTS)
    kwargs.update(overrides)
    if active_page != "unset":
        kwargs["read_active_page"] = _active_page_reader(active_page)
    return asyncio.run(
        advance_search_page(
            page=page,
            page_num=page_num,
            logger=(logs.append if logs is not None else (lambda _m: None)),
            wait_after_click=_noop_random_sleep,
            **kwargs,
        )
    )


def _search_response(page_num: int) -> FakeResponse:
    return FakeResponse(
        url=f"https://example.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?page={page_num}"
    )


def test_advance_search_page_stops_when_no_next_button() -> None:
    page = FakePage(next_button_count=0, outcomes=[])
    logs: list[str] = []

    result = _run(page, logs=logs)

    assert result.advanced is False
    assert result.response is None
    assert result.stop_reason == "no_next_button"
    assert page.locator_stub.clicks == 0
    assert page.waiters_started == 0
    assert logs == ["已到达最后一页，未找到可用的'下一页'按钮，停止翻页。"]


def test_advance_search_page_returns_new_response_on_success() -> None:
    response = _search_response(2)
    page = FakePage(next_button_count=1, outcomes=[response])
    after_click_calls: list[tuple] = []

    async def record_sleep(min_s, max_s):
        after_click_calls.append((min_s, max_s))

    result = asyncio.run(
        advance_search_page(
            page=page,
            page_num=2,
            logger=lambda _message: None,
            wait_after_click=record_sleep,
            **FAST_TIMEOUTS,
        )
    )

    assert result.advanced is True
    assert result.response is response
    assert result.stop_reason is None
    assert page.locator_stub.clicks == 1
    assert page.locator_stub.scrolls == 1
    assert page.locator_stub.click_timeout == 10000
    # 监听只挂一次，而且覆盖"首轮 + 延长等待"整个窗口（再加余量）
    assert page.waiters_started == 1
    assert page.waiter_timeouts[0] > 50 + 200
    assert after_click_calls == [(2, 5)]


def test_slow_response_is_awaited_without_clicking_again() -> None:
    # 首轮 50ms 超时，接口 120ms 才回：旧实现这里会再点一次"下一页"跳到 N+2
    response = _search_response(2)
    page = FakePage(next_button_count=1, outcomes=[("after", 0.12, response)])
    logs: list[str] = []

    result = _run(page, logs=logs, extended_timeout_ms=1000)

    assert result.advanced is True
    assert result.response is response
    assert page.locator_stub.clicks == 1
    assert page.waiters_started == 1
    assert len(logs) == 1
    assert "不重复点击" in logs[0]
    assert "第 2 页" in logs[0]


def test_reclicks_once_when_pagination_still_shows_previous_page() -> None:
    response = _search_response(2)
    page = FakePage(next_button_count=1, outcomes=["never", response])
    logs: list[str] = []

    result = _run(page, logs=logs, active_page=1)

    assert result.advanced is True
    assert result.response is response
    assert page.locator_stub.clicks == 2
    assert page.locator_stub.scrolls == 2
    assert page.waiters_started == 2
    assert page.cancelled_waiters == 1  # 第一次的监听被收掉，不会泄漏
    assert any("仍显示第 1 页" in line and "补点一次" in line for line in logs)


def test_does_not_reclick_when_pagination_already_shows_target_page() -> None:
    page = FakePage(next_button_count=1, outcomes=["never"])
    logs: list[str] = []

    result = _run(page, logs=logs, active_page=2)

    assert result.advanced is False
    assert result.stop_reason == "response_missed"
    assert page.locator_stub.clicks == 1
    assert page.waiters_started == 1
    assert page.cancelled_waiters == 1
    assert any("已在第 2 页" in line and "不再重复点击" in line for line in logs)


def test_unknown_page_number_reclicks_only_once_then_stops() -> None:
    page = FakePage(next_button_count=1, outcomes=["never", "never"])
    logs: list[str] = []

    result = _run(page, logs=logs, active_page=None)

    assert result.advanced is False
    assert result.response is None
    assert result.stop_reason == "response_timeout"
    assert page.locator_stub.clicks == 2
    assert page.waiters_started == 2
    assert page.cancelled_waiters == 2
    assert any("无法读取当前页码" in line and "只补点一次" in line for line in logs)
    assert logs[-1] == "等待第 2 页搜索响应超时（已点击'下一页' 2 次），停止翻页。"


def test_advance_search_page_stops_when_click_times_out() -> None:
    page = FakePage(
        next_button_count=1,
        outcomes=["never"],
        click_error=PlaywrightTimeoutError("click timeout"),
    )
    logs: list[str] = []

    result = _run(page, logs=logs)

    assert result.advanced is False
    assert result.response is None
    assert result.stop_reason == "click_timeout"
    assert page.locator_stub.clicks == 1
    assert page.cancelled_waiters == 1
    assert logs == ["第 2 页下一页按钮点击超时，停止翻页。"]


def test_rejected_waiter_stops_pagination_instead_of_raising() -> None:
    page = FakePage(next_button_count=1, outcomes=[RuntimeError("Page closed")])
    logs: list[str] = []

    result = _run(page, logs=logs)

    assert result.advanced is False
    assert result.stop_reason == "response_error"
    assert page.locator_stub.clicks == 1
    assert logs == ["等待第 2 页搜索响应时出错，停止翻页: Page closed"]


class _FakeActiveLocator:
    def __init__(self, count: int, text: str | None = None, error: Exception | None = None):
        self._count = count
        self._text = text
        self._error = error

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def inner_text(self, timeout=None) -> str:
        if self._error is not None:
            raise self._error
        return self._text or ""


class _FakeActivePage:
    def __init__(self, locators: dict):
        self._locators = locators

    def locator(self, selector: str):
        return self._locators.get(selector, _FakeActiveLocator(0))


def test_read_active_page_number_falls_back_across_selectors() -> None:
    page = _FakeActivePage(
        {
            "[class*='search-pagination'] [class*='active']": _FakeActiveLocator(0),
            "[class*='search-pagination'] [aria-current='page']": _FakeActiveLocator(1, " 3 "),
        }
    )

    assert asyncio.run(read_active_page_number(page)) == 3


def test_read_active_page_number_returns_none_when_unreadable() -> None:
    assert asyncio.run(read_active_page_number(_FakeActivePage({}))) is None

    broken = _FakeActivePage(
        {
            "[class*='search-pagination'] [class*='active']": _FakeActiveLocator(
                1, error=RuntimeError("detached")
            ),
            ".ant-pagination-item-active": _FakeActiveLocator(1, "下一页"),
        }
    )
    assert asyncio.run(read_active_page_number(broken)) is None


def test_is_search_results_response_matches_exact_search_api() -> None:
    response = FakeResponse(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?foo=bar",
        method="POST",
    )

    assert is_search_results_response(response) is True


def test_is_search_results_response_rejects_search_shade_api() -> None:
    response = FakeResponse(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search.shade/1.0/?foo=bar",
        method="POST",
    )

    assert is_search_results_response(response) is False


def test_is_search_results_response_rejects_non_post_request() -> None:
    response = FakeResponse(
        url="https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?foo=bar",
        method="GET",
    )

    assert is_search_results_response(response) is False
