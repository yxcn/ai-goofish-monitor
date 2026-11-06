from src.scraper import filter_items_by_price


def _items():
    return [
        {"商品ID": "cheap", "当前售价": "¥800"},
        {"商品ID": "comma", "当前售价": "¥1,494"},
        {"商品ID": "wan", "当前售价": "1.2万"},
        {"商品ID": "empty", "当前售价": ""},
        {"商品ID": "broken", "当前售价": "价格异常"},
        {"商品ID": "edge", "当前售价": "10000"},
    ]


def _ids(items):
    return [item["商品ID"] for item in items]


def test_drops_items_outside_range_and_parses_price_formats():
    kept = filter_items_by_price(_items(), "1000", "10000")
    # 800 太低、1.2万 太高；"¥1,494" 能解析；空价格和"价格异常"解析不了的保留，交给后面判断；边界值包含
    assert _ids(kept) == ["comma", "empty", "broken", "edge"]


def test_only_one_bound_or_no_bound():
    assert _ids(filter_items_by_price(_items(), "1000", None)) == ["comma", "wan", "empty", "broken", "edge"]
    assert _ids(filter_items_by_price(_items(), None, "1000")) == ["cheap", "empty", "broken"]
    assert _ids(filter_items_by_price(_items(), "", "")) == _ids(_items())
    assert _ids(filter_items_by_price(_items(), None, None)) == _ids(_items())


def test_inverted_bounds_are_swapped():
    assert _ids(filter_items_by_price(_items(), "10000", "1000")) == _ids(filter_items_by_price(_items(), "1000", "10000"))


def test_numeric_bounds_and_odd_inputs_are_tolerated():
    assert _ids(filter_items_by_price(_items(), 1000, 10000)) == ["comma", "empty", "broken", "edge"]
    assert filter_items_by_price([], "1", "2") == []
    assert filter_items_by_price(None, "1", "2") == []
    assert filter_items_by_price([{"商品ID": "no-price"}], "1", "2") == [{"商品ID": "no-price"}]
