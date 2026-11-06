"""/ws 的同源校验：WebSocket 不受 CORS 约束，跨源页面不能连上来收任务状态广播。"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import src.app as app_module  # 在切换工作目录之前导入：StaticFiles 挂载时要求仓库里的 static/ 存在
from src.api import auth as auth_module
from src.api.routes.websocket import is_origin_allowed


@pytest.mark.parametrize(
    "origin,host,expected",
    [
        (None, "example.com", True),  # 非浏览器客户端没有 Origin：放行
        ("http://example.com", "example.com", True),
        ("http://example.com:8000", "example.com:8000", True),
        ("https://example.com", "example.com:443", True),
        ("http://EXAMPLE.com", "example.com", True),
        ("http://127.0.0.1:8000", "localhost:8000", True),
        ("http://localhost:8000", "127.0.0.1:8000", True),
        ("http://[::1]:8000", "127.0.0.1:8000", True),
        ("http://evil.example", "example.com", False),
        ("http://example.com:9000", "example.com:8000", False),
        ("http://example.com", "example.com:8000", False),
        ("http://localhost:9000", "127.0.0.1:8000", False),
        ("null", "example.com", False),
        ("", "example.com", False),
        ("http://example.com", None, False),
        ("http://example.com", "", False),
        ("http://example.com:notaport", "example.com", False),
    ],
)
def test_is_origin_allowed(origin, host, expected):
    assert is_origin_allowed(origin, host) is expected


@pytest.fixture()
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app_module.app)
    username, password = auth_module._credentials()
    assert client.post("/auth/status", json={"username": username, "password": password}).status_code == 200
    return client


def test_websocket_rejects_cross_origin_even_when_authenticated(logged_in_client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with logged_in_client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as websocket:
            websocket.receive_text()
    assert excinfo.value.code == 1008


def test_websocket_accepts_same_origin_and_origin_less_clients(logged_in_client):
    # TestClient 的 Host 是 testserver
    with logged_in_client.websocket_connect("/ws", headers={"origin": "http://testserver"}):
        pass
    with logged_in_client.websocket_connect("/ws"):
        pass


def test_websocket_still_requires_auth_for_same_origin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app_module.app)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as websocket:
            websocket.receive_text()
    assert excinfo.value.code == 1008
