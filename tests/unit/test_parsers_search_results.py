import asyncio
import re

from src.parsers import _parse_search_results_json


def _search_item(
    item_id,
    *,
    title="iPhone 15 256G 蓝色",
    price_texts=("¥", "3,999"),
    publish_time="1700000000000",
    target_url="fleamarket://item?id={id}",
    click_args="default",
    main_overrides=None,
):
    main = {
        "title": title,
        "price": [{"text": text} for text in price_texts],
        "area": "上海",
        "userNickName": "卖家A",
        "itemId": item_id,
        "oriPrice": "¥5999",
        "picUrl": "p.jpg",
        "fishTags": {"r1": {"tagList": [{"data": {"content": "验货宝"}}]}},
    }
    main.update(main_overrides or {})
    if click_args == "default":
        click_args = {"publishTime": publish_time, "wantNum": "3", "tag": "freeship"}
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": main,
                    "clickParam": {"args": click_args},
                    "targetUrl": target_url.format(id=item_id) if isinstance(target_url, str) else target_url,
                }
            }
        }
    }


def _parse(items):
    return asyncio.run(_parse_search_results_json({"data": {"resultList": items}}, "测试页"))


def test_one_bad_item_does_not_empty_the_page(capsys):
    # 以前整页包在一个 try 里：第 2 个商品 targetUrl 为 null 触发 None.replace，整页返回 []，上层顺带停止翻页
    items = [
        _search_item("1"),
        _search_item("2", target_url=None),
        _search_item("3"),
    ]
    parsed = _parse(items)

    assert [item["商品ID"] for item in parsed] == ["1", "3"]
    assert parsed[0]["商品链接"] == "https://www.goofish.com/item?id=1"
    assert parsed[0]["商品标签"] == ["包邮", "验货宝"]
    assert parsed[0]["当前售价"] == "¥3,999"
    assert "第 2/3 个商品解析失败" in capsys.readouterr().out


def test_int_publish_time_is_formatted_instead_of_crashing_on_isdigit():
    parsed = _parse([
        _search_item("1", publish_time=1700000000000),
        _search_item("2", publish_time="1700000000000"),
        _search_item("3", publish_time=None),
        _search_item("4", publish_time="not-a-timestamp"),
    ])

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", parsed[0]["发布时间"])
    assert parsed[0]["发布时间"] == parsed[1]["发布时间"]
    assert parsed[2]["发布时间"] == "未知时间"
    assert parsed[3]["发布时间"] == "未知时间"


def test_wan_price_converts_when_numeric_and_keeps_text_otherwise():
    parsed = _parse([
        _search_item("1", price_texts=("¥", "1.2万")),
        _search_item("2", price_texts=("¥", "1.2万起")),
        _search_item("3", price_texts=("当前价", "¥", "1,494")),
    ])

    assert parsed[0]["当前售价"] == "¥12000"
    assert parsed[1]["当前售价"] == "¥1.2万起"
    assert parsed[2]["当前售价"] == "¥1,494"


def test_non_dict_click_args_and_ex_content_are_tolerated():
    weird_main = {
        "data": {
            "item": {
                "main": {
                    "exContent": "oops",
                    "clickParam": {"args": None},
                    "targetUrl": "fleamarket://item?id=9",
                }
            }
        }
    }
    parsed = _parse([_search_item("1", click_args=["not", "a", "dict"]), weird_main])

    assert parsed[0]["“想要”人数"] == "NaN"
    assert parsed[0]["发布时间"] == "未知时间"
    assert parsed[0]["商品标签"] == ["验货宝"]
    assert parsed[1]["商品标题"] == "未知标题"
    assert parsed[1]["当前售价"] == ""
    assert parsed[1]["商品链接"] == "https://www.goofish.com/item?id=9"


def test_missing_result_list_returns_empty_list():
    assert asyncio.run(_parse_search_results_json({"data": {}}, "空页")) == []
    assert asyncio.run(_parse_search_results_json({"data": {"resultList": "nope"}}, "坏页")) == []
    assert asyncio.run(_parse_search_results_json(None, "无")) == []
