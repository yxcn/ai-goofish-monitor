"""
环境变量管理器
负责读取和更新 .env 文件，并在读取时回退到运行时环境变量

写入规则：已有键按行就地替换，新键追加到末尾，注释/空行/无值键原样保留；
先写同目录临时文件再 os.replace，替换失败（.env 以单文件 bind mount 进容器时会 EBUSY/EXDEV）
回退为原地写入 + fsync。所有读写用一把锁串行化。
"""
import io
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import dotenv_values
from dotenv.parser import parse_stream


_PLAIN_ENV_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_./:-]+$")


class EnvManager:
    """环境变量管理器"""

    def __init__(self, env_file: str = ".env"):
        self.env_file = Path(env_file)
        self._lock = threading.RLock()
        self._ensure_env_file_exists()

    def _ensure_env_file_exists(self):
        """确保 .env 文件存在"""
        with self._lock:
            if not self.env_file.exists():
                self.env_file.touch()

    def read_env(self) -> Dict[str, str]:
        """读取所有环境变量（不做 ${VAR} 插值：WEBHOOK_BODY 之类的模板里 ${title} 必须原样保留）"""
        with self._lock:
            if not self.env_file.exists():
                return {}
            loaded = dotenv_values(self.env_file, encoding="utf-8", interpolate=False)
        return {
            key: value
            for key, value in loaded.items()
            if key and value is not None
        }

    def get_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取单个环境变量的值，优先读取 .env，缺失时再回退到运行时环境变量"""
        env_vars = self.read_env()
        if key in env_vars:
            return env_vars[key]

        runtime_value = os.getenv(key)
        if runtime_value is not None:
            return runtime_value

        return default

    def update_values(self, updates: Dict[str, str]) -> bool:
        """批量更新环境变量"""
        return self.apply_changes(updates=updates)

    def apply_changes(
        self,
        updates: Dict[str, str],
        deletions: List[str] | None = None,
    ) -> bool:
        """批量更新并删除环境变量"""
        try:
            with self._lock:
                return self._rewrite_env(dict(updates or {}), list(deletions or []))
        except Exception as e:
            print(f"更新环境变量失败: {e}")
            return False

    def set_value(self, key: str, value: str) -> bool:
        """设置单个环境变量"""
        return self.update_values({key: value})

    def delete_keys(self, keys: List[str]) -> bool:
        """删除指定的环境变量"""
        try:
            with self._lock:
                return self._rewrite_env({}, list(keys))
        except Exception as e:
            print(f"删除环境变量失败: {e}")
            return False

    # ------------------------------------------------------------------ 写入

    def _rewrite_env(self, updates: Dict[str, str], deletions: List[str]) -> bool:
        """按行就地改写：只动被更新/删除的键所在的那几行，其余内容逐字节保留。"""
        self._ensure_env_file_exists()
        original = self.env_file.read_text(encoding="utf-8")
        new_text = self._render(original, updates, deletions)
        if new_text == original:
            return True
        self._write_text_atomic(new_text)
        return True

    def _render(self, original: str, updates: Dict[str, str], deletions: List[str]) -> str:
        pending = dict(updates)
        to_delete = set(deletions)
        chunks: List[str] = []
        # parse_stream 给出每个绑定在原文里的精确片段（含注释、空行、多行引号值），据此逐段重组
        for binding in parse_stream(io.StringIO(original)):
            key = binding.key
            text = binding.original.string
            if key is None or binding.error:
                chunks.append(text)
                continue
            if key in to_delete:
                pending.pop(key, None)
                continue
            if key in pending:
                leading = text[: len(text) - len(text.lstrip("\r\n"))]
                prefix = "export " if text.lstrip().startswith("export ") else ""
                chunks.append(f"{leading}{prefix}{key}={self._serialize_value(pending.pop(key))}\n")
                continue
            chunks.append(text)

        result = "".join(chunks)
        if pending:
            if result and not result.endswith("\n"):
                result += "\n"
            for key, value in pending.items():
                result += f"{key}={self._serialize_value(value)}\n"
        return result

    def _write_text_atomic(self, text: str) -> None:
        directory = self.env_file.parent if str(self.env_file.parent) else Path(".")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.env_file.name}.", suffix=".tmp", dir=str(directory))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self._copy_mode(self.env_file, tmp_path)
            try:
                os.replace(tmp_path, self.env_file)
            except OSError as exc:
                # bind mount 的单文件不允许 rename 替换（EBUSY/EXDEV/EPERM）：退回原地写入
                print(f"原子替换 {self.env_file} 失败 ({exc})，改为原地写入")
                self._write_in_place(text)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _write_in_place(self, text: str) -> None:
        with open(self.env_file, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _copy_mode(source: Path, target: Path) -> None:
        try:
            os.chmod(target, source.stat().st_mode & 0o7777)
        except OSError:
            pass

    def _write_env(self, env_vars: Dict[str, str]) -> bool:
        """兼容旧调用：用给定键值全量重写（仍走原子写入）。"""
        try:
            with self._lock:
                lines = "".join(f"{key}={self._serialize_value(value)}\n" for key, value in env_vars.items())
                self._write_text_atomic(lines)
            return True
        except Exception as e:
            print(f"写入 .env 文件失败: {e}")
            return False

    def _serialize_value(self, value: str) -> str:
        text = str(value)
        if text == "":
            return ""
        if _PLAIN_ENV_VALUE_PATTERN.fullmatch(text):
            return text
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'


# 全局实例
env_manager = EnvManager()
