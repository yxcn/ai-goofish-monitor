import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import accounts, settings as settings_routes
from src.api.routes.settings import resolve_ai_test_credentials
from src.core.path_safety import (
    normalize_url_for_compare,
    validate_proxy_pool,
    validate_proxy_url,
    validate_relative_dir,
    validate_url_scheme,
)


# ---------- path_safety ----------

@pytest.mark.parametrize("value", ["/etc", "/app/.docker", "../state", "state/../..", "~/x", "C:\\Users", "..\\x"])
def test_relative_dir_rejects_escapes(value):
    with pytest.raises(ValueError):
        validate_relative_dir(value, field_label="ACCOUNT_STATE_DIR", default="state")


def test_relative_dir_normalizes_and_defaults():
    assert validate_relative_dir("", field_label="d", default="state") == "state"
    assert validate_relative_dir(None, field_label="d", default="state") == "state"
    assert validate_relative_dir("./state/", field_label="d", default="state") == "state"
    assert validate_relative_dir("'accounts/pool'", field_label="d", default="state") == "accounts/pool"
    assert validate_relative_dir("state\\sub", field_label="d", default="state") == "state/sub"


def test_url_scheme_validation():
    assert validate_url_scheme("https://api.openai.com/v1", field_label="u") == "https://api.openai.com/v1"
    assert validate_url_scheme("", field_label="u") == ""
    with pytest.raises(ValueError):
        validate_url_scheme("file:///etc/passwd", field_label="u")
    with pytest.raises(ValueError):
        validate_url_scheme("api.openai.com/v1", field_label="u")
    assert validate_proxy_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    with pytest.raises(ValueError):
        validate_proxy_url("ftp://x")
    assert validate_proxy_pool("http://a:1,\nsocks5h://b:2") == "http://a:1,\nsocks5h://b:2"
    with pytest.raises(ValueError):
        validate_proxy_pool("http://a:1, nonsense")


def test_normalize_url_for_compare_ignores_case_and_trailing_slash():
    assert normalize_url_for_compare("HTTPS://Api.OpenAI.com/v1/") == normalize_url_for_compare("https://api.openai.com/v1")
    assert normalize_url_for_compare("https://a.com/v1") != normalize_url_for_compare("https://b.com/v1")
    assert normalize_url_for_compare("") == ""


# ---------- AI 测试连接：已存 Key 不能发往新地址 ----------

STORED = dict(stored_api_key="sk-stored", stored_base_url="https://api.openai.com/v1", stored_proxy_url="")


def test_ai_test_reuses_stored_key_only_for_stored_address():
    key, base, proxy = resolve_ai_test_credentials({"OPENAI_BASE_URL": "https://api.openai.com/v1/"}, **STORED)
    assert (key, base, proxy) == ("sk-stored", "https://api.openai.com/v1/", "")

    with pytest.raises(ValueError, match="重新填写 API Key"):
        resolve_ai_test_credentials({"OPENAI_BASE_URL": "https://attacker.example/v1"}, **STORED)
    with pytest.raises(ValueError, match="重新填写 API Key"):
        resolve_ai_test_credentials(
            {"OPENAI_BASE_URL": "https://api.openai.com/v1", "PROXY_URL": "http://attacker.example:8080"}, **STORED
        )

    # 带了新 Key 就允许换地址（那是用户自己的 Key）
    key, base, _ = resolve_ai_test_credentials(
        {"OPENAI_BASE_URL": "https://other.example/v1", "OPENAI_API_KEY": "sk-new"}, **STORED
    )
    assert (key, base) == ("sk-new", "https://other.example/v1")

    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        resolve_ai_test_credentials({"OPENAI_BASE_URL": "file:///etc/passwd", "OPENAI_API_KEY": "sk"}, **STORED)
    with pytest.raises(ValueError, match="尚未配置"):
        resolve_ai_test_credentials({"OPENAI_BASE_URL": ""}, stored_api_key="", stored_base_url="", stored_proxy_url="")


def _settings_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.include_router(accounts.router)
    return TestClient(app)


def test_ai_test_endpoint_rejects_new_address_without_key_before_any_request(tmp_path, monkeypatch):
    client = _settings_client(tmp_path, monkeypatch)
    monkeypatch.setattr(settings_routes.env_manager, "get_value", lambda key, default=None: {
        "OPENAI_API_KEY": "sk-stored", "OPENAI_BASE_URL": "https://api.openai.com/v1", "PROXY_URL": "",
    }.get(key, default))

    response = client.post("/api/settings/ai/test", json={"OPENAI_BASE_URL": "https://attacker.example/v1", "OPENAI_MODEL_NAME": "x"})
    assert response.status_code == 400
    assert "重新填写 API Key" in response.json()["detail"]


def test_rotation_and_ai_updates_reject_unsafe_values_before_writing(tmp_path, monkeypatch):
    client = _settings_client(tmp_path, monkeypatch)
    writes = []
    monkeypatch.setattr(settings_routes.env_manager, "update_values", lambda updates: writes.append(updates) or True)

    assert client.put("/api/settings/rotation", json={"ACCOUNT_STATE_DIR": "/root/.docker"}).status_code == 400
    assert client.put("/api/settings/rotation", json={"ACCOUNT_STATE_DIR": "../"}).status_code == 400
    assert client.put("/api/settings/rotation", json={"PROXY_POOL": "javascript:alert(1)"}).status_code == 400
    assert client.put("/api/settings/ai", json={"OPENAI_BASE_URL": "ftp://x"}).status_code == 400
    assert client.put("/api/settings/ai", json={"PROXY_URL": "not a url"}).status_code == 400
    assert writes == []

    monkeypatch.setattr(settings_routes, "_reload_env", lambda: None)
    ok = client.put("/api/settings/rotation", json={"ACCOUNT_STATE_DIR": "./accounts/", "ACCOUNT_ROTATION_ENABLED": True})
    assert ok.status_code == 200
    assert writes == [{"ACCOUNT_STATE_DIR": "accounts", "ACCOUNT_ROTATION_ENABLED": "true"}]


def test_accounts_state_dir_falls_back_when_env_points_outside_project(monkeypatch, capsys):
    monkeypatch.setattr(accounts.env_manager, "get_value", lambda key, default=None: "/etc")
    assert accounts._state_dir() == "state"
    assert "不安全的 ACCOUNT_STATE_DIR" in capsys.readouterr().out

    monkeypatch.setattr(accounts.env_manager, "get_value", lambda key, default=None: '"state/pool"')
    assert accounts._state_dir() == "state/pool"
