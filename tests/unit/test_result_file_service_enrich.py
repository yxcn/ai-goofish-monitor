from src.services import result_file_service as rfs


def _snap(run, item_id, price, hour):
    return {"run_id": f"run-{run}", "item_id": item_id, "price": price, "title": f"ThinkPad T14 #{item_id}", "snapshot_time": f"2026-11-06T{hour:02d}:00:00"}


def test_enrich_only_fills_missing_price_insight_and_shares_one_sold_reference(monkeypatch):
    snapshots = [_snap(1, "A", 1500, 8), _snap(1, "B", 1100, 8), _snap(2, "A", 1500, 9), _snap(3, "A", 1450, 10), _snap(3, "C", 1400, 10)]
    calls = {"sold": 0}

    def fake_sold(keyword):
        calls["sold"] += 1
        return [{"price": 1499}, {"price": 1399}]

    monkeypatch.setattr(rfs, "load_price_snapshots", lambda keyword: snapshots)
    monkeypatch.setattr(rfs, "load_visible_result_item_ids", lambda filename: {"A", "C"})
    monkeypatch.setattr(rfs, "load_sold_listings", fake_sold)

    stored = {"price_verdict": "偏贵", "sold_reference": {"sample_count": 3}}
    records = [
        {"商品信息": {"商品ID": "A", "当前售价": "¥1450"}, "price_insight": stored},   # 抓取时已算好：原样保留
        {"商品信息": {"商品ID": "C", "当前售价": "¥1400"}},                            # 老记录：读时补
        {"商品信息": {"商品ID": "zzz", "当前售价": "¥1"}},                              # 没有快照的商品
    ]
    enriched = rfs.enrich_records_with_price_insight(records, "ThinkPad_T14_full_data.jsonl")

    assert enriched[0]["price_insight"] is stored
    filled = enriched[1]["price_insight"]
    assert filled["sold_reference"]["sold_listing_count"] == 2 and filled["sold_reference"]["vanished_count"] == 1
    assert filled["market_sample_count"] == 2
    assert enriched[2]["price_insight"]["observation_count"] == 0
    assert calls["sold"] == 1

    # 全都已有 price_insight 时根本不读库
    monkeypatch.setattr(rfs, "load_price_snapshots", lambda keyword: (_ for _ in ()).throw(AssertionError("should not load")))
    same = rfs.enrich_records_with_price_insight([records[0]], "ThinkPad_T14_full_data.jsonl")
    assert same[0]["price_insight"] is stored
