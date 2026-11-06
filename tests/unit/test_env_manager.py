import os

import pytest

from src.infrastructure.config import env_manager as env_manager_module
from src.infrastructure.config.env_manager import EnvManager


def test_get_value_prefers_env_file_when_key_present(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEBHOOK_URL=https://hooks.example.com/new\n", encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.com/old")

    manager = EnvManager(str(env_file))

    assert manager.get_value("WEBHOOK_URL") == "https://hooks.example.com/new"


def test_get_value_falls_back_to_runtime_when_key_missing_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.com/runtime")

    manager = EnvManager(str(env_file))

    assert manager.get_value("WEBHOOK_URL") == "https://hooks.example.com/runtime"


ORIGINAL_ENV = (
    "# AI 设置\n"
    "OPENAI_API_KEY=sk-old\n"
    "\n"
    "# 通知模板里的 ${title} 是运行时占位符，不是 .env 变量\n"
    'WEBHOOK_BODY={"title":"${title}","price":"${price}"}\n'
    "KEY_WITHOUT_VALUE\n"
    "NTFY_TOPIC_URL=https://ntfy.sh/demo   # 行尾注释\n"
)


def _manager(tmp_path, content=ORIGINAL_ENV):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return EnvManager(str(env_file)), env_file


def test_read_env_does_not_interpolate_placeholders(tmp_path):
    manager, _ = _manager(tmp_path)
    assert manager.read_env()["WEBHOOK_BODY"] == '{"title":"${title}","price":"${price}"}'


def test_update_keeps_comments_blank_lines_placeholders_and_order(tmp_path):
    manager, env_file = _manager(tmp_path)

    assert manager.apply_changes({"OPENAI_API_KEY": "sk-new", "NEW_KEY": "v1"}) is True

    text = env_file.read_text(encoding="utf-8")
    assert text == (
        "# AI 设置\n"
        "OPENAI_API_KEY=sk-new\n"
        "\n"
        "# 通知模板里的 ${title} 是运行时占位符，不是 .env 变量\n"
        'WEBHOOK_BODY={"title":"${title}","price":"${price}"}\n'
        "KEY_WITHOUT_VALUE\n"
        "NTFY_TOPIC_URL=https://ntfy.sh/demo   # 行尾注释\n"
        "NEW_KEY=v1\n"
    )
    # 再读回来，模板里的 ${...} 仍然原样
    assert manager.read_env()["WEBHOOK_BODY"] == '{"title":"${title}","price":"${price}"}'
    assert manager.read_env()["OPENAI_API_KEY"] == "sk-new"


def test_delete_removes_only_that_line(tmp_path):
    manager, env_file = _manager(tmp_path)

    assert manager.apply_changes({}, deletions=["OPENAI_API_KEY", "MISSING"]) is True

    text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in text
    assert text.startswith("# AI 设置\n\n# 通知模板")
    assert "KEY_WITHOUT_VALUE\n" in text


def test_values_with_special_characters_round_trip(tmp_path):
    manager, _ = _manager(tmp_path, "")
    body = '{"a": "x y", "quote": "\\"q\\""}\nline2'
    assert manager.set_value("WEBHOOK_BODY", body) is True
    assert manager.read_env()["WEBHOOK_BODY"] == body


def test_failed_write_leaves_original_file_untouched(tmp_path, monkeypatch):
    manager, env_file = _manager(tmp_path)

    def exploding_mkstemp(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(env_manager_module.tempfile, "mkstemp", exploding_mkstemp)

    assert manager.apply_changes({"OPENAI_API_KEY": "sk-new"}) is False
    assert env_file.read_text(encoding="utf-8") == ORIGINAL_ENV
    assert list(tmp_path.iterdir()) == [env_file]


def test_rename_failure_falls_back_to_in_place_write(tmp_path, monkeypatch):
    """docker 把 ./.env 以单文件 bind mount 进容器时 os.replace 会报 EBUSY，此时原地写入。"""
    manager, env_file = _manager(tmp_path)

    def busy_replace(*_args, **_kwargs):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(env_manager_module.os, "replace", busy_replace)

    assert manager.apply_changes({"OPENAI_API_KEY": "sk-new"}) is True
    text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-new\n" in text
    assert 'WEBHOOK_BODY={"title":"${title}","price":"${price}"}' in text
    assert list(tmp_path.iterdir()) == [env_file]


def test_noop_update_does_not_rewrite_file(tmp_path):
    manager, env_file = _manager(tmp_path)
    before = env_file.stat().st_mtime_ns
    os.utime(env_file, ns=(before - 10_000_000_000, before - 10_000_000_000))
    stamped = env_file.stat().st_mtime_ns

    assert manager.apply_changes({"OPENAI_API_KEY": "sk-old"}) is True
    assert env_file.stat().st_mtime_ns == stamped


def test_export_prefix_is_preserved_on_update(tmp_path):
    manager, env_file = _manager(tmp_path, "export OPENAI_API_KEY=sk-old\nOTHER=1\n")

    assert manager.apply_changes({"OPENAI_API_KEY": "sk-new"}) is True

    assert env_file.read_text(encoding="utf-8") == "export OPENAI_API_KEY=sk-new\nOTHER=1\n"
    assert manager.read_env()["OPENAI_API_KEY"] == "sk-new"
