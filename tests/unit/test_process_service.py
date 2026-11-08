import asyncio
import sys
from types import SimpleNamespace

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    # 子进程按 id 选任务：任务名不唯一，且以 '-' 开头的名称会被 argparse 当成选项
    command = service._build_spawn_command(7, "-task-a")

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-id",
        "7",
        "--debug-limit",
        "1",
    ]


def _no_skip(*_args, **_kwargs):
    return SimpleNamespace(
        skip=False, should_notify=False, reason="", consecutive_failures=0, paused_until=None
    )


def test_concurrent_start_of_same_task_spawns_only_one_process(monkeypatch, tmp_path):
    """cron 触发恰逢手动点"启动"：两个 start_task 并发进来，只能起一个子进程。"""
    spawned = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = _no_skip

        async def fake_resolve_cookie_path(_task_id):
            return None

        monkeypatch.setattr(service, "_resolve_cookie_path", fake_resolve_cookie_path)

        async def slow_create_subprocess_exec(*_args, **_kwargs):
            # spawn 期间让出事件循环，老实现会在这里被第二个 start_task 插进来
            await asyncio.sleep(0.05)
            process = FakeProcess(pid=1000 + len(spawned))
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_create_subprocess_exec)
        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )

        results = await asyncio.gather(
            service.start_task(3, "dup"),
            service.start_task(3, "dup"),
        )
        assert sorted(results) == [False, True]
        assert len(spawned) == 1
        assert service.is_running(3) is True

        # 不同任务互不阻塞
        assert await service.start_task(4, "other") is True
        assert len(spawned) == 2

        for process in spawned:
            process.finish(0)
        for task_id in (3, 4):
            await service._await_exit_watcher(task_id)

    asyncio.run(run_scenario())


def test_resolve_cookie_path_reads_task_by_id_off_the_event_loop(monkeypatch, tmp_path):
    from src.services import process_service as module

    calls = []

    def fake_find_task_by_id_sync(task_id):
        calls.append(task_id)
        return SimpleNamespace(account_state_file=" state/acc.json ")

    monkeypatch.setattr(module, "find_task_by_id_sync", fake_find_task_by_id_sync)
    service = ProcessService()

    assert asyncio.run(service._resolve_cookie_path(9)) == "state/acc.json"
    assert calls == [9]


def _patch_log_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.services.process_service.build_task_log_path",
        lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
    )


def test_open_log_file_rotates_oversized_log_and_overwrites_previous_rotation(monkeypatch, tmp_path):
    from src.services import process_service as module

    _patch_log_dir(monkeypatch, tmp_path)
    log_path = tmp_path / "task-1.log"
    oversized = module.LOG_ROTATE_MAX_BYTES + 1
    log_path.write_bytes(b"x" * oversized)
    rotated = tmp_path / "task-1.log.1"
    rotated.write_text("stale rotation", encoding="utf-8")

    path, handle = ProcessService()._open_log_file(1, "t")
    handle.write("new run\n")
    handle.close()

    assert path == str(log_path)
    assert log_path.read_text(encoding="utf-8") == "new run\n"
    assert rotated.stat().st_size == oversized  # 旧的 .1 被覆盖
    assert not (tmp_path / "task-1.log.2").exists()


def test_open_log_file_keeps_appending_to_small_log(monkeypatch, tmp_path):
    from src.services import process_service as module

    _patch_log_dir(monkeypatch, tmp_path)
    log_path = tmp_path / "task-2.log"
    log_path.write_bytes(b"x" * module.LOG_ROTATE_MAX_BYTES)  # 恰好等于阈值：不轮转

    _path, handle = ProcessService()._open_log_file(2, "t")
    handle.write("more")
    handle.close()

    assert log_path.stat().st_size == module.LOG_ROTATE_MAX_BYTES + 4
    assert not (tmp_path / "task-2.log.1").exists()


def test_spawn_uses_start_new_session_instead_of_preexec_fn(monkeypatch, tmp_path):
    captured = {}

    async def fake_create_subprocess_exec(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess(pid=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    service = ProcessService()
    with open(tmp_path / "x.log", "a", encoding="utf-8") as handle:
        asyncio.run(service._spawn_process(1, "t", handle))

    assert "preexec_fn" not in captured
    assert captured["start_new_session"] is (sys.platform != "win32")
    assert captured["env"]["PYTHONUTF8"] == "1"


def test_spawn_does_not_inherit_server_stdin(monkeypatch, tmp_path):
    # 服务在终端里跑时 stdin 是 tty；子进程继承它，调试模式收尾的 input() 会让每轮都卡住
    captured = {}

    async def fake_create_subprocess_exec(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess(pid=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    service = ProcessService()
    with open(tmp_path / "x.log", "a", encoding="utf-8") as handle:
        asyncio.run(service._spawn_process(1, "t", handle))

    assert captured["stdin"] is asyncio.subprocess.DEVNULL
