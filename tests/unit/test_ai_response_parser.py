import pytest

from src.services.ai_response_parser import (
    extract_ai_response_content,
    parse_ai_response_json,
    EmptyAIResponseError,
)


def test_parse_ai_response_json_uses_first_object_when_multiple_json_objects_are_concatenated():
    content = """```json
{"is_recommended": true, "reason": "first"}
{"is_recommended": false, "reason": "second"}
```"""

    result = parse_ai_response_json(content)

    assert result == {"is_recommended": True, "reason": "first"}


def test_parse_ai_response_json_extracts_json_from_wrapped_text():
    content = """分析结果如下：

```json
{"is_recommended": true, "reason": "wrapped"}
```

请按第一份结果处理。"""

    result = parse_ai_response_json(content)

    assert result == {"is_recommended": True, "reason": "wrapped"}


def test_parse_ai_response_json_raises_when_no_json_exists():
    with pytest.raises(ValueError):
        parse_ai_response_json("没有任何 JSON 内容")


def test_extract_ai_response_content_with_none_content_but_valid_reasoning_content():
    """当 content 为 None 但 reasoning_content 有值时，应该成功提取 reasoning_content 的内容"""
    # 创建 mock 对象模拟 OpenAI 风格的响应
    message = type('Message', (), {
        'content': None,
        'reasoning_content': '这是推理内容'
    })()
    choice = type('Choice', (), {'message': message})()
    response = type('Response', (), {'choices': [choice]})()

    result = extract_ai_response_content(response)

    assert result == '这是推理内容'


def test_extract_ai_response_content_raises_when_content_and_reasoning_content_are_empty():
    """当 content 和 reasoning_content 都为空时，应该抛出 EmptyAIResponseError"""
    # 创建 mock 对象
    message = type('Message', (), {
        'content': None,
        'reasoning_content': None
    })()
    choice = type('Choice', (), {'message': message})()
    response = type('Response', (), {'choices': [choice]})()

    with pytest.raises(EmptyAIResponseError):
        extract_ai_response_content(response)


# -- 截断识别 --

from types import SimpleNamespace

from src.services.ai_response_parser import AIResponseTruncatedError, is_truncated_response


def test_extract_raises_truncated_when_finish_reason_is_length():
    message = SimpleNamespace(content='{"is_recommended": true, "reason": "half')
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")])

    with pytest.raises(AIResponseTruncatedError) as exc_info:
        extract_ai_response_content(response)

    assert exc_info.value.partial_content.startswith('{"is_recommended"')


def test_extract_prefers_truncation_over_empty_when_reasoning_ate_the_budget():
    message = SimpleNamespace(content=None, reasoning_content="思考思考思考")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")])

    with pytest.raises(AIResponseTruncatedError):
        extract_ai_response_content(response)


def test_extract_returns_content_when_finish_reason_is_stop():
    message = SimpleNamespace(content='{"ok": true}')
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])
    assert extract_ai_response_content(response) == '{"ok": true}'


def test_extract_raises_truncated_for_responses_api_incomplete_output():
    response = SimpleNamespace(
        output_text='{"is_recommended": true, "reason": "ha',
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    with pytest.raises(AIResponseTruncatedError) as exc_info:
        extract_ai_response_content(response)
    assert exc_info.value.partial_content.endswith('"ha')

    completed = SimpleNamespace(output_text='{"ok": true}', status="completed", incomplete_details=None)
    assert extract_ai_response_content(completed) == '{"ok": true}'


def test_is_truncated_response_accepts_dicts_and_ignores_plain_text():
    assert is_truncated_response({"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}) is True
    assert is_truncated_response({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]}) is False
    assert is_truncated_response({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}) is True
    assert is_truncated_response({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}) is True
    assert is_truncated_response({"status": "completed", "incomplete_details": None}) is False
    assert is_truncated_response("plain text") is False
    assert is_truncated_response(None) is False
    assert is_truncated_response(SimpleNamespace(output_text="")) is False
