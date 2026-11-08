"""Claude 作为第二个 AI 提供方：消息转换、重试、商品分析 / 生成标准分支、系统设置接口。"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.ai_handler as ai_handler
import src.infrastructure.external.ai_client as ai_client_module
from src.api import dependencies as deps
from src.api.routes import settings as settings_routes
from src.infrastructure.config.settings import AISettings, normalize_ai_provider
from src.infrastructure.external.ai_client import AIClient
from src.infrastructure.external.claude_client import (
    CLAUDE_MIN_OUTPUT_TOKENS,
    DEFAULT_CLAUDE_BASE_URL,
    DEFAULT_CLAUDE_MODEL,
    REFUSAL_FALLBACK_BETA,
    ClaudeCallState,
    ClaudeRefusalError,
    build_claude_messages,
    build_claude_request_params,
    call_claude_with_retries,
    claude_total_timeout,
    create_async_claude_client,
    extract_claude_text,
)
from src.services.ai_request_compat import BACKOFF, classify_ai_error
from src.services.ai_response_parser import AIResponseTruncatedError, EmptyAIResponseError


VALID_JSON = (
    '{"prompt_version":"v1","is_recommended":true,"reason":"ok",'
    '"risk_tags":[],"criteria_analysis":{"seller_type":{"status":"PASS"}}}'
)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _response(*blocks, stop_reason="end_turn", model="claude-opus-5-5", usage=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, model=model, stop_details=None, usage=usage)


def _usage(input_tokens=1000, output_tokens=400, cache_read=2000, cache_write=500):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


class _StatusError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code


class _FakeClaude:
    """记录请求；按顺序返回 / 抛出 outcomes 里的东西。messages 和 beta.messages 共用一份记录。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create_plain)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create_beta))

    async def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _create_plain(self, **kwargs):
        return await self._next("plain", kwargs)

    async def _create_beta(self, **kwargs):
        return await self._next("beta", kwargs)


def _run_call(client, **overrides):
    params = dict(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=4000,
        state=ClaudeCallState(),
        base_url=None,
        backoff_schedule=(0, 0, 0),
        log=lambda _msg: None,
    )
    params.update(overrides)
    return asyncio.run(call_claude_with_retries(client, **params))


# ---------------------------------------------------------------- 消息转换 / 请求参数


def test_build_claude_messages_hoists_system_and_converts_data_url_images():
    system_parts, messages = build_claude_messages(
        [
            {"role": "system", "content": "分析标准"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    {"type": "text", "text": "商品 JSON"},
                ],
            },
        ]
    )

    assert system_parts == ["分析标准"]
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
                {"type": "text", "text": "商品 JSON"},
            ],
        }
    ]


def test_request_params_cache_the_system_prompt_and_never_send_temperature():
    params = build_claude_request_params(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "x"}],
        system_parts=["分析标准"],
        max_tokens=16000,
    )

    assert params["system"] == [
        {"type": "text", "text": "分析标准", "cache_control": {"type": "ephemeral"}}
    ]
    assert "temperature" not in params
    assert params["max_tokens"] == 16000


def test_extract_claude_text_skips_thinking_and_reads_stop_reason():
    thinking = SimpleNamespace(type="thinking", thinking="")
    assert extract_claude_text(_response(thinking, _text_block(" OK "))) == "OK"

    with pytest.raises(AIResponseTruncatedError) as truncated:
        extract_claude_text(_response(_text_block('{"a":'), stop_reason="max_tokens"))
    assert truncated.value.partial_content == '{"a":'

    with pytest.raises(ClaudeRefusalError):
        extract_claude_text(_response(stop_reason="refusal"))
    with pytest.raises(EmptyAIResponseError):
        extract_claude_text(_response(thinking))


def test_anthropic_connection_errors_are_retried_with_backoff():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assert classify_ai_error(anthropic.APIConnectionError(request=request)) == BACKOFF
    assert classify_ai_error(anthropic.APITimeoutError(request=request)) == BACKOFF


