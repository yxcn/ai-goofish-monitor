"""推送侧 NotificationService 进程内复用：.env 不变只构造一次，.env 变了自动重建，reset 强制重建。"""
import asyncio
import os
from types import SimpleNamespace

import pytest

import src.ai_handler as ai_handler


class _FakeService:
    def __init__(self, label: str):
        self.label = label
        self.clients = [object()]
        self.sent = []

    async def send_notification(self, product_data, reason):
        self.sent.append((product_data, reason))
        return {"ntfy": {"success": True, "message": f"ok via {self.label}"}}


@pytest.fixture
def counted_builder(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NTFY_TOPIC_URL=https://ntfy.example/topic\n", encoding="utf-8")
    monkeypatch.setattr(ai_handler, "env_manager", SimpleNamespace(env_file=env_file))
    builds = {"n": 0, "services": []}

    def fake_build(settings=None):
        builds["n"] += 1
        service = _FakeService(f"build-{builds['n']}")
        builds["services"].append(service)
        return service

    monkeypatch.setattr(ai_handler, "build_notification_service", fake_build)
    ai_handler.reset_notification_service()
    yield builds, env_file
    ai_handler.reset_notification_service()


def _send(n: int = 1):
    results = None
    for _ in range(n):
        results = asyncio.run(ai_handler.send_ntfy_notification({"商品标题": "x", "当前售价": "1"}, "好价"))
    return results


def test_service_is_built_once_and_reused_across_notifications(counted_builder):
    builds, _env_file = counted_builder

    results = _send(5)

    assert builds["n"] == 1
    assert results == {"ntfy": {"success": True, "message": "ok via build-1"}}
    assert len(builds["services"][0].sent) == 5


def test_service_is_rebuilt_when_env_file_changes(counted_builder):
    builds, env_file = counted_builder
    _send(2)
    assert builds["n"] == 1

    # Web 端改了通知设置：内容和 mtime 都变
    stat = env_file.stat()
    env_file.write_text("NTFY_TOPIC_URL=https://ntfy.example/other\n", encoding="utf-8")
    os.utime(env_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    results = _send(3)

    assert builds["n"] == 2
    assert results["ntfy"]["message"] == "ok via build-2"
    assert len(builds["services"][1].sent) == 3


def test_service_is_rebuilt_on_mtime_change_even_with_same_size(counted_builder):
    builds, env_file = counted_builder
    _send()
    stat = env_file.stat()
    os.utime(env_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    _send()

    assert builds["n"] == 2


def test_reset_notification_service_forces_rebuild(counted_builder):
    builds, _env_file = counted_builder
    _send(2)
    assert builds["n"] == 1

    ai_handler.reset_notification_service()
    _send()

    assert builds["n"] == 2


def test_missing_env_file_still_caches(counted_builder, monkeypatch, tmp_path):
    builds, _env_file = counted_builder
    monkeypatch.setattr(ai_handler, "env_manager", SimpleNamespace(env_file=tmp_path / "missing.env"))
    ai_handler.reset_notification_service()

    _send(3)

    assert builds["n"] == 1


def test_get_notification_service_returns_same_instance(counted_builder):
    first = ai_handler.get_notification_service()
    second = ai_handler.get_notification_service()

    assert first is second


def test_no_clients_skips_sending_without_rebuilding(counted_builder, monkeypatch, capsys):
    builds, _env_file = counted_builder

    def fake_build(settings=None):
        builds["n"] += 1
        service = _FakeService("empty")
        service.clients = []
        return service

    monkeypatch.setattr(ai_handler, "build_notification_service", fake_build)
    ai_handler.reset_notification_service()

    assert _send(2) == {}
    assert builds["n"] == 1
    assert "未在 .env 文件中配置任何通知服务" in capsys.readouterr().out
