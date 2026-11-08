"""
Claude（Anthropic Messages API）适配。

项目里的消息都按 OpenAI Chat Completions 的格式拼（``image_url`` data URL、单条 user 消息），
这里负责：转成 Claude 的内容块、发请求、按 stop_reason 取正文、带分类重试。

只在 AI_PROVIDER=claude 时用到；``anthropic`` 包按需导入，没装也不影响 OpenAI。
"""
import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from src.services.ai_request_compat import (
    BACKOFF,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    FAIL,
    TRUNCATION_TOKEN_MULTIPLIER,
    classify_ai_error,
    extract_status_code,
    retry_backoff_seconds,
)
from src.services.ai_response_parser import (
    AIResponseTruncatedError,
    EmptyAIResponseError,
)


DEFAULT_CLAUDE_BASE_URL = "https://api.anthropic.com"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_API_MODE = "claude_messages"

# Claude Opus 5.5 的思考关不掉，max_tokens 是「思考 + 正文」的总上限，原来的 4000 会截掉正文。
# 16000 仍在 SDK 非流式请求的允许范围内。
CLAUDE_MIN_OUTPUT_TOKENS = 16000
# 带思考 + 多张图的单次请求比 OpenAI 慢，120s 偏紧
CLAUDE_MIN_REQUEST_TIMEOUT_SECONDS = 240.0

# 拒答兜底：被安全分类器拒答时，API 在同一次调用里自动换一个 Claude 模型重跑。
# 只对官方地址 + 支持它的模型开；服务端不认这个 beta 时自动关掉（见 ClaudeCallState）。
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
REFUSAL_FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
)

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+/-]+);base64,(?P<data>.*)$", re.DOTALL)


class ClaudeRefusalError(RuntimeError):
    """Claude 拒绝回答（stop_reason=refusal，HTTP 200）。换参数重试没用，直接失败。"""


@dataclass
class ClaudeCallState:
    """跨请求复用的探测结果：服务端不接受拒答兜底参数时，本进程后续请求不再带它。"""

    refusal_fallback_enabled: bool = True

    def reset(self) -> None:
        self.refusal_fallback_enabled = True


def normalize_claude_base_url(value: Optional[str]) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_CLAUDE_BASE_URL


def normalize_claude_model(value: Optional[str]) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_CLAUDE_MODEL


def claude_request_timeout(configured: Optional[float]) -> float:
    try:
        value = float(configured or 0)
    except (TypeError, ValueError):
        value = 0.0
    return max(value, CLAUDE_MIN_REQUEST_TIMEOUT_SECONDS)


def claude_total_timeout(configured_total: Optional[float], request_timeout: float) -> Optional[float]:
    """整次分析（含重试）的总时限：关闭（None/0）就保持关闭，否则至少够两次请求。"""
    if not configured_total or configured_total <= 0:
        return configured_total
    return max(float(configured_total), float(request_timeout) * 2)


def supports_refusal_fallback(model: str, base_url: Optional[str]) -> bool:
    base = normalize_claude_base_url(base_url).rstrip("/").lower()
    return base == DEFAULT_CLAUDE_BASE_URL and model in REFUSAL_FALLBACK_MODELS


def create_async_claude_client(
    *,
    api_key: str,
    base_url: Optional[str],
    timeout: float,
    max_retries: int,
    proxy_url: Optional[str] = None,
):
    """建 AsyncAnthropic。地址和 Key 都显式传入：不让 SDK 去读 ANTHROPIC_* 环境变量。"""
    import anthropic  # noqa: WPS433 - 只有选了 Claude 才需要这个包

    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "base_url": normalize_claude_base_url(base_url),
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if proxy_url:
        kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(proxy=proxy_url)
    return anthropic.AsyncAnthropic(**kwargs)