def test_total_timeout_leaves_room_for_two_claude_requests():
    assert claude_total_timeout(360, 240) == 480
    assert claude_total_timeout(1000, 240) == 1000
    assert claude_total_timeout(None, 240) is None


# ---------------------------------------------------------------- 带重试的调用


def test_call_retries_when_postprocess_rejects_the_text():
    client = _FakeClaude([_response(_text_block("not json")), _response(_text_block(VALID_JSON))])

    def postprocess(text):
        if text == "not json":
            raise EmptyAIResponseError("bad")
        return {"text": text}

    result, model = _run_call(client, postprocess=postprocess)

    assert result == {"text": VALID_JSON}
    assert model == "claude-opus-5-5"
    assert len(client.calls) == 2
    # 4000 是 OpenAI 用的上限；Claude 的思考也算在里面，至少给到 16000
    assert client.calls[0][1]["max_tokens"] == CLAUDE_MIN_OUTPUT_TOKENS


def test_call_backs_off_on_overloaded_then_succeeds():
    client = _FakeClaude([_StatusError(529, "overloaded"), _response(_text_block("OK"))])
    assert _run_call(client)[0] == "OK"
    assert len(client.calls) == 2


def test_call_fails_fast_on_authentication_error():
    client = _FakeClaude([_StatusError(401, "invalid x-api-key")])
    with pytest.raises(_StatusError):
        _run_call(client)
    assert len(client.calls) == 1


def test_call_raises_output_limit_only_once_when_truncated():
    client = _FakeClaude(
        [
            _response(_text_block("{"), stop_reason="max_tokens"),
            _response(_text_block("{"), stop_reason="max_tokens"),
        ]
    )
    with pytest.raises(AIResponseTruncatedError):
        _run_call(client)
    assert [call[1]["max_tokens"] for call in client.calls] == [16000, 24000]


def test_refusal_fallback_only_for_official_endpoint_and_supported_models():
    official = _FakeClaude([_response(_text_block("OK"))])
    _run_call(official, model="claude-opus-5-5", base_url="")
    kind, kwargs = official.calls[0]
    assert kind == "beta"
    assert kwargs["betas"] == [REFUSAL_FALLBACK_BETA]
    assert kwargs["fallbacks"] == "default"

    relay = _FakeClaude([_response(_text_block("OK"))])
    _run_call(relay, model="claude-opus-5-5", base_url="https://relay.example/")
    assert relay.calls[0][0] == "plain"

    haiku = _FakeClaude([_response(_text_block("OK"))])
    _run_call(haiku, model="claude-haiku-5-5")
    assert haiku.calls[0][0] == "plain"


def test_refusal_fallback_is_dropped_when_server_rejects_the_beta():
    state = ClaudeCallState()
    client = _FakeClaude(
        [
            _StatusError(400, "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header."),
            _response(_text_block("OK")),
        ]
    )

    assert _run_call(client, state=state)[0] == "OK"
    assert [call[0] for call in client.calls] == ["beta", "plain"]
    assert state.refusal_fallback_enabled is False


def test_async_client_ignores_anthropic_env_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://not-this.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    client = create_async_claude_client(api_key="sk-explicit", base_url="", timeout=10, max_retries=0)
    try:
        assert str(client.base_url).rstrip("/") == DEFAULT_CLAUDE_BASE_URL
        assert client.api_key == "sk-explicit"
    finally:
        asyncio.run(client.close())


# ---------------------------------------------------------------- 商品分析（爬虫子进程）


@pytest.fixture
def _claude_selected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ai_handler.reset_compat_profile()
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (0, 0, 0))
    monkeypatch.setattr(ai_handler, "AI_PROVIDER", "claude")
    monkeypatch.setattr(ai_handler, "CLAUDE_MODEL_NAME", "claude-opus-5-5")
    monkeypatch.setattr(ai_handler, "CLAUDE_BASE_URL", DEFAULT_CLAUDE_BASE_URL)
    monkeypatch.setattr(ai_handler, "client", None)
    yield
    ai_handler.reset_compat_profile()


