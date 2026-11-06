"""FailureGuard 多进程 / 多线程并发写：文件必须始终是合法 JSON、计数准确、不往任务层抛异常。

背景：Web UI 为每个任务起独立子进程，多任务同时失败时会并发写同一个状态文件。
旧实现对数据文件本身 flock（每次 os.replace 换 inode，锁不互斥）并共用固定的 .tmp 名，
曾经出现 FileNotFoundError 冒到 scrape_xianyu 和 0 字节的 .corrupt 文件。
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

from src.failure_guard import FailureGuard

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _guard(path, threshold=10**6):
    return FailureGuard(
        path=str(path),
        threshold=threshold,
        pause_seconds=3600,
        tz_name="Asia/Shanghai",
    )


def _leftovers(directory: Path) -> list:
    """残留的临时文件 / 损坏现场文件（.lock 文件是正常产物，不算）。"""
    return sorted(
        p.name for p in directory.iterdir() if ".tmp" in p.name or ".corrupt." in p.name
    )


def test_concurrent_threads_keep_file_valid_and_counts_exact(tmp_path):
    guard_path = tmp_path / "guard.json"
    per_thread, thread_count = 20, 8
    errors: list = []

    def worker(index: int) -> None:
        # 每个线程用自己的实例，模拟互不相知的调用方
        guard = _guard(guard_path)
        try:
            for i in range(per_thread):
                guard.record_failure("shared", f"err-{index}-{i}")
                guard.record_failure(f"task-{index}", "err")
                guard.should_skip_start("shared")
        except Exception as exc:  # pragma: no cover - 失败时给出明确信息
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = json.loads(guard_path.read_text(encoding="utf-8"))
    assert data["tasks"]["shared"]["consecutive_failures"] == per_thread * thread_count
    for index in range(thread_count):
        assert data["tasks"][f"task-{index}"]["consecutive_failures"] == per_thread
    assert _leftovers(tmp_path) == []


_WORKER_SCRIPT = """
import sys
from src.failure_guard import FailureGuard

path, shared_key, own_key, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
guard = FailureGuard(path=path, threshold=10**6, pause_seconds=3600, tz_name="Asia/Shanghai")
for i in range(count):
    guard.record_failure(shared_key, f"err-{own_key}-{i}")
    guard.record_failure(own_key, "err")
    guard.should_skip_start(shared_key)
guard.record_success(own_key)
"""


def test_concurrent_processes_do_not_corrupt_or_raise(tmp_path):
    guard_path = tmp_path / "logs" / "guard.json"
    process_count, per_process = 5, 15

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER_SCRIPT,
                str(guard_path),
                "shared",
                f"own-{index}",
                str(per_process),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(process_count)
    ]
    outputs = [proc.communicate(timeout=120) for proc in procs]

    for proc, (_, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, stderr
        assert "Traceback" not in stderr, stderr
        assert "[FailureGuard] 警告" not in stderr, stderr

    data = json.loads(guard_path.read_text(encoding="utf-8"))
    assert data["tasks"]["shared"]["consecutive_failures"] == process_count * per_process
    for index in range(process_count):
        # 每个进程最后 record_success 了自己的 key
        assert data["tasks"][f"own-{index}"]["consecutive_failures"] == 0
        assert data["tasks"][f"own-{index}"]["last_success_at"]
    assert _leftovers(guard_path.parent) == []


def test_first_run_does_not_leave_zero_byte_corrupt_marker(tmp_path):
    # 旧实现 open(path, "a+") 先创建空文件，再 json.load 失败把它改名成 .corrupt.*
    guard_path = tmp_path / "logs" / "guard.json"
    guard = _guard(guard_path)

    assert guard.should_skip_start("task").skip is False
    result = guard.record_failure("task", "err")

    assert result["consecutive_failures"] == 1
    assert _leftovers(guard_path.parent) == []
    assert (tmp_path / "logs" / "guard.json.lock").exists()


def test_empty_state_file_is_not_treated_as_corrupt(tmp_path):
    guard_path = tmp_path / "guard.json"
    guard_path.write_text("", encoding="utf-8")
    guard = _guard(guard_path)

    assert guard.should_skip_start("task").skip is False
    assert _leftovers(tmp_path) == []


def test_file_errors_are_swallowed_and_reported_as_warnings(tmp_path, capsys):
    # 父"目录"是个普通文件，所有文件操作都会失败：不能冒到任务层
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    guard = _guard(blocker / "guard.json", threshold=1)

    result = guard.record_failure("task", "err")
    decision = guard.should_skip_start("task")
    guard.record_success("task")

    assert result["consecutive_failures"] == 0
    assert result["opened_circuit"] is False
    assert "error" in result
    assert decision.skip is False
    assert decision.reason == "guard_unavailable"
    assert capsys.readouterr().err.count("[FailureGuard] 警告") == 3
