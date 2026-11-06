"""Task-level failure circuit breaker.

目标:
- 当登录态失效/风控导致任务持续失败时，避免无限重试、避免高频请求。
- 失败达到阈值后暂停任务一段时间。
- 暂停期间最多每天通知一次，直到用户更新 cookies / 登录态文件后自动恢复。

说明:
- 仅使用标准库，既可被 API 主进程使用，也可被爬虫子进程使用。
- Web UI 为每个任务起独立子进程，多任务同时失败时会并发读写同一个状态文件，
  所以所有读写都在跨进程锁内完成，写入走"唯一临时文件 + os.replace"。
- 状态文件只是辅助设施：任何文件层面的异常都在这里打警告吞掉，不让它冒到任务层把任务判成"异常终止"。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple


try:
    from zoneinfo import ZoneInfo  # py3.9+

    def _load_tz(name: str):
        return ZoneInfo(name)


except Exception:  # pragma: no cover

    def _load_tz(name: str):
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now(tz_name: str, now: Optional[datetime] = None) -> datetime:
    if now is not None:
        return now
    tz = _load_tz(tz_name)
    if tz is None:
        return datetime.now()
    return datetime.now(tz)


def _today_str(tz_name: str, now: Optional[datetime] = None) -> str:
    return _now(tz_name, now=now).date().isoformat()


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_mtime(path: Optional[str]) -> Optional[float]:
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _cookie_changed(
    cookie_path: Optional[str], previous_mtime: Optional[float]
) -> bool:
    if not cookie_path:
        return False
    current = _get_mtime(cookie_path)
    if current is None or previous_mtime is None:
        return False
    return current > (previous_mtime + 1e-6)


def _warn(message: str) -> None:
    print(f"[FailureGuard] 警告: {message}", file=sys.stderr)


class _CrossProcessLock:
    """跨进程 + 跨线程互斥锁，锁的是独立的 ``<path>.lock`` 文件而不是数据文件本身。

    数据文件每次保存都被 ``os.replace`` 换成新 inode：如果直接对它 flock，进程 A 锁住旧 inode、
    进程 B 随后 ``open`` 到的是新 inode，两把锁互不相干，等于没锁。``.lock`` 文件只被打开、
    从不被替换，inode 稳定。进程内再套一把 RLock：同一进程多线程也互斥，flock 不可用的平台
    （Windows）仍有线程级保护；可重入是为了同一线程内嵌套使用时不会自己把自己锁死。
    """

    def __init__(self, path: str):
        self._lock_path = f"{path}.lock"
        self._rlock = threading.RLock()
        self._depth = 0
        self._fh = None

    def __enter__(self):
        self._rlock.acquire()
        if self._depth == 0:
            try:
                self._acquire_file_lock()
            except BaseException:
                self._rlock.release()
                raise
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._depth -= 1
        try:
            if self._depth == 0:
                self._release_file_lock()
        finally:
            self._rlock.release()
        return False

    def _acquire_file_lock(self) -> None:
        _ensure_parent_dir(self._lock_path)
        self._fh = open(self._lock_path, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # 没有 fcntl（Windows）或文件系统不支持锁：退化为仅线程锁
            pass

    def _release_file_lock(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            fh.close()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    # 空文件（刚被创建、还没写入）不算损坏，否则第一次运行就会留下一个 0 字节的 .corrupt 文件
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        # 文件损坏时保留现场，避免无限解析失败。
        try:
            ts = str(int(time.time()))
            os.replace(path, f"{path}.corrupt.{ts}")
        except Exception:
            pass
        return {}


def _atomic_write_json(path: str, data: dict) -> None:
    """先写到同目录下的唯一临时文件，再 os.replace 到目标。

    临时文件名不能是固定的 ``<path>.tmp``：两个进程同时写会互相截断，
    其中一个 ``os.replace`` 时文件已被对方挪走，直接 FileNotFoundError。
    """
    _ensure_parent_dir(path)
    directory = os.path.dirname(path) or "."
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


@dataclass(frozen=True)
class SkipDecision:
    skip: bool
    should_notify: bool
    reason: str
    paused_until: Optional[datetime]
    consecutive_failures: int


def _not_paused_decision(consecutive: int = 0, reason: str = "not_paused") -> SkipDecision:
    return SkipDecision(
        skip=False,
        should_notify=False,
        reason=reason,
        paused_until=None,
        consecutive_failures=consecutive,
    )


class FailureGuard:
    def __init__(
        self,
        path: Optional[str] = None,
        *,
        threshold: Optional[int] = None,
        pause_seconds: Optional[int] = None,
        tz_name: Optional[str] = None,
    ):
        self.path = (
            path
            or os.getenv("TASK_FAILURE_GUARD_PATH")
            or "logs/task-failure-guard.json"
        )
        self.threshold = max(
            1, threshold or _as_int(os.getenv("TASK_FAILURE_THRESHOLD"), 3)
        )
        self.pause_seconds = max(
            60,
            pause_seconds
            or _as_int(os.getenv("TASK_FAILURE_PAUSE_SECONDS"), 24 * 60 * 60),
        )
        self.tz_name = tz_name or os.getenv("TASK_FAILURE_TZ") or "Asia/Shanghai"
        self._lock = _CrossProcessLock(self.path)

    def _load(self) -> dict:
        data = _read_json_file(self.path)
        if "tasks" not in data or not isinstance(data.get("tasks"), dict):
            data = {"version": 1, "tasks": {}}
        data.setdefault("version", 1)
        return data

    def _save(self, data: dict) -> None:
        _atomic_write_json(self.path, data)

    def _transaction(self, mutate: Callable[[dict], Tuple[Any, bool]]) -> Any:
        """在跨进程锁内执行 读 -> ``mutate(data)`` -> 按需写回。

        ``mutate`` 返回 ``(结果, 是否写回)``。读和写在同一把锁里完成，
        两个进程不会各自基于旧内容算出结果再互相覆盖。
        """
        with self._lock:
            data = self._load()
            result, dirty = mutate(data)
            if dirty:
                self._save(data)
            return result

    def _update_task(self, task_key: str, updater) -> dict:
        def _mutate(data: dict) -> Tuple[dict, bool]:
            tasks = data.setdefault("tasks", {})
            entry = tasks.get(task_key) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry = updater(entry) or entry
            tasks[task_key] = entry
            return entry, True

        return self._transaction(_mutate)

    @staticmethod
    def _reset_entry(current: datetime) -> dict:
        return {
            "consecutive_failures": 0,
            "paused_until": None,
            "last_notified_date": None,
            "last_failure_reason": None,
            "last_failure_at": None,
            "last_success_at": _dt_to_str(current),
            "cookie_path": None,
            "cookie_mtime": None,
        }

    def record_success(self, task_key: str, *, now: Optional[datetime] = None) -> None:
        current = _now(self.tz_name, now=now)
        try:
            self._update_task(task_key, lambda _entry: self._reset_entry(current))
        except Exception as exc:
            _warn(f"记录任务 '{task_key}' 成功状态失败（已忽略）: {exc!r}")

    def should_skip_start(
        self,
        task_key: str,
        *,
        cookie_path: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SkipDecision:
        current = _now(self.tz_name, now=now)
        today = _today_str(self.tz_name, now=current)

        def _decide(data: dict) -> Tuple[SkipDecision, bool]:
            tasks = data.setdefault("tasks", {})
            entry = tasks.get(task_key) or {}
            if not isinstance(entry, dict):
                entry = {}

            paused_until = _str_to_dt(entry.get("paused_until"))
            consecutive = _as_int(entry.get("consecutive_failures"), 0)
            last_reason = (entry.get("last_failure_reason") or "").strip() or "未知错误"
            last_notified_date = entry.get("last_notified_date")

            previous_cookie_mtime = entry.get("cookie_mtime")
            if previous_cookie_mtime is not None:
                try:
                    previous_cookie_mtime = float(previous_cookie_mtime)
                except (TypeError, ValueError):
                    previous_cookie_mtime = None

            # 比对 mtime 必须用记录失败时实际使用的那个登录态文件（rotate 模式下是池里的某个文件），
            # 调用方传进来的路径只在记录里没有时兜底。否则根 state.json 比池文件新就会被当成
            # "已更新"而误解除暂停；而 auto 模式下调用方传 None 时，用户更新池文件也永远恢复不了。
            recorded_cookie_path = entry.get("cookie_path")
            effective_cookie_path = (
                recorded_cookie_path
                if isinstance(recorded_cookie_path, str) and recorded_cookie_path.strip()
                else cookie_path
            )

            if (
                paused_until
                and paused_until > current
                and effective_cookie_path
                and _cookie_changed(effective_cookie_path, previous_cookie_mtime)
            ):
                # cookies / 登录态更新 => 自动恢复
                tasks[task_key] = self._reset_entry(current)
                return _not_paused_decision(reason="cookie_updated"), True

            if paused_until and current < paused_until:
                should_notify = last_notified_date != today
                if should_notify:
                    # 和判定放在同一把锁里：两个进程同时启动不会都认为"今天还没通知过"
                    touched = dict(entry)
                    touched["last_notified_date"] = today
                    tasks[task_key] = touched
                return (
                    SkipDecision(
                        skip=True,
                        should_notify=should_notify,
                        reason=last_reason,
                        paused_until=paused_until,
                        consecutive_failures=consecutive,
                    ),
                    should_notify,
                )

            return _not_paused_decision(consecutive), False

        try:
            return self._transaction(_decide)
        except Exception as exc:
            _warn(f"读取任务 '{task_key}' 熔断状态失败，本次按未暂停处理: {exc!r}")
            return _not_paused_decision(reason="guard_unavailable")

    def record_failure(
        self,
        task_key: str,
        reason: str,
        *,
        cookie_path: Optional[str] = None,
        min_failures_to_pause: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        current = _now(self.tz_name, now=now)
        today = _today_str(self.tz_name, now=current)
        cookie_mtime = _get_mtime(cookie_path)

        effective_threshold = max(1, int(min_failures_to_pause or self.threshold))

        result = {
            "should_notify": False,
            "opened_circuit": False,
            "paused_until": None,
            "consecutive_failures": 0,
        }

        def _apply(entry: dict) -> dict:
            entry = dict(entry or {})
            previous_paused_until = _str_to_dt(entry.get("paused_until"))
            was_paused = bool(previous_paused_until and current < previous_paused_until)

            prev_mtime = entry.get("cookie_mtime")
            try:
                prev_mtime = float(prev_mtime) if prev_mtime is not None else None
            except (TypeError, ValueError):
                prev_mtime = None

            # 只有和上次记录的是同一个文件、且它变新了，才算"用户更新了登录态"并清零计数。
            # rotate 模式下每次随机选到不同的池文件，拿另一个文件的 mtime 比较没有意义，
            # 否则换到一个更新的文件就把连续失败清零，熔断永远开不了。
            recorded_path = entry.get("cookie_path")
            same_file = not recorded_path or recorded_path == cookie_path
            if cookie_path and same_file and _cookie_changed(cookie_path, prev_mtime):
                entry["consecutive_failures"] = 0
                entry["paused_until"] = None
                entry["last_notified_date"] = None

            consecutive = _as_int(entry.get("consecutive_failures"), 0) + 1
            entry["consecutive_failures"] = consecutive
            entry["last_failure_reason"] = (reason or "未知错误")[:1000]
            entry["last_failure_at"] = _dt_to_str(current)
            if cookie_path:
                if recorded_path != cookie_path:
                    # 换了文件，旧 mtime 不再有参考意义
                    entry["cookie_mtime"] = None
                entry["cookie_path"] = cookie_path
                if cookie_mtime is not None:
                    entry["cookie_mtime"] = cookie_mtime

            opened = False
            if consecutive >= effective_threshold:
                paused_until = current + timedelta(seconds=self.pause_seconds)
                entry["paused_until"] = _dt_to_str(paused_until)
                opened = not was_paused

                if entry.get("last_notified_date") != today:
                    entry["last_notified_date"] = today
                    result["should_notify"] = True

                result["paused_until"] = paused_until
            else:
                entry["paused_until"] = None

            result["opened_circuit"] = opened
            result["consecutive_failures"] = consecutive
            return entry

        try:
            self._update_task(task_key, _apply)
        except Exception as exc:
            _warn(f"记录任务 '{task_key}' 失败计数失败（已忽略）: {exc!r}")
            result["error"] = repr(exc)
        return result