def test_get_ai_analysis_uses_claude_when_selected(monkeypatch, tmp_path, _claude_selected):
    image = tmp_path / "item.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    fake = _FakeClaude(
        [_response(_text_block("```json\n" + VALID_JSON + "\n```"), model="claude-opus-5-5", usage=_usage())]
    )
    monkeypatch.setattr(ai_handler, "claude_client", fake)
    for key in ("CLAUDE_PRICE_INPUT_USD_PER_M", "CLAUDE_PRICE_OUTPUT_USD_PER_M", "CLAUDE_PRICE_CACHE_READ_USD_PER_M"):
        monkeypatch.setattr(ai_handler, key, None)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "9", "商品标题": "ThinkPad T14"}},
            image_paths=[str(image)],
            prompt_text="分析标准：只要 T14",
        )
    )

    assert result["is_recommended"] is True
    assert result["ai_provider"] == "claude"
    assert result["ai_model"] == "claude-opus-5-5"
    # 没手填单价：按内置的 Opus 5.5 官方价（4 / 20，缓存读 0.2，缓存写 5）算
    usage = result["ai_usage"]
    assert usage["calls"] == 1
    assert (usage["input_tokens"], usage["cache_read_tokens"], usage["cache_write_tokens"], usage["output_tokens"]) == (1000, 2000, 500, 400)
    assert usage["total_tokens"] == 3900
    assert usage["cost_usd"] == pytest.approx(0.0149)
    assert usage["elapsed_ms"] >= 0
    _kind, request = fake.calls[0]
    assert request["system"][0]["text"] == "分析标准：只要 T14"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    user_content = request["messages"][0]["content"]
    assert user_content[0]["type"] == "image"
    assert user_content[0]["source"]["media_type"] == "image/png"
    assert "ThinkPad T14" in user_content[-1]["text"]
    assert "分析标准：只要 T14" not in user_content[-1]["text"]
    assert "temperature" not in request


def test_get_ai_analysis_retries_claude_when_required_fields_are_missing(monkeypatch, _claude_selected):
    fake = _FakeClaude(
        [
            _response(_text_block('{"reason":"缺字段"}')),
            _response(_text_block(VALID_JSON)),
        ]
    )
    monkeypatch.setattr(ai_handler, "claude_client", fake)

    result = asyncio.run(
        ai_handler.get_ai_analysis({"商品信息": {"商品ID": "1"}}, image_paths=[], prompt_text="标准")
    )

    assert result["prompt_version"] == "v1"
    assert len(fake.calls) == 2


def test_get_ai_analysis_skips_when_claude_client_is_missing(monkeypatch, _claude_selected):
    monkeypatch.setattr(ai_handler, "claude_client", None)
    result = asyncio.run(
        ai_handler.get_ai_analysis({"商品信息": {"商品ID": "1"}}, image_paths=[], prompt_text="标准")
    )
    assert result is None


def test_openai_results_record_provider_and_model(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ai_handler.reset_compat_profile()

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=VALID_JSON, model="gpt-test-2026")

    fake_client = SimpleNamespace(
        responses=SimpleNamespace(create=fake_create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )
    monkeypatch.setattr(ai_handler, "AI_PROVIDER", "openai")
    monkeypatch.setattr(ai_handler, "client", fake_client)
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")

    result = asyncio.run(
        ai_handler.get_ai_analysis({"商品信息": {"商品ID": "1"}}, image_paths=[], prompt_text="标准")
    )

    assert result["ai_provider"] == "openai"
    assert result["ai_model"] == "gpt-test-2026"


# ---------------------------------------------------------------- 生成分析标准（AIClient）


def _claude_ai_client(fake):
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        uses_claude=True,
        claude_api_key="sk-test",
        claude_base_url="",
        claude_model_name="",
        proxy_url=None,
        enable_response_format=True,
        is_configured=lambda: True,
    )
    client.client = fake
    client.retry_backoff_seconds = (0, 0, 0)
    return client


