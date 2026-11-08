"""
AI 客户端封装
提供统一的 AI 调用接口
"""
import asyncio
import copy
import ipaddress
import os
import json
import base64
from typing import Any, Dict, List, Optional
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dotenv import load_dotenv
from openai import AsyncOpenAI
from src.ai_message_builder import (
    build_analysis_text_prompt,
    build_user_message_content,
)
from src.infrastructure.config.settings import AISettings
from src.infrastructure.config.env_manager import env_manager
from src.services.ai_request_compat import (
    BACKOFF,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    FAIL,
    TRUNCATION_TOKEN_MULTIPLIER,
    AIRuntimeLimits,
    CompatProfile,
    build_image_data_url,
    classify_ai_error,
    create_ai_response_async,
    describe_compat_changes,
    read_image_mime,
    retry_backoff_seconds,
)
from src.services.ai_response_parser import (
    AIResponseTruncatedError,
    extract_ai_response_content,
    parse_ai_response_json,
)
from src.infrastructure.external.claude_client import (
    ClaudeCallState,
    call_claude_with_retries,
    claude_request_timeout,
    claude_total_timeout,
    create_async_claude_client,
    normalize_claude_model,
)


class AIAnalysisTimeoutError(TimeoutError):
    """单个商品的 AI 分析（含所有重试）超过了总时限。"""


def redact_url_userinfo(url: str) -> str:
    """把 URL 里的 ``user:password@`` 换成 ``***@``，供打日志用；None / 空串 / 无账号的 URL 原样返回。

    代理地址常写成 ``http://user:pass@host:port``，直接 print 会把密码留在任务日志里。
    也兼容没写 scheme 的 ``user:pass@host:port``。
    """
    if not url or not isinstance(url, str):
        return url
    prefix = ""
    candidate = url
    if "://" not in url:
        # 没有 scheme 时 urlsplit 会把 "user" 当成 scheme，先补上 // 让它按 netloc 解析
        candidate = f"//{url}"
        prefix = "//"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    hostport = parts.netloc.rsplit("@", 1)[1]
    redacted = urlunsplit(parts._replace(netloc=f"***@{hostport}"))
    if prefix and redacted.startswith(prefix):
        redacted = redacted[len(prefix):]
    return redacted


# ---- 送给模型的商品 JSON 精简 ----
# 图片已经以附件形式给了模型，JSON 里的图片 URL 列表只是在烧 token
_IMAGE_URL_FIELDS = ("商品图片列表", "商品主图链接", "商品主图", "商品图片", "卖家头像链接")
_SELLER_ITEM_KEEP_FIELDS = ("商品标题", "商品价格", "商品状态", "发布标签")
# base_prompt.txt 里描述了 评价来源角色 / 该用户在此交易中的角色 的含义，这两个字段必须保留
_RATING_KEEP_FIELDS = ("评价类型", "评价时间", "评价来源角色", "该用户在此交易中的角色", "评价内容")
AI_PAYLOAD_MAX_SELLER_ITEMS = 30
AI_PAYLOAD_MAX_RATINGS = 30
AI_PAYLOAD_RATING_TEXT_CHARS = 80


def _compact_item_link(link: Any) -> Any:
    """商品链接只保留 ``?id=``：搜索接口返回的 targetUrl 带一长串埋点参数，对判断没用。"""
    if not isinstance(link, str) or "?" not in link:
        return link
    try:
        parts = urlsplit(link)
    except ValueError:
        return link
    ids = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key == "id"]
    return urlunsplit(parts._replace(query=urlencode(ids[:1]) if ids else "", fragment=""))


