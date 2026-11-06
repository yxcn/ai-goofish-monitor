"""
用户可配置的路径 / URL 的校验。

这些值来自 Web 设置页，会被后端直接 open / makedirs / 当作请求地址使用，
所以只接受项目内的相对目录和明确的 URL scheme。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

HTTP_SCHEMES = {"http", "https"}
PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_relative_dir(value: Optional[str], *, field_label: str, default: str) -> str:
    """只接受项目根下的相对子目录；空值回落到默认目录。"""
    text = str(value or "").strip().strip("\"'")
    if not text:
        return default
    normalized = text.replace("\\", "/")
    if os.path.isabs(normalized) or normalized.startswith("~") or _WINDOWS_DRIVE_RE.match(normalized):
        raise ValueError(f"{field_label} 只能是项目内的相对目录，不接受绝对路径：{value}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        return default
    if ".." in parts:
        raise ValueError(f"{field_label} 不能包含 '..'：{value}")
    return "/".join(parts)


def is_safe_relative_dir(value: Optional[str]) -> bool:
    try:
        validate_relative_dir(value, field_label="目录", default="state")
    except ValueError:
        return False
    return True


def validate_url_scheme(value: Optional[str], *, field_label: str, schemes: Iterable[str] = HTTP_SCHEMES,
                        allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field_label} 不能为空")
    parsed = urlparse(text)
    allowed = set(schemes)
    if parsed.scheme.lower() not in allowed or not parsed.netloc:
        raise ValueError(f"{field_label} 必须是 {'/'.join(sorted(allowed))} 开头的合法 URL：{value}")
    return text


def validate_proxy_url(value: Optional[str], *, field_label: str = "代理地址") -> str:
    return validate_url_scheme(value, field_label=field_label, schemes=PROXY_SCHEMES)


def validate_proxy_pool(value: Optional[str], *, field_label: str = "PROXY_POOL") -> str:
    """代理池按逗号 / 换行分隔，每一项都必须是合法代理 URL。"""
    text = str(value or "").strip()
    if not text:
        return ""
    entries = [entry.strip() for entry in re.split(r"[\n,]+", text) if entry.strip()]
    for entry in entries:
        validate_proxy_url(entry, field_label=field_label)
    return text


def normalize_url_for_compare(value: Optional[str]) -> str:
    """比较"地址是否变了"用：忽略大小写的 scheme/host 和末尾斜杠。"""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/").lower()
    path = parsed.path.rstrip("/")
    rebuilt = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    if parsed.query:
        rebuilt += f"?{parsed.query}"
    return rebuilt
