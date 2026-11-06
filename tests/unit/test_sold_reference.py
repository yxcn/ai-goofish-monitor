from src.services.price_history_service import (
    build_item_price_context,
    build_price_verdict,
    build_sold_reference,
    find_vanished_listings,
    select_sold_listings,
)


def _snap(run: int, item_id: str, price: float, day: int = 6, hour: int = 8):
    return {
        "run_id": f"run-{run}",
        "item_id": item_id,
        "price": price,
        "title": f"ThinkPad T14 #{item_id}",
        "snapshot_time": f"2026-11-{day:02d}T{hour:02d}:00:00",
    }


def _snapshots():
    # run1: A B C，run2: A B，run3: A，run4: A D  → B 在 run2 后消失，C 在 run1 后消失，D 还在
    return [
        _snap(1, "A", 1500, hour=8), _snap(1, "B", 1100, hour=8), _snap(1, "C", 1000, hour=8),
        _snap(2, "A", 1500, hour=9), _snap(2, "B", 1050, hour=9),
        _snap(3, "A", 1450, hour=10),
        _snap(4, "A", 1450, hour=11), _snap(4, "D", 1700, hour=11),
    ]


def test_vanished_listings_need_two_absent_runs_and_keep_last_price():
    vanished = {entry["item_id"]: entry for entry in find_vanished_listings(_snapshots())}
    assert set(vanished) == {"B", "C"}
    assert vanished["B"]["price"] == 1050.0 and vanished["B"]["last_seen_at"] == "2026-11-06T09:00:00"
    assert vanished["C"]["source"] == "vanished"
    # 只缺最新一轮不算消失（搜索排序抖动）
    assert find_vanished_listings(_snapshots()[:5]) == [{"item_id": "C", "title": "ThinkPad T14 #C", "price": 1000.0, "last_seen_at": "2026-11-06T08:00:00", "source": "vanished"}]
    # 单轮没有"消失"；超出窗口的也不算
    assert find_vanished_listings(_snapshots()[:3]) == []
    assert find_vanished_listings([_snap(1, "old", 900, day=1), _snap(2, "x", 1000, day=6), _snap(3, "x", 1000, day=6)], window_days=2) == []
    assert find_vanished_listings([]) == []


def test_sold_reference_merges_vanished_listings_and_sold_items():
    reference = build_sold_reference(_snapshots(), [{"price": 1499}, {"price": "oops"}])
    assert reference["sample_count"] == 3
    assert (reference["vanished_count"], reference["sold_listing_count"]) == (2, 1)
    assert reference["median_price"] == 1050.0
    assert reference["reliable"] is True
    assert build_sold_reference(_snapshots())["reliable"] is False
    assert build_sold_reference([])["sample_count"] == 0


def test_select_sold_listings_filters_similar_non_accessory_items():
    records = [
        {
            "搜索关键字": "ThinkPad T14",
            "商品信息": {"商品ID": "1", "品类ID": 50025387, "商品属性": {"分类": "笔记本电脑"}},
            "卖家信息": {"卖家发布的商品列表": [
                {"商品ID": "s1", "商品标题": "出售自用Thinkpad s2 gen6笔记本电脑", "商品价格": "1499", "商品状态": "已售", "品类ID": 50025387},
                {"商品ID": "s2", "商品标题": "联想t14s主板，i7+16g内存", "商品价格": "600", "商品状态": "已售", "品类ID": 1},
                {"商品ID": "s3", "商品标题": "ThinkPad T480", "商品价格": "1200", "商品状态": "在售"},
                {"商品ID": "s4", "商品标题": "儿童滑板车", "商品价格": "100", "商品状态": "已售"},
                {"商品ID": "s5", "商品标题": "ThinkPad X1 键盘", "商品价格": "80", "商品状态": "已售", "品类ID": 50025387},
            ]},
        },
        {"搜索关键字": "ThinkPad T14", "商品信息": {"商品ID": "2"}, "卖家信息": {"卖家发布的商品列表": [
            {"商品ID": "s1", "商品标题": "重复的那台", "商品价格": "1499", "商品状态": "已售", "品类ID": 50025387},
            {"商品ID": "s6", "商品标题": "thinkpad t14 gen2 已出", "商品价格": "¥1800", "商品状态": "已售"},
        ]}},
        "junk",
    ]
    picked = select_sold_listings(records, "ThinkPad T14")
    assert [(entry["item_id"], entry["price"], entry["source"]) for entry in picked] == [("s1", 1499.0, "sold"), ("s6", 1800.0, "sold")]


def test_price_verdict_prefers_sold_reference_then_listing_median():
    sold = {"reliable": True, "median_price": 1200.0, "sample_count": 5}
    verdict = build_price_verdict(1050.0, market_median=1700.0, market_sample_count=8, sold_reference=sold)
    assert verdict["price_verdict"] == "性价比高" and verdict["price_verdict_key"] == "good_deal"
    assert verdict["price_verdict_basis"] == "sold" and verdict["reference_price"] == 1200.0
    assert verdict["price_diff_percent"] == -12.5
    assert verdict["price_verdict_text"] == "比成交参考价低 12%（成交参考价 ¥1200，5 条）→ 性价比高"

    verdict = build_price_verdict(1900.0, market_median=1700.0, market_sample_count=8, sold_reference={"reliable": False, "median_price": 1049.0, "sample_count": 2})
    assert verdict["price_verdict"] == "偏贵" and verdict["price_verdict_basis"] == "listing"
    assert verdict["price_verdict_text"] == "比挂牌中位价高 12%（挂牌中位价 ¥1700，8 条）→ 偏贵"

    verdict = build_price_verdict(1705.0, market_median=1700.0, market_sample_count=8, sold_reference=None)
    assert verdict["price_verdict"] == "价格正常"
    assert verdict["price_verdict_text"] == "与挂牌中位价持平（挂牌中位价 ¥1700，8 条）→ 价格正常"

    verdict = build_price_verdict(1500.0, market_median=1700.0, market_sample_count=2, sold_reference=None)
    assert verdict["price_verdict_key"] == "insufficient" and verdict["reference_price"] is None
    assert build_price_verdict(None, market_median=1700.0, market_sample_count=8, sold_reference=sold)["price_verdict"] == "样本不足"


def test_item_price_context_carries_sold_reference_and_verdict():
    context = build_item_price_context(_snapshots(), item_id="A", current_price=1450.0, sold_listings=[{"price": 1499}])
    assert context["market_sample_count"] == 2
    assert context["sold_reference"]["sample_count"] == 3 and context["sold_reference"]["median_price"] == 1050.0
    assert context["price_verdict_basis"] == "sold"
    assert context["price_verdict"] == "偏贵"
    assert context["vs_sold_reference_percent"] == 38.1
    assert context["vs_listing_median_percent"] == -7.9
    assert context["deal_label"]  # 旧的评分字段还在
