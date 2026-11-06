"""
设置管理路由
"""
import asyncio
import importlib
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import get_process_service
from src.core.path_safety import (
    normalize_url_for_compare,
    validate_proxy_pool,
    validate_proxy_url,
    validate_relative_dir,
    validate_url_scheme,
)
from src.infrastructure.config.env_manager import env_manager
from src.infrastructure.config.settings import (
    AISettings,
    reload_settings,
)
from src.services.ai_request_compat import (
    CHAT_COMPLETIONS_API_MODE,
    RESPONSES_API_MODE,
    build_ai_request_params,
    create_ai_response_sync,
    is_chat_completions_api_unsupported_error,
    is_max_tokens_unsupported_error,
    is_responses_api_unsupported_error,
    use_max_completion_tokens,
)
from src.services.ai_response_parser import extract_ai_response_content
from src.services.notification_config_service import (
    NotificationSettingsValidationError,
    build_configured_channels,
    build_notification_settings_response,
    build_notification_status_flags,
    load_notification_settings,
    model_dump,
    prepare_notification_test_settings,
    prepare_notification_settings_update,
)
from src.services.notification_service import build_notification_service
from src.services.process_service import ProcessService


# reload_settings() 会重绑 settings 模块里的 scraper_settings 全局；from-import 拿到的是陈旧对象，
# 这里持有模块本身，每次经属性取最新实例（from ...config import settings 拿到的是对象不是模块，所以用 importlib）
_settings_module = importlib.import_module("src.infrastructure.config.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])
AI_TEST_PROMPT = "Reply with OK only."
AI_TEST_MAX_OUTPUT_TOKENS = 32


def _reload_env() -> None:
    load_dotenv(dotenv_path=env_manager.env_file, override=True, interpolate=False)
    reload_settings()


def _env_bool(key: str, default: bool = False) -> bool:
    value = env_manager.get_value(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    value = env_manager.get_value(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _normalize_bool_value(value: bool) -> str:
    return "true" if value else "false"


class NotificationSettingsModel(BaseModel):
    """通知设置模型"""

    NTFY_TOPIC_URL: Optional[str] = None
    PUSHPLUS_TOKEN: Optional[str] = None
    PUSHPLUS_TOPIC: Optional[str] = None
    GOTIFY_URL: Optional[str] = None
    GOTIFY_TOKEN: Optional[str] = None
    BARK_URL: Optional[str] = None
    WX_BOT_URL: Optional[str] = None
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    TELEGRAM_API_BASE_URL: Optional[str] = None
    WEBHOOK_URL: Optional[str] = None
    WEBHOOK_METHOD: Optional[str] = None
    WEBHOOK_HEADERS: Optional[str] = None
    WEBHOOK_CONTENT_TYPE: Optional[str] = None
    WEBHOOK_QUERY_PARAMETERS: Optional[str] = None
    WEBHOOK_BODY: Optional[str] = None
    PCURL_TO_MOBILE: Optional[bool] = None


class NotificationTestRequest(BaseModel):
    """通知测试请求"""

    channel: Optional[str] = None
    settings: NotificationSettingsModel = Field(default_factory=NotificationSettingsModel)


class AISettingsModel(BaseModel):
    """AI设置模型"""

    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = None
    OPENAI_MODEL_NAME: Optional[str] = None
    SKIP_AI_ANALYSIS: Optional[bool] = None
    PROXY_URL: Optional[str] = None


class RotationSettingsModel(BaseModel):
    ACCOUNT_ROTATION_ENABLED: Optional[bool] = None
    ACCOUNT_ROTATION_MODE: Optional[str] = None
    ACCOUNT_ROTATION_RETRY_LIMIT: Optional[int] = None
    ACCOUNT_BLACKLIST_TTL: Optional[int] = None
    ACCOUNT_STATE_DIR: Optional[str] = None
    PROXY_ROTATION_ENABLED: Optional[bool] = None
    PROXY_ROTATION_MODE: Optional[str] = None
    PROXY_POOL: Optional[str] = None
    PROXY_ROTATION_RETRY_LIMIT: Optional[int] = None
    PROXY_BLACKLIST_TTL: Optional[int] = None


@router.get("/notifications")
async def get_notification_settings():
    return build_notification_settings_response(load_notification_settings())


@router.put("/notifications")
async def update_notification_settings(settings: NotificationSettingsModel):
    try:
        updates, deletions, merged_settings = prepare_notification_settings_update(
            model_dump(settings, exclude_unset=True),
            load_notification_settings(),
        )
    except NotificationSettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    success = await asyncio.to_thread(env_manager.apply_changes, updates=updates, deletions=deletions)
    if not success:
        raise HTTPException(status_code=500, detail="更新通知设置失败")

    await asyncio.to_thread(_reload_env)
    return {
        "message": "通知设置已成功更新",
        "configured_channels": build_configured_channels(merged_settings),
    }


@router.post("/notifications/test")
async def test_notification_settings(payload: NotificationTestRequest):
    try:
        merged_settings = prepare_notification_test_settings(
            model_dump(payload.settings, exclude_unset=True),
            load_notification_settings(),
            channel=payload.channel,
        )
    except NotificationSettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = build_notification_service(merged_settings)
    if not service.clients:
        if payload.channel:
            raise HTTPException(
                status_code=422,
                detail=f"渠道 {payload.channel} 未配置或不受支持",
            )
        raise HTTPException(status_code=422, detail="请至少配置一个可用的通知渠道")

    results = await service.send_test_notification()
    if payload.channel:
        if payload.channel not in results:
            raise HTTPException(
                status_code=422,
                detail=f"渠道 {payload.channel} 未配置或不受支持",
            )
        results = {payload.channel: results[payload.channel]}

    return {
        "message": "测试通知已执行",
        "results": results,
    }


@router.get("/rotation")
async def get_rotation_settings():
    return {
        "ACCOUNT_ROTATION_ENABLED": _env_bool("ACCOUNT_ROTATION_ENABLED", False),
        "ACCOUNT_ROTATION_MODE": env_manager.get_value("ACCOUNT_ROTATION_MODE", "per_task"),
        "ACCOUNT_ROTATION_RETRY_LIMIT": _env_int("ACCOUNT_ROTATION_RETRY_LIMIT", 2),
        "ACCOUNT_BLACKLIST_TTL": _env_int("ACCOUNT_BLACKLIST_TTL", 300),
        "ACCOUNT_STATE_DIR": env_manager.get_value("ACCOUNT_STATE_DIR", "state"),
        "PROXY_ROTATION_ENABLED": _env_bool("PROXY_ROTATION_ENABLED", False),
        "PROXY_ROTATION_MODE": env_manager.get_value("PROXY_ROTATION_MODE", "per_task"),
        "PROXY_POOL": env_manager.get_value("PROXY_POOL", ""),
        "PROXY_ROTATION_RETRY_LIMIT": _env_int("PROXY_ROTATION_RETRY_LIMIT", 2),
        "PROXY_BLACKLIST_TTL": _env_int("PROXY_BLACKLIST_TTL", 300),
    }


@router.put("/rotation")
async def update_rotation_settings(settings: RotationSettingsModel):
    updates = {}
    payload = model_dump(settings, exclude_unset=True)
    try:
        if "ACCOUNT_STATE_DIR" in payload:
            # 账号接口会在这个目录下读写删 *.json，放开绝对路径等于任意目录文件操作
            payload["ACCOUNT_STATE_DIR"] = validate_relative_dir(
                payload["ACCOUNT_STATE_DIR"], field_label="ACCOUNT_STATE_DIR", default="state"
            )
        if "PROXY_POOL" in payload:
            payload["PROXY_POOL"] = validate_proxy_pool(payload["PROXY_POOL"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key, value in payload.items():
        if isinstance(value, bool):
            updates[key] = _normalize_bool_value(value)
        else:
            updates[key] = str(value)
    success = await asyncio.to_thread(env_manager.update_values, updates)
    if not success:
        raise HTTPException(status_code=500, detail="更新轮换设置失败")
    await asyncio.to_thread(_reload_env)
    return {"message": "轮换设置已成功更新"}


@router.get("/status")
async def get_system_status(
    process_service: ProcessService = Depends(get_process_service),
):
    state_file = "xianyu_state.json"
    login_state_exists = os.path.exists(state_file)
    env_file_exists = os.path.exists(env_manager.env_file)
    openai_api_key = env_manager.get_value("OPENAI_API_KEY", "")
    openai_base_url = env_manager.get_value("OPENAI_BASE_URL", "")
    openai_model_name = env_manager.get_value("OPENAI_MODEL_NAME", "")
    ai_settings = AISettings()
    notification_settings = load_notification_settings()
    running_task_ids = [
        task_id
        for task_id, process in process_service.processes.items()
        if process and process.returncode is None
    ]

    return {
        "ai_configured": ai_settings.is_configured(),
        "notification_configured": notification_settings.has_any_notification_enabled(),
        "headless_mode": _settings_module.scraper_settings.run_headless,
        "running_in_docker": _settings_module.scraper_settings.running_in_docker,
        "scraper_running": len(running_task_ids) > 0,
        "running_task_ids": running_task_ids,
        "login_state_file": {
            "exists": login_state_exists,
            "path": state_file,
        },
        "env_file": {
            "exists": env_file_exists,
            "openai_api_key_set": bool(openai_api_key),
            "openai_base_url_set": bool(openai_base_url),
            "openai_model_name_set": bool(openai_model_name),
            **build_notification_status_flags(notification_settings),
        },
        "configured_notification_channels": build_configured_channels(notification_settings),
    }


@router.get("/ai")
async def get_ai_settings():
    return {
        "OPENAI_BASE_URL": env_manager.get_value("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL_NAME": env_manager.get_value("OPENAI_MODEL_NAME", ""),
        "SKIP_AI_ANALYSIS": env_manager.get_value("SKIP_AI_ANALYSIS", "false").lower() == "true",
        "PROXY_URL": env_manager.get_value("PROXY_URL", ""),
    }


@router.put("/ai")
async def update_ai_settings(settings: AISettingsModel):
    updates = {}
    try:
        if settings.OPENAI_BASE_URL is not None:
            settings.OPENAI_BASE_URL = validate_url_scheme(settings.OPENAI_BASE_URL, field_label="OPENAI_BASE_URL")
        if settings.PROXY_URL is not None:
            settings.PROXY_URL = validate_proxy_url(settings.PROXY_URL, field_label="PROXY_URL")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if settings.OPENAI_API_KEY is not None:
        updates["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
    if settings.OPENAI_BASE_URL is not None:
        updates["OPENAI_BASE_URL"] = settings.OPENAI_BASE_URL
    if settings.OPENAI_MODEL_NAME is not None:
        updates["OPENAI_MODEL_NAME"] = settings.OPENAI_MODEL_NAME
    if settings.SKIP_AI_ANALYSIS is not None:
        updates["SKIP_AI_ANALYSIS"] = str(settings.SKIP_AI_ANALYSIS).lower()
    if settings.PROXY_URL is not None:
        updates["PROXY_URL"] = settings.PROXY_URL

    success = await asyncio.to_thread(env_manager.update_values, updates)
    if not success:
        raise HTTPException(status_code=500, detail="更新AI设置失败")
    await asyncio.to_thread(_reload_env)
    return {"message": "AI设置已成功更新"}


def resolve_ai_test_credentials(submitted: dict, *, stored_api_key: str, stored_base_url: str,
                                stored_proxy_url: str) -> tuple[str, str, str]:
    """决定测试连接用哪把 Key。

    已保存的 Key 只能发往已保存的地址（含代理）：请求体里换了 OPENAI_BASE_URL 或 PROXY_URL
    却没带新 Key，就拒绝，否则任何能调这个接口的人都能把存好的 Key 发到自己的服务器上。
    """
    submitted_api_key = str(submitted.get("OPENAI_API_KEY") or "").strip()
    base_url = validate_url_scheme(submitted.get("OPENAI_BASE_URL"), field_label="OPENAI_BASE_URL")
    proxy_url = validate_proxy_url(submitted.get("PROXY_URL"), field_label="PROXY_URL")
    if submitted_api_key:
        return submitted_api_key, base_url, proxy_url
    address_changed = (
        normalize_url_for_compare(base_url) != normalize_url_for_compare(stored_base_url)
        or normalize_url_for_compare(proxy_url) != normalize_url_for_compare(stored_proxy_url)
    )
    if address_changed:
        raise ValueError("更换 API 地址或代理后请重新填写 API Key 再测试：已保存的 Key 不会发往新地址")
    if not stored_api_key:
        raise ValueError("尚未配置 OPENAI_API_KEY，请先填写")
    return stored_api_key, base_url, proxy_url


def _run_ai_connectivity_test(api_key: str, base_url: str, proxy_url: str, model_name: str):
    from openai import OpenAI
    import httpx

    client_params = {
        "api_key": api_key,
        "base_url": base_url,
        "timeout": httpx.Timeout(30.0),
    }

    if proxy_url:
        client_params["http_client"] = httpx.Client(proxy=proxy_url)

    client = OpenAI(**client_params)
    messages = [{"role": "user", "content": AI_TEST_PROMPT}]
    api_mode = CHAT_COMPLETIONS_API_MODE
    use_completion_tokens = False
    response = None

    for attempt in range(3):
        request_params = build_ai_request_params(
            api_mode,
            model=model_name,
            messages=messages,
            max_output_tokens=AI_TEST_MAX_OUTPUT_TOKENS,
        )
        if use_completion_tokens:
            request_params = use_max_completion_tokens(request_params)
        try:
            response = create_ai_response_sync(client, api_mode, request_params)
            break
        except Exception as exc:
            if (
                api_mode == CHAT_COMPLETIONS_API_MODE
                and is_chat_completions_api_unsupported_error(exc)
            ):
                api_mode = RESPONSES_API_MODE
            elif not use_completion_tokens and is_max_tokens_unsupported_error(exc):
                use_completion_tokens = True
            else:
                raise
            if attempt == 2:
                raise
    return response


@router.post("/ai/test")
async def test_ai_settings(settings: dict):
    """测试AI模型设置是否有效"""
    try:
        api_key, base_url, proxy_url = resolve_ai_test_credentials(
            settings,
            stored_api_key=env_manager.get_value("OPENAI_API_KEY", "") or "",
            stored_base_url=env_manager.get_value("OPENAI_BASE_URL", "") or "",
            stored_proxy_url=env_manager.get_value("PROXY_URL", "") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        model_name = settings.get("OPENAI_MODEL_NAME", "")
        # 同步 HTTP 调用（最多 3 次、各 30s 超时）放到线程里，不卡事件循环
        response = await asyncio.to_thread(
            _run_ai_connectivity_test, api_key, base_url, proxy_url, model_name
        )
        return {
            "success": True,
            "message": "AI模型连接测试成功！",
            "response": extract_ai_response_content(response),
        }
    except Exception as exc:
        # 只回显异常类型和状态码，不把上游响应正文原样带回前端
        status_code = getattr(exc, "status_code", None)
        detail = f"{type(exc).__name__}" + (f" (HTTP {status_code})" if status_code else "")
        return {
            "success": False,
            "message": f"AI模型连接测试失败: {detail}",
        }
