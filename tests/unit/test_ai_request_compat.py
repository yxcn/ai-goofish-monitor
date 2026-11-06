from src.services.ai_request_compat import (
    is_json_output_unsupported_error,
    is_responses_api_unsupported_error,
    is_temperature_unsupported_error,
    remove_temperature_param,
)


def test_is_temperature_unsupported_error_detects_unsupported_message():
    err = Exception("temperature is not supported by this gateway")
    assert is_temperature_unsupported_error(err) is True


def test_remove_temperature_param_removes_only_temperature():
    params = {"model": "x", "temperature": 0.5, "max_output_tokens": 128}
    result = remove_temperature_param(params)

    assert "temperature" not in result
    assert result["model"] == "x"
    assert result["max_output_tokens"] == 128


def test_is_responses_api_unsupported_error_detects_gemini_plain_404():
    class _Resp:
        text = ""

    class _Err(Exception):
        status_code = 404
        body = ""
        response = _Resp()

        def __str__(self):
            return "Error code: 404"

    assert is_responses_api_unsupported_error(_Err()) is True


# -- is_json_output_unsupported_error tests --


def test_json_output_error_detected_via_body_param_response_format():
    """Vercel AI Gateway returns 400 with param='response_format'."""

    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "type": "invalid_request_error",
            "param": "response_format",
            "code": "invalid_request_error",
        }

    assert is_json_output_unsupported_error(_Err()) is True


def test_json_output_error_detected_via_body_param_response_format_type():
    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "param": "response_format.type",
        }

    assert is_json_output_unsupported_error(_Err()) is True


def test_json_output_error_detected_via_legacy_string_matching():
    err = Exception(
        "response_format.type is not supported by this model"
    )
    assert is_json_output_unsupported_error(err) is True


def test_json_output_error_not_triggered_by_unrelated_400():
    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "param": "messages",
        }

    assert is_json_output_unsupported_error(_Err()) is False


def test_json_output_error_not_triggered_without_body():
    err = Exception("some random 400 error")
    assert is_json_output_unsupported_error(err) is False


OPENAI_MAX_TOKENS_ERROR = (
    "Error code: 400 - {'error': {'message': \"Unsupported parameter: 'max_tokens' is not "
    "supported with this model. Use 'max_completion_tokens' instead.\", 'type': "
    "'invalid_request_error', 'param': 'max_tokens', 'code': 'unsupported_parameter'}}"
)


def test_max_tokens_error_detected_from_openai_message():
    from src.services.ai_request_compat import (
        is_json_output_unsupported_error,
        is_max_tokens_unsupported_error,
    )

    err = Exception(OPENAI_MAX_TOKENS_ERROR)
    assert is_max_tokens_unsupported_error(err) is True
    # 不应误判成其他兼容性问题
    assert is_temperature_unsupported_error(err) is False
    assert is_json_output_unsupported_error(err) is False


def test_max_tokens_error_detected_via_body_param():
    from src.services.ai_request_compat import is_max_tokens_unsupported_error

    class FakeError(Exception):
        body = {"message": "Unsupported parameter", "param": "max_tokens"}

    class FakeWrappedError(Exception):
        body = {"error": {"message": "Unsupported parameter", "param": "max_tokens"}}

    assert is_max_tokens_unsupported_error(FakeError("400")) is True
    assert is_max_tokens_unsupported_error(FakeWrappedError("400")) is True
    assert is_max_tokens_unsupported_error(Exception("max_tokens must be > 0")) is False


def test_use_max_completion_tokens_renames_only_token_limit():
    from src.services.ai_request_compat import use_max_completion_tokens

    params = {"model": "x", "max_tokens": 4000, "temperature": 0.1}
    result = use_max_completion_tokens(params)

    assert result == {"model": "x", "max_completion_tokens": 4000, "temperature": 0.1}
    assert params["max_tokens"] == 4000  # 原参数不被修改
    assert use_max_completion_tokens({"model": "x"}) == {"model": "x"}


# -- 重试分类 / 兼容性缓存 / 运行时限制 / 图片 MIME --

import json as _json

import httpx
import openai

from src.services.ai_request_compat import (
    BACKOFF,
    CHAT_COMPLETIONS_API_MODE,
    FAIL,
    RESPONSES_API_MODE,
    RETRY,
    AIRuntimeLimits,
    CompatProfile,
    classify_ai_error,
    detect_image_mime,
    extract_status_code,
    read_image_mime,
    retry_backoff_seconds,
)
from src.services.ai_response_parser import (
    AIResponseFormatError,
    AIResponseTruncatedError,
    EmptyAIResponseError,
)


def _status_error(status: int, message: str = "boom", body=None):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=body)


