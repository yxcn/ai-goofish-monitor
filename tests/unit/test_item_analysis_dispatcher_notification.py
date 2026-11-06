import asyncio

from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher, ItemAnalysisJob


def _job(item_id: str = "1") -> ItemAnalysisJob:
    return ItemAnalysisJob(
        keyword="t14",
        task_name="T14",
        decision_mode="ai",
        analyze_images=False,
        prompt_text="prompt",
        keyword_rules=(),
        final_record={"商品信息": {"商品ID": item_id, "商品链接": f"https://www.goofish.com/item?id={item_id}"}},
        seller_id=None,
        zhima_credit_text=None,
        registration_duration_text="",
    )


async def _seller_loader(user_id):
    return {}


async def _image_downloader(product_id, image_urls, task_name):
    return []


async def _recommend(record, image_paths, prompt_text):
    return {"analysis_source": "ai", "is_recommended": True, "reason": "好价", "keyword_hit_count": 0}


def _run(dispatcher: ItemAnalysisDispatcher, job: ItemAnalysisJob):
    async def go():
        dispatcher.submit(job)
        await dispatcher.join()

    asyncio.run(go())


def test_dispatcher_skips_notification_when_record_was_not_saved():
    notifications = []

    async def notifier(item_data, reason):
        notifications.append(item_data["商品ID"])
        return {"telegram": {"success": True}}

    async def failing_saver(record, keyword):
        return False

    dispatcher = ItemAnalysisDispatcher(
        concurrency=1, skip_ai_analysis=False, seller_loader=_seller_loader,
        image_downloader=_image_downloader, ai_analyzer=_recommend, notifier=notifier, saver=failing_saver,
    )
    _run(dispatcher, _job())

    # 没落库就不推：否则下次运行会把它当新商品再推一次
    assert notifications == []
    assert dispatcher.completed_count == 0


def test_dispatcher_records_notification_status_and_logs_failed_channels(capsys):
    saved = []
    statuses = []

    async def notifier(item_data, reason):
        return {
            "telegram": {"success": True, "message": "发送成功", "attempts": 1},
            "wecom": {"success": False, "message": "3 次尝试均失败: errcode 93000", "attempts": 3},
        }

    async def saver(record, keyword):
        saved.append(record)
        return True

    async def status_writer(record, keyword, status):
        statuses.append((keyword, status))

    dispatcher = ItemAnalysisDispatcher(
        concurrency=1, skip_ai_analysis=False, seller_loader=_seller_loader,
        image_downloader=_image_downloader, ai_analyzer=_recommend, notifier=notifier, saver=saver,
        notification_status_writer=status_writer,
    )
    _run(dispatcher, _job("7"))

    assert len(saved) == 1 and dispatcher.completed_count == 1
    keyword, status = statuses[0]
    assert keyword == "t14"
    assert status["成功"] == ["telegram"]
    assert status["失败"] == {"wecom": "3 次尝试均失败: errcode 93000"}
    assert status["时间"]
    assert saved[0]["通知状态"] == status
    assert "商品 #7 有 1 个渠道推送失败" in capsys.readouterr().out


def test_dispatcher_tolerates_notifier_without_results():
    async def legacy_notifier(item_data, reason):
        return None

    async def saver(record, keyword):
        return True

    dispatcher = ItemAnalysisDispatcher(
        concurrency=1, skip_ai_analysis=False, seller_loader=_seller_loader,
        image_downloader=_image_downloader, ai_analyzer=_recommend, notifier=legacy_notifier, saver=saver,
    )
    _run(dispatcher, _job())
    assert dispatcher.completed_count == 1
