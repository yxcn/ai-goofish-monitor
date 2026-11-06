import asyncio
import json
import sqlite3
import threading

from src.domain.models.task import Task, TaskCreate, TaskUpdate
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import (
    SqliteTaskRepository,
    find_task_by_id_sync,
    find_task_by_name_sync,
)
from src.services.task_service import TaskNotFoundError, TaskService


def _task(**overrides) -> Task:
    payload = dict(
        task_name="T14",
        enabled=True,
        keyword="ThinkPad T14",
        description="自用",
        max_pages=1,
        personal_only=True,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/t14_criteria.txt",
    )
    payload.update(overrides)
    return Task(**payload)


def _repo(tmp_path, legacy_config_file=None) -> SqliteTaskRepository:
    return SqliteTaskRepository(db_path=str(tmp_path / "app.sqlite3"), legacy_config_file=legacy_config_file)


def test_new_tasks_get_sequential_ids_starting_at_zero(tmp_path):
    repo = _repo(tmp_path)
    first = repo._save_sync(_task(task_name="A"))
    second = repo._save_sync(_task(task_name="B"))
    assert (first.id, second.id) == (0, 1)


def test_concurrent_creates_never_share_an_id(tmp_path):
    repo = _repo(tmp_path)
    repo._save_sync(_task(task_name="seed"))
    ids: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker(index: int):
        try:
            barrier.wait()
            ids.append(repo._save_sync(_task(task_name=f"T{index}")).id)
        except Exception as exc:  # pragma: no cover - 只为暴露失败原因
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert len(repo._find_all_sync()) == 7


def test_status_update_touches_only_is_running(tmp_path):
    repo = _repo(tmp_path)
    saved = repo._save_sync(_task())

    # 另一处先改了 description；随后的状态更新不能把它冲掉（老实现整行 REPLACE 会）
    repo._save_sync(saved.model_copy(update={"description": "改过的描述"}))
    assert repo._update_running_status_sync(saved.id, True) is True

    loaded = repo._find_by_id_sync(saved.id)
    assert loaded.is_running is True
    assert loaded.description == "改过的描述"
    assert repo._update_running_status_sync(999, True) is False


def test_task_service_status_update_uses_single_column_path(tmp_path):
    repo = _repo(tmp_path)
    service = TaskService(repo)

    async def scenario():
        created = await service.create_task(
            TaskCreate(task_name="S", keyword="s", description="d", max_pages=1, personal_only=True)
        )
        await service.update_task(created.id, TaskUpdate(description="新描述"))
        task = await service.update_task_status(created.id, True)
        assert task.is_running is True and task.description == "新描述"
        try:
            await service.update_task_status(42, True)
        except TaskNotFoundError:
            return True
        return False

    assert asyncio.run(scenario()) is True


def test_update_of_existing_task_does_not_replace_row(tmp_path):
    repo = _repo(tmp_path)
    saved = repo._save_sync(_task())
    repo._save_sync(saved.model_copy(update={"keyword": "ThinkPad T14s"}))
    rows = repo._find_all_sync()
    assert len(rows) == 1 and rows[0].keyword == "ThinkPad T14s" and rows[0].id == saved.id


def test_save_with_explicit_unknown_id_inserts(tmp_path):
    repo = _repo(tmp_path)
    saved = repo._save_sync(_task(id=5))
    assert saved.id == 5
    assert repo._find_by_id_sync(5) is not None


def test_find_by_id_and_by_name_helpers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    repo = _repo(tmp_path)
    first = repo._save_sync(_task(task_name="dup", keyword="first"))
    second = repo._save_sync(_task(task_name="dup", keyword="second"))

    assert find_task_by_id_sync(second.id).keyword == "second"
    assert find_task_by_name_sync("dup").id == first.id
    assert find_task_by_id_sync(99) is None


def test_corrupt_row_is_skipped_instead_of_breaking_find_all(tmp_path, capsys):
    repo = _repo(tmp_path)
    good = repo._save_sync(_task(task_name="good"))
    with sqlite_connection(repo.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET decision_mode = 'weird', keyword_rules_json = 'not json' WHERE id = ?",
            (good.id,),
        )
        conn.commit()
    repo._save_sync(_task(task_name="still fine"))

    tasks = repo._find_all_sync()
    assert [task.task_name for task in tasks] == ["still fine"]
    assert repo._find_by_id_sync(good.id) is None
    assert "跳过无法解析的任务记录" in capsys.readouterr().out


def test_legacy_config_import_skips_invalid_entries(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "task_name": "ok",
                    "keyword": "sony a7m4",
                    "description": "好机",
                    "max_pages": 2,
                    "personal_only": True,
                    "keyword_rule_groups": [{"include_keywords": ["a7m4", "验货宝"]}],
                },
                {"task_name": "broken", "keyword": "x", "max_pages": "many", "personal_only": True},
                "not-an-object",
                {"task_name": "escape", "keyword": "x", "max_pages": 1, "personal_only": True,
                 "ai_prompt_criteria_file": "../../etc/passwd"},
                {"task_name": "ok2", "keyword": "ipad", "description": "d", "max_pages": 1, "personal_only": False},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repo = _repo(tmp_path, legacy_config_file=str(config))

    tasks = repo._find_all_sync()

    assert [(task.id, task.task_name) for task in tasks] == [(0, "ok"), (4, "ok2")]
    assert tasks[0].keyword_rules == ["a7m4", "验货宝"]
    out = capsys.readouterr().out
    assert "broken" in out and "escape" in out
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
