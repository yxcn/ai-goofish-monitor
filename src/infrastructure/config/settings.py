"""
统一配置管理模块
使用 Pydantic 进行类型安全的配置管理
"""
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _USING_PYDANTIC_SETTINGS = True
except ImportError:
    from pydantic import BaseSettings
    _USING_PYDANTIC_SETTINGS = False
from pydantic import Field
try:
    from pydantic import field_validator
except ImportError:  # pydantic v1 回退：没有 field_validator，沿用其内建布尔解析
    def field_validator(*_args, **_kwargs):
        return lambda func: func
from typing import Optional
import os

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"

# 布尔环境变量的统一写法。config.py（爬虫子进程）、pydantic 配置模型、通知配置服务三处都走这里，
# 避免 RUN_HEADLESS=0 在爬虫里仍是无头、ENABLE_THINKING=1 等于 false 之类的分歧。
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f"})


def parse_env_bool(value, default: bool) -> bool:
    """解析布尔环境变量：接受 1/0、true/false、yes/no、on/off、y/n（大小写、首尾空白不敏感）；
    None 或无法识别的写法返回 default。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def _coerce_env_bool(cls, value, info):
    """pydantic before-validator：布尔字段统一经 parse_env_bool，无法识别时回落到字段默认值。"""
    field = cls.model_fields.get(info.field_name)
    default = field.default if field is not None and isinstance(field.default, bool) else False
    return parse_env_bool(value, default)


# AI 提供方：系统设置里手动切换，默认 OpenAI（含各类 OpenAI 兼容服务）
AI_PROVIDER_OPENAI = "openai"
AI_PROVIDER_CLAUDE = "claude"
AI_PROVIDERS = (AI_PROVIDER_OPENAI, AI_PROVIDER_CLAUDE)


def normalize_ai_provider(value) -> str:
    """AI_PROVIDER 的统一解析：大小写、首尾空白不敏感；空值或不认识的写法一律回落到 openai。"""
    text = str(value or "").strip().lower()
    return text if text in AI_PROVIDERS else AI_PROVIDER_OPENAI


def _env_field(default, env_name: str, **kwargs):
    if _USING_PYDANTIC_SETTINGS:
        return Field(default, validation_alias=env_name, **kwargs)
    return Field(default, env=env_name, **kwargs)


if _USING_PYDANTIC_SETTINGS:
    class _EnvSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            protected_namespaces=(),
        )
else:
    class _EnvSettings(BaseSettings):
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"
            protected_namespaces = ()


class AISettings(_EnvSettings):
    """AI模型配置"""
    provider: str = _env_field(AI_PROVIDER_OPENAI, "AI_PROVIDER")
    api_key: Optional[str] = _env_field(None, "OPENAI_API_KEY")
    base_url: str = _env_field("", "OPENAI_BASE_URL")
    model_name: str = _env_field("", "OPENAI_MODEL_NAME")
    proxy_url: Optional[str] = _env_field(None, "PROXY_URL")
    debug_mode: bool = _env_field(False, "AI_DEBUG_MODE")
    enable_response_format: bool = _env_field(True, "ENABLE_RESPONSE_FORMAT")
    enable_thinking: bool = _env_field(False, "ENABLE_THINKING")
    skip_analysis: bool = _env_field(False, "SKIP_AI_ANALYSIS")
    # Claude：不用 ANTHROPIC_* 这组名字，免得 SDK 或外部 shell（如 Claude Code 终端）里的同名变量混进来
    claude_api_key: Optional[str] = _env_field(None, "CLAUDE_API_KEY")
    claude_base_url: str = _env_field("", "CLAUDE_BASE_URL")
    claude_model_name: str = _env_field("", "CLAUDE_MODEL_NAME")

    @field_validator("debug_mode", "enable_response_format", "enable_thinking", "skip_analysis", mode="before")
    @classmethod
    def _coerce_bools(cls, value, info):
        return _coerce_env_bool(cls, value, info)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value):
        return normalize_ai_provider(value)

    @property
    def uses_claude(self) -> bool:
        return self.provider == AI_PROVIDER_CLAUDE

    def is_configured(self) -> bool:
        """检查当前选中的 AI 提供方是否已配置（OpenAI 要地址和模型名；Claude 要 Key，地址和模型有默认值）"""
        if self.uses_claude:
            return bool(self.claude_api_key)
        return bool(self.base_url and self.model_name)


class NotificationSettings(_EnvSettings):
    """通知服务配置"""
    ntfy_topic_url: Optional[str] = _env_field(None, "NTFY_TOPIC_URL")
    pushplus_token: Optional[str] = _env_field(None, "PUSHPLUS_TOKEN")
    pushplus_topic: Optional[str] = _env_field(None, "PUSHPLUS_TOPIC")
    gotify_url: Optional[str] = _env_field(None, "GOTIFY_URL")
    gotify_token: Optional[str] = _env_field(None, "GOTIFY_TOKEN")
    bark_url: Optional[str] = _env_field(None, "BARK_URL")
    wx_bot_url: Optional[str] = _env_field(None, "WX_BOT_URL")
    telegram_bot_token: Optional[str] = _env_field(None, "TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = _env_field(None, "TELEGRAM_CHAT_ID")
    telegram_api_base_url: Optional[str] = _env_field(
        DEFAULT_TELEGRAM_API_BASE_URL,
        "TELEGRAM_API_BASE_URL",
    )
    webhook_url: Optional[str] = _env_field(None, "WEBHOOK_URL")
    webhook_method: str = _env_field("POST", "WEBHOOK_METHOD")
    webhook_headers: Optional[str] = _env_field(None, "WEBHOOK_HEADERS")
    webhook_content_type: str = _env_field("JSON", "WEBHOOK_CONTENT_TYPE")
    webhook_query_parameters: Optional[str] = _env_field(None, "WEBHOOK_QUERY_PARAMETERS")
    webhook_body: Optional[str] = _env_field(None, "WEBHOOK_BODY")
    pcurl_to_mobile: bool = _env_field(True, "PCURL_TO_MOBILE")

    @field_validator("pcurl_to_mobile", mode="before")
    @classmethod
    def _coerce_bools(cls, value, info):
        return _coerce_env_bool(cls, value, info)

    def has_any_notification_enabled(self) -> bool:
        """检查是否配置了任何通知服务"""
        return any([
            self.ntfy_topic_url,
            self.pushplus_token,
            self.wx_bot_url,
            self.gotify_url and self.gotify_token,
            self.bark_url,
            self.telegram_bot_token and self.telegram_chat_id,
            self.webhook_url
        ])


class ScraperSettings(_EnvSettings):
    """爬虫相关配置"""
    run_headless: bool = _env_field(True, "RUN_HEADLESS")
    login_is_edge: bool = _env_field(False, "LOGIN_IS_EDGE")
    running_in_docker: bool = _env_field(False, "RUNNING_IN_DOCKER")
    state_file: str = _env_field("xianyu_state.json", "STATE_FILE")

    @field_validator("run_headless", "login_is_edge", "running_in_docker", mode="before")
    @classmethod
    def _coerce_bools(cls, value, info):
        return _coerce_env_bool(cls, value, info)


class AppSettings(_EnvSettings):
    """应用主配置"""
    server_port: int = _env_field(8000, "SERVER_PORT")
    web_username: str = _env_field("admin", "WEB_USERNAME")
    web_password: str = _env_field("admin123", "WEB_PASSWORD")
    # <1 表示不清理历史任务日志（cleanup_task_logs 会跳过），不要在这里用 ge=1 让 =0 在 import 阶段炸掉
    task_log_retention_days: int = _env_field(7, "TASK_LOG_RETENTION_DAYS")

    # 文件路径配置
    config_file: str = "config.json"
    image_save_dir: str = "images"
    task_image_dir_prefix: str = "task_images_"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 创建必要的目录
        os.makedirs(self.image_save_dir, exist_ok=True)


# 全局配置实例（单例模式）
_settings_instance = None

def get_settings() -> AppSettings:
    """获取全局配置实例"""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = AppSettings()
    return _settings_instance


def reload_settings() -> None:
    """重新加载全局配置实例"""
    global _settings_instance, settings, ai_settings, notification_settings, scraper_settings
    from dotenv import load_dotenv
    from src.infrastructure.config.env_manager import env_manager

    load_dotenv(dotenv_path=env_manager.env_file, override=True, interpolate=False)
    _settings_instance = None
    settings = get_settings()
    ai_settings = AISettings()
    notification_settings = NotificationSettings()
    scraper_settings = ScraperSettings()


# 导出便捷访问的配置实例
settings = get_settings()
ai_settings = AISettings()
notification_settings = NotificationSettings()
scraper_settings = ScraperSettings()
