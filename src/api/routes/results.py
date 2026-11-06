"""
结果文件管理路由
"""
import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from enum import Enum

from pydantic import BaseModel
from urllib.parse import quote

from src.services.price_history_service import build_price_history_insights
from src.services.result_export_service import build_results_csv
from src.services.result_file_service import (
    enrich_records_with_price_insight,
    validate_result_filename,
)
from src.services.result_storage_service import (
    REVIEW_STATUSES,
    build_result_ndjson,
    delete_result_file_records,
    list_result_filenames,
    load_all_result_records,
    load_inbox_counts,
    load_result_blacklist_keywords,
    load_review_summary,
    load_visible_result_item_ids,
    query_result_records,
    result_file_exists,
    save_result_blacklist_keywords,
    update_item_review_status,
    update_item_status,
)


router = APIRouter(prefix="/api/results", tags=["results"])

DEFAULT_EXPORT_FILENAME = "export.csv"


def _build_download_headers(export_name: str) -> dict[str, str]:
    ascii_name = export_name.encode("ascii", "ignore").decode("ascii")
    if ascii_name != export_name or not ascii_name:
        ascii_name = DEFAULT_EXPORT_FILENAME
    encoded_name = quote(export_name, safe="")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }


def _parse_review_statuses(raw: str | None) -> list[str] | None:
    """逗号分隔的处理状态筛选；为空表示不过滤，出现未知值抛 ValueError（路由映射为 400）。"""
    if raw is None:
        return None
    statuses = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not statuses:
        return None
    unknown = [status for status in statuses if status not in REVIEW_STATUSES]
    if unknown:
        raise ValueError(f"未知的处理状态: {', '.join(unknown)}，可选值: {', '.join(REVIEW_STATUSES)}")
    return statuses


@router.get("/files")
async def get_result_files():
    """获取所有结果文件列表"""
    return {"files": await list_result_filenames()}


@router.get("/files/{filename:path}")
async def download_result_file(filename: str):
    """下载指定的结果文件"""
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not filename.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="只能下载 .jsonl 文件")
    if not await result_file_exists(filename):
        raise HTTPException(status_code=404, detail="文件不存在")
    return Response(
        content=await build_result_ndjson(filename),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/files/{filename:path}")
async def delete_result_file(filename: str):
    """删除指定的结果文件"""
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not filename.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="只能删除 .jsonl 文件")
    deleted_rows = await delete_result_file_records(filename)
    if deleted_rows <= 0:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"message": f"文件 {filename} 已成功删除"}


@router.get("/inbox-count")
async def get_inbox_count():
    """所有结果文件的收件箱数量（可见、推荐且未处理的记录），含按文件分布"""
    return await load_inbox_counts()


