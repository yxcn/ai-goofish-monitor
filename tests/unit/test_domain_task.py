import pytest
from pydantic import ValidationError
from src.domain.models.task import Task, TaskGenerateRequest, TaskUpdate


def test_task_can_start_and_stop():
    task = Task(
        id=1,
        task_name="Sony A7M4",
        enabled=True,
        keyword="sony a7m4",
        description="body",
        max_pages=2,
        personal_only=True,
        min_price=None,
        max_price=None,
        cron=None,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/sony_a7m4_criteria.txt",
        is_running=False,
    )

    assert task.can_start() is True
    assert task.can_stop() is False

    running = task.model_copy(update={"is_running": True})
    assert running.can_start() is False
    assert running.can_stop() is True


def test_task_apply_update():
    task = Task(
        id=1,
        task_name="Sony A7M4",
        enabled=True,
        keyword="sony a7m4",
        description="body",
        max_pages=2,
        personal_only=True,
        min_price=None,
        max_price=None,
        cron=None,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/sony_a7m4_criteria.txt",
        is_running=False,
    )

    update = TaskUpdate(enabled=False, max_pages=5)
    updated = task.apply_update(update)

    assert updated.enabled is False
    assert updated.max_pages == 5
    assert updated.task_name == task.task_name


def test_legacy_keyword_groups_are_flattened_to_keyword_rules():
    task = Task(
        id=1,
        task_name="Sony A7M4",
        enabled=True,
        keyword="sony a7m4",
        description="body",
        max_pages=2,
        personal_only=True,
        min_price=None,
        max_price=None,
        cron=None,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/sony_a7m4_criteria.txt",
        decision_mode="keyword",
        keyword_rule_groups=[
            {"name": "组1", "include_keywords": ["a7m4", "验货宝"], "exclude_keywords": ["瑕疵"]},
            {"name": "组2", "include_keywords": ["全画幅", "a7m4"], "exclude_keywords": ["拆修"]},
        ],
        is_running=False,
    )

    assert task.keyword_rules == ["a7m4", "验货宝", "全画幅"]


def test_generate_request_accepts_legacy_group_payload():
    req = TaskGenerateRequest(
        task_name="legacy",
        keyword="sony a7m4",
        description="",
        decision_mode="keyword",
        keyword_rule_groups=[{"include_keywords": ["a7m4", "验货宝"], "exclude_keywords": ["瑕疵"]}],
    )
    assert req.keyword_rules == ["a7m4", "验货宝"]


def test_generate_request_enables_image_analysis_by_default():
    req = TaskGenerateRequest(
        task_name="Sony A7M4",
        keyword="sony a7m4",
        description="只看机身成色和卖家信用。",
        decision_mode="ai",
    )
    assert req.analyze_images is True


def test_generate_request_infers_fixed_account_strategy_from_state_file():
    req = TaskGenerateRequest(
        task_name="Sony A7M4",
        keyword="sony a7m4",
        description="只看机身成色和卖家信用。",
        decision_mode="ai",
        account_state_file="state/acc_1.json",
    )

    assert req.account_strategy == "fixed"


def test_generate_request_requires_state_file_for_fixed_account_strategy():
    try:
        TaskGenerateRequest(
            task_name="Sony A7M4",
            keyword="sony a7m4",
            description="只看机身成色和卖家信用。",
            decision_mode="ai",
            account_strategy="fixed",
        )
    except ValueError as exc:
        assert "固定账号模式下必须选择账号" in str(exc)
        return

    raise AssertionError("固定账号模式应要求 account_state_file")


def _full_task(**overrides):
    payload = dict(
        id=1,
        task_name="T14",
        enabled=True,
        keyword="thinkpad t14",
        max_pages=1,
        personal_only=True,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/t14_criteria.txt",
        account_strategy="fixed",
        account_state_file="state/yx_1369.json",
    )
    payload.update(overrides)
    return Task(**payload)


def test_partial_update_keeps_account_binding():
    # 生命周期钩子每次启动/结束只更新 is_running，之前会把账号绑定抹成 auto/None
    task = _full_task()
    assert TaskUpdate(is_running=True).model_dump(exclude_unset=True) == {"is_running": True}

    updated = task.apply_update(TaskUpdate(is_running=True))
    assert (updated.account_strategy, updated.account_state_file) == ("fixed", "state/yx_1369.json")

    updated = task.apply_update(TaskUpdate(enabled=False))
    assert (updated.account_strategy, updated.account_state_file) == ("fixed", "state/yx_1369.json")


def test_update_with_account_fields_still_normalizes_them():
    task = _full_task()
    cleared = task.apply_update(TaskUpdate(account_state_file="", account_strategy="fixed"))
    assert cleared.account_state_file is None
    assert cleared.account_strategy == "fixed"

    derived = task.apply_update(TaskUpdate(account_state_file="state/other.json", account_strategy=""))
    assert (derived.account_strategy, derived.account_state_file) == ("fixed", "state/other.json")

    only_strategy = task.apply_update(TaskUpdate(account_strategy="rotate"))
    assert (only_strategy.account_strategy, only_strategy.account_state_file) == ("rotate", "state/yx_1369.json")


@pytest.mark.parametrize(
    "field, value",
    [
        ("ai_prompt_criteria_file", "/app/.env"),
        ("ai_prompt_criteria_file", "../.env"),
        ("ai_prompt_criteria_file", "prompts/../.env"),
        ("ai_prompt_criteria_file", "state/yx_1369.json"),
        ("ai_prompt_base_file", "C:\\Windows\\win.ini"),
        ("ai_prompt_base_file", "~/.ssh/id_rsa"),
        ("account_state_file", "/etc/passwd"),
        ("account_state_file", "../.env"),
        ("account_state_file", "state/notes.txt"),
    ],
)
def test_task_file_paths_must_stay_inside_project(field, value):
    with pytest.raises(ValidationError):
        _full_task(**{field: value})
    with pytest.raises(ValidationError):
        TaskUpdate(**{field: value})


def test_task_file_paths_accept_project_relative_values():
    task = _full_task(ai_prompt_criteria_file="", account_state_file="xianyu_state.json")
    assert task.ai_prompt_criteria_file == ""
    assert task.account_state_file == "xianyu_state.json"

    normalized = _full_task(ai_prompt_criteria_file="./prompts//t14_criteria.txt", account_state_file="state\\yx.json")
    assert normalized.ai_prompt_criteria_file == "prompts/t14_criteria.txt"
    assert normalized.account_state_file == "state/yx.json"
    assert TaskUpdate(ai_prompt_criteria_file=None).model_dump(exclude_unset=True) == {"ai_prompt_criteria_file": None}
