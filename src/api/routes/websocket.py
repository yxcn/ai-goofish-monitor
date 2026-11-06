"""
WebSocket 路由
提供实时通信功能
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set
from urllib.parse import urlsplit

from src.api.auth import websocket_user


router = APIRouter()

# 全局 WebSocket 连接管理
active_connections: Set[WebSocket] = set()

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def _normalize_host(hostname: str | None) -> str:
    name = (hostname or "").strip().lower().strip("[]")
    if name in _LOOPBACK_HOSTS or name.startswith("127."):
        return "localhost"
    return name


def is_origin_allowed(origin: str | None, host: str | None) -> bool:
    """WebSocket 不受 CORS 约束，这里自己做同源校验。

    没有 Origin 头（非浏览器客户端）放行；有 Origin 时要求其 host（含端口）与请求的 Host 头一致，
    127.0.0.1 / localhost / ::1 互换视为一致。端口缺省时按 Origin 的 scheme 补 80/443。
    """
    if origin is None:
        return True
    origin = origin.strip()
    if not origin or not host or not host.strip():
        return False
    try:
        origin_parts = urlsplit(origin)
        host_parts = urlsplit(f"//{host.strip()}")
        origin_port = origin_parts.port
        host_port = host_parts.port
    except ValueError:
        return False
    if not origin_parts.hostname or not host_parts.hostname:
        return False
    if _normalize_host(origin_parts.hostname) != _normalize_host(host_parts.hostname):
        return False
    default_port = _DEFAULT_PORTS.get((origin_parts.scheme or "").lower())
    if origin_port is None:
        origin_port = default_port
    if host_port is None:
        host_port = default_port
    return origin_port == host_port


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    """WebSocket 端点：同源校验 + 和 HTTP 接口同样的认证（浏览器自动带会话 cookie）。"""
    if not is_origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.accept()
        await websocket.close(code=1008, reason="跨源连接被拒绝")
        return
    if websocket_user(websocket) is None:
        # 先 accept 再以 1008 关闭，浏览器端才能拿到明确的策略违规码并停止重连
        await websocket.accept()
        await websocket.close(code=1008, reason="未登录")
        return
    await websocket.accept()
    active_connections.add(websocket)

    try:
        # 保持连接并接收消息
        while True:
            # 接收客户端消息（如果有的话）
            data = await websocket.receive_text()
            # 这里可以处理客户端发送的消息
            # 目前我们主要用于服务端推送，所以暂时不处理
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception as e:
        print(f"WebSocket 错误: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)


async def broadcast_message(message_type: str, data: dict):
    """向所有连接的客户端广播消息"""
    message = {
        "type": message_type,
        "data": data
    }

    # 移除已断开的连接
    disconnected = set()

    for connection in active_connections:
        try:
            await connection.send_json(message)
        except Exception:
            disconnected.add(connection)

    # 清理断开的连接
    for connection in disconnected:
        active_connections.discard(connection)
