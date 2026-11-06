"""SKIP_AI_ANALYSIS：跳过分析的商品全部推送，但 analysis_source 不能是 "ai"（dashboard 的 AI 推荐数只统计 ai）。"""
import asyncio

from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher, ItemAnalysisJob


def test_skip_ai_result_is_marked_skipped_and_still_notified():
    saved = []
    notified = []
    ai_calls = {"n": 0}

    async def seller_loader(user_id: str):
        return {"卖家ID": user_id}

    async def image_downloader(product_id: str, image_urls: list[str], task_name: str):
        return []

    async def ai_analyzer(record: dict, image_paths: list[str], prompt_text: str):
        ai_calls["n"] += 1
        return {"analysis_source": "ai", "is_recommended": False, "reason": "不该被调用"}

    async def notifier(item_data: dict, reason: str):
        notified.append((item_data["商品ID"], reason))
        return {"ntfy": {"success": True, "message": "ok"}}

    async def saver(record: dict, keyword: str):
        saved.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=True,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(
            ItemAnalysisJob(
                keyword="demo",
                task_name="Demo",
                decision_mode="ai",
                analyze_images=False,
                prompt_text="prompt",
                keyword_rules=(),
                final_record={"商品信息": {"商品ID": "42", "商品图片列表": []}, "卖家信息": {}},
                seller_id="seller-1",
                zhima_credit_text="优秀",
                registration_duration_text="来闲鱼1年",
            )
        )
        await dispatcher.join()

    asyncio.run(run())

    assert ai_calls["n"] == 0
    analysis = saved[0]["ai_analysis"]
    assert analysis["analysis_source"] == "skipped"
    assert analysis["analysis_source"] != "ai"
    assert analysis["is_recommended"] is True
    assert "SKIP_AI_ANALYSIS" in analysis["reason"]
    assert analysis["keyword_hit_count"] == 0
    # 跳过分析 = 全部推送
    assert notified == [("42", analysis["reason"])]


def test_build_skip_ai_result_shape():
    dispatcher = ItemAnalysisDispatcher.__new__(ItemAnalysisDispatcher)

    result = dispatcher._build_skip_ai_result()

    assert result == {
        "analysis_source": "skipped",
        "is_recommended": True,
        "reason": "已跳过 AI 分析（SKIP_AI_ANALYSIS），直接通知",
        "keyword_hit_count": 0,
    }
