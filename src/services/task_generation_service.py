"""
任务生成作业服务
"""
import asyncio
from copy import deepcopy
import threading
import time
from typing import Awaitable, Callable, Dict, Iterable, Optional
from uuid import uuid4

from src.domain.models.task import Task
from src.domain.models.task_generation import TaskGenerationJob, TaskGenerationStep

DEFAULT_GENERATION_STEPS: tuple[tuple[str, str], ...] = (
    ("prepare", "接收创建请求"),
    ("reference", "读取参考文件"),
    ("prompt", "构建提示词"),
    ("llm", "调用 AI 生成标准"),
    ("persist", "保存分析标准"),
    ("task", "创建任务记录"),
)

# 已完成/失败的作业保留给前端轮询用，但不能只增不删：最多留这么多个、最长留这么久
MAX_FINISHED_JOBS = 50
FINISHED_JOB_TTL_SECONDS = 3600


class TaskGenerationService:
    """管理 AI 任务生成的后台作业状态"""

    def __init__(
        self,
        step_specs: Iterable[tuple[str, str]] = DEFAULT_GENERATION_STEPS,
        *,
        max_finished_jobs: int = MAX_FINISHED_JOBS,
        finished_job_ttl_seconds: float = FINISHED_JOB_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._step_specs = tuple(step_specs)
        self._jobs: Dict[str, TaskGenerationJob] = {}
        self._lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._max_finished_jobs = max(0, int(max_finished_jobs))
        self._finished_job_ttl_seconds = float(finished_job_ttl_seconds)
        self._clock = clock
        # job_id -> 完成/失败时刻（clock 的单位），只记终态作业；运行中的永不清理
        self._finished_at: Dict[str, float] = {}

    async def create_job(self, task_name: str) -> TaskGenerationJob:
        job = TaskGenerationJob(
            job_id=uuid4().hex,
            task_name=task_name,
            steps=[
                TaskGenerationStep(key=key, label=label)
                for key, label in self._step_specs
            ],
        )
        with self._lock:
            self._prune_finished_jobs()
            self._jobs[job.job_id] = job
            return deepcopy(job)

    async def get_job(self, job_id: str) -> Optional[TaskGenerationJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return deepcopy(job)

    def track(self, coroutine: Awaitable[None]) -> None:
        thread: Optional[threading.Thread] = None

        def runner() -> None:
            try:
                asyncio.run(coroutine)
            finally:
                if thread is None:
                    return
                with self._lock:
                    self._workers.discard(thread)

        thread = threading.Thread(target=runner, daemon=True)
        with self._lock:
            self._workers.add(thread)
        thread.start()

    async def advance(self, job_id: str, step_key: str, message: str) -> TaskGenerationJob:
        with self._lock:
            job = self._require_job(job_id)
            target_index = self._find_step_index(job, step_key)
            job.status = "running"
            job.current_step = step_key
            job.message = message
            for index, step in enumerate(job.steps):
                if step.status == "failed":
                    continue
                if index < target_index:
                    step.status = "completed"
                elif index == target_index:
                    step.status = "running"
                    step.message = message
                elif step.status != "pending":
                    step.status = "pending"
                    step.message = ""
            return deepcopy(job)

    async def complete(self, job_id: str, task: Task, message: str) -> TaskGenerationJob:
        with self._lock:
            job = self._require_job(job_id)
            job.status = "completed"
            job.current_step = None
            job.message = message
            job.error = None
            job.task = task
            for step in job.steps:
                if step.status != "failed":
                    step.status = "completed"
            self._mark_finished(job_id)
            return deepcopy(job)

    async def fail(
        self,
        job_id: str,
        error: str,
        step_key: Optional[str] = None,
    ) -> TaskGenerationJob:
        with self._lock:
            job = self._require_job(job_id)
            failed_step = step_key or job.current_step
            job.status = "failed"
            job.error = error
            job.message = error
            job.current_step = failed_step
            if failed_step:
                step = self._find_step(job, failed_step)
                if step:
                    step.status = "failed"
                    step.message = error
            self._mark_finished(job_id)
            return deepcopy(job)

    def _mark_finished(self, job_id: str) -> None:
        """调用方须持有 self._lock。"""
        self._finished_at[job_id] = self._clock()
        self._prune_finished_jobs()

    def _prune_finished_jobs(self) -> None:
        """调用方须持有 self._lock。按 TTL 和数量上限清理终态作业。"""
        if not self._finished_at:
            return
        now = self._clock()
        expired = [
            job_id
            for job_id, finished_at in self._finished_at.items()
            if now - finished_at > self._finished_job_ttl_seconds
        ]
        for job_id in expired:
            self._drop_job(job_id)
        overflow = len(self._finished_at) - self._max_finished_jobs
        if overflow > 0:
            oldest_first = sorted(self._finished_at, key=self._finished_at.__getitem__)
            for job_id in oldest_first[:overflow]:
                self._drop_job(job_id)

    def _drop_job(self, job_id: str) -> None:
        self._finished_at.pop(job_id, None)
        self._jobs.pop(job_id, None)

    def _require_job(self, job_id: str) -> TaskGenerationJob:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"任务生成作业不存在: {job_id}")
        return job

    def _find_step(self, job: TaskGenerationJob, step_key: str) -> Optional[TaskGenerationStep]:
        for step in job.steps:
            if step.key == step_key:
                return step
        return None

    def _find_step_index(self, job: TaskGenerationJob, step_key: str) -> int:
        for index, step in enumerate(job.steps):
            if step.key == step_key:
                return index
        raise KeyError(f"未知的任务生成步骤: {step_key}")
