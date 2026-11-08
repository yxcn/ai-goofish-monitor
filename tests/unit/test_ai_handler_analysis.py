import asyncio
from types import SimpleNamespace

import pytest

import src.ai_handler as ai_handler
import src.config as app_config


def _build_fake_client(responses_create_impl, chat_create_impl=None):
    responses = SimpleNamespace(create=responses_create_impl)
    chat = SimpleNamespace(
        completions=SimpleNamespace(create=chat_create_impl or responses_create_impl)
    )
    return SimpleNamespace(responses=responses, chat=chat)


@pytest.fixture(autouse=True)
def _fresh_compat_profile(monkeypatch):
    """兼容性探测结果在模块级缓存，测试之间必须清掉；退避间隔归零避免真的 sleep。"""
    ai_handler.reset_compat_profile()
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (0, 0, 0))
    yield
    ai_handler.reset_compat_profile()


def test_get_ai_analysis_stops_after_internal_retries_when_content_is_none(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    call_count = {"value": 0}

    async def fake_create(**_kwargs):
        call_count["value"] += 1
        return SimpleNamespace(output_text="")

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    with pytest.raises(ValueError, match="AI响应内容为空"):
        asyncio.run(
            ai_handler.get_ai_analysis(
                {"商品信息": {"商品ID": "1", "商品标题": "测试商品"}},
                image_paths=[],
                prompt_text="请输出 JSON",
            )
        )

    assert call_count["value"] == 4


def test_get_ai_analysis_returns_parsed_json(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    call_count = {"value": 0}

    async def fake_create(**_kwargs):
        call_count["value"] += 1
        return SimpleNamespace(
            output_text=(
                '{"prompt_version":"v1","is_recommended":true,'
                '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
            )
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "2", "商品标题": "测试商品2"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    assert call_count["value"] == 1


def test_get_ai_analysis_retries_without_structured_output_when_model_rejects_it(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
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
                    message=SimpleNamespace(
                        content=(
                            '{"prompt_version":"v1","is_recommended":true,'
                            '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
                        )
                    )
                )
            ]
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "3", "商品标题": "测试商品3"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0]["messages"][0]["role"] == "user"
    assert request_history[0]["response_format"]["type"] == "json_object"
    assert "response_format" not in request_history[1]
    assert ai_handler.ENABLE_RESPONSE_FORMAT is True


def test_get_ai_analysis_falls_back_to_responses_when_chat_completions_api_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
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
        return SimpleNamespace(
            output_text=(
                '{"prompt_version":"v1","is_recommended":true,'
                '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
            )
        )

    monkeypatch.setattr(
        ai_handler,
        "client",
        _build_fake_client(fake_responses_create, fake_chat_create),
    )
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "4", "商品标题": "测试商品4"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0][0] == "chat"
    assert request_history[0][1]["messages"][0]["role"] == "user"
    assert request_history[1][0] == "responses"
    assert request_history[1][1]["text"]["format"]["type"] == "json_object"
    assert request_history[2][0] == "responses"
    assert "text" not in request_history[2][1]


def test_get_ai_analysis_retries_without_temperature_when_gateway_rejects_it(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception("temperature is unsupported for this model")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"prompt_version":"v1","is_recommended":true,'
                            '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
                        )
                    )
                )
            ]
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "4", "商品标题": "测试商品4"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0]["temperature"] == 0.1
    assert "temperature" not in request_history[1]


def test_get_ai_analysis_uses_first_json_object_when_model_returns_multiple_objects(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    async def fake_create(**_kwargs):
        return SimpleNamespace(
            output_text="""```json
{"prompt_version":"v1","is_recommended":true,"reason":"first","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}
{"prompt_version":"v1","is_recommended":false,"reason":"second","risk_tags":[],"criteria_analysis":{"seller_type":"商家"}}
```"""
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "5", "商品标题": "测试商品5"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    assert result["reason"] == "first"


def test_get_ai_analysis_switches_to_max_completion_tokens_when_model_rejects_max_tokens(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception(
                "Error code: 400 - Unsupported parameter: 'max_tokens' is not supported "
                "with this model. Use 'max_completion_tokens' instead."
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"prompt_version":"v1","is_recommended":true,'
                            '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
                        )
                    )
                )
            ]
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "5", "商品标题": "测试商品5"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0]["max_tokens"] == 4000
    assert "max_tokens" not in request_history[1]
    assert request_history[1]["max_completion_tokens"] == 4000


