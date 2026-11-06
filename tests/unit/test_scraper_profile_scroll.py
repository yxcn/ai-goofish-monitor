"""卖家主页滚动加载：每收到一页就立刻继续滚，而不是每页都等满 8 秒。"""

import asyncio
import time

import src.scraper as scraper_module
from src.scraper import scrape_user_profile

HEAD_URL = "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.head/1.0/"
ITEMS_URL = "https://h5api.m.goofish.com/h5/mtop.idle.web.xyh.item.list/1.0/"
RATES_URL = "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.rate.list/1.0/"


class FakeResponse:
    def __init__(self, url: str, payload: dict):
        self.url = url
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _item_page(start: int, count: int, next_page: bool) -> dict:
    cards = [
        {
            "cardData": {
                "id": start + i,
                "title": f"item {start + i}",
                "itemStatus": 0,
                "priceInfo": {"price": "100"},
                "picInfo": {"picUrl": "pic"},
            }
        }
        for i in range(count)
    ]
    return {"data": {"cardList": cards, "nextPage": next_page}}


def _rating_page(start: int, count: int, next_page: bool) -> dict:
    cards = [
        {
            "cardData": {
                "rateId": start + i,
                "feedback": "好评",
                "rate": 1,
                "rateTagList": [{"text": "来自买家"}],
            }
        }
        for i in range(count)
    ]
    return {"data": {"cardList": cards, "nextPage": next_page}}


class FakeTabLocator:
    def __init__(self, page, count: int):
        self._page = page
        self._count = count

    async def count(self) -> int:
        return self._count

    async def click(self) -> None:
        await self._page.open_rating_tab()


class FakeProfilePage:
    """goto 时立刻给头部 + 第一页商品；每次滚动后延迟一小会儿再给下一页（模拟接口返回）。"""

    def __init__(
        self,
        item_pages: list[dict],
        rating_pages: list[dict],
        *,
        delivery_delay: float = 0.02,
        has_rating_tab: bool = True,
    ):
        self._item_pages = list(item_pages)
        self._rating_pages = list(rating_pages)
        self._delay = delivery_delay
        self._has_rating_tab = has_rating_tab
        self._handlers: list = []
        self._phase = "items"
        self.scrolls = 0
        self.closed = False

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self._handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self._handlers.remove(handler)

    async def _emit(self, response: FakeResponse) -> None:
        for handler in list(self._handlers):
            await handler(response)

    def _deliver_later(self, response: FakeResponse) -> None:
        async def _run():
            await asyncio.sleep(self._delay)
            await self._emit(response)

        asyncio.create_task(_run())

    async def goto(self, url: str, **_kwargs) -> None:
        await self._emit(
            FakeResponse(HEAD_URL, {"data": {"module": {"base": {"displayName": "seller"}}}})
        )
        if self._item_pages:
            await self._emit(FakeResponse(ITEMS_URL, self._item_pages.pop(0)))

    async def evaluate(self, _script: str) -> None:
        self.scrolls += 1
        if self._phase == "items" and self._item_pages:
            self._deliver_later(FakeResponse(ITEMS_URL, self._item_pages.pop(0)))
        elif self._phase == "ratings" and self._rating_pages:
            self._deliver_later(FakeResponse(RATES_URL, self._rating_pages.pop(0)))

    def locator(self, _selector: str) -> FakeTabLocator:
        return FakeTabLocator(self, 1 if self._has_rating_tab else 0)

    async def open_rating_tab(self) -> None:
        self._phase = "ratings"
        if self._rating_pages:
            self._deliver_later(FakeResponse(RATES_URL, self._rating_pages.pop(0)))

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakeProfilePage):
        self.page = page

    async def new_page(self) -> FakeProfilePage:
        return self.page


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def test_profile_scroll_continues_as_soon_as_each_page_arrives(monkeypatch):
    monkeypatch.setattr(scraper_module, "random_sleep", _no_sleep)
    page = FakeProfilePage(
        item_pages=[_item_page(0, 2, True), _item_page(2, 2, True), _item_page(4, 1, False)],
        rating_pages=[_rating_page(0, 2, True), _rating_page(2, 1, False)],
    )

    started = time.monotonic()
    profile = asyncio.run(scrape_user_profile(FakeContext(page), "seller-1"))
    elapsed = time.monotonic() - started

    assert [item["商品ID"] for item in profile["卖家发布的商品列表"]] == [0, 1, 2, 3, 4]
    assert len(profile["卖家收到的评价列表"]) == 3
    assert profile["卖家昵称"] == "seller"
    # 商品：首页由 goto 给出，再滚 2 次拿到第 2、3 页；评价：点 tab 拿第 1 页，再滚 1 次
    assert page.scrolls == 3
    # 旧实现每页都要把 8 秒等满，这里 5 页会超过 30 秒
    assert elapsed < 2.0
    assert page.closed is True


def test_profile_scroll_stops_after_timeout_when_no_new_page_arrives(monkeypatch):
    monkeypatch.setattr(scraper_module, "random_sleep", _no_sleep)
    monkeypatch.setattr(scraper_module, "PROFILE_SCROLL_PAGE_TIMEOUT_SECONDS", 0.1)
    # nextPage=true 但再没有响应回来：超时后退出，已拿到的商品保留
    page = FakeProfilePage(
        item_pages=[_item_page(0, 2, True)],
        rating_pages=[],
        has_rating_tab=False,
    )

    profile = asyncio.run(scrape_user_profile(FakeContext(page), "seller-2"))

    assert len(profile["卖家发布的商品列表"]) == 2
    assert page.scrolls == 1
    assert "卖家收到的评价列表" not in profile
    assert page.closed is True


def test_profile_scroll_stops_when_a_page_response_is_malformed(monkeypatch):
    monkeypatch.setattr(scraper_module, "random_sleep", _no_sleep)
    monkeypatch.setattr(scraper_module, "PROFILE_SCROLL_PAGE_TIMEOUT_SECONDS", 5)
    # 第二页 data 为 null：回调里抛异常 -> 停止事件和"到页"事件都要 set，不能卡到超时
    page = FakeProfilePage(
        item_pages=[_item_page(0, 1, True), {"data": None}],
        rating_pages=[],
        has_rating_tab=False,
    )

    started = time.monotonic()
    profile = asyncio.run(scrape_user_profile(FakeContext(page), "seller-3"))

    assert len(profile["卖家发布的商品列表"]) == 1
    assert page.scrolls == 1
    assert time.monotonic() - started < 2.0
