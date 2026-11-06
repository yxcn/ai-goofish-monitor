"""
Web 端认证。

两种凭据任选其一：
- HTTP Basic（脚本 / curl 用）：Authorization: Basic base64(username:password)
- 会话 cookie（浏览器用）：POST /auth/status 校验密码后签发，HttpOnly，HMAC 签名，30 天过期

签名密钥由 WEB_USERNAME + WEB_PASSWORD 派生：改密码即让所有已登录会话失效，
伪造 cookie 需要知道密码本身。所有 /api/* 路由和 /ws 都要求通过这里的校验，
之前"登录"只是前端 localStorage 的一个标记，后端从未校验任何请求。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import Mapping, Optional

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import Response

import importlib

# 包的 __init__ 把 settings 对象导出成同名属性，直接 from ... import settings 拿到的是对象不是模块
settings_module = importlib.import_module("src.infrastructure.config.settings")

SESSION_COOKIE_NAME = "goofish_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600
DEFAULT_WEB_USERNAME = "admin"
DEFAULT_WEB_PASSWORD = "admin123"


def _credentials() -> tuple[str, str]:
    # reload_settings() 会重绑模块全局，所以每次通过模块属性取，不缓存对象引用
    current = settings_module.settings
    return str(current.web_username or ""), str(current.web_password or "")


def is_default_password() -> bool:
    username, password = _credentials()
    return username == DEFAULT_WEB_USERNAME and password == DEFAULT_WEB_PASSWORD


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def verify_password(username: str, password: str) -> bool:
    expected_user, expected_password = _credentials()
    user_ok = _constant_time_equals(username or "", expected_user)
    password_ok = _constant_time_equals(password or "", expected_password)
    return user_ok and password_ok


def _session_key() -> bytes:
    username, password = _credentials()
    return hashlib.sha256(f"goofish-session:{username}:{password}".encode("utf-8")).digest()


def create_session_token(username: str, *, now: Optional[float] = None) -> str:
    expires_at = int((now if now is not None else time.time()) + SESSION_MAX_AGE_SECONDS)
    payload = f"{username}|{expires_at}".encode("utf-8")
    signature = hmac.new(_session_key(), payload, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{signature}"


def verify_session_token(token: Optional[str], *, now: Optional[float] = None) -> Optional[str]:
    """合法且未过期返回用户名，否则返回 None。"""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError):
        return None
    expected = hmac.new(_session_key(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        username, expires_text = payload.decode("utf-8").rsplit("|", 1)
        expires_at = int(expires_text)
    except (UnicodeDecodeError, ValueError):
        return None
    if expires_at < (now if now is not None else time.time()):
        return None
    expected_user, _ = _credentials()
    if not _constant_time_equals(username, expected_user):
        return None
    return username


def _basic_auth_user(header_value: Optional[str]) -> Optional[str]:
    if not header_value or not header_value.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header_value[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username if verify_password(username, password) else None


def authenticate(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Optional[str]:
    """先看 Basic 头，再看会话 cookie。"""
    # Starlette 的 Headers 不区分大小写；这里兼容普通 dict（测试 / 其他调用方）
    user = _basic_auth_user(headers.get("authorization") or headers.get("Authorization"))
    if user is not None:
        return user
    return verify_session_token(cookies.get(SESSION_COOKIE_NAME))


async def require_auth(request: Request) -> str:
    """挂在所有 API router 上的依赖；未认证返回 401。

    不带 WWW-Authenticate: Basic，否则浏览器会对 fetch 的 401 弹出原生登录框。
    """
    user = authenticate(request.headers, request.cookies)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def websocket_user(websocket: WebSocket) -> Optional[str]:
    return authenticate(websocket.headers, websocket.cookies)


def set_session_cookie(response: Response, username: str, *, secure: bool = False) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(username),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
