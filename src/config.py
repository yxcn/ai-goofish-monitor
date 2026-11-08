import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.infrastructure.config.settings import AI_PROVIDER_CLAUDE, normalize_ai_provider, parse_env_bool
from src.infrastructure.external.ai_client import _sanitize_no_proxy_env, redact_url_userinfo  # noqa: F401
from src.infrastructure.external.claude_client import (
    claude_request_timeout,
    create_async_claude_client,
    normalize_claude_base_url,
    normalize_claude_model,
)
from src.services.ai_request_compat import AIRuntimeLimits


load_dotenv(interpolate=False)  # 不展开 ${...}：WEBHOOK_BODY 之类的模板占位符要原样保留

# --- File Paths & Directories ---
STATE_FILE = "xianyu_state.json"
IMAGE_SAVE_DIR = "images"
CONFIG_FILE = "config.json"
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)

# 任务隔离的临时图片目录前缀
TASK_IMAGE_DIR_PREFIX = "task_images_"

# --- API URL Patterns ---
API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"

# --- Environment Variables ---
# AI 提供方：openai（默认，含各类 OpenAI 兼容服务）或 claude，在 Web UI 系统设置里切换
AI_PROVIDER = normalize_ai_provider(os.getenv("AI_PROVIDER"))
API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL_NAME = os.getenv("OPENAI_MODEL_NAME")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_BASE_URL = normalize_claude_base_url(os.getenv("CLAUDE_BASE_URL"))
CLAUDE_MODEL_NAME = normalize_claude_model(os.getenv("CLAUDE_MODEL_NAME"))
PROXY_URL = os.getenv("PROXY_URL")
NTFY_TOPIC_URL = os.getenv("NTFY_TOPIC_URL")
GOTIFY_URL = os.getenv("GOTIFY_URL")
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN")
BARK_URL = os.getenv("BARK_URL")
WX_BOT_URL = os.getenv("WX_BOT_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_METHOD = os.getenv("WEBHOOK_METHOD", "POST").upper()
WEBHOOK_HEADERS = os.getenv("WEBHOOK_HEADERS")
WEBHOOK_CONTENT_TYPE = os.getenv("WEBHOOK_CONTENT_TYPE", "JSON").upper()
WEBHOOK_QUERY_PARAMETERS = os.getenv("WEBHOOK_QUERY_PARAMETERS")
WEBHOOK_BODY = os.getenv("WEBHOOK_BODY")
# 布尔开关统一用 parse_env_bool：1/0、true/false、yes/no、on/off 都认，和 pydantic 配置模型保持一致
RUN_HEADLESS = parse_env_bool(os.getenv("RUN_HEADLESS"), True)
LOGIN_IS_EDGE = parse_env_bool(os.getenv("LOGIN_IS_EDGE"), False)
RUNNING_IN_DOCKER = parse_env_bool(os.getenv("RUNNING_IN_DOCKER"), False)
AI_DEBUG_MODE = parse_env_bool(os.getenv("AI_DEBUG_MODE"), False)
SKIP_AI_ANALYSIS = parse_env_bool(os.getenv("SKIP_AI_ANALYSIS"), False)
ENABLE_THINKING = parse_env_bool(os.getenv("ENABLE_THINKING"), False)
ENABLE_RESPONSE_FORMAT = parse_env_bool(os.getenv("ENABLE_RESPONSE_FORMAT"), True)

# --- AI 超时 / 重试 / 图片限制 ---
# AI_REQUEST_TIMEOUT_SECONDS      单次请求超时（默认 120s；SDK 默认 600s 太长）
# AI_SDK_MAX_RETRIES              openai SDK 自带的重试次数（默认 1；外层还有自己的分类重试）
# AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS 单个商品整次分析的总时限（默认 timeout×3；0 = 不限制）
# AI_MAX_IMAGES_PER_ITEM          每个商品最多送给模型多少张图（默认 6）
# AI_MAX_IMAGE_BYTES              单张图片大小上限，超过则跳过（默认 4MB）
AI_RUNTIME_LIMITS = AIRuntimeLimits.from_env()
AI_REQUEST_TIMEOUT_SECONDS = AI_RUNTIME_LIMITS.request_timeout_seconds
AI_SDK_MAX_RETRIES = AI_RUNTIME_LIMITS.sdk_max_retries
AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS = AI_RUNTIME_LIMITS.total_timeout_seconds
AI_MAX_IMAGES_PER_ITEM = AI_RUNTIME_LIMITS.max_images_per_item
AI_MAX_IMAGE_BYTES = AI_RUNTIME_LIMITS.max_image_bytes

# --- Headers ---
IMAGE_DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0',
    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# --- Client Initialization ---
# Claude 客户端只在 AI_PROVIDER=claude 时创建；OpenAI 那套保持原样
claude_client = None
CLAUDE_REQUEST_TIMEOUT_SECONDS = claude_request_timeout(AI_REQUEST_TIMEOUT_SECONDS)

if AI_PROVIDER == AI_PROVIDER_CLAUDE:
    client = None
    if not CLAUDE_API_KEY:
        print("警告：已选择 Claude 作为 AI 提供方，但未设置 CLAUDE_API_KEY。AI相关功能将不可用。")
    else:
        try:
            _sanitize_no_proxy_env()
            if PROXY_URL:
                print(f"正在为AI请求使用HTTP/S代理: {redact_url_userinfo(PROXY_URL)}")
                # 和 OpenAI 分支一致：图片下载（requests）也走同一个代理
                os.environ['HTTP_PROXY'] = PROXY_URL
                os.environ['HTTPS_PROXY'] = PROXY_URL
            claude_client = create_async_claude_client(
                api_key=CLAUDE_API_KEY,
                base_url=CLAUDE_BASE_URL,
                timeout=CLAUDE_REQUEST_TIMEOUT_SECONDS,
                max_retries=AI_SDK_MAX_RETRIES,
                proxy_url=PROXY_URL or None,
            )
        except Exception as e:
            print(f"初始化 Claude 客户端时出错: {e}")
            claude_client = None
elif not all([BASE_URL, MODEL_NAME]):  # 检查 OpenAI 配置是否齐全
    print("警告：未在 .env 文件中完整设置 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。AI相关功能可能无法使用。")
    client = None
else:
    try:
        # 旧客户端和 AIClient 必须在创建前共用同一套 NO_PROXY 清理。
        _sanitize_no_proxy_env()
        if PROXY_URL:
            print(f"正在为AI请求使用HTTP/S代理: {redact_url_userinfo(PROXY_URL)}")
            # httpx 会自动从环境变量中读取代理设置
            os.environ['HTTP_PROXY'] = PROXY_URL
            os.environ['HTTPS_PROXY'] = PROXY_URL

        # openai 客户端内部的 httpx 会自动从环境变量中获取代理配置
        client = AsyncOpenAI(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
            max_retries=AI_SDK_MAX_RETRIES,
        )
    except Exception as e:
        print(f"初始化 OpenAI 客户端时出错: {e}")
        client = None

# 检查AI客户端是否成功初始化
if not client:
    # 在 prompt_generator.py 中，如果 client 为 None，会直接报错退出
    # 在 spider_v2.py 中，AI分析会跳过
    # 为了保持一致性，这里只打印警告，具体逻辑由调用方处理
    pass

# 检查关键配置
if not all([BASE_URL, MODEL_NAME]) and 'prompt_generator.py' in sys.argv[0]:
    sys.exit("错误：请确保在 .env 文件中完整设置了 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。(OPENAI_API_KEY 对于某些服务是可选的)")

def get_ai_request_params(**kwargs):
    """
    构建AI请求参数，根据ENABLE_THINKING和ENABLE_RESPONSE_FORMAT环境变量决定是否添加相应参数
    """
    if ENABLE_THINKING:
        kwargs["extra_body"] = {"enable_thinking": False}
    
    # 如果禁用结构化输出，则移除 text.format 配置
    if not ENABLE_RESPONSE_FORMAT and "text" in kwargs:
        text_config = kwargs.get("text")
        if isinstance(text_config, dict):
            text_config = dict(text_config)
            text_config.pop("format", None)
            if text_config:
                kwargs["text"] = text_config
            else:
                del kwargs["text"]
    
    return kwargs
