import asyncio

import pytest

from src.domain.models.task import TaskCreate, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import DuplicateTaskNameError, TaskNotFoundError, TaskService


def _service(tmp_path) -> TaskService:
    return TaskService(SqliteTaskRepository(db_path=str(tmp_path / "app.sqlite3"), legacy_config_file=None))


def _create(name: str, keyword: str = "kw") -> TaskCreate:
    return TaskCreate(task_name=name, keyword=keyword, description="d", max_pages=1, personal_only=True)


def test_create_rejects_duplicate_and_blank_names(tmp_path):
    service = _service(tmp_path)

    async def scenario():
        await service.create_task(_create("Sony A7M4"))
        with pytest.raises(DuplicateTaskNameError):
            await service.create_task(_create("  Sony A7M4 "))
        with pytest.raises(ValueError):
            await service.create_task(_create("   "))
        assert len(await service.get_all_tasks()) == 1

    asyncio.run(scenario())


def test_rename_rejects_duplicate_but_allows_same_name_and_missing_task_is_404_class(tmp_path):
    service = _service(tmp_path)

    async def scenario():
        first = await service.create_task(_create("A"))
        second = await service.create_task(_create("B"))
        with pytest.raises(DuplicateTaskNameError):
            await service.update_task(second.id, TaskUpdate(task_name="A"))
        # 改成自己的名字 / 只改别的字段都不受影响
        await service.update_task(first.id, TaskUpdate(task_name="A", description="x"))
        await service.update_task(second.id, TaskUpdate(task_name="C"))
        with pytest.raises(TaskNotFoundError):
            await service.update_task(99, TaskUpdate(description="x"))

    asyncio.run(scenario())


def test_api_maps_duplicate_name_to_400_and_missing_task_to_404(api_client, sample_task_payload):
    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200

    duplicate = api_client.post("/api/tasks/", json=sample_task_payload)
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.json()["detail"]

    second = dict(sample_task_payload, task_name="Other")
    assert api_client.post("/api/tasks/", json=second).status_code == 200
    rename = api_client.patch("/api/tasks/1", json={"task_name": sample_task_payload["task_name"]})
    assert rename.status_code == 400

    missing = api_client.patch("/api/tasks/42", json={"description": "x"})
    assert missing.status_code == 404

    generate_dup = api_client.post(
        "/api/tasks/generate",
        json={
            "task_name": sample_task_payload["task_name"],
            "keyword": "sony",
            "description": "",
            "decision_mode": "keyword",
            "keyword_rules": ["a7m4"],
        },
    )
    assert generate_dup.status_code == 400