@router.get("/{filename}")
async def get_result_file_content(
    filename: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    recommended_only: bool = Query(False),  # 兼容旧参数，等价于 ai_recommended_only
    ai_recommended_only: bool = Query(False),
    keyword_recommended_only: bool = Query(False),
    include_hidden: bool = Query(False),
    sort_by: str = Query("crawl_time"),
    sort_order: str = Query("desc"),
    review_status: str | None = Query(None),  # 逗号分隔：new,contacted,excluded,bought
    recommended: bool | None = Query(None),
):
    """读取指定的 .jsonl 文件内容，支持分页、筛选和排序"""
    if ai_recommended_only and keyword_recommended_only:
        raise HTTPException(status_code=400, detail="AI推荐筛选与关键词推荐筛选不能同时开启。")

    if recommended_only and not ai_recommended_only and not keyword_recommended_only:
        ai_recommended_only = True

    try:
        validate_result_filename(filename)
        review_statuses = _parse_review_statuses(review_status)
        total_items, items = await query_result_records(
            filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
            include_hidden=include_hidden,
            review_statuses=review_statuses,
            recommended=recommended,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取结果文件时出错: {exc}")
    if total_items <= 0 and not await result_file_exists(filename):
        raise HTTPException(status_code=404, detail="结果文件未找到")
    # 价格快照读库是同步 IO，放到线程里，别卡住事件循环（调度器靠它按时触发）
    paginated_results = await asyncio.to_thread(enrich_records_with_price_insight, items, filename)

    return {
        "total_items": total_items,
        "page": page,
        "limit": limit,
        "items": paginated_results
    }


@router.get("/{filename}/insights")
async def get_result_file_insights(filename: str):
    try:
        validate_result_filename(filename)
        keyword = filename.replace("_full_data.jsonl", "")
        visible_item_ids = await asyncio.to_thread(load_visible_result_item_ids, filename)
        return await asyncio.to_thread(
            build_price_history_insights, keyword, visible_item_ids=visible_item_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{filename}/review-summary")
async def get_result_review_summary(filename: str):
    """结果文件可见记录按处理状态分桶：inbox / not_recommended / contacted / excluded / bought / total"""
    try:
        validate_result_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await load_review_summary(filename)


@router.get("/{filename}/export")
async def export_result_file_content(
    filename: str,
    recommended_only: bool = Query(False),
    ai_recommended_only: bool = Query(False),
    keyword_recommended_only: bool = Query(False),
    include_hidden: bool = Query(False),
    sort_by: str = Query("crawl_time"),
    sort_order: str = Query("desc"),
    review_status: str | None = Query(None),
    recommended: bool | None = Query(None),
):
    if ai_recommended_only and keyword_recommended_only:
        raise HTTPException(status_code=400, detail="AI推荐筛选与关键词推荐筛选不能同时开启。")
    if recommended_only and not ai_recommended_only and not keyword_recommended_only:
        ai_recommended_only = True

    try:
        validate_result_filename(filename)
        review_statuses = _parse_review_statuses(review_status)
        results = await load_all_result_records(
            filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            review_statuses=review_statuses,
            recommended=recommended,
        )
        enriched = await asyncio.to_thread(enrich_records_with_price_insight, results, filename)
        csv_text = build_results_csv(enriched)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导出结果文件时出错: {exc}")
    if not results and not await result_file_exists(filename):
        raise HTTPException(status_code=404, detail="结果文件未找到")

    export_name = filename.replace(".jsonl", ".csv")
    headers = _build_download_headers(export_name)
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)


class ItemStatus(str, Enum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    EXPIRED = "expired"


class UpdateStatusRequest(BaseModel):
    status: ItemStatus


class ReviewStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    EXCLUDED = "excluded"
    BOUGHT = "bought"


class UpdateReviewStatusRequest(BaseModel):
    review_status: ReviewStatus


class BlacklistRulesRequest(BaseModel):
    keywords: list[str]


@router.patch("/{filename}/items/{item_id}/status")
async def patch_item_status(filename: str, item_id: str, body: UpdateStatusRequest):
    """更新指定商品的状态（active/hidden/expired）"""
    try:
        validate_result_filename(filename)
        updated = await update_item_status(filename, item_id, body.status.value)
        if not updated:
            raise HTTPException(status_code=404, detail="商品未找到")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": "状态已更新", "status": body.status.value}


@router.patch("/{filename}/items/{item_id}/review")
async def patch_item_review_status(filename: str, item_id: str, body: UpdateReviewStatusRequest):
    """更新指定商品的处理状态（new/contacted/excluded/bought）；设回 new 会清掉处理时间"""
    try:
        validate_result_filename(filename)
        result = await update_item_review_status(filename, item_id, body.review_status.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="商品未找到")
    return {"message": "已更新", **result}


@router.get("/{filename}/blacklist-rules")
async def get_result_blacklist_rules(filename: str):
    try:
        validate_result_filename(filename)
        keywords = await load_result_blacklist_keywords(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"keywords": keywords}


@router.put("/{filename}/blacklist-rules")
async def put_result_blacklist_rules(filename: str, body: BlacklistRulesRequest):
    try:
        validate_result_filename(filename)
        keywords = await save_result_blacklist_keywords(filename, body.keywords)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": "黑名单规则已更新", "keywords": keywords}