def test_classify_parse_and_empty_errors_are_retryable():
    assert classify_ai_error(EmptyAIResponseError("AI响应内容为空。")) == RETRY
    assert classify_ai_error(AIResponseFormatError("缺字段")) == RETRY
    assert classify_ai_error(_json.JSONDecodeError("Expecting value", "doc", 0)) == RETRY


def test_classify_compat_probe_errors_are_retryable():
    assert classify_ai_error(Exception("temperature is not supported by this gateway")) == RETRY
    assert (
        classify_ai_error(
            _status_error(
                400,
                "Error code: 400 - response_format.type `json_object` is not supported by this model",
                body={"param": "response_format"},
            )
        )
        == RETRY
    )
    assert classify_ai_error(Exception("Error code: 404 - page not found"), CHAT_COMPLETIONS_API_MODE) == RETRY
    assert classify_ai_error(
        Exception("Error code: 400 - Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")
    ) == RETRY


def test_classify_rate_limit_server_and_transport_errors_back_off():
    assert classify_ai_error(_status_error(429, "Error code: 429 - rate limited")) == BACKOFF
    assert classify_ai_error(_status_error(500, "Error code: 500 - internal")) == BACKOFF
    assert classify_ai_error(_status_error(503, "Error code: 503 - overloaded")) == BACKOFF
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    assert classify_ai_error(openai.APIConnectionError(request=request)) == BACKOFF
    assert classify_ai_error(openai.APITimeoutError(request=request)) == BACKOFF
    assert classify_ai_error(ConnectionResetError("reset")) == BACKOFF
    assert classify_ai_error(TimeoutError("read timed out")) == BACKOFF


def test_classify_deterministic_client_errors_fail_fast():
    for status in (400, 401, 403, 404, 413):
        assert classify_ai_error(_status_error(status, f"Error code: {status} - nope")) == FAIL, status

    class _WithStatusAttr(Exception):
        status_code = 401

    assert classify_ai_error(_WithStatusAttr("Incorrect API key provided")) == FAIL
    assert classify_ai_error(Exception("Error code: 413 - request entity too large")) == FAIL
    assert classify_ai_error(Exception("Error code: 401 - Incorrect API key provided")) == FAIL


def test_classify_unknown_and_truncation_errors_fail():
    assert classify_ai_error(ValueError("不支持的 AI 消息片段类型")) == FAIL
    assert classify_ai_error(RuntimeError("something odd")) == FAIL
    assert classify_ai_error(AIResponseTruncatedError("截断")) == FAIL


def test_extract_status_code_from_attribute_response_and_message():
    assert extract_status_code(_status_error(429)) == 429

    class _Attr(Exception):
        status_code = 503

    assert extract_status_code(_Attr("x")) == 503
    assert extract_status_code(Exception("Error code: 401 - bad key")) == 401
    assert extract_status_code(Exception("HTTP/1.1 502 Bad Gateway")) == 502
    assert extract_status_code(Exception("400 Client Error: Bad Request for url: x")) == 400
    assert extract_status_code(Exception("no code here")) is None

    class _NonNumericCode(Exception):
        code = "invalid_api_key"

    assert extract_status_code(_NonNumericCode("x")) is None


def test_retry_backoff_seconds_follows_schedule_and_clamps():
    assert retry_backoff_seconds(1) == 2.0
    assert retry_backoff_seconds(2) == 5.0
    assert retry_backoff_seconds(3) == 10.0
    assert retry_backoff_seconds(7) == 10.0
    assert retry_backoff_seconds(1, (0, 0)) == 0.0
    assert retry_backoff_seconds(1, ()) == 0.0


def test_compat_profile_learns_once_and_builds_adjusted_params():
    profile = CompatProfile()
    messages = [{"role": "user", "content": "hi"}]

    params = profile.build_request_params(
        model="m", messages=messages, temperature=0.1, max_output_tokens=100, enable_json_output=True
    )
    assert params["response_format"] == {"type": "json_object"}
    assert params["temperature"] == 0.1 and params["max_tokens"] == 100

    assert profile.learn_from_error(
        Exception("Error code: 400 - response_format.type `json_object` is not supported by this model")
    ) == ["json_output"]
    assert profile.learn_from_error(Exception("temperature is unsupported for this model")) == ["temperature"]
    assert profile.learn_from_error(
        Exception("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")
    ) == ["max_completion_tokens"]

    params = profile.build_request_params(
        model="m", messages=messages, temperature=0.1, max_output_tokens=100, enable_json_output=True
    )
    assert "response_format" not in params
    assert "temperature" not in params
    assert "max_tokens" not in params and params["max_completion_tokens"] == 100

    # 已经学过的错误再出现：不算新改动，调用方应按状态码判定
    assert profile.learn_from_error(Exception("temperature is unsupported for this model")) == []
    assert profile.learn_from_error(Exception("Error code: 401 - bad key")) == []

    profile.reset()
    params = profile.build_request_params(
        model="m", messages=messages, temperature=0.1, max_output_tokens=100, enable_json_output=True
    )
    assert params["response_format"] == {"type": "json_object"} and params["temperature"] == 0.1


