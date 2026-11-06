"""AI 请求兼容性辅助逻辑。"""

import copy
import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.services.ai_response_parser import (
    AIResponseFormatError,
    AIResponseTruncatedError,
    EmptyAIResponseError,
)


RESPONSES_API_MODE = "responses"
CHAT_COMPLETIONS_API_MODE = "chat_completions"
INPUT_TEXT_TYPE = "input_text"
INPUT_IMAGE_TYPE = "input_image"
IMAGE_DETAIL_AUTO = "auto"
JSON_OUTPUT_TYPE = "json_object"
UNSUPPORTED_JSON_OUTPUT_MARKERS = (
    "not supported by this model",
    "json_object",
    "json_schema",
    "text.format",
    "response_format.type",
)
RESPONSES_API_UNSUPPORTED_MARKERS = (
    "404 page not found",
    "page not found",
    "/responses",
    "/v1/responses",
)
CHAT_COMPLETIONS_API_UNSUPPORTED_MARKERS = (
    "404 page not found",
    "page not found",
    "/chat/completions",
    "/v1/chat/completions",
)
UNSUPPORTED_TEMPERATURE_MARKERS = (
    "temperature",
    "sampling temperature",
)

# --- 重试策略 ---
# classify_ai_error 的三种结论
RETRY = "retry"      # 解析失败 / 空响应 / 兼容性探测：立刻换参数再试
BACKOFF = "backoff"  # 429 / 5xx / 网络超时：指数退避后再试
FAIL = "fail"        # 其余 4xx（401/403/404/413/非兼容性 400）等确定性错误：直接失败

# 429/5xx 的退避间隔（秒）：第 1、2、3 次重试
DEFAULT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0, 10.0)
# 单个请求最多尝试次数（不含兼容性探测引起的换参数重试）
DEFAULT_MAX_ATTEMPTS = 4
# 兼容性探测最多换多少次参数；api_mode 在 chat/responses 之间最多切换次数
MAX_COMPAT_ADJUSTMENTS = 6
MAX_API_MODE_SWITCHES = 2
# 输出被截断时把 max_output_tokens 放大的倍数，只重试一次
TRUNCATION_TOKEN_MULTIPLIER = 1.5

# --- 运行时限制的默认值与环境变量名 ---
DEFAULT_AI_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_AI_SDK_MAX_RETRIES = 1
DEFAULT_AI_TOTAL_TIMEOUT_MULTIPLIER = 3
DEFAULT_AI_MAX_IMAGES_PER_ITEM = 6
DEFAULT_AI_MAX_IMAGE_BYTES = 4 * 1024 * 1024

ENV_AI_REQUEST_TIMEOUT_SECONDS = "AI_REQUEST_TIMEOUT_SECONDS"
ENV_AI_SDK_MAX_RETRIES = "AI_SDK_MAX_RETRIES"
ENV_AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS = "AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS"
ENV_AI_MAX_IMAGES_PER_ITEM = "AI_MAX_IMAGES_PER_ITEM"
ENV_AI_MAX_IMAGE_BYTES = "AI_MAX_IMAGE_BYTES"

_STATUS_CODE_PATTERNS = (
    re.compile(r"\b(?:error|status)\s*code[:\s]+(\d{3})\b", re.IGNORECASE),
    re.compile(r"\bHTTP(?:/\d(?:\.\d)?)?\s+(\d{3})\b", re.IGNORECASE),
    re.compile(r"^\s*(\d{3})\s+(?:client|server)\s+error\b", re.IGNORECASE),
)

# 文件头 → MIME（只认 OpenAI 多模态接口支持的几种）
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
DEFAULT_IMAGE_MIME = "image/jpeg"


