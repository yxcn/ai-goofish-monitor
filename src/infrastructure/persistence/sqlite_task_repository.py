"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import retry_on_lock, sqlite_connection


# 以 JSON 数组存储的列表型字段：模型字段名 -> 列名为 <字段名>_json
JSON_LIST_FIELDS = (
    "title_include_keywords",
    "extra_keywords",
    "title_exclude_keywords",
    "attribute_rules",
)

TASK_COLUMNS = (
    "task_name", "enabled", "keyword", "description", "analyze_images",
    "max_pages", "personal_only", "min_price", "max_price", "cron",
    "ai_prompt_base_file", "ai_prompt_criteria_file", "account_state_file",
    "account_strategy", "free_shipping", "new_publish_option", "region",
    "decision_mode", "keyword_rules_json", "title_include_keywords_json",
    "extra_keywords_json", "title_exclude_keywords_json", "attribute_rules_json",
    "is_running",
)


def _row_to_task(row) -> Task | None:
    """一行转 Task；坏行打印并返回 None，不让整表查询抛错。"""
    payload = dict(row)
    try:
        payload["enabled"] = bool(payload["enabled"])
        payload["analyze_images"] = bool(payload["analyze_images"])
        payload["personal_only"] = bool(payload["personal_only"])
        payload["free_shipping"] = bool(payload["free_shipping"])
        payload["is_running"] = bool(payload["is_running"])
        payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
        for field in JSON_LIST_FIELDS:
            payload[field] = json.loads(payload.pop(f"{field}_json", None) or "[]")
        return Task(**payload)
    except Exception as exc:
        task_id = payload.get("id")
        name = payload.get("task_name") or "?"
        print(f"[tasks] 跳过无法解析的任务记录 id={task_id} name='{name}'：{exc}")
        return None


def _rows_to_tasks(rows) -> List[Task]:
    tasks: List[Task] = []
    for row in rows:
        task = _row_to_task(row)
        if task is not None:
            tasks.append(task)
    return tasks


def find_task_by_name_sync(task_name: str) -> Task | None:
    """按名称查找（任务名不唯一时返回 id 最小的那个，仅用于兼容手工命令行）。"""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC LIMIT 1",
            (task_name,),
        ).fetchone()
    return _row_to_task(row) if row else None


def find_task_by_id_sync(task_id: int) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def find_by_name(self, task_name: str) -> List[Task]:
        return await asyncio.to_thread(self._find_by_name_sync, task_name)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    async def update_running_status(self, task_id: int, is_running: bool) -> bool:
        return await asyncio.to_thread(self._update_running_status_sync, task_id, is_running)

    def _bootstrap(self) -> None:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )

    def _find_all_sync(self) -> List[Task]:
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return _rows_to_tasks(rows)

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _find_by_name_sync(self, task_name: str) -> List[Task]:
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC",
                (task_name,),
            ).fetchall()
        return _rows_to_tasks(rows)

    @retry_on_lock
    def _save_sync(self, task: Task) -> Task:
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            # BEGIN IMMEDIATE 先拿写锁，取号 + 插入在同一事务里，两个并发创建不会拿到同一个 id
            conn.execute("BEGIN IMMEDIATE")
            try:
                task_id = task.id
                if task_id is None:
                    task_id = self._next_task_id(conn)
                    self._insert(conn, task_id, task)
                else:
                    cursor = self._update(conn, task_id, task)
                    if cursor.rowcount == 0:
                        self._insert(conn, task_id, task)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return task.model_copy(update={"id": task_id})

    def _insert(self, conn, task_id: int, task: Task) -> None:
        payload = self._task_values(task.model_copy(update={"id": task_id}))
        columns = ("id",) + TASK_COLUMNS
        conn.execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})",
            payload,
        )

    def _update(self, conn, task_id: int, task: Task):
        payload = self._task_values(task.model_copy(update={"id": task_id}))
        assignments = ", ".join(f"{column} = :{column}" for column in TASK_COLUMNS)
        return conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", payload)

    @retry_on_lock
    def _update_running_status_sync(self, task_id: int, is_running: bool) -> bool:
        """只改 is_running 一列，不走整行覆盖，避免把并发中的其他字段更新吃掉。"""
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE tasks SET is_running = ? WHERE id = ?",
                (int(bool(is_running)), task_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    @retry_on_lock
    def _delete_sync(self, task_id: int) -> bool:
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        return cursor.rowcount > 0

    def _next_task_id(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM tasks").fetchone()
        return int(row["max_id"]) + 1

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values["enabled"] = int(task.enabled)
        values["analyze_images"] = int(task.analyze_images)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        values["keyword_rules_json"] = json.dumps(task.keyword_rules or [], ensure_ascii=False)
        values.pop("keyword_rules", None)
        for field in JSON_LIST_FIELDS:
            values[f"{field}_json"] = json.dumps(getattr(task, field) or [], ensure_ascii=False)
            values.pop(field, None)
        return values
