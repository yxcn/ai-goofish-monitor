import pytest

from src.domain.models.task import Task, TaskGenerateRequest, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository


def _task(**overrides) -> Task:
    payload = dict(
        id=0,
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


def test_new_list_fields_default_to_empty():
    task = _task()
    assert task.extra_keywords == []
    assert task.title_exclude_keywords == []
    assert task.attribute_rules == []


def test_fields_are_normalized_from_strings():
    task = _task(
        extra_keywords="ThinkPad T14s, ThinkBook 14,thinkpad t14s",
        title_exclude_keywords="主板，键盘",
        attribute_rules="分类 = 笔记本电脑\n内存容量>=16GB\n\n",
    )
    assert task.extra_keywords == ["ThinkPad T14s", "ThinkBook 14"]
    assert task.title_exclude_keywords == ["主板", "键盘"]
    assert task.attribute_rules == ["分类 = 笔记本电脑", "内存容量 >= 16GB"]


def test_invalid_attribute_rule_is_rejected_with_the_offending_line():
    with pytest.raises(ValueError, match="内存容量 很大"):
        _task(attribute_rules=["内存容量 很大"])
    with pytest.raises(ValueError):
        TaskGenerateRequest(task_name="T14", keyword="T14", description="x", attribute_rules="内存容量 >= 很大")


def test_update_leaves_untouched_fields_alone():
    task = _task(extra_keywords=["T14s"], attribute_rules=["分类 = 笔记本电脑"])
    update = TaskUpdate(title_exclude_keywords="主板")
    assert update.extra_keywords is None and update.attribute_rules is None

    updated = task.apply_update(update)
    assert updated.extra_keywords == ["T14s"]
    assert updated.attribute_rules == ["分类 = 笔记本电脑"]
    assert updated.title_exclude_keywords == ["主板"]
    assert task.apply_update(TaskUpdate(attribute_rules=[])).attribute_rules == []


def test_repository_round_trips_and_migrates_new_columns(tmp_path):
    db_path = str(tmp_path / "app.sqlite3")
    repo = SqliteTaskRepository(db_path=db_path, legacy_config_file=None)
    saved = repo._save_sync(
        _task(
            extra_keywords=["ThinkPad T14s", "MateBook 14"],
            title_exclude_keywords=["主板"],
            attribute_rules=["内存容量 >= 16GB"],
        )
    )
    loaded = repo._find_by_id_sync(saved.id)
    assert loaded.extra_keywords == ["ThinkPad T14s", "MateBook 14"]
    assert loaded.title_exclude_keywords == ["主板"]
    assert loaded.attribute_rules == ["内存容量 >= 16GB"]

    # 模拟只有 title_include_keywords_json 的上一版库：删掉新列后重新打开应自动补列
    import sqlite3

    conn = sqlite3.connect(db_path)
    for column in ("extra_keywords_json", "title_exclude_keywords_json", "attribute_rules_json"):
        conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    conn.commit()
    conn.close()

    reopened = SqliteTaskRepository(db_path=db_path, legacy_config_file=None)._find_by_id_sync(saved.id)
    assert reopened.extra_keywords == []
    assert reopened.attribute_rules == []
    assert reopened.title_include_keywords == []