def test_compat_profile_api_mode_switching_is_bounded():
    profile = CompatProfile()
    assert profile.learn_from_error(Exception("Error code: 404 - page not found")) == ["api_mode"]
    assert profile.api_mode == RESPONSES_API_MODE
    assert profile.learn_from_error(Exception("Error code: 404 - page not found")) == ["api_mode"]
    assert profile.api_mode == CHAT_COMPLETIONS_API_MODE
    # 来回切够两次就不再切，避免死循环
    assert profile.learn_from_error(Exception("Error code: 404 - page not found")) == []

    params = profile.build_request_params(model="m", messages=[{"role": "user", "content": "hi"}])
    assert "messages" in params

    profile.api_mode = RESPONSES_API_MODE
    params = profile.build_request_params(
        model="m", messages=[{"role": "user", "content": "hi"}], max_output_tokens=5, enable_json_output=True
    )
    assert "input" in params and params["max_output_tokens"] == 5
    assert params["text"]["format"]["type"] == "json_object"


def test_runtime_limits_defaults():
    limits = AIRuntimeLimits.from_env({})
    assert limits.request_timeout_seconds == 120.0
    assert limits.sdk_max_retries == 1
    assert limits.total_timeout_seconds == 360.0
    assert limits.max_images_per_item == 6
    assert limits.max_image_bytes == 4 * 1024 * 1024


def test_runtime_limits_from_env_with_overrides_and_bad_values():
    limits = AIRuntimeLimits.from_env(
        {
            "AI_REQUEST_TIMEOUT_SECONDS": "30",
            "AI_SDK_MAX_RETRIES": "0",
            "AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS": "0",
            "AI_MAX_IMAGES_PER_ITEM": "3",
            "AI_MAX_IMAGE_BYTES": "not-a-number",
        }
    )
    assert limits.request_timeout_seconds == 30.0
    assert limits.sdk_max_retries == 0
    assert limits.total_timeout_seconds is None  # 0 = 不限制
    assert limits.max_images_per_item == 3
    assert limits.max_image_bytes == 4 * 1024 * 1024  # 非法值回退默认

    limits = AIRuntimeLimits.from_env({"AI_REQUEST_TIMEOUT_SECONDS": "-5", "AI_MAX_IMAGES_PER_ITEM": "0"})
    assert limits.request_timeout_seconds == 120.0
    assert limits.total_timeout_seconds == 360.0
    assert limits.max_images_per_item == 1

    limits = AIRuntimeLimits.from_env({"AI_REQUEST_TIMEOUT_SECONDS": "40", "AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS": "90"})
    assert limits.total_timeout_seconds == 90.0


def test_detect_image_mime_by_magic_bytes(tmp_path):
    assert detect_image_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 12) == "image/jpeg"
    assert detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"
    assert detect_image_mime(b"GIF89a" + b"\x00" * 10) == "image/gif"
    assert detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    # 认不出来（含 heic）回退 jpeg
    assert detect_image_mime(b"\x00\x00\x00\x18ftypheic") == "image/jpeg"
    assert detect_image_mime(b"") == "image/jpeg"
    assert detect_image_mime(None) == "image/jpeg"

    png = tmp_path / "a.jpg"  # 扩展名撒谎，按文件头认
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert read_image_mime(str(png)) == "image/png"
    assert read_image_mime(str(tmp_path / "missing.jpg")) == "image/jpeg"
    assert read_image_mime(None) == "image/jpeg"


def test_classify_treats_already_learned_compat_errors_as_deterministic_failures():
    # 参数已经按 CompatProfile 换过，同样的报错再来就不该再当成"换参数即可"
    json_error = _status_error(
        400, "Error code: 400 - response_format.type `json_object` is not supported by this model"
    )
    assert classify_ai_error(json_error) == RETRY
    assert classify_ai_error(json_error, allow_compat_retry=False) == FAIL

    # 没有状态码的纯文本兼容性报错也一样：不是网络问题，直接失败
    assert classify_ai_error(Exception("temperature is unsupported for this model"), allow_compat_retry=False) == FAIL
    # 但解析类错误和 429/5xx 不受这个开关影响
    assert classify_ai_error(EmptyAIResponseError("空"), allow_compat_retry=False) == RETRY
    assert classify_ai_error(_status_error(429, "Error code: 429"), allow_compat_retry=False) == BACKOFF
