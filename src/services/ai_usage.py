"""
AI 用量与成本：把各家响应里的 usage 归一成同一组字段、累加一件商品的所有尝试、按单价估算美元成本。

写进 ``ai_analysis.ai_usage`` 的字段（都是本商品所有请求的合计，含重试）：

- ``input_tokens``        未命中缓存的输入 token
- ``cache_read_tokens``   命中缓存的输入 token（OpenAI 的 cached_tokens / Claude 的 cache_read_input_tokens）
- ``cache_write_tokens``  写入缓存的输入 token（只有 Claude 会报）
- ``output_tokens``       输出 token（Claude 含思考 token，这就是账单口径）
- ``total_tokens``        上面四项之和
- ``calls``               实际发出的请求次数
- ``elapsed_ms``          从开始分析到拿到结果的墙钟时间（含重试和退避）
- ``cost_usd``            按单价算出的美元成本；没有单价、或网关没返回 usage 时为 None

网关一次 usage 都没返回时，token 相关字段全部为 None 而不是 0，前端据此不显示「0 tok」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.infrastructure.config.settings import AI_PROVIDER_CLAUDE


@dataclass(frozen=True)
class ModelPrices:
    """美元 / 百万 token。"""

    input_usd_per_m: float
    output_usd_per_m: float
    cache_read_usd_per_m: float
    cache_write_usd_per_m: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "input": self.input_usd_per_m,
            "output": self.output_usd_per_m,
            "cache_read": self.cache_read_usd_per_m,
            "cache_write": self.cache_write_usd_per_m,
        }


def _official(input_price: float, output_price: float, cache_read: Optional[float] = None) -> ModelPrices:
    """Anthropic 官方价：缓存读默认输入价的 1/10，缓存写（5 分钟）是输入价的 1.25 倍。"""
    return ModelPrices(
        input_usd_per_m=input_price,
        output_usd_per_m=output_price,
        cache_read_usd_per_m=input_price * 0.1 if cache_read is None else cache_read,
        cache_write_usd_per_m=input_price * 1.25,
    )


# Anthropic 官方 API 价目（2026-11 快照）。只对 Claude 提供方生效；用中转站或价格变了，在系统设置里填单价覆盖。
CLAUDE_BUILTIN_PRICES: Dict[str, ModelPrices] = {
    "claude-fable-5-1": _official(10.0, 50.0, cache_read=0.25),
    "claude-fable-5": _official(10.0, 50.0),
    "claude-opus-5-5": _official(4.0, 20.0, cache_read=0.20),
    "claude-opus-5": _official(5.0, 25.0),
    "claude-opus-4-8": _official(5.0, 25.0),
    "claude-opus-4-7": _official(5.0, 25.0),
    "claude-opus-4-6": _official(5.0, 25.0),
    "claude-sonnet-5-5": _official(2.0, 10.0, cache_read=0.20),
    "claude-sonnet-5": _official(2.0, 10.0, cache_read=0.20),
    "claude-sonnet-4-6": _official(3.0, 15.0),
    "claude-haiku-5-5": _official(0.10, 0.50),
    "claude-haiku-4-5": _official(1.0, 5.0),
}


def builtin_claude_prices(model: Optional[str]) -> Optional[ModelPrices]:
    """按模型名查内置价目：先精确匹配，再取最长前缀（``claude-sonnet-5-5-20260801`` 这类带日期 / 别名的写法）。

    网关常把模型名写成 ``anthropic/claude-sonnet-5-5``，只看最后一段。
    """
    text = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if not text:
        return None
    if text in CLAUDE_BUILTIN_PRICES:
        return CLAUDE_BUILTIN_PRICES[text]
    best_key = ""
    for key in CLAUDE_BUILTIN_PRICES:
        if text.startswith(key) and len(key) > len(best_key):
            best_key = key
    return CLAUDE_BUILTIN_PRICES.get(best_key) if best_key else None


# 系统设置 / .env 里的单价键，按提供方各一组（输入、输出、缓存命中），单位都是美元 / 百万 token
AI_PRICE_SETTING_FIELDS = (
    "OPENAI_PRICE_INPUT_USD_PER_M",
    "OPENAI_PRICE_OUTPUT_USD_PER_M",
    "OPENAI_PRICE_CACHE_READ_USD_PER_M",
    "CLAUDE_PRICE_INPUT_USD_PER_M",
    "CLAUDE_PRICE_OUTPUT_USD_PER_M",
    "CLAUDE_PRICE_CACHE_READ_USD_PER_M",
)


def parse_price_env(value: Any) -> Optional[float]:
    """环境变量里的单价：空 / 非数字 / 负数 / 非有限数都当没配置。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def normalize_price_setting(value: Any, *, field_label: str) -> str:
    """设置页提交的单价：空串表示清空；否则必须是非负有限数，规范成 ``2`` / ``0.2`` 这样的写法存进 .env。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"{field_label} 必须是数字（美元 / 百万 token）") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_label} 必须是不小于 0 的数字")
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def resolve_model_prices(
    provider: Optional[str],
    model: Optional[str],
    *,
    input_usd_per_m: Optional[float] = None,
    output_usd_per_m: Optional[float] = None,
    cache_read_usd_per_m: Optional[float] = None,
) -> Optional[ModelPrices]:
    """决定用哪组单价：系统设置里填了输入 + 输出就用它；否则 Claude 官方模型查内置表；其余算不出。

    手填时缓存命中价留空：Claude 按输入价 1/10（官方比例），OpenAI 兼容按输入价（多数中转站不打折）。
    """
    if input_usd_per_m is not None and output_usd_per_m is not None:
        if provider == AI_PROVIDER_CLAUDE:
            cache_read = input_usd_per_m * 0.1 if cache_read_usd_per_m is None else cache_read_usd_per_m
            cache_write = input_usd_per_m * 1.25
        else:
            cache_read = input_usd_per_m if cache_read_usd_per_m is None else cache_read_usd_per_m
            cache_write = input_usd_per_m
        return ModelPrices(
            input_usd_per_m=input_usd_per_m,
            output_usd_per_m=output_usd_per_m,
            cache_read_usd_per_m=cache_read,
            cache_write_usd_per_m=cache_write,
        )
    if provider == AI_PROVIDER_CLAUDE:
        return builtin_claude_prices(model)
    return None


def _read(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def extract_usage(response: Any, provider: str) -> Optional[Dict[str, int]]:
    """从一次响应里读 usage，归一成四个 token 数；响应没带 usage 时返回 None（部分中转站就是不给）。

    - Claude：input_tokens 不含缓存部分，缓存读写各自单独一项；
    - OpenAI Chat Completions：prompt_tokens / completion_tokens，命中缓存的在 prompt_tokens_details.cached_tokens 里，
      且已经算在 prompt_tokens 内；
    - OpenAI Responses API：input_tokens / output_tokens，缓存在 input_tokens_details.cached_tokens 里，同样已含在输入数内。
    """
    usage = _read(response, "usage")
    if usage is None:
        return None

    if provider == AI_PROVIDER_CLAUDE:
        input_tokens = _count(_read(usage, "input_tokens"))
        output_tokens = _count(_read(usage, "output_tokens"))
        if input_tokens is None and output_tokens is None:
            return None
        return {
            "input_tokens": input_tokens or 0,
            "cache_read_tokens": _count(_read(usage, "cache_read_input_tokens")) or 0,
            "cache_write_tokens": _count(_read(usage, "cache_creation_input_tokens")) or 0,
            "output_tokens": output_tokens or 0,
        }

    prompt_tokens = _count(_read(usage, "prompt_tokens"))
    details_key = "prompt_tokens_details"
    if prompt_tokens is None:
        prompt_tokens = _count(_read(usage, "input_tokens"))
        details_key = "input_tokens_details"
    completion_tokens = _count(_read(usage, "completion_tokens"))
    if completion_tokens is None:
        completion_tokens = _count(_read(usage, "output_tokens"))
    if prompt_tokens is None and completion_tokens is None:
        return None
    prompt_tokens = prompt_tokens or 0
    cached = min(_count(_read(_read(usage, details_key), "cached_tokens")) or 0, prompt_tokens)
    return {
        "input_tokens": prompt_tokens - cached,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
        "output_tokens": completion_tokens or 0,
    }


@dataclass
class AIUsageTotals:
    """一件商品所有请求的累计；每次拿到响应（成功、截断都算）就 add_response 一次。"""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    usage_reported: bool = False

    def add_response(self, response: Any, provider: str) -> None:
        self.calls += 1
        usage = extract_usage(response, provider)
        if usage is None:
            return
        self.usage_reported = True
        self.input_tokens += usage["input_tokens"]
        self.cache_read_tokens += usage["cache_read_tokens"]
        self.cache_write_tokens += usage["cache_write_tokens"]
        self.output_tokens += usage["output_tokens"]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens + self.output_tokens

    def estimate_cost_usd(self, prices: Optional[ModelPrices]) -> Optional[float]:
        if prices is None or not self.usage_reported:
            return None
        cost = (
            self.input_tokens * prices.input_usd_per_m
            + self.cache_read_tokens * prices.cache_read_usd_per_m
            + self.cache_write_tokens * prices.cache_write_usd_per_m
            + self.output_tokens * prices.output_usd_per_m
        ) / 1_000_000
        return round(cost, 6)

    def to_record(self, *, elapsed_ms: int, prices: Optional[ModelPrices]) -> Dict[str, Any]:
        known = self.usage_reported
        return {
            "input_tokens": self.input_tokens if known else None,
            "cache_read_tokens": self.cache_read_tokens if known else None,
            "cache_write_tokens": self.cache_write_tokens if known else None,
            "output_tokens": self.output_tokens if known else None,
            "total_tokens": self.total_tokens if known else None,
            "calls": self.calls,
            "elapsed_ms": max(int(elapsed_ms), 0),
            "cost_usd": self.estimate_cost_usd(prices),
        }


def describe_usage(record: Dict[str, Any]) -> str:
    """日志里的一句话：耗时 8.2s · 输入 8123 / 缓存命中 2048 / 输出 912 token · 成本 $0.0312。"""
    parts = [f"耗时 {record.get('elapsed_ms', 0) / 1000:.1f}s"]
    calls = record.get("calls") or 0
    if calls > 1:
        parts[0] += f"（{calls} 次请求）"
    if record.get("total_tokens") is None:
        parts.append("网关未返回 token 用量")
        return " · ".join(parts)
    tokens = f"输入 {record['input_tokens']}"
    if record.get("cache_read_tokens"):
        tokens += f" / 缓存命中 {record['cache_read_tokens']}"
    if record.get("cache_write_tokens"):
        tokens += f" / 缓存写入 {record['cache_write_tokens']}"
    tokens += f" / 输出 {record['output_tokens']} token"
    parts.append(tokens)
    cost = record.get("cost_usd")
    parts.append(f"成本 ${cost:.4f}" if cost is not None else "未配置单价，不算成本")
    return " · ".join(parts)