# -- 重试分类 / 兼容性缓存 / 截断 / 总时限 / 图片 --

import httpx
import openai

from src.infrastructure.external.ai_client import AIAnalysisTimeoutError
from src.services.ai_response_parser import AIResponseTruncatedError

VALID_JSON = (
    '{"prompt_version":"v1","is_recommended":true,'
    '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
)


def _chat_response(content=VALID_JSON, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)]
    )


def _status_error(status: int, message: str):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=None)


def _install_client(monkeypatch, fake_create):
    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)


def _analyze(product_id="9", image_paths=None):
    return asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": product_id, "商品标题": "测试商品"}},
            image_paths=image_paths or [],
            prompt_text="请输出 JSON",
        )
    )


def test_get_ai_analysis_fails_fast_on_authentication_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(401, "Error code: 401 - Incorrect API key provided")

    _install_client(monkeypatch, fake_create)

    with pytest.raises(openai.APIStatusError):
        _analyze()

    assert calls["n"] == 1


@pytest.mark.parametrize(
    "status, message",
    [
        (400, "Error code: 400 - messages[0].content is invalid"),
        (403, "Error code: 403 - forbidden"),
        (404, "Error code: 404 - The model `x` does not exist"),
        (413, "Error code: 413 - request entity too large"),
    ],
)
def test_get_ai_analysis_does_not_retry_other_deterministic_4xx(monkeypatch, tmp_path, status, message):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(status, message)

    _install_client(monkeypatch, fake_create)

    with pytest.raises(openai.APIStatusError):
        _analyze()

    assert calls["n"] == 1


def test_get_ai_analysis_backs_off_on_rate_limit_then_succeeds(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (2, 5, 10))
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ai_handler.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _status_error(429, "Error code: 429 - rate limited")
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    result = _analyze()

    assert result["is_recommended"] is True
    assert calls["n"] == 3
    assert sleeps == [2, 5]


def test_get_ai_analysis_gives_up_on_persistent_server_errors(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai_handler, "AI_RETRY_BACKOFF_SECONDS", (2, 5, 10))
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ai_handler.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(503, "Error code: 503 - overloaded")

    _install_client(monkeypatch, fake_create)

    with pytest.raises(openai.APIStatusError):
        _analyze()

    assert calls["n"] == 4
    assert sleeps == [2, 5, 10]


def test_get_ai_analysis_reuses_compat_profile_for_subsequent_items(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if "response_format" in kwargs:
            raise Exception(
                "Error code: 400 - response_format.type `json_object` is not supported by this model."
            )
        if "temperature" in kwargs:
            raise Exception("temperature is unsupported for this model")
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    assert _analyze("1")["reason"] == "ok"
    assert len(request_history) == 3  # 第一个商品交了两次学费

    assert _analyze("2")["reason"] == "ok"
    assert len(request_history) == 4  # 第二个商品一次成功
    assert "response_format" not in request_history[3]
    assert "temperature" not in request_history[3]

    ai_handler.reset_compat_profile()
    assert _analyze("3")["reason"] == "ok"
    assert "response_format" in request_history[4]  # 重置后重新探测


def test_get_ai_analysis_raises_output_limit_once_when_truncated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            return _chat_response('{"prompt_version":"v1","is_recommended":true,"reason":"half', "length")
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    result = _analyze()

    assert result["reason"] == "ok"
    assert request_history[0]["max_tokens"] == 4000
    assert request_history[1]["max_tokens"] == 6000


def test_get_ai_analysis_fails_when_still_truncated_after_raising_limit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        return _chat_response('{"prompt_version":"v1","is_recommended":true,"reason":"half', "length")

    _install_client(monkeypatch, fake_create)

    with pytest.raises(AIResponseTruncatedError):
        _analyze()

    assert calls["n"] == 2  # 只放大一次，不盲重试


def test_get_ai_analysis_retries_when_required_fields_are_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_response('{"is_recommended": true}')
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    assert _analyze()["reason"] == "ok"
    assert calls["n"] == 2


def test_get_ai_analysis_enforces_total_timeout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai_handler, "AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS", 0.05)

    async def fake_create(**_kwargs):
        await asyncio.sleep(5)
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    with pytest.raises(AIAnalysisTimeoutError):
        _analyze()