def test_ai_client_call_ai_goes_to_claude_with_default_model():
    fake = _FakeClaude([_response(_text_block("生成的分析标准"))])
    client = _claude_ai_client(fake)

    text = asyncio.run(
        client._call_ai(
            [{"role": "user", "content": "生成标准"}],
            temperature=0.5,
            max_output_tokens=8192,
            enable_json_output=False,
            timeout=300.0,
        )
    )

    assert text == "生成的分析标准"
    _kind, request = fake.calls[0]
    assert request["model"] == DEFAULT_CLAUDE_MODEL
    assert request["max_tokens"] == CLAUDE_MIN_OUTPUT_TOKENS
    assert request["timeout"] == 300.0
    assert "temperature" not in request
    assert "response_format" not in request


def test_ai_client_initializes_claude_client_when_selected(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(ai_client_module, "create_async_claude_client", fake_create)
    client = _claude_ai_client(None)
    client.limits = ai_client_module.AIRuntimeLimits(request_timeout_seconds=120, sdk_max_retries=1)

    assert client._initialize_client() is not None
    assert captured["api_key"] == "sk-test"
    assert captured["timeout"] == 240.0  # Claude 单次请求超时下限
    assert captured["max_retries"] == 1


def test_ai_settings_provider_parsing_and_configured_check(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("AI_PROVIDER", "CLAUDE_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)

    assert normalize_ai_provider(" Claude ") == "claude"
    assert normalize_ai_provider("gemini") == "openai"
    assert normalize_ai_provider(None) == "openai"

    monkeypatch.setenv("AI_PROVIDER", "claude")
    assert AISettings().is_configured() is False
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-test")
    assert AISettings().is_configured() is True

    monkeypatch.setenv("AI_PROVIDER", "openai")
    assert AISettings().is_configured() is False  # OpenAI 仍要地址 + 模型名


def test_spider_config_builds_claude_client_when_selected(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "AI_PROVIDER": "claude",
            "CLAUDE_API_KEY": "sk-test",
            "CLAUDE_BASE_URL": "",
            "CLAUDE_MODEL_NAME": "",
            "ANTHROPIC_BASE_URL": "https://not-this.example",
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
                "import anthropic\n"
                "from src import config\n"
                "assert config.client is None\n"
                "assert isinstance(config.claude_client, anthropic.AsyncAnthropic)\n"
                "assert str(config.claude_client.base_url).rstrip('/') == 'https://api.anthropic.com'\n"
                "assert config.CLAUDE_MODEL_NAME == 'claude-sonnet-5-5'\n"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------- 系统设置接口


class _IdleProcessService:
    processes: dict = {}


def _settings_client(tmp_path, monkeypatch, stored=None):
    monkeypatch.chdir(tmp_path)
    values = dict(stored or {})
    writes = []

    def get_value(key, default=None):
        return values.get(key, default)

    def update_values(updates):
        writes.append(dict(updates))
        values.update(updates)
        return True

    monkeypatch.setattr(settings_routes.env_manager, "get_value", get_value)
    monkeypatch.setattr(settings_routes.env_manager, "update_values", update_values)
    monkeypatch.setattr(settings_routes, "_reload_env", lambda: None)
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[deps.get_process_service] = lambda: _IdleProcessService()
    return TestClient(app), writes


def test_get_ai_settings_reports_provider_but_never_keys(tmp_path, monkeypatch):
    client, _ = _settings_client(
        tmp_path,
        monkeypatch,
        {"AI_PROVIDER": "claude", "CLAUDE_API_KEY": "sk-secret", "CLAUDE_MODEL_NAME": "claude-sonnet-5-5"},
    )

    body = client.get("/api/settings/ai").json()

    assert body["AI_PROVIDER"] == "claude"
    assert body["CLAUDE_MODEL_NAME"] == "claude-sonnet-5-5"
    assert "sk-secret" not in str(body)


def test_switching_to_claude_requires_a_key(tmp_path, monkeypatch):
    client, writes = _settings_client(tmp_path, monkeypatch)

    rejected = client.put("/api/settings/ai", json={"AI_PROVIDER": "claude"})
    assert rejected.status_code == 400
    assert "Claude API Key" in rejected.json()["detail"]
    assert client.put("/api/settings/ai", json={"AI_PROVIDER": "gemini"}).status_code == 400
    assert client.put("/api/settings/ai", json={"CLAUDE_BASE_URL": "file:///etc/passwd"}).status_code == 400
    assert writes == []

    ok = client.put(
        "/api/settings/ai",
        json={"AI_PROVIDER": "claude", "CLAUDE_API_KEY": "sk-new", "CLAUDE_MODEL_NAME": " claude-opus-5-5 "},
    )
    assert ok.status_code == 200
    assert writes == [{"AI_PROVIDER": "claude", "CLAUDE_API_KEY": "sk-new", "CLAUDE_MODEL_NAME": "claude-opus-5-5"}]

    # Key 已保存后，单独切换提供方不用再填
    assert client.put("/api/settings/ai", json={"AI_PROVIDER": "openai"}).status_code == 200
    assert client.put("/api/settings/ai", json={"AI_PROVIDER": "claude"}).status_code == 200


def test_status_reports_active_provider_and_claude_key(tmp_path, monkeypatch):
    client, _ = _settings_client(tmp_path, monkeypatch, {"AI_PROVIDER": "claude", "CLAUDE_API_KEY": "sk"})

    body = client.get("/api/settings/status").json()

    assert body["ai_provider"] == "claude"
    assert body["env_file"]["claude_api_key_set"] is True


def test_claude_test_never_sends_stored_key_to_a_new_address(tmp_path, monkeypatch):
    client, _ = _settings_client(tmp_path, monkeypatch, {"CLAUDE_API_KEY": "sk-stored"})
    monkeypatch.setattr(
        settings_routes,
        "create_sync_claude_client",
        lambda **_kwargs: pytest.fail("不该发出请求"),
    )

    response = client.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "CLAUDE_BASE_URL": "https://attacker.example"},
    )

    assert response.status_code == 400
    assert "重新填写 API Key" in response.json()["detail"]


def test_claude_test_uses_stored_key_for_official_endpoint(tmp_path, monkeypatch):
    client, _ = _settings_client(tmp_path, monkeypatch, {"CLAUDE_API_KEY": "sk-stored"})
    captured = {}

    class _FakeSyncClaude:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            captured["request"] = kwargs
            return _response(_text_block("OK"))

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(settings_routes, "create_sync_claude_client", lambda **kw: _FakeSyncClaude(**kw))

    body = client.post(
        "/api/settings/ai/test",
        json={"provider": "claude", "CLAUDE_BASE_URL": "https://api.anthropic.com/"},
    ).json()

    assert body["success"] is True
    assert body["response"] == "OK"
    assert captured["client"]["api_key"] == "sk-stored"
    assert captured["request"]["model"] == DEFAULT_CLAUDE_MODEL
    assert captured["closed"] is True


def test_call_claude_with_retries_reports_every_response_including_truncated_ones():
    """on_response 每个拿到的响应都回调：截断那次的 token 也是真花的。"""
    fake = _FakeClaude(
        [
            _response(_text_block("{"), stop_reason="max_tokens", usage=_usage(output_tokens=4000)),
            _response(_text_block(VALID_JSON), usage=_usage(output_tokens=300)),
        ]
    )
    seen = []
    result, model = _run_call(fake, on_response=seen.append)
    assert model == "claude-opus-5-5"
    assert VALID_JSON in result
    assert [response.usage.output_tokens for response in seen] == [4000, 300]
