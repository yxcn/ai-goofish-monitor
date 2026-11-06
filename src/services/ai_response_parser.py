"""
AI 响应解析工具
"""
import json
from typing import Any


class EmptyAIResponseError(ValueError):
    """AI 返回了空内容。"""


class AIResponseTruncatedError(ValueError):
    """AI 输出被输出长度上限截断（Chat Completions 的 finish_reason=length，
    或 Responses API 的 status=incomplete / incomplete_details.reason=max_output_tokens）。

    截断的 JSON 不能当成正常响应解析——顶层解析失败后"取第一个能解析的 JSON"
    会拿到内层对象，再按缺字段去盲重试。调用方应提高输出上限后重试一次。
    """

    def __init__(self, message: str, *, partial_content: str = ""):
        super().__init__(message)
        self.partial_content = partial_content


class AIResponseFormatError(ValueError):
    """AI 响应能解析成 JSON，但缺少必需字段或字段类型不对。"""


# 各家网关对"输出被长度截断"的标记
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_TRUNCATED_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "max_tokens", "length"})


def _read(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def is_truncated_response(response: Any) -> bool:
    """判断 AI 响应是否因输出长度上限被截断。纯函数，不抛异常。"""
    if response is None or isinstance(response, (str, bytes, bytearray)):
        return False

    # Chat Completions: choices[0].finish_reason == "length"
    choices = _read(response, "choices")
    if choices:
        try:
            first = choices[0]
        except (IndexError, KeyError, TypeError):
            first = None
        finish_reason = _read(first, "finish_reason")
        if isinstance(finish_reason, str) and finish_reason.lower() in _TRUNCATED_FINISH_REASONS:
            return True

    # Responses API: status == "incomplete" 且 incomplete_details.reason == "max_output_tokens"
    status = _read(response, "status")
    incomplete_details = _read(response, "incomplete_details")
    reason = _read(incomplete_details, "reason")
    if isinstance(reason, str) and reason.lower() in _TRUNCATED_INCOMPLETE_REASONS:
        return True
    if isinstance(status, str) and status.lower() == "incomplete" and incomplete_details is not None:
        # 有 incomplete_details 但 reason 不认识：仍视为未完成输出，交给调用方加长重试
        return reason is None or isinstance(reason, str)
    return False


def extract_ai_response_content(response: Any) -> str:
    """从不同形态的 AI 响应中提取文本内容。

    输出被长度上限截断时抛出 ``AIResponseTruncatedError``（带上已有的部分内容），
    不会把半截 JSON 当成正常响应返回。
    """
    if response is None:
        raise EmptyAIResponseError("AI响应对象为空。")

    if isinstance(response, (bytes, bytearray)):
        text = response.decode("utf-8", errors="replace")
        return _normalize_text_content(text)

    if isinstance(response, str):
        return _normalize_text_content(response)

    truncated = is_truncated_response(response)

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        if truncated:
            raise AIResponseTruncatedError(
                "AI输出被输出长度上限截断（Responses API 返回 incomplete/max_output_tokens）。",
                partial_content=output_text,
            )
        return _normalize_text_content(output_text)

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is None:
            raise EmptyAIResponseError("AI响应缺少 message。")
        content = getattr(message, "content", None)

        if truncated:
            try:
                partial = _coerce_content_parts(content)
            except ValueError:
                partial = ""
            raise AIResponseTruncatedError(
                "AI输出被输出长度上限截断（finish_reason=length）。",
                partial_content=partial or "",
            )

        # 智谱等 OpenAI 兼容网关在某些模式下会把输出放在 reasoning_content 而非 content
        try:
            return _normalize_text_content(_coerce_content_parts(content))
        except EmptyAIResponseError:
            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                return _normalize_text_content(_coerce_content_parts(reasoning_content))
            raise

    raise ValueError(f"无法识别的AI响应类型: {type(response).__name__}")


def parse_ai_response_json(content: str) -> dict:
    """解析 AI 文本响应中的 JSON。"""
    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return _extract_first_json_value(cleaned, exc)


def _coerce_content_parts(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    if not isinstance(content, list):
        raise ValueError(f"AI响应内容类型不受支持: {type(content).__name__}")

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _normalize_text_content(content: str) -> str:
    text = str(content).strip()
    if not text:
        raise EmptyAIResponseError("AI响应内容为空。")
    return text


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _extract_first_json_value(
    content: str,
    fallback_error: json.JSONDecodeError,
):
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None

    for start_index, char in enumerate(content):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[start_index:])
            return parsed
        except json.JSONDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise fallback_error
