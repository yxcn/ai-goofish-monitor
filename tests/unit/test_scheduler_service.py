from types import SimpleNamespace

from src.services.scheduler_service import SchedulerService


def test_scheduler_tolerates_late_fires_and_runs_one_instance_per_task():
    service = SchedulerService(SimpleNamespace())
    defaults = service.scheduler._job_defaults
    # 默认 misfire_grace_time=1：事件循环被同步 IO 卡住超过 1 秒，整点任务就被静默跳过
    assert defaults["misfire_grace_time"] == 300
    assert defaults["coalesce"] is True
    assert defaults["max_instances"] == 1
