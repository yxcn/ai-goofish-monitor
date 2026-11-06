"""TaskGenerationService 的终态作业清理：按数量上限和 TTL，运行中的永不清理。"""
import asyncio

from src.services.task_generation_service import (
    FINISHED_JOB_TTL_SECONDS,
    MAX_FINISHED_JOBS,
    TaskGenerationService,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_defaults_keep_at_most_50_finished_jobs_for_an_hour():
    service = TaskGenerationService()
    assert MAX_FINISHED_JOBS == 50
    assert FINISHED_JOB_TTL_SECONDS == 3600
    assert service._max_finished_jobs == 50
    assert service._finished_job_ttl_seconds == 3600


def test_finished_jobs_are_capped_oldest_first():
    clock = FakeClock()
    service = TaskGenerationService(max_finished_jobs=3, clock=clock)

    async def scenario():
        finished = []
        for index in range(5):
            job = await service.create_job(f"t{index}")
            if index % 2:
                await service.fail(job.job_id, "boom", step_key="prepare")
            else:
                await service.complete(job.job_id, None, "done")
            finished.append(job.job_id)
            clock.advance(1)
        running = await service.create_job("still running")
        await service.advance(running.job_id, "llm", "calling")
        return finished, running.job_id

    finished, running_id = asyncio.run(scenario())

    survivors = [asyncio.run(service.get_job(job_id)) is not None for job_id in finished]
    assert survivors == [False, False, True, True, True]
    assert asyncio.run(service.get_job(running_id)) is not None


def test_finished_jobs_expire_after_ttl_but_running_ones_stay():
    clock = FakeClock()
    service = TaskGenerationService(finished_job_ttl_seconds=60, clock=clock)

    async def scenario():
        done = await service.create_job("done")
        await service.complete(done.job_id, None, "ok")
        active = await service.create_job("active")
        await service.advance(active.job_id, "reference", "reading")

        clock.advance(61)
        trigger = await service.create_job("trigger")  # 创建新作业时顺带清理
        return done.job_id, active.job_id, trigger.job_id

    done_id, active_id, trigger_id = asyncio.run(scenario())

    assert asyncio.run(service.get_job(done_id)) is None
    assert asyncio.run(service.get_job(active_id)) is not None
    assert asyncio.run(service.get_job(trigger_id)) is not None


def test_recent_finished_jobs_stay_queryable():
    clock = FakeClock()
    service = TaskGenerationService(clock=clock)

    async def scenario():
        job = await service.create_job("quick")
        await service.fail(job.job_id, "nope")
        clock.advance(30)
        await service.create_job("another")
        return job.job_id

    job_id = asyncio.run(scenario())
    job = asyncio.run(service.get_job(job_id))
    assert job is not None
    assert job.status == "failed"