def _truncate_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _compact_seller_items(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    compacted = []
    for entry in items[:AI_PAYLOAD_MAX_SELLER_ITEMS]:
        if not isinstance(entry, dict):
            continue
        compacted.append({key: entry[key] for key in _SELLER_ITEM_KEEP_FIELDS if key in entry})
    return compacted


def _compact_ratings(ratings: Any) -> Any:
    if not isinstance(ratings, list):
        return ratings
    compacted = []
    for entry in ratings[:AI_PAYLOAD_MAX_RATINGS]:
        if not isinstance(entry, dict):
            continue
        slim = {key: entry[key] for key in _RATING_KEEP_FIELDS if key in entry}
        if "评价内容" in slim:
            slim["评价内容"] = _truncate_text(slim["评价内容"], AI_PAYLOAD_RATING_TEXT_CHARS)
        compacted.append(slim)
    return compacted


def _compact_item_info(item_info: dict) -> dict:
    slim = {key: value for key, value in item_info.items() if key not in _IMAGE_URL_FIELDS}
    if "商品链接" in slim:
        slim["商品链接"] = _compact_item_link(slim["商品链接"])
    return slim


def _compact_seller_info(seller_info: dict) -> dict:
    slim = {key: value for key, value in seller_info.items() if key not in _IMAGE_URL_FIELDS}
    if "卖家发布的商品列表" in slim:
        slim["卖家发布的商品列表"] = _compact_seller_items(slim["卖家发布的商品列表"])
    if "卖家收到的评价列表" in slim:
        slim["卖家收到的评价列表"] = _compact_ratings(slim["卖家收到的评价列表"])
    return slim


def build_ai_product_payload(product_data: dict) -> dict:
    """返回送给模型的精简副本，不改动入参。

    - 去掉 商品图片列表 / 商品主图 之类的图片 URL（图片已作为附件发送）；
    - 商品链接 只保留 ``?id=``；
    - 卖家发布的商品列表 最多 30 条，每条只留 商品标题 / 商品价格 / 商品状态 / 发布标签；
    - 卖家收到的评价列表 最多 30 条，每条只留 评价类型 / 评价时间 / 评价来源角色 /
      该用户在此交易中的角色 / 评价内容（截到 80 字）；
    - 其他字段原样保留（作为卖家的好评数 等汇总字段都在 卖家信息 顶层，不受影响）。
    """
    if not isinstance(product_data, dict):
        return product_data
    payload: dict = {}
    for key, value in product_data.items():
        if key in _IMAGE_URL_FIELDS:
            continue
        if key == "商品信息" and isinstance(value, dict):
            payload[key] = copy.deepcopy(_compact_item_info(value))
        elif key == "卖家信息" and isinstance(value, dict):
            payload[key] = copy.deepcopy(_compact_seller_info(value))
        elif key == "商品链接":
            payload[key] = _compact_item_link(value)
        else:
            payload[key] = copy.deepcopy(value)
    return payload


def dump_ai_product_json(product_data: dict) -> str:
    """精简 + 紧凑序列化（不缩进），两条 AI 调用链共用。"""
    return json.dumps(
        build_ai_product_payload(product_data),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalize_no_proxy_host(part: str) -> str:
    """Return an httpx-safe NO_PROXY host, or the original entry if unchanged."""
    candidate = part.strip()
    if candidate.startswith("[") and "]" in candidate:
        inner, _, suffix = candidate[1:].partition("]")
        unwrapped = f"{inner}{suffix}"
        host = unwrapped.split("/", 1)[0]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return part
        candidate = unwrapped

    if "/" not in candidate:
        return candidate

    host, _, _prefix = candidate.partition("/")
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return candidate
    return host


def _sanitize_no_proxy_env() -> None:
    """Normalize IPv6 entries in NO_PROXY / no_proxy for httpx <= 0.28.1.

    Two forms are rejected while building proxy mounts:

    - ``::1/128`` is wrapped as ``[::1/128]``, and the prefix is parsed as a port.
    - ``[::1]`` is not recognized as IPv6, so it becomes ``all://*[::1]`` and
      fails with ``Invalid port: ':1]'``.

    httpx only does exact-host comparison, so stripping the prefix length and
    unwrapping bracketed IPv6 literals is safe. IPv4 CIDR entries are kept.

    See https://github.com/encode/httpx/pull/3741
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        parts = [h.strip() for h in value.split(",")]
        cleaned = [_normalize_no_proxy_host(part) for part in parts]
        if cleaned != parts:
            os.environ[key] = ",".join(cleaned)


class AIClient:
    """AI 客户端封装"""

    def __init__(self):
        self.settings: Optional[AISettings] = None
        self.client: Optional[AsyncOpenAI] = None
        self.limits: AIRuntimeLimits = AIRuntimeLimits()
        # 兼容性探测结果按客户端实例缓存：第一个商品学到的（api_mode / response_format /
        # temperature / max_tokens 是否可用）后续商品直接复用；refresh() 时重置。
        self.compat_profile: CompatProfile = CompatProfile()
        self.claude_state: ClaudeCallState = ClaudeCallState()
        self.retry_backoff_seconds: tuple[float, ...] = DEFAULT_RETRY_BACKOFF_SECONDS
        self.refresh()

    def _load_settings(self) -> None:
        load_dotenv(dotenv_path=env_manager.env_file, override=True, interpolate=False)
        self.settings = AISettings()
        self.limits = AIRuntimeLimits.from_env()

    def refresh(self) -> None:
        self._load_settings()
        self.compat_profile = CompatProfile()
        self.claude_state = ClaudeCallState()
        self.client = self._initialize_client()

    # 测试里常用 AIClient.__new__ 绕过 __init__，这几个访问器给缺失的属性兜底
    def _get_limits(self) -> AIRuntimeLimits:
        limits = getattr(self, "limits", None)
        if limits is None:
            limits = AIRuntimeLimits()
            self.limits = limits
        return limits

    def _get_compat_profile(self) -> CompatProfile:
        profile = getattr(self, "compat_profile", None)
        if profile is None:
            profile = CompatProfile()
            self.compat_profile = profile
        return profile

    def _get_backoff_schedule(self) -> tuple[float, ...]:
        schedule = getattr(self, "retry_backoff_seconds", None)
        return tuple(schedule) if schedule is not None else DEFAULT_RETRY_BACKOFF_SECONDS

    def _get_claude_state(self) -> ClaudeCallState:
        state = getattr(self, "claude_state", None)
        if state is None:
            state = ClaudeCallState()
            self.claude_state = state
        return state

    def _uses_claude(self) -> bool:
        settings = getattr(self, "settings", None)
        return bool(settings is not None and getattr(settings, "uses_claude", False))

    def _initialize_claude_client(self):
        try:
            if self.settings.proxy_url:
                print(f"正在为 AI 请求使用代理: {redact_url_userinfo(self.settings.proxy_url)}")
                os.environ['HTTP_PROXY'] = self.settings.proxy_url
                os.environ['HTTPS_PROXY'] = self.settings.proxy_url

            _sanitize_no_proxy_env()

            limits = self._get_limits()
            return create_async_claude_client(
                api_key=self.settings.claude_api_key,
                base_url=self.settings.claude_base_url,
                timeout=claude_request_timeout(limits.request_timeout_seconds),
                max_retries=limits.sdk_max_retries,
                proxy_url=self.settings.proxy_url or None,
            )
        except Exception as e:
            print(f"初始化 Claude 客户端失败: {e}")
            return None

    def _initialize_client(self) -> Optional[AsyncOpenAI]:
        """初始化当前 AI 提供方的客户端（默认 OpenAI；AI_PROVIDER=claude 时是 AsyncAnthropic）"""
        if not self.settings or not self.settings.is_configured():
            print("警告：AI 配置不完整，AI 功能将不可用")
            return None

        if self._uses_claude():
            return self._initialize_claude_client()

        try:
            if self.settings.proxy_url:
                print(f"正在为 AI 请求使用代理: {redact_url_userinfo(self.settings.proxy_url)}")
                os.environ['HTTP_PROXY'] = self.settings.proxy_url
                os.environ['HTTPS_PROXY'] = self.settings.proxy_url

            _sanitize_no_proxy_env()

            limits = self._get_limits()
            return AsyncOpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                timeout=limits.request_timeout_seconds,
                max_retries=limits.sdk_max_retries,
            )
        except Exception as e:
            print(f"初始化 AI 客户端失败: {e}")
            return None

    def is_available(self) -> bool:
        """检查 AI 客户端是否可用"""
        return self.client is not None

    async def close(self) -> None:
        """关闭底层异步客户端，避免事件循环结束后再触发清理。"""
        client = self.client
        self.client = None
        if client is None:
            return

        close = getattr(client, "close", None)
        if close is None:
            return
        await close()

    @staticmethod
    def encode_image(image_path: str) -> Optional[str]:
        """将图片编码为 Base64"""
        if not image_path or not os.path.exists(image_path):
            return None
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            print(f"编码图片失败: {e}")
            return None

    async def analyze(
        self,
        product_data: Dict,
        image_paths: List[str],
        prompt_text: str
    ) -> Optional[Dict]:
        """
        分析商品数据

        Args:
            product_data: 商品数据
            image_paths: 图片路径列表
            prompt_text: 分析提示词

        Returns:
            分析结果
        """
        if not self.is_available():
            print("AI 客户端不可用")
            return None

        limits = self._get_limits()
        total_timeout = limits.total_timeout_seconds
        if self._uses_claude():
            total_timeout = claude_total_timeout(
                total_timeout, claude_request_timeout(limits.request_timeout_seconds)
            )
        try:
            messages = self._build_messages(product_data, image_paths, prompt_text)
            if total_timeout and total_timeout > 0:
                try:
                    response = await asyncio.wait_for(self._call_ai(messages), timeout=total_timeout)
                except asyncio.TimeoutError:
                    raise AIAnalysisTimeoutError(
                        f"AI 分析超过总时限 {total_timeout:.0f}s（含重试），已放弃。"
                    ) from None
            else:
                response = await self._call_ai(messages)
            return self._parse_response(response)
        except Exception as e:
            print(f"AI 分析失败: {e}")
            return None

    def _build_messages(self, product_data: Dict, image_paths: List[str], prompt_text: str) -> List[Dict]:
        """构建 AI 消息。商品 JSON 先精简再紧凑序列化；图片只取前 N 张、跳过超大图，MIME 按文件头判断。"""
        product_json = dump_ai_product_json(product_data)
        limits = self._get_limits()
        selected_paths = list(image_paths or [])
        if len(selected_paths) > limits.max_images_per_item:
            print(
                f"商品图片 {len(selected_paths)} 张，超过上限 {limits.max_images_per_item}，"
                "只取前几张送给模型"
            )
            selected_paths = selected_paths[: limits.max_images_per_item]

        image_data_urls: List[str] = []
        for path in selected_paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            if size is not None and size > limits.max_image_bytes:
                print(f"图片 {os.path.basename(path)} 大小 {size} 字节超过上限，跳过")
                continue
            base64_img = self.encode_image(path)
            if base64_img:
                image_data_urls.append(build_image_data_url(base64_img, read_image_mime(path)))

        text_prompt = build_analysis_text_prompt(
            product_json,
            prompt_text,
            include_images=bool(image_data_urls),
        )
        user_content = build_user_message_content(text_prompt, image_data_urls)
        return [{"role": "user", "content": user_content}]

    async def _call_ai(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.1,
        max_output_tokens: int = 4000,
        enable_json_output: Optional[bool] = None,
        timeout: Optional[float] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> str:
        """调用 AI API。

        重试策略（见 ai_request_compat.classify_ai_error）：
        - 兼容性探测错误：按 CompatProfile 换参数立刻重试，结果缓存到实例供后续商品复用；
        - 空响应 / 解析类错误：立刻重试；
        - 429 / 5xx / 网络超时：按 2s、5s、10s 退避重试；
        - 其余 4xx（401/403/404/413/非兼容性 400）：直接失败；
        - 输出被截断：max_output_tokens 放大 1.5 倍只重试一次。
        ``timeout`` 为单次请求超时（秒），缺省用客户端构造时的值。
        AI_PROVIDER=claude 时走 Claude（不发 temperature，也没有 JSON 模式开关）。
        """
        if self._uses_claude():
            return await self._call_claude(
                messages,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
                max_attempts=max_attempts,
            )

        profile = self._get_compat_profile()
        want_json = (
            self.settings.enable_response_format
            if enable_json_output is None
            else enable_json_output
        )
        backoff_schedule = self._get_backoff_schedule()
        current_max_output_tokens = max_output_tokens
        attempt = 0
        truncation_retries = 0

        while True:
            request_params = profile.build_request_params(
                model=self.settings.model_name,
                messages=messages,
                temperature=temperature,
                max_output_tokens=current_max_output_tokens,
                enable_json_output=want_json,
            )
            if getattr(self.settings, "enable_thinking", False):
                request_params["extra_body"] = {"enable_thinking": False}
            if timeout is not None:
                request_params["timeout"] = timeout

            try:
                response = await create_ai_response_async(
                    self.client,
                    profile.api_mode,
                    request_params,
                )
                return extract_ai_response_content(response)
            except AIResponseTruncatedError as exc:
                if truncation_retries >= 1 or not current_max_output_tokens:
                    raise
                truncation_retries += 1
                current_max_output_tokens = int(current_max_output_tokens * TRUNCATION_TOKEN_MULTIPLIER)
                print(
                    f"AI 输出被截断，将 max_output_tokens 提高到 {current_max_output_tokens} 后重试一次"
                )
                continue
            except Exception as exc:
                changes = profile.learn_from_error(exc)
                if changes:
                    print(
                        f"检测到兼容性问题，{describe_compat_changes(changes, profile)}后重试"
                        f"（已缓存：{profile.describe()}）"
                    )
                    continue

                # profile 没学到新东西：同样的"兼容性"报错再出现就不是兼容性问题
                kind = classify_ai_error(exc, profile.api_mode, allow_compat_retry=False)
                if kind == FAIL:
                    raise
                attempt += 1
                if attempt >= max_attempts:
                    raise
                if kind == BACKOFF:
                    delay = retry_backoff_seconds(attempt, backoff_schedule)
                    print(
                        f"AI 调用遇到可恢复错误（{type(exc).__name__}），"
                        f"{delay:g}s 后重试 ({attempt + 1}/{max_attempts}): {exc}"
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    print(f"AI 响应异常（{exc}），正在自动重试 ({attempt + 1}/{max_attempts})")

    async def _call_claude(
        self,
        messages: List[Dict],
        *,
        max_output_tokens: int,
        timeout: Optional[float],
        max_attempts: int,
    ) -> str:
        text, _model_name = await call_claude_with_retries(
            self.client,
            model=normalize_claude_model(self.settings.claude_model_name),
            messages=messages,
            max_tokens=max_output_tokens,
            state=self._get_claude_state(),
            base_url=self.settings.claude_base_url,
            timeout=claude_request_timeout(timeout) if timeout is not None else None,
            max_attempts=max_attempts,
            backoff_schedule=self._get_backoff_schedule(),
        )
        return text

    def _parse_response(self, response_text: str) -> Optional[Dict]:
        """解析 AI 响应"""
        try:
            return parse_ai_response_json(response_text)
        except json.JSONDecodeError:
            print(f"无法解析 AI 响应: {response_text[:100]}")
            return None
