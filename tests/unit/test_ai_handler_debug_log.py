"""AI 调试日志：只在 AI_DEBUG_MODE 下写，内容是请求摘要 + 结果 / 错误，文件名带商品 ID，清理一天一次。"""
import asyncio
import json
import os
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

import src.ai_handler as ai_handler
import src.config as app_config


OK_RESPONSE = (
    '{"prompt_version":"v1","is_recommended":true,'
    '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
)


def _build_fake_client(create_impl):
    responses = SimpleNamespace(create=create_impl)
    chat = SimpleNamespace(completions=SimpleNamespace(create=create_impl))
    return SimpleNamespace(responses=responses, chat=chat)


def _status_error(status: int, message: str):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ai_handler.reset_compat_profile()
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (0, 0, 0))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "_last_ai_log_cleanup_at", None)
    yield
    ai_handler.reset_compat_profile()


def _write_fake_jpeg(path, size: int) -> None:
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (size - 4)
    path.write_bytes(body)


def _record():
    return {
        "任务名称": "Sony A7M4",
        "商品信息": {"商品ID": "9001", "商品标题": "测试商品", "商品图片列表": ["https://img.example/a.jpg"]},
        "卖家信息": {"卖家昵称": "小明"},
    }


def test_nothing_is_written_or_printed_when_debug_is_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", False)

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=OK_RESPONSE)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))

    result = asyncio.run(ai_handler.get_ai_analysis(_record(), image_paths=[], prompt_text="请输出 JSON"))

    assert result["is_recommended"] is True
    assert not (tmp_path / "logs").exists()
    out = capsys.readouterr().out
    assert "AI分析请求已保存" not in out
    assert "AI DEBUG" not in out


def test_debug_log_contains_request_summary_and_parsed_result(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", True)
    image_path = tmp_path / "pic.jpg"
    _write_fake_jpeg(image_path, 300)

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=OK_RESPONSE)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))

    result = asyncio.run(
        ai_handler.get_ai_analysis(_record(), image_paths=[str(image_path)], prompt_text="请输出 JSON")
    )

    assert result["is_recommended"] is True
    logs_dir = tmp_path / "logs" / "ai"
    files = sorted(os.listdir(logs_dir))
    assert len(files) == 1
    assert files[0].endswith("_9001.log")

    payload = json.loads((logs_dir / files[0]).read_text(encoding="utf-8"))
    assert payload["product_id"] == "9001"
    assert payload["task_name"] == "Sony A7M4"
    assert payload["title"] == "测试商品"
    assert payload["model"] == "fake-model"
    assert payload["api_mode"] == ai_handler.get_compat_profile().api_mode
    assert payload["message_count"] == 1
    assert payload["text_head"].startswith("请基于你的专业知识")
    assert "请输出 JSON" in payload["text_head"]
    assert len(payload["text_head"]) <= ai_handler.AI_DEBUG_LOG_TEXT_CHARS
    assert payload["text_chars"] >= len(payload["text_head"])
    assert payload["image_count"] == 1
    assert payload["image_bytes"] == [300]
    assert payload["result"]["is_recommended"] is True
    assert payload["error"] is None
    # 图片 URL 列表已从送模型的 JSON 里去掉
    assert "img.example" not in payload["text_head"]


def test_debug_log_truncates_text_to_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", True)

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=OK_RESPONSE)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))

    asyncio.run(ai_handler.get_ai_analysis(_record(), image_paths=[], prompt_text="长提示词" * 2000))

    logs_dir = tmp_path / "logs" / "ai"
    payload = json.loads((logs_dir / os.listdir(logs_dir)[0]).read_text(encoding="utf-8"))
    assert len(payload["text_head"]) == ai_handler.AI_DEBUG_LOG_TEXT_CHARS
    assert payload["text_chars"] > ai_handler.AI_DEBUG_LOG_TEXT_CHARS


def test_debug_log_records_final_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", True)

    async def fake_create(**_kwargs):
        raise _status_error(401, "Error code: 401 - Incorrect API key provided")

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))

    with pytest.raises(openai.APIStatusError):
        asyncio.run(ai_handler.get_ai_analysis(_record(), image_paths=[], prompt_text="请输出 JSON"))

    logs_dir = tmp_path / "logs" / "ai"
    files = os.listdir(logs_dir)
    assert len(files) == 1 and files[0].endswith("_9001.log")
    payload = json.loads((logs_dir / files[0]).read_text(encoding="utf-8"))
    assert payload["result"] is None
    assert payload["error"].startswith("APIStatusError:")
    assert "401" in payload["error"]


def test_log_cleanup_runs_at_most_once_per_day(monkeypatch, tmp_path):
    calls = {"n": 0}
    monkeypatch.setattr(ai_handler, "cleanup_ai_logs", lambda *_a, **_k: calls.__setitem__("n", calls["n"] + 1))

    assert ai_handler._maybe_cleanup_ai_logs(str(tmp_path)) is True
    assert ai_handler._maybe_cleanup_ai_logs(str(tmp_path)) is False
    assert ai_handler._maybe_cleanup_ai_logs(str(tmp_path)) is False
    assert calls["n"] == 1

    # 过了一天再调才会再清理
    monkeypatch.setattr(
        ai_handler,
        "_last_ai_log_cleanup_at",
        time.monotonic() - ai_handler.AI_DEBUG_LOG_CLEANUP_INTERVAL_SECONDS - 1,
    )
    assert ai_handler._maybe_cleanup_ai_logs(str(tmp_path)) is True
    assert calls["n"] == 2


def test_debug_mode_cleans_logs_only_once_across_many_products(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_handler, "AI_DEBUG_MODE", True)
    calls = {"n": 0}
    monkeypatch.setattr(ai_handler, "cleanup_ai_logs", lambda *_a, **_k: calls.__setitem__("n", calls["n"] + 1))

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=OK_RESPONSE)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))

    for index in range(3):
        record = _record()
        record["商品信息"]["商品ID"] = f"id-{index}"
        asyncio.run(ai_handler.get_ai_analysis(record, image_paths=[], prompt_text="请输出 JSON"))

    assert calls["n"] == 1
    names = sorted(os.listdir(tmp_path / "logs" / "ai"))
    assert len(names) == 3
    assert {name.rsplit("_", 1)[1] for name in names} == {"id-0.log", "id-1.log", "id-2.log"}


def test_cleanup_ai_logs_still_parses_new_filenames_with_product_id(tmp_path):
    logs_dir = tmp_path / "logs" / "ai"
    logs_dir.mkdir(parents=True)
    old = logs_dir / "20200101_000000_old-item.log"
    fresh = logs_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_fresh-item.log"
    junk = logs_dir / "notes.txt"
    for path in (old, fresh, junk):
        path.write_text("{}", encoding="utf-8")

    ai_handler.cleanup_ai_logs(str(logs_dir), keep_days=1)

    assert not old.exists()
    assert fresh.exists()
    assert junk.exists()