def create_sync_claude_client(
    *,
    api_key: str,
    base_url: Optional[str],
    timeout: float,
    max_retries: int = 0,
    proxy_url: Optional[str] = None,
):
    """设置页「测试连接」用的同步客户端（放在线程里跑）。"""
    import anthropic  # noqa: WPS433

    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "base_url": normalize_claude_base_url(base_url),
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if proxy_url:
        kwargs["http_client"] = anthropic.DefaultHttpxClient(proxy=proxy_url)
    return anthropic.Anthropic(**kwargs)


# ---------------------------------------------------------------- 消息转换


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def _image_block(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = item.get("image_url")
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url.strip():
        raise ValueError("图片消息片段缺少有效的 image_url。")
    match = _DATA_URL_RE.match(url.strip())
    if match:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": match.group("mime"),
                "data": match.group("data"),
            },
        }
    return {"type": "image", "source": {"type": "url", "url": url.strip()}}


def _convert_part(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        return {"type": "text", "text": item}
    if not isinstance(item, dict):
        raise ValueError(f"AI消息片段类型不受支持: {type(item).__name__}")
    item_type = item.get("type")
    if item_type in {"text", "input_text"}:
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError("文本消息片段缺少 text 字段。")
        return {"type": "text", "text": text}
    if item_type in {"image_url", "input_image"}:
        return _image_block(item)
    raise ValueError(f"不支持的 AI 消息片段类型: {item_type}")


def build_claude_messages(
    messages: Iterable[Dict[str, Any]],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """OpenAI 风格消息 → (system 文本列表, Claude messages)。system/developer 消息提到顶层。"""
    system_parts: List[str] = []
    converted: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content).strip()
            if text:
                system_parts.append(text)
            continue
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
        elif isinstance(content, list):
            converted.append({"role": role, "content": [_convert_part(part) for part in content]})
        else:
            raise ValueError(f"AI消息内容类型不受支持: {type(content).__name__}")
    return system_parts, converted


def build_claude_request_params(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    system_parts: List[str],
    max_tokens: int,
) -> Dict[str, Any]:
    """不发 temperature（新模型和新 API 版本都会 400）；system 末块打缓存标记。

    分析标准每个商品都一样，放在 system 里缓存，同一轮后面的商品按缓存价计费；
    商品 JSON 和图片在缓存标记之后，每个商品不同，不进缓存。
    """
    params: Dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": messages,
    }
    if system_parts:
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": text} for text in system_parts]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        params["system"] = blocks
    return params


# ---------------------------------------------------------------- 响应解析


