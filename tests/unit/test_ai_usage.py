"""AI 用量归一化、累计、价目表和成本估算。"""
from types import SimpleNamespace

import pytest

from src.services.ai_usage import (
    AIUsageTotals,
    ModelPrices,
    builtin_claude_prices,
    describe_usage,
    extract_usage,
    normalize_price_setting,
    parse_price_env,
    resolve_model_prices,
)


# ---------------------------------------------------------------- extract_usage


def test_extract_usage_chat_completions_splits_cached_tokens_out_of_prompt_tokens():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=200,
            prompt_tokens_details=SimpleNamespace(cached_tokens=300),
        )
    )
    assert extract_usage(response, "openai") == {
        "input_tokens": 700,
        "cache_read_tokens": 300,
        "cache_write_tokens": 0,
        "output_tokens": 200,
    }


def test_extract_usage_responses_api_accepts_dict_shapes():
    response = {"usage": {"input_tokens": 500, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 100}}}
    assert extract_usage(response, "openai") == {
        "input_tokens": 400,
        "cache_read_tokens": 100,
        "cache_write_tokens": 0,
        "output_tokens": 50,
    }


def test_extract_usage_claude_keeps_cache_read_and_write_separate():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=400,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=500,
        )
    )
    assert extract_usage(response, "claude") == {
        "input_tokens": 1000,
        "cache_read_tokens": 2000,
        "cache_write_tokens": 500,
        "output_tokens": 400,
    }


def test_extract_usage_returns_none_when_gateway_omits_usage():
    assert extract_usage("plain text", "openai") is None
    assert extract_usage(SimpleNamespace(output_text="x"), "openai") is None
    assert extract_usage(SimpleNamespace(usage=SimpleNamespace()), "openai") is None
    assert extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=None, output_tokens=None)), "claude") is None
    # 缓存数比输入数还大的脏数据：不会算出负的输入
    weird = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1, prompt_tokens_details=SimpleNamespace(cached_tokens=99)))
    assert extract_usage(weird, "openai")["input_tokens"] == 0


# ---------------------------------------------------------------- AIUsageTotals


def test_totals_accumulate_every_call_and_mark_tokens_unknown_when_nothing_reported():
    totals = AIUsageTotals()
    totals.add_response(SimpleNamespace(output_text="no usage here"), "openai")
    assert totals.calls == 1
    record = totals.to_record(elapsed_ms=1234, prices=ModelPrices(1, 2, 3, 4))
    assert record == {
        "input_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "calls": 1,
        "elapsed_ms": 1234,
        "cost_usd": None,
    }

    totals.add_response(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10)), "openai")
    totals.add_response(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=200, completion_tokens=20)), "openai")
    assert totals.calls == 3
    assert totals.input_tokens == 300 and totals.output_tokens == 30 and totals.total_tokens == 330
    record = totals.to_record(elapsed_ms=-5, prices=None)
    assert record["total_tokens"] == 330 and record["cost_usd"] is None and record["elapsed_ms"] == 0


def test_estimate_cost_uses_all_four_token_buckets():
    totals = AIUsageTotals(input_tokens=1000, cache_read_tokens=2000, cache_write_tokens=500, output_tokens=400, calls=1, usage_reported=True)
    prices = builtin_claude_prices("claude-opus-5-5")
    # 1000*4 + 2000*0.2 + 500*5 + 400*20 = 14900 / 1e6
    assert totals.estimate_cost_usd(prices) == pytest.approx(0.0149)
    assert totals.to_record(elapsed_ms=10, prices=prices)["cost_usd"] == pytest.approx(0.0149)


# ---------------------------------------------------------------- 价目表


