"""布尔环境变量三处统一解析 + TASK_LOG_RETENTION_DAYS=0 不再炸 import + 代理 URL 脱敏。"""
import importlib

import pytest

import src.config as config_module
from src.config import redact_url_userinfo
from src.infrastructure.config.env_manager import EnvManager
from src.infrastructure.config.settings import (
    AISettings,
    AppSettings,
    NotificationSettings,
    ScraperSettings,
    parse_env_bool,
)
from src.services import notification_config_service
from src.services.task_log_cleanup_service import cleanup_task_logs


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True), ("0", False),
        ("true", True), ("FALSE", False),
        ("Yes", True), ("no", False),
        ("on", True), ("OFF", False),
        ("y", True), ("N", False),
        (" true ", True), ("\t0\n", False),
    ],
)
def test_parse_env_bool_accepts_common_spellings(raw, expected):
    # default 取反，确认结果来自解析而不是回落
    assert parse_env_bool(raw, not expected) is expected


@pytest.mark.parametrize("raw", [None, "", "   ", "maybe", "2", "yes please"])
def test_parse_env_bool_falls_back_to_default_when_unrecognized(raw):
    assert parse_env_bool(raw, True) is True
    assert parse_env_bool(raw, False) is False


def test_parse_env_bool_passes_real_bools_through():
    assert parse_env_bool(True, False) is True
    assert parse_env_bool(False, True) is False


def test_pydantic_settings_use_the_shared_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 别读仓库里的 .env
    monkeypatch.setenv("RUN_HEADLESS", "0")
    monkeypatch.setenv("LOGIN_IS_EDGE", " Yes ")
    monkeypatch.setenv("RUNNING_IN_DOCKER", "maybe")  # 无法识别 -> 字段默认 False
    monkeypatch.setenv("ENABLE_THINKING", "1")
    monkeypatch.setenv("ENABLE_RESPONSE_FORMAT", "garbage")  # 无法识别 -> 字段默认 True
    monkeypatch.setenv("SKIP_AI_ANALYSIS", "on")
    monkeypatch.setenv("PCURL_TO_MOBILE", "off")

    scraper = ScraperSettings()
    assert scraper.run_headless is False
    assert scraper.login_is_edge is True
    assert scraper.running_in_docker is False

    ai = AISettings()
    assert ai.enable_thinking is True
    assert ai.enable_response_format is True
    assert ai.skip_analysis is True

    assert NotificationSettings().pcurl_to_mobile is False


def test_task_log_retention_zero_means_no_cleanup_instead_of_import_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_LOG_RETENTION_DAYS", "0")

    assert AppSettings().task_log_retention_days == 0
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "task_1.log").write_text("x", encoding="utf-8")
    assert cleanup_task_logs(str(logs_dir), keep_days=0) == []
    assert (logs_dir / "task_1.log").exists()


def _reload_config_with_env(tmp_path, **env):
    """在隔离目录 + 指定环境变量下重新执行 src/config.py，取快照后恢复到原状态。"""
    keys = ("RUN_HEADLESS", "ENABLE_THINKING", "LOGIN_IS_EDGE", "ENABLE_RESPONSE_FORMAT", "SKIP_AI_ANALYSIS")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir(tmp_path)
            for name, value in env.items():
                mp.setenv(name, value)
            module = importlib.reload(config_module)
            return {key: getattr(module, key) for key in keys}
    finally:
        importlib.reload(config_module)


def test_scraper_config_parses_bool_env_like_the_settings_models(tmp_path):
    # 老实现：RUN_HEADLESS 只认字面 "false"，ENABLE_THINKING 只认字面 "true"
    snapshot = _reload_config_with_env(
        tmp_path, RUN_HEADLESS="0", ENABLE_THINKING="1", LOGIN_IS_EDGE="YES", ENABLE_RESPONSE_FORMAT="off", SKIP_AI_ANALYSIS="nonsense"
    )
    assert snapshot["RUN_HEADLESS"] is False
    assert snapshot["ENABLE_THINKING"] is True
    assert snapshot["LOGIN_IS_EDGE"] is True
    assert snapshot["ENABLE_RESPONSE_FORMAT"] is False
    assert snapshot["SKIP_AI_ANALYSIS"] is False  # 回落默认


def test_config_no_longer_exposes_unused_pcurl_flag():
    assert not hasattr(config_module, "PCURL_TO_MOBILE")


def test_notification_loader_uses_shared_parser(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PCURL_TO_MOBILE=off\n", encoding="utf-8")
    monkeypatch.setattr(notification_config_service, "env_manager", EnvManager(str(env_file)))
    assert notification_config_service.load_notification_settings().pcurl_to_mobile is False

    env_file.write_text("PCURL_TO_MOBILE=1\n", encoding="utf-8")
    monkeypatch.setattr(notification_config_service, "env_manager", EnvManager(str(env_file)))
    assert notification_config_service.load_notification_settings().pcurl_to_mobile is True

    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(notification_config_service, "env_manager", EnvManager(str(env_file)))
    assert notification_config_service.load_notification_settings().pcurl_to_mobile is True  # 默认


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://user:p%40ss@proxy.example:8080/path?x=1", "http://***@proxy.example:8080/path?x=1"),
        ("socks5://alice:secret@10.0.0.1:1080", "socks5://***@10.0.0.1:1080"),
        ("http://token@proxy.example", "http://***@proxy.example"),
        ("http://proxy.example:8080", "http://proxy.example:8080"),
        ("", ""),
    ],
)
def test_redact_url_userinfo(url, expected):
    assert redact_url_userinfo(url) == expected
    assert "secret" not in redact_url_userinfo(url)
