import asyncio

from src.parsers import (
    _parse_item_card_labels,
    _parse_user_items_data,
    account_certifications,
    parse_ratings_data,
    parse_seller_detail,
    parse_user_head_data,
)


def test_head_parser_adds_ip_location_followers_and_certifications():
    # 字段名来自一次真实的 mtop.idle.web.user.page.head 返回
    head_json = {
        "data": {
            "baseInfo": {
                "tags": {
                    "real_name_certification_77": True,
                    "real_person_certification_77": True,
                    "idle_zhima_zheng": False,
                    "xianyu_user_upgrade": True,
                }
            },
            "module": {
                "social": {"followers": "12", "following": "3"},
                "tabs": {"item": {"number": 1}, "rate": {"number": "1"}},
                "base": {"ipLocation": "山东省", "displayName": "x***7", "avatar": {"avatar": "a.png"}, "ylzTags": []},
            },
        }
    }
    head = asyncio.run(parse_user_head_data(head_json))

    assert head["卖家IP属地"] == "山东省"
    assert (head["卖家粉丝数"], head["卖家关注数"]) == (12, 3)
    assert head["卖家认证"] == ["实名认证", "实人认证"]
    assert head["卖家昵称"] == "x***7" and head["卖家信用等级"] == "暂无"


def test_head_parser_tolerates_missing_social_and_tags():
    head = asyncio.run(parse_user_head_data({"data": {"module": {"base": {"displayName": "a"}}}}))
    assert head["卖家IP属地"] == ""
    assert head["卖家粉丝数"] is None
    assert head["卖家认证"] == []
    assert account_certifications(None) == []
    assert account_certifications({"real_name_certification_12": "1", "idle_zhima_zheng": True}) == ["实名认证", "芝麻信用认证"]


def _item_card():
    return {
        "cardData": {
            "id": "1097788657985",
            "title": "ThinkPad T14笔记本电脑",
            "itemStatus": 1,
            "categoryId": 50025387,
            "priceInfo": {"price": "1494"},
            "picInfo": {"picUrl": "p.jpg"},
            "detailParams": {"postInfo": "包邮"},
            "itemLabelDataVO": {
                "labelData": {
                    "r1": {"tagList": [{"data": {"type": "img", "content": "验货宝"}}]},
                    "r2": {"tagList": [{"data": {"content": "48小时内发布"}}]},
                    "r3": {"tagList": [{"data": {"content": "¥2300", "isStrikeThrough": "true"}}, {"data": {"content": "11人想要"}}]},
                }
            },
        }
    }


def test_item_list_parser_keeps_old_fields_and_adds_card_labels():
    items = asyncio.run(_parse_user_items_data([_item_card()]))
    assert items == [
        {
            "商品ID": "1097788657985",
            "商品标题": "ThinkPad T14笔记本电脑",
            "商品价格": "1494",
            "商品主图": "p.jpg",
            "商品状态": "已售",
            "商品标签": ["验货宝", "包邮"],
            "商品原价": "¥2300",
            "想要人数": 11,
            "发布标签": "48小时内发布",
            "品类ID": 50025387,
        }
    ]


def test_item_card_labels_omit_missing_and_skip_malformed_tags():
    assert _parse_item_card_labels({}) == {}
    extras = _parse_item_card_labels(
        {"itemLabelDataVO": {"labelData": {"r1": {"tagList": ["bad", {"data": None}, {"data": {"content": " "}}]}, "r2": "nope"}}, "categoryId": ""}
    )
    assert extras == {}


def test_ratings_parser_adds_rater_ip_location():
    ratings = asyncio.run(parse_ratings_data([
        {"cardData": {"rateId": 1, "feedback": "爽快", "rate": 1, "rateTagList": [{"text": "卖家"}], "ipAddress": "中国湖北省"}},
        {"cardData": {"rateId": 2, "feedback": "好", "rate": 1, "rateTagList": [{"text": "买家"}]}},
    ]))
    assert ratings[0]["评价者IP属地"] == "中国湖北省"
    assert ratings[1]["评价者IP属地"] == ""


def test_seller_detail_parser_reads_account_level_fields():
    seller_do = {
        "hasSoldNumInteger": 0,
        "itemCount": 1,
        "userRegDay": 932,
        "registerTime": 1713383646000,
        "identityTags": [{"text": "实人认证已通过", "type": "1"}, "junk"],
        "xianyuSummary": "卖出过0件宝贝。",
        "city": "淄博",
    }
    facts = parse_seller_detail(seller_do)
    assert facts["卖家卖出件数"] == 0
    assert facts["卖家在售数量"] == 1
    assert facts["卖家注册天数"] == 932
    assert facts["卖家注册日期"].startswith("2024-04-1")
    assert facts["卖家实人认证"] is True
    assert facts["卖家交易摘要"] == "卖出过0件宝贝。"
    assert "卖家所在城市" not in facts


def test_seller_detail_parser_omits_missing_fields():
    assert parse_seller_detail({}) == {}
    assert parse_seller_detail(None) == {}
    assert parse_seller_detail({"identityTags": [{"text": "实人认证未通过"}], "registerTime": "oops"}) == {"卖家实人认证": False}