def test_builtin_claude_prices_match_exact_prefix_and_gateway_alias():
    sonnet = builtin_claude_prices("claude-sonnet-5-5")
    assert (sonnet.input_usd_per_m, sonnet.output_usd_per_m, sonnet.cache_read_usd_per_m) == (2.0, 10.0, 0.2)
    assert sonnet.cache_write_usd_per_m == pytest.approx(2.5)
    assert builtin_claude_prices("anthropic/claude-sonnet-5-5-20260801") is sonnet
    assert builtin_claude_prices(" Claude-Sonnet-5-5 ") is sonnet
    # 最长前缀：claude-opus-5-5 不能被 claude-opus-5 抢走
    assert builtin_claude_prices("claude-opus-5-5").input_usd_per_m == 4.0
    assert builtin_claude_prices("claude-opus-5").input_usd_per_m == 5.0
    assert builtin_claude_prices("gpt-5") is None
    assert builtin_claude_prices("") is None
    assert builtin_claude_prices(None) is None


def test_resolve_model_prices_prefers_overrides_then_builtin_table():
    # 手填：Claude 缓存命中留空按输入价 1/10，缓存写入 1.25 倍
    claude = resolve_model_prices("claude", "claude-sonnet-5-5", input_usd_per_m=1.0, output_usd_per_m=5.0)
    assert (claude.input_usd_per_m, claude.output_usd_per_m) == (1.0, 5.0)
    assert claude.cache_read_usd_per_m == pytest.approx(0.1)
    assert claude.cache_write_usd_per_m == pytest.approx(1.25)
    # 手填：OpenAI 缓存命中留空按输入价
    openai = resolve_model_prices("openai", "gpt-5", input_usd_per_m=1.0, output_usd_per_m=5.0)
    assert openai.cache_read_usd_per_m == 1.0 and openai.cache_write_usd_per_m == 1.0
    assert resolve_model_prices("openai", "gpt-5", input_usd_per_m=1.0, output_usd_per_m=5.0, cache_read_usd_per_m=0.3).cache_read_usd_per_m == 0.3
    # 只填一个不算配置：Claude 回落内置表，OpenAI 算不出
    assert resolve_model_prices("claude", "claude-haiku-5-5", input_usd_per_m=1.0).input_usd_per_m == 0.10
    assert resolve_model_prices("openai", "gpt-5", input_usd_per_m=1.0) is None
    assert resolve_model_prices("openai", "gpt-5") is None
    assert resolve_model_prices("claude", "some-relay-model") is None


# ---------------------------------------------------------------- 设置 / 环境变量解析


def test_parse_price_env_ignores_blank_and_invalid_values():
    assert parse_price_env("2") == 2.0
    assert parse_price_env(" 0.25 ") == 0.25
    assert parse_price_env("") is None
    assert parse_price_env(None) is None
    assert parse_price_env("abc") is None
    assert parse_price_env("-1") is None
    assert parse_price_env("inf") is None


def test_normalize_price_setting_canonicalizes_or_rejects():
    assert normalize_price_setting("10.50", field_label="X") == "10.5"
    assert normalize_price_setting(2, field_label="X") == "2"
    assert normalize_price_setting(0, field_label="X") == "0"
    assert normalize_price_setting("", field_label="X") == ""
    assert normalize_price_setting(None, field_label="X") == ""
    with pytest.raises(ValueError, match="X 必须是数字"):
        normalize_price_setting("abc", field_label="X")
    with pytest.raises(ValueError, match="不小于 0"):
        normalize_price_setting("-1", field_label="X")
    with pytest.raises(ValueError, match="不小于 0"):
        normalize_price_setting("nan", field_label="X")


def test_describe_usage_covers_known_unknown_and_unpriced():
    assert describe_usage({"elapsed_ms": 8200, "calls": 1, "total_tokens": None}) == "耗时 8.2s · 网关未返回 token 用量"
    text = describe_usage(
        {
            "elapsed_ms": 12000,
            "calls": 2,
            "total_tokens": 3350,
            "input_tokens": 1000,
            "cache_read_tokens": 2000,
            "cache_write_tokens": 0,
            "output_tokens": 350,
            "cost_usd": 0.0149,
        }
    )
    assert text == "耗时 12.0s（2 次请求） · 输入 1000 / 缓存命中 2000 / 输出 350 token · 成本 $0.0149"
    assert describe_usage(
        {"elapsed_ms": 1000, "calls": 1, "total_tokens": 11, "input_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 1, "cost_usd": None}
    ).endswith("未配置单价，不算成本")
