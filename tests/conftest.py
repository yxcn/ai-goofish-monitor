import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Add repository root to the path so package imports work consistently
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

# 测试不能跟着本机 .env 选 AI 提供方：设置页切到 Claude 后，src.config 会用 .env 里的真 Key
# 建 Claude 客户端，按 OpenAI 写的测试就会真的去调 Claude API（花钱）。
# load_dotenv 不覆盖已有的环境变量，所以在第一次导入 src 之前先钉死；每个测试开头再钉一次（见下面的 fixture），
# 因为有些路径会 load_dotenv(override=True) 把仓库 .env 重新灌进 os.environ。
_PINNED_AI_ENV = {
    "AI_PROVIDER": "openai",
    "CLAUDE_API_KEY": "",
    "CLAUDE_BASE_URL": "",
    "CLAUDE_MODEL_NAME": "",
}
os.environ.update(_PINNED_AI_ENV)

from src.api import dependencies as deps
from src.api.routes import tasks
from src.infrastructure.persistence.sqlite_bootstrap import reset_bootstrap_state
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.services.task_generation_service import TaskGenerationService


REPO_PROMPTS_DIR = repo_root / "prompts"


def _repo_prompt_files() -> set[str]:
    if not REPO_PROMPTS_DIR.is_dir():
        return set()
    return {path.name for path in REPO_PROMPTS_DIR.iterdir()}


@pytest.fixture(autouse=True, scope="session")
def _repo_prompts_untouched():
    # 测试曾把 AI 生成的分析标准写进仓库的 prompts/，每跑一次多一个文件；整轮跑完核对一次
    before = _repo_prompt_files()
    yield
    after = _repo_prompt_files()
    added = sorted(after - before)
    removed = sorted(before - after)
    assert not added and not removed, (
        f"测试改动了仓库的 prompts/ 目录：新增 {added}，删除 {removed}。"
        "写文件的测试请用 monkeypatch.chdir(tmp_path)。"
    )


@pytest.fixture(autouse=True)
def _pin_ai_provider_env(monkeypatch):
    for key, value in _PINNED_AI_ENV.items():
        monkeypatch.setenv(key, value)


class RealClaudeRequestBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_real_claude_requests(monkeypatch):
    # 兜底：哪条路径漏了上面的钉值，也不能真的发请求到 Anthropic。要测 Claude 请用假客户端。
    try:
        from anthropic import _base_client
    except ImportError:
        return

    def _blocked(*args, **kwargs):
        raise RealClaudeRequestBlocked("测试里不允许真的调用 Claude API，请换成假客户端")

    async def _blocked_async(*args, **kwargs):
        _blocked()

    monkeypatch.setattr(_base_client.SyncAPIClient, "request", _blocked)
    monkeypatch.setattr(_base_client.AsyncAPIClient, "request", _blocked_async)


@pytest.fixture(autouse=True)
def _fresh_sqlite_bootstrap():
    # bootstrap_sqlite_storage() 每个库路径只跑一次 DDL；测试之间切目录/换库，标记要清掉
    reset_bootstrap_state()
    yield
    reset_bootstrap_state()


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture()
def load_json_fixture(fixtures_dir):
    def _load(name: str):
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture()
def sample_task_payload():
    return {
        "task_name": "Sony A7M4",
        "enabled": True,
        "keyword": "sony a7m4",
        "description": "Good condition body with accessories",
        "analyze_images": True,
        "max_pages": 2,
        "personal_only": True,
        "min_price": "8000",
        "max_price": "16000",
        "cron": "*/15 * * * *",
        "ai_prompt_base_file": "prompts/base_prompt.txt",
        "ai_prompt_criteria_file": "prompts/sony_a7m4_criteria.txt",
        "decision_mode": "ai",
        "keyword_rules": [],
    }


class FakeProcessService:
    def __init__(self):
        self.started = []
        self.stopped = []
        self._on_started = None
        self._on_stopped = None

    def set_lifecycle_hooks(self, *, on_started=None, on_stopped=None):
        self._on_started = on_started
        self._on_stopped = on_stopped

    async def start_task(self, task_id: int, task_name: str) -> bool:
        self.started.append((task_id, task_name))
        if self._on_started:
            await self._on_started(task_id)
        return True

    async def stop_task(self, task_id: int):
        self.stopped.append(task_id)
        if self._on_stopped:
            await self._on_stopped(task_id)



class FakeSchedulerService:
    def __init__(self):
        self.reload_calls = 0
        self.next_run_times = {}

    async def reload_jobs(self, tasks):
        self.reload_calls += 1
        base = datetime(2026, 3, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.next_run_times = {
            task.id: base + timedelta(minutes=(index + 1) * 15)
            for index, task in enumerate(tasks)
            if task.id is not None and task.enabled and task.cron
        }

    def get_next_run_time(self, task_id: int):
        return self.next_run_times.get(task_id)


@pytest.fixture()
def api_context(tmp_path, monkeypatch):
    # 任务接口会按相对路径写 prompts/ 等目录（如 AI 生成的分析标准），切到临时目录，别留在仓库里
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.json"
    config_file.write_text("[]", encoding="utf-8")
    db_path = tmp_path / "app.sqlite3"

    repository = SqliteTaskRepository(
        db_path=str(db_path),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    process_service = FakeProcessService()
    scheduler_service = FakeSchedulerService()
    task_generation_service = TaskGenerationService()

    app = FastAPI()
    app.include_router(tasks.router)

    def override_get_task_service():
        return task_service

    def override_get_process_service():
        return process_service

    def override_get_scheduler_service():
        return scheduler_service

    def override_get_task_generation_service():
        return task_generation_service

    async def mark_started(task_id: int):
        await task_service.update_task_status(task_id, True)

    async def mark_stopped(task_id: int):
        task = await task_service.get_task(task_id)
        if task:
            await task_service.update_task_status(task_id, False)

    process_service.set_lifecycle_hooks(on_started=mark_started, on_stopped=mark_stopped)

    app.dependency_overrides[deps.get_task_service] = override_get_task_service
    app.dependency_overrides[deps.get_process_service] = override_get_process_service
    app.dependency_overrides[deps.get_scheduler_service] = override_get_scheduler_service
    app.dependency_overrides[deps.get_task_generation_service] = override_get_task_generation_service

    return {
        "app": app,
        "config_file": config_file,
        "db_path": db_path,
        "process_service": process_service,
        "scheduler_service": scheduler_service,
        "task_generation_service": task_generation_service,
    }


@pytest.fixture()
def api_client(api_context):
    return TestClient(api_context["app"])
