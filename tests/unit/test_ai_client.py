import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.infrastructure.external.ai_client import AIClient, _sanitize_no_proxy_env
from src.services.ai_request_compat import build_responses_input


def _build_fake_client(responses_create_impl, chat_create_impl=None):
    responses = SimpleNamespace(create=responses_create_impl)
    chat = SimpleNamespace(
        completions=SimpleNamespace(create=chat_create_impl or responses_create_impl)
    )
    return SimpleNamespace(responses=responses, chat=chat)


def test_build_messages_without_images_uses_text_only_content():
    client = AIClient.__new__(AIClient)

    messages = client._build_messages(
        {"商品信息": {"商品标题": "MacBook Pro M2"}, "卖家信息": {"卖家信用等级": "优秀"}},
        [],
        "只分析文字描述和卖家资质。",
    )

    content = messages[0]["content"]
    assert isinstance(content, str)
    assert "MacBook Pro M2" in content
    assert "未提供商品图片" in content


def test_build_messages_with_images_uses_multimodal_content(monkeypatch):
    client = AIClient.__new__(AIClient)
    monkeypatch.setattr(AIClient, "encode_image", staticmethod(lambda _path: "ZmFrZQ=="))

    messages = client._build_messages(
        {"商品信息": {"商品标题": "MacBook Pro M2"}},
        ["fake-image.jpg"],
        "结合图片和文字综合判断。",
    )

    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[-1]["type"] == "text"


def test_build_responses_input_converts_multimodal_messages():
    result = build_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ZmFrZQ=="}},
                    {"type": "text", "text": "hello"},
                ],
            }
        ]
    )

    assert result == [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64,ZmFrZQ==",
                    "detail": "auto",
                },
                {"type": "input_text", "text": "hello"},
            ],
        }
    ]


def test_call_ai_retries_without_structured_output_when_model_rejects_it():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=True,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `response_format.type` specified in "
                "the request are not valid: `json_object` is not supported by "
                "this model.', 'param': 'response_format.type'}}"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}')
                )
            ]
        )

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0]["messages"][0]["content"] == "hi"
    assert request_history[0]["response_format"]["type"] == "json_object"
    assert "response_format" not in request_history[1]


def test_call_ai_falls_back_to_responses_when_chat_completions_api_is_missing():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=True,
        enable_thinking=False,
    )
    request_history = []

    async def fake_chat_create(**kwargs):
        request_history.append(("chat", kwargs))
        raise Exception("Error code: 404 - page not found")

    async def fake_responses_create(**kwargs):
        request_history.append(("responses", kwargs))
        if len([item for item in request_history if item[0] == "responses"]) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `text.format.type` specified in "
                "the request are not valid: `json_object` is not supported by "
                "this model.', 'param': 'text.format.type'}}"
            )
        return SimpleNamespace(output_text='{"ok":true}')

    client.client = _build_fake_client(fake_responses_create, fake_chat_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0][0] == "chat"
    assert request_history[1][0] == "responses"
    assert request_history[1][1]["text"]["format"]["type"] == "json_object"
    assert request_history[2][0] == "responses"
    assert "text" not in request_history[2][1]


def test_call_ai_retries_without_temperature_when_gateway_rejects_it():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception("temperature is not supported by this gateway")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}')
                )
            ]
        )

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0]["temperature"] == 0.1
    assert "temperature" not in request_history[1]


def test_call_ai_retries_when_response_content_is_empty():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) < 4:
            return SimpleNamespace(output_text="")
        return SimpleNamespace(output_text='{"ok":true}')

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert len(request_history) == 4


def test_call_ai_raises_after_all_empty_response_retries_are_exhausted():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        return SimpleNamespace(output_text="")

    client.client = _build_fake_client(fake_create)

    with pytest.raises(ValueError, match="AI响应内容为空"):
        asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert len(request_history) == 4


def test_close_closes_underlying_async_client_and_clears_reference():
    client = AIClient.__new__(AIClient)
    close_state = {"closed": False}

    async def fake_close():
        close_state["closed"] = True

    client.client = SimpleNamespace(close=fake_close)

    asyncio.run(client.close())

    assert close_state["closed"] is True
    assert client.client is None


