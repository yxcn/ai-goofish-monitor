"""
任务管理服务
封装任务相关的业务逻辑
"""
from typing import List, Optional
from src.domain.models.task import Task, TaskCreate, TaskUpdate
from src.domain.repositories.task_repository import TaskRepository


class TaskNotFoundError(ValueError):
    """任务不存在（路由映射为 404；其他 ValueError 是请求数据问题，映射为 400）"""


class DuplicateTaskNameError(ValueError):
    """任务名已被占用。子进程按任务定位配置，名称重复会让重名任务永远跑第一个的配置。"""


def _normalize_task_name(task_name: str | None) -> str:
    return str(task_name or "").strip()


class TaskService:
    """任务管理服务"""

    def __init__(self, repository: TaskRepository):
        self.repository = repository

    async def get_all_tasks(self) -> List[Task]:
        """获取所有任务"""
        return await self.repository.find_all()

    async def get_task(self, task_id: int) -> Optional[Task]:
        """获取单个任务"""
        return await self.repository.find_by_id(task_id)

    async def ensure_task_name_available(
        self,
        task_name: str,
        *,
        exclude_task_id: int | None = None,
    ) -> None:
        """任务名必须非空且在所有任务里唯一（排除自身）。"""
        normalized = _normalize_task_name(task_name)
        if not normalized:
            raise ValueError("任务名称不能为空")
        for task in await self.repository.find_all():
            if exclude_task_id is not None and task.id == exclude_task_id:
                continue
            if _normalize_task_name(task.task_name) == normalized:
                raise DuplicateTaskNameError(f"任务名称 '{normalized}' 已存在，请换一个名称")

    async def create_task(self, task_create: TaskCreate) -> Task:
        """创建新任务"""
        await self.ensure_task_name_available(task_create.task_name)
        task = Task(**task_create.model_dump(), is_running=False)
        return await self.repository.save(task)

    async def update_task(self, task_id: int, task_update: TaskUpdate) -> Task:
        """更新任务"""
        task = await self.repository.find_by_id(task_id)
        if not task:
            raise TaskNotFoundError(f"任务 {task_id} 不存在")

        if task_update.task_name is not None and (
            _normalize_task_name(task_update.task_name) != _normalize_task_name(task.task_name)
        ):
            await self.ensure_task_name_available(task_update.task_name, exclude_task_id=task_id)

        updated_task = task.apply_update(task_update)
        return await self.repository.save(updated_task)

    async def delete_task(self, task_id: int) -> bool:
        """删除任务"""
        return await self.repository.delete(task_id)

    async def update_task_status(self, task_id: int, is_running: bool) -> Task:
        """更新任务运行状态：只改 is_running 一列，不整行覆盖。"""
        update_running_status = getattr(self.repository, "update_running_status", None)
        if update_running_status is None:
            return await self.update_task(task_id, TaskUpdate(is_running=is_running))

        updated = await update_running_status(task_id, is_running)
        if not updated:
            raise TaskNotFoundError(f"任务 {task_id} 不存在")
        task = await self.repository.find_by_id(task_id)
        if not task:
            raise TaskNotFoundError(f"任务 {task_id} 不存在")
        return task
