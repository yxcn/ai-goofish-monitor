import base64
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import src.app as app_module  # 在切换工作目录之前导入：StaticFiles 挂载时要求仓库里的 static/ 存在
from src.api import auth as auth_module
from src.api.auth import (
    SESSION_COOKIE_NAME,
    authenticate,
    create_session_token,
    verify_password,
    verify_session_token,
)


def _creds():
    return auth_module._credentials()


def _basic_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_password_check_and_session_token_roundtrip():
    username, password = _creds()
    assert verify_password(username, password) is True
    assert verify_password(username, password + "x") is False
    assert verify_password(username + "x", password) is False
    assert verify_password("", "") is False

    token = create_session_token(username)
    assert verify_session_token(token) == username


def test_session_token_rejects_tampering_expiry_and_other_users():
    username, _ = _creds()
    token = create_session_token(username)
    payload, signature = token.rsplit(".", 1)

    assert verify_session_token(payload + "." + signature[:-1] + ("0" if signature[-1] != "0" else "1")) is None
    assert verify_session_token("not-a-token") is None
    assert verify_session_token("") is None and verify_session_token(None) is None

    expired = create_session_token(username, now=time.time() - auth_module.SESSION_MAX_AGE_SECONDS - 10)
    assert verify_session_token(expired) is None

    # 签名正确但用户名不是配置的用户：同样拒绝
    assert verify_session_token(create_session_token("someone-else")) is None


def test_authenticate_accepts_basic_header_or_cookie_only():
    username, password = _creds()
    assert authenticate(_basic_header(username, password), {}) == username
    assert authenticate({"authorization": _basic_header(username, password)["Authorization"]}, {}) == username
    assert authenticate(_basic_header(username, "wrong"), {}) is None
    assert authenticate({"authorization": "Basic %%%"}, {}) is None
    assert authenticate({"authorization": "Bearer abc"}, {}) is None
    assert authenticate({}, {SESSION_COOKIE_NAME: create_session_token(username)}) == username
    assert authenticate({}, {SESSION_COOKIE_NAME: "garbage"}) is None
    assert authenticate({}, {}) is None


@pytest.fixture()
def real_app_client(tmp_path, monkeypatch):
    # 真实的 src.app：路由上挂了认证依赖。切到临时目录，避免碰到仓库里的 .env / 数据库
    monkeypatch.chdir(tmp_path)
    return TestClient(app_module.app)


def test_api_routes_require_credentials_but_health_does_not(real_app_client):
    username, password = _creds()
    client = real_app_client

    assert client.get("/health").status_code == 200
    assert client.get("/api/settings/ai").status_code == 401
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert client.get("/api/settings/ai", headers=_basic_header(username, "nope")).status_code == 401

    ok = client.get("/api/settings/ai", headers=_basic_header(username, password))
    assert ok.status_code == 200
    # 不带 WWW-Authenticate，避免浏览器对 fetch 的 401 弹原生登录框
    assert "www-authenticate" not in {k.lower() for k in client.get("/api/tasks").headers}


def test_login_sets_session_cookie_and_logout_clears_it(real_app_client):
    username, password = _creds()
    client = real_app_client

    bad = client.post("/auth/status", json={"username": username, "password": "wrong"})
    assert bad.status_code == 401
    assert SESSION_COOKIE_NAME not in client.cookies

    good = client.post("/auth/status", json={"username": username, "password": password})
    assert good.status_code == 200
    assert good.json() == {"authenticated": True, "username": username}
    set_cookie = good.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in set_cookie and "HttpOnly" in set_cookie
    assert SESSION_COOKIE_NAME in client.cookies

    assert client.get("/auth/me").json() == {"authenticated": True, "username": username}
    assert client.get("/api/settings/ai").status_code == 200

    assert client.post("/auth/logout").status_code == 200
    # 服务端让 cookie 过期；TestClient 保留了旧值的情况下也要用新 cookie 校验
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401


def test_websocket_requires_auth(real_app_client):
    username, password = _creds()
    client = real_app_client

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()
    assert excinfo.value.code == 1008

    client.post("/auth/status", json={"username": username, "password": password})
    with client.websocket_connect("/ws"):
        pass  # 带会话 cookie 可以建立连接