def build_responses_input(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 Chat Completions 风格的消息转换为 Responses API 输入。"""
    input_items: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        input_items.append(
            {
                "role": role,
                "content": _build_input_content(message.get("content")),
            }
        )
    return input_items


def add_json_text_format(
    request_params: Dict[str, Any],
    enabled: bool,
) -> Dict[str, Any]:
    """按需附加 Responses API 的结构化 JSON 输出参数。"""
    next_params = dict(request_params)
    if not enabled:
        return next_params

    text_config = dict(next_params.get("text") or {})
    text_config["format"] = {"type": JSON_OUTPUT_TYPE}
    next_params["text"] = text_config
    return next_params


def add_json_response_format(
    request_params: Dict[str, Any],
    enabled: bool,
) -> Dict[str, Any]:
    """按需附加 Chat Completions 的 JSON 输出参数。"""
    next_params = dict(request_params)
    if enabled:
        next_params["response_format"] = {"type": JSON_OUTPUT_TYPE}
    return next_params


def is_json_output_unsupported_error(error: Exception) -> bool:
    """识别模型或网关不支持结构化 JSON 输出参数的错误。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict) and body.get("param") in (
        "response_format",
        "response_format.type",
    ):
        return True

    message = str(error)
    return (
        "not supported" in message.lower()
        and any(marker in message for marker in UNSUPPORTED_JSON_OUTPUT_MARKERS)
    )


def is_responses_api_unsupported_error(error: Exception) -> bool:
    """识别 OpenAI 兼容服务未实现 Responses API 的错误。"""
    return _is_api_unsupported_error(error, RESPONSES_API_UNSUPPORTED_MARKERS)


def is_chat_completions_api_unsupported_error(error: Exception) -> bool:
    """识别 OpenAI 兼容服务未实现 Chat Completions API 的错误。"""
    return _is_api_unsupported_error(error, CHAT_COMPLETIONS_API_UNSUPPORTED_MARKERS)


