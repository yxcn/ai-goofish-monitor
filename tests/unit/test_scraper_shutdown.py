"""scrape_xianyu 收尾阶段与弹窗检测的辅助函数。

- 后台分析任务抛异常不能跳过 browser.close()；任务被取消时取消在途分析而不是等全部完成。
- 风控弹窗合并检测一次（总等待 2 秒）；广告关闭按钮出现就点，最多等 1.5 秒。
"""

import asyncio

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.scraper import (
    _cancel_analysis_dispatcher,
    _close_ad_popup,
    _detect_risk_control_overlay,
    _join_analysis_dispatcher,
)
from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher, ItemAnalysisJob


class FlakyDispatcher:
    def __init__(self, failures: list):
        self._failures = list(failures)
        self.join_calls = 0

    async def join(self) -> None:
        self.join_calls += 1
        if self._failures:
            raise self._failures.pop(0)


def test_join_logs_task_exceptions_and_keeps_waiting():
    dispatcher = FlakyDispatcher([RuntimeError("boom"), ValueError("bad")])

    errors = asyncio.run(_join_analysis_dispatcher(dispatcher, max_rounds=5))

    assert [type(err) for err in errors] == [RuntimeError, ValueError]
    assert dispatcher.join_calls == 3


def test_join_stops_after_max_rounds():
    dispatcher = FlakyDispatcher([RuntimeError(str(i)) for i in range(10)])

    errors = asyncio.run(_join_analysis_dispatcher(dispatcher, max_rounds=3))

    assert len(errors) == 3
    assert dispatcher.join_calls == 3


def test_join_propagates_cancellation():
    class CancellingDispatcher:
        async def join(self) -> None:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_join_analysis_dispatcher(CancellingDispatcher(), max_rounds=3))


def _job(index: int) -> ItemAnalysisJob:
    return ItemAnalysisJob(
        keyword="demo",
        task_name="Demo",
        decision_mode="ai",
        analyze_images=False,
        prompt_text="prompt",
        keyword_rules=(),
        final_record={"商品信息": {"商品ID": str(index), "商品图片列表": []}, "卖家信息": {}},
        seller_id=None,
        zhima_credit_text=None,
        registration_duration_text="来闲鱼1年",
    )


def _blocking_dispatcher(gate: asyncio.Event, saved: list) -> ItemAnalysisDispatcher:
    async def seller_loader(_user_id: str):
        return {}

    async def image_downloader(*_args):
        return []

    async def ai_analyzer(*_args):
        return {"is_recommended": False, "reason": "x"}

    async def notifier(*_args):
        return {}

    async def saver(record: dict, _keyword: str):
        await gate.wait()
        saved.append(record)
        return True

    return ItemAnalysisDispatcher(
        concurrency=2,
        skip_ai_analysis=True,
        seller_loader=seller_loader,
        image_downloader=image_downloader,
        ai_analyzer=ai_analyzer,
        notifier=notifier,
        saver=saver,
    )


def test_cancel_all_cancels_in_flight_jobs_and_join_returns_immediately():
    saved: list = []

    async def run():
        gate = asyncio.Event()  # 永不放开：模拟卡在 AI / 落库上的任务
        dispatcher = _blocking_dispatcher(gate, saved)
        for index in range(3):
            dispatcher.submit(_job(index))
        await asyncio.sleep(0.01)

        cancelled = await _cancel_analysis_dispatcher(dispatcher)
        await asyncio.wait_for(dispatcher.join(), timeout=1)
        return cancelled, dispatcher

    cancelled, dispatcher = asyncio.run(run())

    assert cancelled == 3
    assert dispatcher.completed_count == 0
    assert saved == []
    # 再次调用没有在途任务，返回 0
    assert asyncio.run(dispatcher.cancel_all()) == 0


def test_cancel_helper_swallows_dispatcher_errors():
    class BrokenDispatcher:
        async def cancel_all(self) -> int:
            raise RuntimeError("no loop")

    assert asyncio.run(_cancel_analysis_dispatcher(BrokenDispatcher())) == 0


class FakeOverlayLocator:
    def __init__(self, appears: bool, log: list, name: str = ""):
        self._appears = appears
        self._log = log
        self._name = name
        self.wait_timeouts: list = []

    def or_(self, other: "FakeOverlayLocator") -> "FakeOverlayLocator":
        combined = FakeOverlayLocator(self._appears or other._appears, self._log, "or")
        self._log.append(("or_", self._name, other._name))
        return combined

    def filter(self, visible=None) -> "FakeOverlayLocator":
        self._log.append(("filter", visible))
        return self

    @property
    def first(self) -> "FakeOverlayLocator":
        return self

    async def wait_for(self, state: str, timeout: int) -> None:
        self._log.append(("wait_for", self._name, state, timeout))
        if not self._appears:
            raise PlaywrightTimeoutError("not visible")

    async def is_visible(self) -> bool:
        return self._appears

    async def click(self, timeout: int | None = None) -> None:
        self._log.append(("click", self._name, timeout))


class FakeOverlayPage:
    def __init__(self, visible_selectors: set):
        self.log: list = []
        self._visible = visible_selectors

    def locator(self, selector: str) -> FakeOverlayLocator:
        return FakeOverlayLocator(selector in self._visible, self.log, selector)


def _wait_calls(page: FakeOverlayPage) -> list:
    return [entry for entry in page.log if entry[0] == "wait_for"]


def test_risk_overlay_check_waits_once_for_both_dialogs():
    page = FakeOverlayPage(visible_selectors=set())

    marker = asyncio.run(_detect_risk_control_overlay(page))

    assert marker is None
    # 两个弹窗合并成一个 locator，只等一次、2 秒
    assert len(_wait_calls(page)) == 1
    assert _wait_calls(page)[0][3] == 2000
    assert ("or_", "div.baxia-dialog-mask", "div.J_MIDDLEWARE_FRAME_WIDGET") in page.log
    assert ("filter", True) in page.log


def test_risk_overlay_check_identifies_which_dialog_appeared():
    baxia = FakeOverlayPage(visible_selectors={"div.baxia-dialog-mask"})
    assert asyncio.run(_detect_risk_control_overlay(baxia)) == "baxia-dialog"

    middleware = FakeOverlayPage(visible_selectors={"div.J_MIDDLEWARE_FRAME_WIDGET"})
    assert asyncio.run(_detect_risk_control_overlay(middleware)) == "J_MIDDLEWARE_FRAME_WIDGET"


def test_ad_popup_is_skipped_quickly_when_absent():
    page = FakeOverlayPage(visible_selectors=set())

    closed = asyncio.run(_close_ad_popup(page))

    assert closed is False
    assert _wait_calls(page) == [("wait_for", "div[class*='closeIconBg']", "visible", 1500)]
    assert not [entry for entry in page.log if entry[0] == "click"]


def test_ad_popup_is_clicked_when_it_appears():
    page = FakeOverlayPage(visible_selectors={"div[class*='closeIconBg']"})

    closed = asyncio.run(_close_ad_popup(page))

    assert closed is True
    assert ("click", "div[class*='closeIconBg']", 1500) in page.log
