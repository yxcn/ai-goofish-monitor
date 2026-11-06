from src.keyword_rule_engine import build_search_text, evaluate_keyword_rules


def _sample_record():
    return {
        "商品信息": {
            "商品标题": "Sony A7M4 全画幅相机",
            "当前售价": "10000",
            "商品标签": ["验货宝", "包邮"],
        },
        "卖家信息": {
            "卖家昵称": "摄影器材店",
            "卖家个性签名": "可验机，支持同城面交",
        },
    }


def test_build_search_text_contains_product_and_seller_fields():
    text = build_search_text(_sample_record())
    assert "sony a7m4" in text
    assert "摄影器材店" in text
    assert "支持同城面交" in text


def test_keyword_rules_or_match_any_keyword():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["a7m4", "佳能"], text)
    assert result["is_recommended"] is True
    assert result["analysis_source"] == "keyword"
    assert result["keyword_hit_count"] == 1
    assert result["matched_keywords"] == ["a7m4"]


def test_keyword_rules_count_multiple_hits():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["a7m4", "验货宝", "摄影器材店"], text)
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 3


def test_keyword_rules_case_insensitive_contains():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["SONY", "A7M4"], text)
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 2


def test_keyword_rules_no_match():
    text = build_search_text(_sample_record())
    result = evaluate_keyword_rules(["佳能", "单反"], text)
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_keyword_rules_do_not_partially_match_alphanumeric_prefixes():
    result = evaluate_keyword_rules(["q1"], "富士 q1r5 旗舰相机")
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_keyword_rules_still_match_full_alphanumeric_token():
    result = evaluate_keyword_rules(["q1r5"], "富士 q1r5 旗舰相机")
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] == 1


def test_seller_other_listings_and_ratings_do_not_count_as_a_hit():
    # 搜 iPhone 15 配规则 256G：卖家另一件挂牌的标题、别人写的评价里出现 256G，不算本商品命中
    record = {
        "商品信息": {"商品标题": "iPhone 15 128G 蓝色", "商品描述": "自用一年，无拆修"},
        "卖家信息": {
            "卖家昵称": "小明",
            "卖家发布的商品列表": [{"商品标题": "iPhone 15 Pro 256G 原色钛金属", "商品状态": "在售"}],
            "卖家收到的评价列表": [{"评价内容": "256G 版本很流畅，卖家人很好"}],
            "卖家个人档案": {"性别": "男"},
        },
    }
    text = build_search_text(record)
    assert "256g" not in text
    assert "小明" in text and "无拆修" in text

    result = evaluate_keyword_rules(["256G"], text)
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_product_attribute_keys_and_values_are_searchable():
    record = {"商品信息": {"商品标题": "X1", "商品属性": {"成色": "几乎全新", "品牌": "联想"}}, "卖家信息": None}
    text = build_search_text(record)
    assert "成色" in text and "几乎全新" in text and "联想" in text


def test_product_fields_outside_whitelist_are_excluded_but_seller_scalars_stay():
    # 商品信息只取白名单字段（售价、链接不参与；发货地区参与，黑名单规则可能按地区排除）；卖家信息的单值字段都参与，列表/字典不参与
    record = {
        "商品信息": {"商品标题": "相机", "发货地区": "深圳", "当前售价": "¥2024", "商品链接": "https://x/item?id=2024"},
        "卖家信息": {"卖家标签": "个人闲置", "卖家IP属地": "广东省", "卖家昵称": 123, "卖家认证": ["实名认证"], "卖家个人档案": {"性别": "男"}},
    }
    text = build_search_text(record)
    assert "深圳" in text and "2024" not in text
    assert "个人闲置" in text and "广东省" in text and "123" in text
    assert "实名认证" not in text and "男" not in text