def _read(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_claude_text(response: Any) -> str:
    """只取 text 块（思考块、拒答兜底的 fallback 块都跳过），按 stop_reason 判断截断和拒答。"""
    if response is None:
        raise EmptyAIResponseError("AI响应对象为空。")
    texts: List[str] = []
    for block in _read(response, "content") or []:
        if _read(block, "type") == "text":
            text = _read(block, "text")
            if isinstance(text, str):
                texts.append(text)
    text = "".join(texts).strip()
    stop_reason = _read(response, "stop_reason")
    if stop_reason == "max_tokens":
        raise AIResponseTruncatedError(
            "AI输出被输出长度上限截断（Claude stop_reason=max_tokens）。",
            partial_content=text,
        )
    if stop_reason == "refusal":
        details = _read(response, "stop_details")
        category = _read(details, "category")
        raise ClaudeRefusalError(
            "Claude 拒绝分析这个商品（stop_reason=refusal"
            + (f", category={category}" if category else "")
            + "）。"
        )
    if not text:
        raise EmptyAIResponseError("AI响应内容为空。")
    return text


def response_model_name(response: Any, default: str) -> str:
    model = _read(response, "model")
    return str(model) if isinstance(model, str) and model.strip() else default


def is_refusal_fallback_rejected(error: BaseException) -> bool:
    """服务端 / 中转站不认拒答兜底参数（beta 头或 fallbacks 字段）时返回的 400。"""
    if extract_status_code(error) != 400:
        return False
    message = str(error).lower()
    return "anthropic-beta" in message or "fallback" in message


# ---------------------------------------------------------------- 带重试的调用


async def _create_message(client: Any, params: Dict[str, Any], *, use_fallback: bool, timeout: Optional[float]):
    extra: Dict[str, Any] = {}
    if timeout is not None:
        extra["timeout"] = timeout
    if use_fallback:
        return await client.beta.messages.create(
            **params,
            betas=[REFUSAL_FALLBACK_BETA],
            fallbacks="default",
            **extra,
        )
    return await client.messages.create(**params, **extra)


async def call_claude_with_retries(
    client: Any,
    *,
    model: str,
    messages: Iterable[Dict[str, Any]],
    max_tokens: int,
    state: ClaudeCallState,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_schedule: Iterable[float] = DEFAULT_RETRY_BACKOFF_SECONDS,
    postprocess: Optional[Callable[[str], Any]] = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Tuple[Any, str]:
    """发请求并按 classify_ai_error 的分类重试，返回 (正文或 postprocess 结果, 实际回答的模型)。

    - 空响应 / JSON 解析失败 / 字段缺失（postprocess 抛出）：立刻重试；
    - 429 / 5xx / 529 / 网络超时：按 backoff_schedule 退避；
    - 其余 4xx、拒答：直接失败；
    - 输出被截断：max_tokens 提高到 1.5 倍只再试一次。
    """
    system_parts, claude_messages = build_claude_messages(messages)
    schedule = tuple(backoff_schedule)
    current_max_tokens = max(int(max_tokens or 0), CLAUDE_MIN_OUTPUT_TOKENS)
    attempt = 0
    truncation_retries = 0
    call_index = 0

    while True:
        call_index += 1
        params = build_claude_request_params(
            model=model,
            messages=claude_messages,
            system_parts=system_parts,
            max_tokens=current_max_tokens,
        )
        use_fallback = state.refusal_fallback_enabled and supports_refusal_fallback(model, base_url)
        try:
            response = await _create_message(client, params, use_fallback=use_fallback, timeout=timeout)
            text = extract_claude_text(response)
            result = postprocess(text) if postprocess else text
            return result, response_model_name(response, model)
        except AIResponseTruncatedError as exc:
            log(f"   [AI分析] Claude 第{call_index}次请求输出被截断: {exc}")
            if truncation_retries >= 1:
                raise
            truncation_retries += 1
            current_max_tokens = int(current_max_tokens * TRUNCATION_TOKEN_MULTIPLIER)
            log(f"   [AI分析] 将 Claude 输出上限提高到 {current_max_tokens} 后重试一次...")
            continue
        except Exception as exc:
            if use_fallback and is_refusal_fallback_rejected(exc):
                state.refusal_fallback_enabled = False
                log("   [AI分析] Claude 服务端不接受拒答兜底参数，本进程后续请求不再带它，立即重试。")
                continue
            kind = classify_ai_error(exc, None, allow_compat_retry=False)
            if kind == FAIL:
                log(f"   [AI分析] Claude 第{call_index}次请求失败且不可重试（{type(exc).__name__}）: {exc}")
                raise
            attempt += 1
            if attempt >= max_attempts:
                log(f"   [AI分析] Claude 已达最大尝试次数 {max_attempts}，放弃: {exc}")
                raise
            if kind == BACKOFF:
                delay = retry_backoff_seconds(attempt, schedule)
                log(
                    f"   [AI分析] Claude 第{call_index}次请求遇到可恢复错误（{type(exc).__name__}: {exc}），"
                    f"{delay:g}s 后进行第{attempt + 1}次尝试..."
                )
                if delay > 0:
                    await sleep(delay)
            else:
                log(f"   [AI分析] Claude 第{call_index}次请求响应异常（{exc}），准备第{attempt + 1}次尝试...")
