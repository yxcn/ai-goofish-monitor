"""
任务生成作业执行器
"""
import os
from uuid import uuid4

import aiofiles

from src.domain.models.task import TaskCreate, TaskGenerateRequest
from src.prompt_utils import generate_criteria
from src.services.scheduler_service import SchedulerService
from src.services.task_generation_service import TaskGenerationService
from src.services.task_service import TaskService

PROMPTS_DIR = "prompts"
# 生成分析标准时给 AI 参考的模板（随仓库分发），生成结果绝不能写到这个路径上
REFERENCE_CRITERIA_FILE = f"{PROMPTS_DIR}/macbook_criteria.txt"


def _keyword_slug(keyword: str) -> str:
    slug = "".join(
        char for char in str(keyword or "").lower().replace(" ", "_")
        if char.isalnum() or char in "_-"
    ).strip("_-")
    return slug or "task"


def build_criteria_filename(keyword: str, task_id: int | None = None) -> str:
    """分析标准文件名带任务维度：同关键词的多个任务各用各的文件，互不覆盖。

    task_id 已知时用 `{slug}_{task_id}_criteria.txt`；创建流程里任务还没落库，用 8 位随机后缀。
    """
    suffix = str(task_id) if task_id is not None else uuid4().hex[:8]
    return f"{PROMPTS_DIR}/{_keyword_slug(keyword)}_{suffix}_criteria.txt"


def is_reference_criteria_file(path: str) -> bool:
    # 绝对路径、./ 前缀都归一到同一个绝对路径再比
    return os.path.abspath(path) == os.path.abspath(REFERENCE_CRITERIA_FILE)


def build_task_create(req: TaskGenerateRequest, criteria_file: str) -> TaskCreate:
    return TaskCreate(
        task_name=req.task_name,
        enabled=True,
        keyword=req.keyword,
        description=req.description or "",
        analyze_images=req.analyze_images,
        max_pages=req.max_pages,
        personal_only=req.personal_only,
        min_price=req.min_price,
        max_price=req.max_price,
        cron=req.cron,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file=criteria_file,
        account_state_file=req.account_state_file,
        account_strategy=req.account_strategy,
        free_shipping=req.free_shipping,
        new_publish_option=req.new_publish_option,
        region=req.region,
        decision_mode=req.decision_mode or "ai",
        keyword_rules=req.keyword_rules,
        extra_keywords=req.extra_keywords,
        title_include_keywords=req.title_include_keywords,
        title_exclude_keywords=req.title_exclude_keywords,
        attribute_rules=req.attribute_rules,
    )


async def save_generated_criteria(output_filename: str, generated_criteria: str) -> None:
    if not generated_criteria or not generated_criteria.strip():
        raise RuntimeError("AI 未能生成分析标准，返回内容为空。")
    if is_reference_criteria_file(output_filename):
        raise RuntimeError(f"拒绝覆盖参考模板 {REFERENCE_CRITERIA_FILE}，请换一个关键词或任务。")

    os.makedirs(PROMPTS_DIR, exist_ok=True)
    async with aiofiles.open(output_filename, "w", encoding="utf-8") as file:
        await file.write(generated_criteria)


def cleanup_generated_criteria(output_filename: str, *, existed_before: bool) -> None:
    """失败时只删除本次新建的文件；原本就存在的文件（别的任务在用）和参考模板一律不动。"""
    if existed_before or is_reference_criteria_file(output_filename):
        return
    if os.path.exists(output_filename):
        try:
            os.remove(output_filename)
        except OSError as exc:
            print(f"清理生成失败的分析标准文件 {output_filename} 出错: {exc}")


async def reload_scheduler(
    task_service: TaskService,
    scheduler_service: SchedulerService,
) -> None:
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)


async def advance_job(
    generation_service: TaskGenerationService,
    job_id: str,
    step_key: str,
    message: str,
) -> None:
    await generation_service.advance(job_id, step_key, message)


async def run_ai_generation_job(
    *,
    job_id: str,
    req: TaskGenerateRequest,
    task_service: TaskService,
    scheduler_service: SchedulerService,
    generation_service: TaskGenerationService,
) -> None:
    output_filename = build_criteria_filename(req.keyword)
    existed_before = os.path.exists(output_filename)
    try:
        await advance_job(
            generation_service,
            job_id,
            "prepare",
            "已接收请求，开始准备分析标准。",
        )

        async def report_progress(step_key: str, message: str) -> None:
            await advance_job(generation_service, job_id, step_key, message)

        generated_criteria = await generate_criteria(
            user_description=req.description or "",
            reference_file_path=REFERENCE_CRITERIA_FILE,
            progress_callback=report_progress,
        )

        await advance_job(
            generation_service,
            job_id,
            "persist",
            f"正在保存分析标准到 {output_filename}。",
        )
        await save_generated_criteria(output_filename, generated_criteria)

        await advance_job(
            generation_service,
            job_id,
            "task",
            "分析标准已生成，正在创建任务记录。",
        )
        task = await task_service.create_task(build_task_create(req, output_filename))
        await reload_scheduler(task_service, scheduler_service)
        await generation_service.complete(job_id, task, f"任务“{req.task_name}”创建完成。")
    except Exception as exc:
        cleanup_generated_criteria(output_filename, existed_before=existed_before)
        await generation_service.fail(job_id, f"AI 任务生成失败: {exc}")