def test_parse_response_uses_first_json_object_when_response_contains_multiple_objects():
    client = AIClient.__new__(AIClient)

    result = client._parse_response("""```json
{"ok": true, "reason": "first"}
{"ok": false, "reason": "second"}
```""")

    assert result == {"ok": True, "reason": "first"}


# -- _sanitize_no_proxy_env tests --


def test_sanitize_no_proxy_strips_ipv6_cidr(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.0/8,::1/128")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.0/8,::1"


def test_sanitize_no_proxy_strips_lowercase_variant(monkeypatch):
    monkeypatch.setenv("no_proxy", "localhost,::1/128,fe80::1/64")
    _sanitize_no_proxy_env()
    assert os.environ["no_proxy"] == "localhost,::1,fe80::1"


def test_sanitize_no_proxy_preserves_ipv4_cidr(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8,192.168.0.0/16")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "10.0.0.0/8,192.168.0.0/16"


def test_sanitize_no_proxy_noop_without_env(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _sanitize_no_proxy_env()


def test_sanitize_no_proxy_handles_both_keys(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "::1/128")
    monkeypatch.setenv("no_proxy", "fe80::1/10")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "::1"
    assert os.environ["no_proxy"] == "fe80::1"


def test_sanitize_no_proxy_unwraps_bracketed_ipv6(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "[::1/128],10.0.0.0/8,[not-ipv6]")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1,::1"
    assert os.environ["no_proxy"] == "::1,10.0.0.0/8,[not-ipv6]"


def test_bracketed_ipv6_no_proxy_does_not_break_openai_client(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")
    _sanitize_no_proxy_env()

    from openai import AsyncOpenAI

    AsyncOpenAI(api_key="sk-test", base_url="https://example.com/v1")


def test_legacy_config_client_initializes_with_bracketed_no_proxy(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "localhost,127.0.0.1,[::1]",
            "no_proxy": "localhost,127.0.0.1,[::1]",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://example.com/v1",
            "OPENAI_MODEL_NAME": "fake-model",
            "PROXY_URL": "",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os\n"
                "from openai import AsyncOpenAI\n"
                "from src.config import client\n"
                "assert isinstance(client, AsyncOpenAI)\n"
                "assert '[::1]' not in os.environ.get('NO_PROXY', '')\n"
                "assert '[::1]' not in os.environ.get('no_proxy', '')\n"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_call_ai_switches_to_max_completion_tokens_when_model_rejects_max_tokens():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception(
                "Error code: 400 - Unsupported parameter: 'max_tokens' is not supported "
                "with this model. Use 'max_completion_tokens' instead."
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
        )

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(
        client._call_ai([{"role": "user", "content": "hi"}], max_output_tokens=8192)
    )

    assert response == '{"ok":true}'
    assert request_history[0]["max_tokens"] == 8192
    assert "max_tokens" not in request_history[1]
    assert request_history[1]["max_completion_tokens"] == 8192
    assert request_history[1]["temperature"] == 0.1


# -- 超时 / 重试分类 / 兼容性缓存 / 截断 / 图片 --

import time

import httpx
import openai

import src.infrastructure.external.ai_client as ai_client_module
from src.services.ai_request_compat import AIRuntimeLimits, CompatProfile
from src.services.ai_response_parser import AIResponseTruncatedError


def _settings(**overrides):
    values = dict(model_name="fake-model", enable_response_format=True, enable_thinking=False)
    values.update(overrides)
    return SimpleNamespace(**values)


def _status_error(status: int, message: str):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=None)


def _chat_response(content='{"ok":true}', finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)]
    )


def test_initialize_client_passes_timeout_and_max_retries(monkeypatch):
    captured = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ai_client_module, "AsyncOpenAI", _FakeAsyncOpenAI)
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model_name="m",
        proxy_url=None,
        is_configured=lambda: True,
    )
    client.limits = AIRuntimeLimits(request_timeout_seconds=42, sdk_max_retries=0)

    assert client._initialize_client() is not None
    assert captured["timeout"] == 42
    assert captured["max_retries"] == 0
    assert captured["base_url"] == "https://example.test/v1"


