import asyncio
import re
from types import SimpleNamespace

import pytest

from src.domain.models.task import TaskGenerateRequest
from src.services import task_generation_runner as runner
from src.services.task_generation_service import TaskGenerationService


def test_criteria_filename_is_scoped_per_task():
    assert runner.build_criteria_filename("MacBook Air", task_id=3) == "prompts/macbook_air_3_criteria.txt"

    generated = runner.build_criteria_filename("macbook")
    assert re.fullmatch(r"prompts/macbook_[0-9a-f]{8}_criteria\.txt", generated)
    assert generated != runner.REFERENCE_CRITERIA_FILE
    assert runner.build_criteria_filename("macbook") != generated

    # 关键词只剩非法字符时也不会生成 prompts/_criteria.txt
    assert runner.build_criteria_filename("!!!", task_id=1) == "prompts/task_1_criteria.txt"


def test_saving_onto_reference_template_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="参考模板"):
        asyncio.run(runner.save_generated_criteria(runner.REFERENCE_CRITERIA_FILE, "new text"))
    with pytest.raises(RuntimeError, match="参考模板"):
        asyncio.run(runner.save_generated_criteria("./prompts/macbook_criteria.txt", "new text"))
    assert not (tmp_path / "prompts" / "macbook_criteria.txt").exists()


def test_cleanup_never_touches_preexisting_or_reference_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    reference = tmp_path / runner.REFERENCE_CRITERIA_FILE
    reference.write_text("reference", encoding="utf-8")
    shared = tmp_path / "prompts" / "macbook_criteria_old.txt"
    shared.write_text("old task's criteria", encoding="utf-8")
    fresh = tmp_path / "prompts" / "macbook_ab12cd34_criteria.txt"
    fresh.write_text("just generated", encoding="utf-8")

    runner.cleanup_generated_criteria(str(reference), existed_before=False)
    runner.cleanup_generated_criteria(str(shared), existed_before=True)
    runner.cleanup_generated_criteria(str(fresh), existed_before=False)

    assert reference.read_text(encoding="utf-8") == "reference"
    assert shared.exists()
    assert not fresh.exists()


class _FailingTaskService:
    async def create_task(self, _task_create):
        raise RuntimeError("db down")

    async def get_all_tasks(self):
        return []


def _request() -> TaskGenerateRequest:
    return TaskGenerateRequest(task_name="MB", keyword="macbook", description="只看 M1 以上")


def _run_job(monkeypatch, output_filename: str) -> TaskGenerationService:
    generation_service = TaskGenerationService()
    monkeypatch.setattr(runner, "build_criteria_filename", lambda *_a, **_k: output_filename)

    async def fake_generate_criteria(**_kwargs):
        return "generated criteria text"

    monkeypatch.setattr(runner, "generate_criteria", fake_generate_criteria)

    async def scenario():
        job = await generation_service.create_job("MB")
        await runner.run_ai_generation_job(
            job_id=job.job_id,
            req=_request(),
            task_service=_FailingTaskService(),
            scheduler_service=SimpleNamespace(),
            generation_service=generation_service,
        )
        return await generation_service.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job.status == "failed"
    assert "db down" in (job.error or "")
    return generation_service


def test_failed_generation_removes_only_the_file_it_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = "prompts/macbook_ab12cd34_criteria.txt"

    _run_job(monkeypatch, output)

    assert not (tmp_path / output).exists()


def test_failed_generation_keeps_a_file_that_existed_before(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    output = "prompts/macbook_9_criteria.txt"
    (tmp_path / output).write_text("another task is using this", encoding="utf-8")

    _run_job(monkeypatch, output)

    # 文件被本次生成覆盖了内容（和之前一样），但失败时不能把它删掉
    assert (tmp_path / output).exists()


def test_generation_refuses_to_overwrite_reference_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    reference = tmp_path / runner.REFERENCE_CRITERIA_FILE
    reference.write_text("reference", encoding="utf-8")

    generation_service = TaskGenerationService()
    monkeypatch.setattr(runner, "build_criteria_filename", lambda *_a, **_k: runner.REFERENCE_CRITERIA_FILE)

    async def fake_generate_criteria(**_kwargs):
        return "generated"

    monkeypatch.setattr(runner, "generate_criteria", fake_generate_criteria)

    async def scenario():
        job = await generation_service.create_job("MB")
        await runner.run_ai_generation_job(
            job_id=job.job_id,
            req=_request(),
            task_service=_FailingTaskService(),
            scheduler_service=SimpleNamespace(),
            generation_service=generation_service,
        )
        return await generation_service.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job.status == "failed"
    assert "参考模板" in (job.error or "")
    assert reference.read_text(encoding="utf-8") == "reference"
