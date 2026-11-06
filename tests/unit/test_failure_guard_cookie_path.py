import json
import os
from datetime import datetime, timedelta

from src.failure_guard import FailureGuard


def _guard(tmp_path, threshold=1):
    return FailureGuard(
        path=str(tmp_path / "guard.json"),
        threshold=threshold,
        pause_seconds=24 * 3600,
        tz_name="Asia/Shanghai",
    )


def _write(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_recovers_when_recorded_pool_file_updates_even_if_caller_passes_none(tmp_path):
    # auto 模式且没有根 state.json 时，scraper 调 should_skip_start(cookie_path=None)；
    # 之前这样永远比不出"cookie 更新"，用户重新导出池文件也恢复不了
    pool_file = tmp_path / "state" / "a.json"
    _write(pool_file, 1_700_000_000)
    guard = _guard(tmp_path)
    base = datetime(2026, 3, 4, 12, 0, 0)

    guard.record_failure("task", "login expired", cookie_path=str(pool_file), now=base)
    assert guard.should_skip_start("task", cookie_path=None, now=base).skip is True

    _write(pool_file, 1_700_000_100)
    decision = guard.should_skip_start("task", cookie_path=None, now=base + timedelta(minutes=1))
    assert decision.skip is False
    assert decision.reason == "cookie_updated"


def test_newer_root_state_file_does_not_unpause_when_pool_file_was_used(tmp_path):
    # rotate 模式：失败时用的是池文件 a.json，调用方却传根 state.json；
    # 根文件比 a.json 新不代表 a.json 修好了，不能据此解除暂停
    pool_file = tmp_path / "state" / "a.json"
    root_file = tmp_path / "xianyu_state.json"
    _write(pool_file, 1_700_000_000)
    _write(root_file, 1_700_000_500)
    guard = _guard(tmp_path)
    base = datetime(2026, 3, 4, 12, 0, 0)

    guard.record_failure("task", "risk control", cookie_path=str(pool_file), now=base)
    decision = guard.should_skip_start("task", cookie_path=str(root_file), now=base + timedelta(minutes=1))
    assert decision.skip is True


def test_caller_path_is_used_when_entry_has_no_recorded_path(tmp_path):
    # 旧版 guard 文件里没有 cookie_path：退回用调用方传的路径
    cookie = tmp_path / "xianyu_state.json"
    _write(cookie, 1_700_000_000)
    guard = _guard(tmp_path)
    base = datetime(2026, 3, 4, 12, 0, 0)
    guard.record_failure("task", "err", cookie_path=str(cookie), now=base)

    guard_file = tmp_path / "guard.json"
    data = json.loads(guard_file.read_text(encoding="utf-8"))
    data["tasks"]["task"]["cookie_path"] = None
    guard_file.write_text(json.dumps(data), encoding="utf-8")

    _write(cookie, 1_700_000_100)
    decision = guard.should_skip_start("task", cookie_path=str(cookie), now=base + timedelta(minutes=1))
    assert decision.skip is False


def test_switching_to_a_newer_pool_file_does_not_reset_failure_count(tmp_path):
    # b.json 比 a.json 新，但那不是"a.json 被更新了"，连续失败不该清零
    a = tmp_path / "state" / "a.json"
    b = tmp_path / "state" / "b.json"
    _write(a, 1_700_000_000)
    _write(b, 1_700_000_900)
    guard = _guard(tmp_path, threshold=2)
    base = datetime(2026, 3, 4, 12, 0, 0)

    first = guard.record_failure("task", "err-1", cookie_path=str(a), now=base)
    second = guard.record_failure("task", "err-2", cookie_path=str(b), now=base + timedelta(minutes=5))

    assert first["consecutive_failures"] == 1
    assert second["consecutive_failures"] == 2
    assert second["opened_circuit"] is True


def test_same_file_updated_between_failures_still_resets_count(tmp_path):
    cookie = tmp_path / "state" / "a.json"
    _write(cookie, 1_700_000_000)
    guard = _guard(tmp_path, threshold=3)
    base = datetime(2026, 3, 4, 12, 0, 0)

    guard.record_failure("task", "err-1", cookie_path=str(cookie), now=base)
    guard.record_failure("task", "err-2", cookie_path=str(cookie), now=base)
    _write(cookie, 1_700_000_100)
    third = guard.record_failure("task", "err-3", cookie_path=str(cookie), now=base + timedelta(minutes=1))

    assert third["consecutive_failures"] == 1
    assert third["opened_circuit"] is False