def test_call_ai_fails_fast_on_authentication_error():
    client = AIClient.__new__(AIClient)
    client.settings = _settings()
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(401, "Error code: 401 - Incorrect API key provided")

    client.client = _build_fake_client(fake_create)

    with pytest.raises(openai.APIStatusError):
        asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert calls["n"] == 1


def test_call_ai_backs_off_on_server_errors_then_succeeds():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    client.retry_backoff_seconds = (0.01, 0.01, 0.01)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _status_error(503, "Error code: 503 - overloaded")
        return _chat_response()

    client.client = _build_fake_client(fake_create)

    assert asyncio.run(client._call_ai([{"role": "user", "content": "hi"}])) == '{"ok":true}'
    assert calls["n"] == 3


def test_call_ai_gives_up_after_backoff_attempts_are_exhausted():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    client.retry_backoff_seconds = (0, 0, 0)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(429, "Error code: 429 - rate limited")

    client.client = _build_fake_client(fake_create)

    with pytest.raises(openai.APIStatusError):
        asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert calls["n"] == 4


def test_call_ai_reuses_compat_profile_across_calls_and_resets_on_refresh():
    client = AIClient.__new__(AIClient)
    client.settings = _settings()
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if "response_format" in kwargs:
            raise Exception(
                "Error code: 400 - response_format.type `json_object` is not supported by this model."
            )
        return _chat_response()

    client.client = _build_fake_client(fake_create)

    asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))
    assert len(request_history) == 2

    asyncio.run(client._call_ai([{"role": "user", "content": "hi again"}]))
    assert len(request_history) == 3
    assert "response_format" not in request_history[2]
    assert client.compat_profile.json_output_supported is False

    client.compat_profile = CompatProfile()  # refresh() 做的事
    asyncio.run(client._call_ai([{"role": "user", "content": "third"}]))
    assert "response_format" in request_history[3]


def test_call_ai_raises_output_limit_once_when_truncated():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            return _chat_response('{"ok": tr', "length")
        return _chat_response()

    client.client = _build_fake_client(fake_create)

    assert asyncio.run(client._call_ai([{"role": "user", "content": "hi"}])) == '{"ok":true}'
    assert request_history[0]["max_tokens"] == 4000
    assert request_history[1]["max_tokens"] == 6000


def test_call_ai_fails_when_still_truncated_after_raising_limit():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        return _chat_response('{"ok": tr', "length")

    client.client = _build_fake_client(fake_create)

    with pytest.raises(AIResponseTruncatedError):
        asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert calls["n"] == 2


def test_call_ai_passes_per_request_timeout_when_given():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        return _chat_response()

    client.client = _build_fake_client(fake_create)

    asyncio.run(client._call_ai([{"role": "user", "content": "hi"}], timeout=7.5))
    assert request_history[0]["timeout"] == 7.5

    asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))
    assert "timeout" not in request_history[1]


def test_analyze_enforces_total_timeout_and_returns_none():
    client = AIClient.__new__(AIClient)
    client.settings = _settings(enable_response_format=False)
    client.limits = AIRuntimeLimits(total_timeout_seconds=0.05)

    async def fake_create(**_kwargs):
        await asyncio.sleep(5)
        return _chat_response()

    client.client = _build_fake_client(fake_create)
    started = time.monotonic()

    result = asyncio.run(client.analyze({"商品信息": {"商品标题": "x"}}, [], "prompt"))

    assert result is None
    assert time.monotonic() - started < 2


def test_build_messages_uses_detected_mime_and_caps_images(tmp_path):
    client = AIClient.__new__(AIClient)
    client.limits = AIRuntimeLimits(max_images_per_item=1, max_image_bytes=64)
    png = tmp_path / "a.jpg"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    jpeg = tmp_path / "b.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 8)

    messages = client._build_messages({"商品信息": {"商品标题": "x"}}, [str(png), str(jpeg)], "p")

    content = messages[0]["content"]
    images = [item for item in content if item["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")

    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    messages = client._build_messages({"商品信息": {"商品标题": "x"}}, [str(big)], "p")
    assert isinstance(messages[0]["content"], str)  # 唯一一张图太大被跳过，退回纯文本
