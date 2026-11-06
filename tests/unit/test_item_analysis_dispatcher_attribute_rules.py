import asyncio

from src.services.item_analysis_dispatcher import (
    ATTRIBUTE_RULE_SOURCE,
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)


def _run(items, attribute_rules):
    seller_calls = []
    ai_calls = []
    saved = []

    async def seller_loader(user_id: str):
        seller_calls.append(user_id)
        return {"卖家昵称": user_id}

    async def image_downloader(product_id, image_urls, task_name):
        return []

    async def ai_analyzer(record, image_paths, prompt_text):
        ai_calls.append(record["商品信息"]["商品ID"])
        return {"analysis_source": "ai", "is_recommended": True, "reason": "ok", "keyword_hit_count": 0}

    async def notifier(item_data, reason):
        pass

    async def saver(record, keyword):
        saved.append(record)
        return True

    async def go():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=2,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        for item in items:
            dispatcher.submit(
                ItemAnalysisJob(
                    keyword="ThinkPad T14",
                    task_name="T14",
                    decision_mode="ai",
                    analyze_images=False,
                    prompt_text="prompt",
                    keyword_rules=(),
                    final_record={"商品信息": item, "卖家信息": {}},
                    seller_id=f"seller-{item['商品ID']}",
                    zhima_credit_text="暂无",
                    registration_duration_text="来闲鱼1年",
                    attribute_rules=attribute_rules,
                )
            )
        await dispatcher.join()

    asyncio.run(go())
    return seller_calls, ai_calls, {r["商品信息"]["商品ID"]: r for r in saved}


def test_attribute_rule_failures_skip_seller_and_ai_but_are_still_saved():
    items = [
        {"商品ID": "phone", "商品图片列表": [], "商品属性": {"分类": "手机", "运行内存": "12GB"}},
        {"商品ID": "laptop", "商品图片列表": [], "商品属性": {"分类": "笔记本电脑", "内存容量": "16GB"}},
        {"商品ID": "no-attrs", "商品图片列表": []},
    ]
    seller_calls, ai_calls, saved = _run(items, ("分类 = 笔记本电脑", "内存容量 >= 16GB"))

    assert sorted(ai_calls) == ["laptop", "no-attrs"]
    assert sorted(seller_calls) == ["seller-laptop", "seller-no-attrs"]
    assert set(saved) == {"phone", "laptop", "no-attrs"}

    rejected = saved["phone"]["ai_analysis"]
    assert rejected["is_recommended"] is False
    assert rejected["analysis_source"] == ATTRIBUTE_RULE_SOURCE
    assert rejected["failed_rules"] == ["分类 = 笔记本电脑（实际：手机）"]
    assert "属性预筛未通过" in rejected["reason"]
    assert saved["phone"]["卖家信息"]["卖家注册时长"] == "来闲鱼1年"
    assert saved["laptop"]["ai_analysis"]["analysis_source"] == "ai"


def test_unparseable_rules_are_ignored_rather_than_blocking_analysis():
    items = [{"商品ID": "x", "商品图片列表": [], "商品属性": {"分类": "手机"}}]
    seller_calls, ai_calls, saved = _run(items, ("这不是规则",))
    assert ai_calls == ["x"]
    assert saved["x"]["ai_analysis"]["analysis_source"] == "ai"


def test_no_rules_keeps_the_old_flow():
    items = [{"商品ID": "x", "商品图片列表": [], "商品属性": {"分类": "手机"}}]
    _, ai_calls, _ = _run(items, ())
    assert ai_calls == ["x"]


def test_recommended_items_are_notified_with_their_analysis_attached():
    notified = []

    async def seller_loader(user_id: str):
        return {}

    async def image_downloader(product_id, image_urls, task_name):
        return []

    async def ai_analyzer(record, image_paths, prompt_text):
        return {
            "is_recommended": True,
            "reason": "有条件推荐：配置合适",
            "risk_tags": ["需确认：电池健康度"],
        }

    async def notifier(item_data, reason):
        notified.append((item_data, reason))

    async def saver(record, keyword):
        return True

    async def go():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(
            ItemAnalysisJob(
                keyword="ThinkPad T14",
                task_name="T14",
                decision_mode="ai",
                analyze_images=False,
                prompt_text="prompt",
                keyword_rules=(),
                final_record={"商品信息": {"商品ID": "ok", "商品标题": "T14", "商品图片列表": []}, "卖家信息": {}},
                seller_id=None,
                zhima_credit_text="暂无",
                registration_duration_text="来闲鱼1年",
            )
        )
        await dispatcher.join()

    asyncio.run(go())

    assert len(notified) == 1
    item_data, reason = notified[0]
    assert reason == "有条件推荐：配置合适"
    assert item_data["商品标题"] == "T14"
    assert item_data["ai_analysis"]["risk_tags"] == ["需确认：电池健康度"]
