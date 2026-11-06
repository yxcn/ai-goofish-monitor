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
