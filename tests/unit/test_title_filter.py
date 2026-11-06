from src.title_filter import (
    filter_items_by_title,
    normalize_title_keywords,
    title_filter_reason,
    title_matches_keywords,
)


def test_title_filter_reason_checks_exclusions_before_inclusions():
    assert title_filter_reason("ThinkPad T14 Gen1 锐龙", ["T14"], ["主板", "键盘"]) is None
    assert title_filter_reason("联想ThinkPad T14s Gen1主板 i7", ["T14"], ["主板"]) == "标题含排除词 主板"
    assert title_filter_reason("华为P50 Pro", ["T14"], ["主板"]) == "标题不含 T14"
    assert title_filter_reason("T14 原装键盘", [], ["键盘"]) == "标题含排除词 键盘"
    assert title_filter_reason("anything", [], []) is None


def test_filter_items_by_title_applies_exclusions_to_price_samples():
    page = [
        {"商品标题": "ThinkPad T14 Gen1", "当前售价": "¥1200"},
        {"商品标题": "ThinkPad T14 主板", "当前售价": "¥600"},
    ]
    assert [i["当前售价"] for i in filter_items_by_title(page, ["T14"], ["主板"])] == ["¥1200"]
    assert [i["当前售价"] for i in filter_items_by_title(page, [], ["主板"])] == ["¥1200"]


def test_filter_items_by_title_keeps_only_matching_items_for_price_stats():
    page = [
        {"商品标题": "ThinkPad T14 Gen1 锐龙", "当前售价": "¥1200"},
        {"商品标题": "华为P50 Pro 8+256G", "当前售价": "¥900"},
        {"商品标题": "thinkpad t14 gen2", "当前售价": "¥1800"},
        {"当前售价": "¥700"},
    ]
    kept = filter_items_by_title(page, ["T14"])
    assert [item["当前售价"] for item in kept] == ["¥1200", "¥1800"]


def test_filter_items_by_title_is_passthrough_without_keywords():
    page = [{"商品标题": "华为P50 Pro"}, {"商品标题": "ThinkPad T14"}]
    assert filter_items_by_title(page, []) == page
    assert filter_items_by_title(iter(page), None) == page


def test_no_keywords_means_everything_passes():
    assert title_matches_keywords("个人闲置Redmi Turbo 3", []) is True
    assert title_matches_keywords("个人闲置Redmi Turbo 3", None) is True
    assert title_matches_keywords("", "") is True


def test_title_must_contain_any_keyword_case_insensitively():
    keywords = ["T14"]
    assert title_matches_keywords("ThinkPad T14 Gen1 锐龙7 4750U", keywords) is True
    assert title_matches_keywords("thinkpad t14 gen2 16g", keywords) is True
    assert title_matches_keywords("个人闲置Redmi Turbo 3，换过屏幕", keywords) is False
    assert title_matches_keywords("联想ThinkPad Yoga 14e，i5-1135G7", keywords) is False
    assert title_matches_keywords("出 ThinkPad T460s 14寸商务本", keywords) is False


def test_multiple_keywords_are_or_logic():
    keywords = ["T14", "T 14"]
    assert title_matches_keywords("ThinkPad T 14 轻薄本", keywords) is True
    assert title_matches_keywords("ThinkPad X1 Carbon", keywords) is False


def test_keywords_match_as_substrings_and_leave_fine_judgement_to_ai():
    # 预筛选只负责挡掉明显无关的商品，T14s 之类交给后续 AI 判断
    assert title_matches_keywords("ThinkPad T14s 2021", ["T14"]) is True


def test_empty_title_fails_when_keywords_are_set():
    assert title_matches_keywords("", ["T14"]) is False
    assert title_matches_keywords(None, ["T14"]) is False


def test_normalize_splits_dedupes_and_keeps_original_spelling():
    assert normalize_title_keywords("T14, t14\n ThinkPad ，，") == ["T14", "ThinkPad"]
    assert normalize_title_keywords(["T14", " ", "t14 ", "X1"]) == ["T14", "X1"]
    assert normalize_title_keywords(None) == []