def test_get_ai_analysis_uses_detected_mime_and_caps_images(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGES_PER_ITEM", 2)
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 64)
    png = tmp_path / "1.jpg"  # 扩展名撒谎
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    big = tmp_path / "2.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"\x00" * 200)
    webp = tmp_path / "3.jpg"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        return _chat_response()

    _install_client(monkeypatch, fake_create)

    _analyze(image_paths=[str(png), str(big), str(webp)])

    content = request_history[0]["messages"][0]["content"]
    images = [item for item in content if item.get("type") == "image_url"]
    # 上限 2 张：取前两张；第二张超过 64 字节被跳过；第三张不在前两张里
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_get_ai_analysis_stops_when_gateway_keeps_rejecting_after_profile_adjusted(monkeypatch, tmp_path):
    """response_format 已经按探测结果去掉了，网关还报同样的 400 → 不是兼容性问题，不再重试。"""
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_create(**_kwargs):
        calls["n"] += 1
        raise _status_error(
            400, "Error code: 400 - response_format.type `json_object` is not supported by this model"
        )

    _install_client(monkeypatch, fake_create)

    with pytest.raises(openai.APIStatusError):
        _analyze()

    assert calls["n"] == 2  # 第一次学到"禁用 JSON 输出"并重试，第二次同样报错就放弃


# ---------------------------------------------------------------- 用量 / 成本（ai_usage）

_VALID_JSON = (
    '{"prompt_version":"v1","is_recommended":true,'
    '"reason":"ok","risk_tags":[],"criteria_analysis":{"seller_type":"个人"}}'
)


def test_get_ai_analysis_records_usage_across_truncation_retry(monkeypatch, tmp_path):
    """截断重试的那次请求也花了钱：token 按两次累加，成本按系统设置里的单价算。"""
    monkeypatch.chdir(tmp_path)
    calls = []

    def _usage(output_tokens):
        return SimpleNamespace(
            input_tokens=1000,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=200),
        )

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(
                output_text='{"prompt_version":',
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                usage=_usage(50),
            )
        return SimpleNamespace(output_text=_VALID_JSON, usage=_usage(300))

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "OPENAI_PRICE_INPUT_USD_PER_M", 1.0)
    monkeypatch.setattr(ai_handler, "OPENAI_PRICE_OUTPUT_USD_PER_M", 2.0)
    monkeypatch.setattr(ai_handler, "OPENAI_PRICE_CACHE_READ_USD_PER_M", None)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "10", "商品标题": "测试商品10"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert len(calls) == 2
    usage = result["ai_usage"]
    assert usage["calls"] == 2
    assert usage["input_tokens"] == 1600
    assert usage["cache_read_tokens"] == 400
    assert usage["cache_write_tokens"] == 0
    assert usage["output_tokens"] == 350
    assert usage["total_tokens"] == 2350
    # (1600*1 + 400*1 缓存按输入价 + 350*2) / 1e6
    assert usage["cost_usd"] == pytest.approx(0.0027)
    assert isinstance(usage["elapsed_ms"], int) and usage["elapsed_ms"] >= 0
    assert result["ai_model"] == "fake-model"


def test_get_ai_analysis_marks_tokens_unknown_when_gateway_omits_usage(monkeypatch, tmp_path):
    """中转站不返回 usage：耗时照记，token 和成本为 None，前端据此不显示 0。"""
    monkeypatch.chdir(tmp_path)

    async def fake_create(**_kwargs):
        return SimpleNamespace(output_text=_VALID_JSON)

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(ai_handler, "OPENAI_PRICE_INPUT_USD_PER_M", 1.0)
    monkeypatch.setattr(ai_handler, "OPENAI_PRICE_OUTPUT_USD_PER_M", 2.0)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "11", "商品标题": "测试商品11"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    usage = result["ai_usage"]
    assert usage["calls"] == 1
    assert usage["total_tokens"] is None and usage["input_tokens"] is None
    assert usage["cost_usd"] is None
    assert usage["elapsed_ms"] >= 0
