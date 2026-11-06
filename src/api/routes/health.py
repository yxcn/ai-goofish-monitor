"""
爬虫健康总览路由
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_task_service
from src.failure_guard import FailureGuard
from src.services.crawl_health_service import build_crawler_health, load_recent_runs
from src.services.task_service import TaskService


router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/crawler")
async def get_crawler_health(task_service: TaskService = Depends(get_task_service)):
    """每个任务最近一轮抓取的健康评估 + 熔断器状态，供概览页横幅和任务列表徽标使用。"""
    try:
        tasks = await task_service.get_all_tasks()
        guard_tasks = await asyncio.to_thread(FailureGuard().snapshot)
        return await asyncio.to_thread(build_crawler_health, tasks, guard_tasks)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"加载爬虫健康状态失败: {exc}")


@router.get("/crawler/runs/{task_name}")
async def get_crawler_runs(task_name: str, limit: int = 20):
    """某个任务最近若干轮的运行摘要（排查用）。"""
    limit = max(1, min(int(limit), 100))
    return {"task_name": task_name, "runs": await asyncio.to_thread(load_recent_runs, task_name, limit=limit)}