def build_ai_request_params(
    api_mode: str,
    *,
    model: str,
    messages: Iterable[Dict[str, Any]],
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    enable_json_output: bool = False,
) -> Dict[str, Any]:
    """根据 API 模式构建请求参数。"""
    request_params = {"model": model}
    if api_mode == RESPONSES_API_MODE:
        request_params["input"] = build_responses_input(messages)
        if max_output_tokens is not None:
            request_params["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            request_params["temperature"] = temperature
        return add_json_text_format(request_params, enable_json_output)

    if api_mode == CHAT_COMPLETIONS_API_MODE:
        request_params["messages"] = copy.deepcopy(list(messages))
        if max_output_tokens is not None:
            request_params["max_tokens"] = max_output_tokens
        if temperature is not None:
            request_params["temperature"] = temperature
        return add_json_response_format(request_params, enable_json_output)

    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


async def create_ai_response_async(
    client: Any,
    api_mode: str,
    request_params: Dict[str, Any],
) -> Any:
    """根据 API 模式发起异步请求。"""
    if api_mode == RESPONSES_API_MODE:
        return await client.responses.create(**request_params)
    if api_mode == CHAT_COMPLETIONS_API_MODE:
        return await client.chat.completions.create(**request_params)
    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


def create_ai_response_sync(
    client: Any,
    api_mode: str,
    request_params: Dict[str, Any],
) -> Any:
    """根据 API 模式发起同步请求。"""
    if api_mode == RESPONSES_API_MODE:
        return client.responses.create(**request_params)
    if api_mode == CHAT_COMPLETIONS_API_MODE:
        return client.chat.completions.create(**request_params)
    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


def is_temperature_unsupported_error(error: Exception) -> bool:
    """识别模型或中转站不支持 temperature 参数的错误。"""
    message = str(error).lower()
    return (
        "not supported" in message
        or "unsupported" in message
        or "invalid" in message
        or "参数错误" in message
    ) and any(marker in message for marker in UNSUPPORTED_TEMPERATURE_MARKERS)


def is_max_tokens_unsupported_error(error: Exception) -> bool:
    """识别要求用 max_completion_tokens 代替 max_tokens 的错误（如 OpenAI 新模型）。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        details = body.get("error") if isinstance(body.get("error"), dict) else body
        if details.get("param") == "max_tokens":
            return True

    message = str(error).lower()
    return "max_tokens" in message and "max_completion_tokens" in message


def use_max_completion_tokens(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """把 Chat Completions 的 max_tokens 改名为 max_completion_tokens。"""
    next_params = dict(request_params)
    if "max_tokens" in next_params:
        next_params["max_completion_tokens"] = next_params.pop("max_tokens")
    return next_params


def remove_temperature_param(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """移除 temperature 参数，适配不支持采样温度的模型网关。"""
    next_params = dict(request_params)
    next_params.pop("temperature", None)
    return next_params


def is_compat_probe_error(error: Exception, api_mode: str | None = None) -> bool:
    """这个错误是不是"换个参数就能过"的兼容性问题（而非真正的请求错误）。"""
    if is_json_output_unsupported_error(error):
        return True
    if is_temperature_unsupported_error(error):
        return True
    if is_max_tokens_unsupported_error(error):
        return True
    if api_mode == RESPONSES_API_MODE:
        return is_responses_api_unsupported_error(error)
    if api_mode == CHAT_COMPLETIONS_API_MODE:
        return is_chat_completions_api_unsupported_error(error)
    return (
        is_responses_api_unsupported_error(error)
        or is_chat_completions_api_unsupported_error(error)
    )


def extract_status_code(error: BaseException) -> Optional[int]:
    """从 openai 的 APIStatusError（status_code 属性）或报错文本里取 HTTP 状态码。"""
    for attr in ("status_code", "status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)

    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None) if response is not None else None
    if isinstance(response_status, int) and 100 <= response_status <= 599:
        return response_status

    message = str(error)
    for pattern in _STATUS_CODE_PATTERNS:
        match = pattern.search(message)
        if match:
            return int(match.group(1))
    return None


def _is_transport_error(error: BaseException) -> bool:
    """连接失败、读超时等没有状态码的网络层错误。"""
    try:  # openai 不是硬依赖：spider 子进程里一定有，但纯函数测试里不强求
        from openai import APIConnectionError, APITimeoutError  # noqa: WPS433

        if isinstance(error, (APIConnectionError, APITimeoutError)):
            return True
    except Exception:  # pragma: no cover - openai 未安装
        pass
    try:
        import httpx  # noqa: WPS433

        if isinstance(error, (httpx.TransportError, httpx.TimeoutException)):
            return True
    except Exception:  # pragma: no cover - httpx 未安装
        pass
    return isinstance(error, (OSError, TimeoutError))


def classify_ai_error(
    error: BaseException,
    api_mode: str | None = None,
    *,
    allow_compat_retry: bool = True,
) -> str:
    """把一次 AI 调用的异常分成三类：RETRY / BACKOFF / FAIL。

    - 解析失败、空响应、格式不符、兼容性探测错误（换参数即可）→ RETRY
    - 429、5xx、连接/读超时 → BACKOFF
    - 其余 4xx（400 非兼容性、401、403、404、413 …）以及未知的确定性错误 → FAIL

    ``allow_compat_retry=False`` 时不把兼容性探测错误当成可重试：调用方已经按
    CompatProfile.learn_from_error 换过参数却还报同样的错，说明它不是兼容性问题，
    按状态码处理（400 → FAIL）。
    """
    if isinstance(error, AIResponseTruncatedError):
        # 截断有专门的处理路径（放大 max_output_tokens 重试一次），不走通用重试
        return FAIL
    if isinstance(error, (EmptyAIResponseError, AIResponseFormatError, json.JSONDecodeError)):
        return RETRY
    if (
        allow_compat_retry
        and isinstance(error, Exception)
        and is_compat_probe_error(error, api_mode)
    ):
        return RETRY

    status = extract_status_code(error)
    if status is not None:
        if status == 429 or status >= 500:
            return BACKOFF
        if 400 <= status < 500:
            return FAIL
    if _is_transport_error(error):
        return BACKOFF
    return FAIL


def retry_backoff_seconds(
    retry_index: int,
    schedule: Iterable[float] | None = None,
) -> float:
    """第 retry_index（从 1 开始）次重试前应等待的秒数；超出表长用最后一档。"""
    values = tuple(schedule) if schedule is not None else DEFAULT_RETRY_BACKOFF_SECONDS
    if not values:
        return 0.0
    index = max(0, min(retry_index - 1, len(values) - 1))
    return float(values[index])


@dataclass
class CompatProfile:
    """网关 / 模型的兼容性探测结果，跨商品复用，避免每个商品都先吃几个 400。

    api_mode：Chat Completions 还是 Responses API；
    json_output_supported：response_format / text.format 是否可用；
    temperature_supported：是否接受 temperature；
    use_max_completion_tokens：max_tokens 是否要改名 max_completion_tokens。
    """

    api_mode: str = CHAT_COMPLETIONS_API_MODE
    json_output_supported: bool = True
    temperature_supported: bool = True
    use_max_completion_tokens: bool = False
    api_mode_switches: int = 0
    adjustments: int = 0

    def reset(self) -> None:
        fresh = CompatProfile()
        for field_name in fresh.__dataclass_fields__:
            setattr(self, field_name, getattr(fresh, field_name))

    def build_request_params(
        self,
        *,
        model: str,
        messages: Iterable[Dict[str, Any]],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        enable_json_output: bool = False,
    ) -> Dict[str, Any]:
        """按已学到的兼容性结果构建请求参数。"""
        request_params = build_ai_request_params(
            self.api_mode,
            model=model,
            messages=messages,
            temperature=temperature if self.temperature_supported else None,
            max_output_tokens=max_output_tokens,
            enable_json_output=bool(enable_json_output and self.json_output_supported),
        )
        if self.use_max_completion_tokens:
            request_params = use_max_completion_tokens(request_params)
        return request_params

    def learn_from_error(self, error: Exception) -> list[str]:
        """根据报错调整探测结果，返回这次改动了哪些项（空列表 = 没学到新东西）。"""
        changed: list[str] = []
        if self.adjustments >= MAX_COMPAT_ADJUSTMENTS:
            return changed

        if (
            self.api_mode == CHAT_COMPLETIONS_API_MODE
            and is_chat_completions_api_unsupported_error(error)
            and self.api_mode_switches < MAX_API_MODE_SWITCHES
        ):
            self.api_mode = RESPONSES_API_MODE
            self.api_mode_switches += 1
            changed.append("api_mode")
        elif (
            self.api_mode == RESPONSES_API_MODE
            and is_responses_api_unsupported_error(error)
            and self.api_mode_switches < MAX_API_MODE_SWITCHES
        ):
            self.api_mode = CHAT_COMPLETIONS_API_MODE
            self.api_mode_switches += 1
            changed.append("api_mode")

        if self.json_output_supported and is_json_output_unsupported_error(error):
            self.json_output_supported = False
            changed.append("json_output")
        if self.temperature_supported and is_temperature_unsupported_error(error):
            self.temperature_supported = False
            changed.append("temperature")
        if not self.use_max_completion_tokens and is_max_tokens_unsupported_error(error):
            self.use_max_completion_tokens = True
            changed.append("max_completion_tokens")

        if changed:
            self.adjustments += 1
        return changed

    def describe(self) -> str:
        return (
            f"api_mode={self.api_mode}, json_output={self.json_output_supported}, "
            f"temperature={self.temperature_supported}, "
            f"max_completion_tokens={self.use_max_completion_tokens}"
        )


def describe_compat_changes(changes: Iterable[str], profile: CompatProfile) -> str:
    """把 learn_from_error 的结果翻成日志里的一句话。"""
    texts = []
    for change in changes:
        if change == "api_mode":
            target = "Responses API" if profile.api_mode == RESPONSES_API_MODE else "Chat Completions API"
            texts.append(f"切换到 {target}")
        elif change == "json_output":
            texts.append("禁用结构化 JSON 输出参数")
        elif change == "temperature":
            texts.append("移除 temperature 参数")
        elif change == "max_completion_tokens":
            texts.append("改用 max_completion_tokens")
    return "、".join(texts) or "无"


def _env_positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_non_negative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class AIRuntimeLimits:
    """AI 调用的超时 / 重试 / 图片数量与大小限制，统一从环境变量读取。"""

    request_timeout_seconds: float = DEFAULT_AI_REQUEST_TIMEOUT_SECONDS
    sdk_max_retries: int = DEFAULT_AI_SDK_MAX_RETRIES
    # 单个商品整次分析（含所有重试）的总时限；None 表示不限制
    total_timeout_seconds: Optional[float] = (
        DEFAULT_AI_REQUEST_TIMEOUT_SECONDS * DEFAULT_AI_TOTAL_TIMEOUT_MULTIPLIER
    )
    max_images_per_item: int = DEFAULT_AI_MAX_IMAGES_PER_ITEM
    max_image_bytes: int = DEFAULT_AI_MAX_IMAGE_BYTES

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AIRuntimeLimits":
        env = os.environ if environ is None else environ
        timeout = _env_positive_float(
            env, ENV_AI_REQUEST_TIMEOUT_SECONDS, DEFAULT_AI_REQUEST_TIMEOUT_SECONDS
        )
        raw_total = env.get(ENV_AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS)
        total: Optional[float]
        if raw_total is None or not str(raw_total).strip():
            total = timeout * DEFAULT_AI_TOTAL_TIMEOUT_MULTIPLIER
        else:
            try:
                parsed_total = float(raw_total)
            except (TypeError, ValueError):
                parsed_total = timeout * DEFAULT_AI_TOTAL_TIMEOUT_MULTIPLIER
            # 0 或负数 = 关闭总时限
            total = parsed_total if parsed_total > 0 else None
        return cls(
            request_timeout_seconds=timeout,
            sdk_max_retries=_env_non_negative_int(
                env, ENV_AI_SDK_MAX_RETRIES, DEFAULT_AI_SDK_MAX_RETRIES
            ),
            total_timeout_seconds=total,
            max_images_per_item=max(
                1,
                _env_non_negative_int(
                    env, ENV_AI_MAX_IMAGES_PER_ITEM, DEFAULT_AI_MAX_IMAGES_PER_ITEM
                ),
            ),
            max_image_bytes=max(
                1,
                _env_non_negative_int(env, ENV_AI_MAX_IMAGE_BYTES, DEFAULT_AI_MAX_IMAGE_BYTES),
            ),
        )

    def with_overrides(self, **changes: Any) -> "AIRuntimeLimits":
        return replace(self, **changes)


def detect_image_mime(header: bytes | bytearray | None) -> str:
    """按文件头判断图片 MIME（jpeg/png/webp/gif），判断不出来回退 image/jpeg。"""
    if not header:
        return DEFAULT_IMAGE_MIME
    data = bytes(header[:16])
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return DEFAULT_IMAGE_MIME


def read_image_mime(image_path: str | None) -> str:
    """读取本地文件头判断 MIME；文件缺失或读不了时回退 image/jpeg。"""
    if not image_path:
        return DEFAULT_IMAGE_MIME
    try:
        with open(image_path, "rb") as handle:
            return detect_image_mime(handle.read(16))
    except OSError:
        return DEFAULT_IMAGE_MIME


def build_image_data_url(base64_payload: str, mime: str | None = None) -> str:
    return f"data:{mime or DEFAULT_IMAGE_MIME};base64,{base64_payload}"


def _is_api_unsupported_error(
    error: Exception,
    markers: tuple[str, ...],
) -> bool:
    message = str(error).lower()
    if any(marker in message for marker in markers):
        return True

    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    response_text = getattr(response, "text", None) if response else None
    return (
        status_code == 404
        and message.strip() == "error code: 404"
        and not body
        and not response_text
    )


def _build_input_content(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": INPUT_TEXT_TYPE, "text": content}]
    if not isinstance(content, list):
        raise ValueError(f"AI消息内容类型不受支持: {type(content).__name__}")

    return [_coerce_content_item(item) for item in content]


def _coerce_content_item(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"AI消息片段类型不受支持: {type(item).__name__}")

    item_type = item.get("type")
    if item_type in {"text", INPUT_TEXT_TYPE}:
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError("文本消息片段缺少 text 字段。")
        return {"type": INPUT_TEXT_TYPE, "text": text}

    if item_type in {"image_url", INPUT_IMAGE_TYPE}:
        return _build_image_input_item(item)

    raise ValueError(f"不支持的 AI 消息片段类型: {item_type}")


def _build_image_input_item(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_image = item.get("image_url")
    if isinstance(raw_image, dict):
        image_url = raw_image.get("url")
    else:
        image_url = raw_image

    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("图片消息片段缺少有效的 image_url。")

    return {
        "type": INPUT_IMAGE_TYPE,
        "image_url": image_url,
        "detail": item.get("detail", IMAGE_DETAIL_AUTO),
    }
