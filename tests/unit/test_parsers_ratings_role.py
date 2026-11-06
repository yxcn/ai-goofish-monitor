import asyncio

from src.parsers import calculate_reputation_from_ratings, parse_ratings_data


def _card(rater_role: str, rate: int = 1, feedback: str = "好"):
    return {
        "cardData": {
            "rateId": 1,
            "feedback": feedback,
            "rate": rate,
            "rateTagList": [{"text": rater_role}],
            "raterUserNick": "匿名用户",
            "gmtCreate": "2026-05-18 10:16:20",
            "pictCdnUrlList": [],
        }
    }


# 闲鱼评价列表里的角色标签是“写评价的人”的角色：
# 卖家留下的“很好的买家”说明该用户当时是买家。
RATINGS = [
    _card("卖家", feedback="很好的买家，感谢光临本店"),
    _card("卖家", feedback="下单爽快，收货快，好评"),
    _card("卖家", rate=0, feedback="一般"),
    _card("买家", feedback="卖家发货很快，机器和描述一致"),
]


def test_reputation_counts_follow_the_users_actual_role():
    summary = asyncio.run(calculate_reputation_from_ratings(RATINGS))

    # 三条来自卖家的评价 = 该用户买了三次，其中两次好评
    assert summary["作为买家的好评数"] == "2/3"
    assert summary["作为买家的好评率"] == "66.67%"
    # 一条来自买家的评价 = 该用户卖了一次
    assert summary["作为卖家的好评数"] == "1/1"
    assert summary["作为卖家的好评率"] == "100.00%"


def test_parsed_ratings_expose_the_users_role_in_each_trade():
    parsed = asyncio.run(parse_ratings_data(RATINGS))

    assert [item["评价来源角色"] for item in parsed] == ["卖家", "卖家", "卖家", "买家"]
    assert [item["该用户在此交易中的角色"] for item in parsed] == ["买家", "买家", "买家", "卖家"]


def test_unknown_role_tags_are_not_counted():
    summary = asyncio.run(calculate_reputation_from_ratings([_card("系统")]))
    assert summary["作为卖家的好评数"] == "0/0"
    assert summary["作为买家的好评数"] == "0/0"
    assert asyncio.run(parse_ratings_data([_card("系统")]))[0]["该用户在此交易中的角色"] == "未知"
