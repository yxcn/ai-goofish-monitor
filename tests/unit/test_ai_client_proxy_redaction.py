"""代理 URL 打日志前脱敏：http://user:pass@host:port 不能原样进任务日志。"""
from types import SimpleNamespace

import pytest

import src.infrastructure.external.ai_client as ai_client_module
from src.infrastructure.external.ai_client import AIClient, redact_url_userinfo
from src.services.ai_request_compat import AIRuntimeLimits


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://alice:s3cret@proxy.example:8080", "http://***@proxy.example:8080"),
        ("https://alice:s3cret@proxy.example:8080/path?x=1", "https://***@proxy.example:8080/path?x=1"),
        ("socks5://alice:p%40ss@10.0.0.9:1080", "socks5://***@10.0.0.9:1080"),
        # 密码里有未转义的 @：按最后一个 @ 切分，主机部分保留
        ("http://alice:p@ss@proxy.example:8080", "http://***@proxy.example:8080"),
        # 只有用户名也要打码
        ("http://alice@proxy.example:8080", "http://***@proxy.example:8080"),
        # 没写 scheme 的写法
        ("alice:s3cret@proxy.example:8080", "***@proxy.example:8080"),
    ],
)
def test_redact_url_userinfo_masks_credentials(raw, expected):
    assert redact_url_userinfo(raw) == expected
    assert "alice" not in redact_url_userinfo(raw)
    assert "s3cret" not in redact_url_userinfo(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://proxy.example:8080",
        "http://proxy.example:8080/path?token=abc",
        "proxy.example:8080",
        "",
        None,
    ],
)
def test_redact_url_userinfo_returns_urls_without_userinfo_unchanged(raw):
    assert redact_url_userinfo(raw) == raw


def test_initialize_client_logs_redacted_proxy_but_keeps_real_proxy_in_env(monkeypatch, capsys):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(ai_client_module, "AsyncOpenAI", _FakeAsyncOpenAI)
    proxy_url = "http://alice:s3cret@proxy.example:8080"
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model_name="m",
        proxy_url=proxy_url,
        is_configured=lambda: True,
    )
    client.limits = AIRuntimeLimits()

    assert client._initialize_client() is not None

    out = capsys.readouterr().out
    assert "正在为 AI 请求使用代理: http://***@proxy.example:8080" in out
    assert "s3cret" not in out
    assert "alice" not in out
    # 真正拿去连代理的环境变量仍然是完整地址
    import os

    assert os.environ["HTTP_PROXY"] == proxy_url
    assert os.environ["HTTPS_PROXY"] == proxy_url
