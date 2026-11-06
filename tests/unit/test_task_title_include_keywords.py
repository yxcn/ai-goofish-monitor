import sqlite3

from src.domain.models.task import Task, TaskGenerateRequest, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository


def _base_task(**overrides) -> Task:
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


def test_task_defaults_to_no_title_filter():
    assert _base_task().title_include_keywords == []


def test_task_normalizes_title_keywords_from_string_or_list():
    assert _base_task(title_include_keywords="T14, t14 ,X1").title_include_keywords == ["T14", "X1"]
    assert _base_task(title_include_keywords=["T14", "", "T14"]).title_include_keywords == ["T14"]


def test_task_update_distinguishes_untouched_from_cleared():
    untouched = TaskUpdate(max_price="2000")
    assert untouched.title_include_keywords is None
    assert "title_include_keywords" not in untouched.model_dump(exclude_unset=True)

    cleared = TaskUpdate(title_include_keywords=[])
    assert cleared.title_include_keywords == []

    task = _base_task(title_include_keywords=["T14"])
    assert task.apply_update(untouched).title_include_keywords == ["T14"]
    assert task.apply_update(cleared).title_include_keywords == []


def test_generate_request_accepts_title_keywords():
    req = TaskGenerateRequest(task_name="T14", keyword="ThinkPad T14", description="自用", title_include_keywords="T14")
    assert req.title_include_keywords == ["T14"]


def test_repository_round_trips_title_keywords(tmp_path):
    db_path = str(tmp_path / "app.sqlite3")
    repository = SqliteTaskRepository(db_path=db_path, legacy_config_file=None)

    saved = repository._save_sync(_base_task(title_include_keywords=["T14", "T 14"]))
    loaded = repository._find_by_id_sync(saved.id)

    assert loaded is not None
    assert loaded.title_include_keywords == ["T14", "T 14"]


def test_repository_migrates_database_created_before_title_filter(tmp_path):
    db_path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            task_name TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            description TEXT,
            analyze_images INTEGER NOT NULL,
            max_pages INTEGER NOT NULL,
            personal_only INTEGER NOT NULL,
            min_price TEXT,
            max_price TEXT,
            cron TEXT,
            ai_prompt_base_file TEXT NOT NULL,
            ai_prompt_criteria_file TEXT NOT NULL,
            account_state_file TEXT,
            account_strategy TEXT NOT NULL,
            free_shipping INTEGER NOT NULL,
            new_publish_option TEXT,
            region TEXT,
            decision_mode TEXT NOT NULL,
            keyword_rules_json TEXT NOT NULL,
            is_running INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tasks (
            id, task_name, enabled, keyword, description, analyze_images, max_pages,
            personal_only, ai_prompt_base_file, ai_prompt_criteria_file, account_strategy,
            free_shipping, decision_mode, keyword_rules_json, is_running
        ) VALUES (0, 'T14', 1, 'ThinkPad T14', '自用', 1, 1, 1, 'prompts/base_prompt.txt',
                  'prompts/t14_criteria.txt', 'auto', 1, 'ai', '[]', 0)
        """
    )
    conn.commit()
    conn.close()

    repository = SqliteTaskRepository(db_path=db_path, legacy_config_file=None)
    tasks = repository._find_all_sync()

    assert [task.task_name for task in tasks] == ["T14"]
    assert tasks[0].title_include_keywords == []

    updated = repository._save_sync(tasks[0].model_copy(update={"title_include_keywords": ["T14"]}))
    assert repository._find_by_id_sync(updated.id).title_include_keywords == ["T14"]
