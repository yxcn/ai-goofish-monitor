import asyncio
import base64
import json
import os
import re
import sys
import shutil
import traceback
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests

# 设置标准输出编码为UTF-8，解决Windows控制台编码问题
if sys.platform.startswith('win'):
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())

from src.config import (
    AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS,
    AI_DEBUG_MODE,
    AI_MAX_IMAGE_BYTES,
    AI_MAX_IMAGES_PER_ITEM,
    IMAGE_DOWNLOAD_HEADERS,
    IMAGE_SAVE_DIR,
    TASK_IMAGE_DIR_PREFIX,
    MODEL_NAME,
    ENABLE_RESPONSE_FORMAT,
    client,
    get_ai_request_params,
)
from src.ai_message_builder import (
    build_analysis_text_prompt,
    build_user_message_content,
)
from src.infrastructure.external.ai_client import AIAnalysisTimeoutError
from src.services.ai_response_parser import (
    AIResponseFormatError,
    AIResponseTruncatedError,
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)
from src.services.ai_request_compat import (
    BACKOFF,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    FAIL,
    TRUNCATION_TOKEN_MULTIPLIER,
    CompatProfile,
    build_image_data_url,
    classify_ai_error,
    create_ai_response_async,
    describe_compat_changes,
    read_image_mime,
    retry_backoff_seconds,
)
from src.services.notification_service import build_notification_service
from src.utils import convert_goofish_link


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY = max(
    1,
    _positive_int(os.getenv("IMAGE_DOWNLOAD_CONCURRENCY", "3"), 3),
)

# 单张图片下载：尝试次数与重试间隔（秒）；超大图不重试
IMAGE_DOWNLOAD_RETRIES = 2
IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS = 3
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20
IMAGE_DOWNLOAD_CHUNK_SIZE = 8192

# AI 调用：默认输出上限、429/5xx 退避表、总尝试次数（可在测试里 monkeypatch）
AI_MAX_OUTPUT_TOKENS = 4000
AI_RETRY_BACKOFF_SECONDS = DEFAULT_RETRY_BACKOFF_SECONDS
AI_MAX_ATTEMPTS = DEFAULT_MAX_ATTEMPTS

# 兼容性探测结果在模块级缓存：同一进程里后续商品直接复用第一个商品学到的参数
_compat_profile = CompatProfile()


def get_compat_profile() -> CompatProfile:
    return _compat_profile


def reset_compat_profile() -> None:
    """重新加载配置 / 换模型时调用，清掉已缓存的兼容性探测结果。"""
    _compat_profile.reset()


class ImageTooLargeError(Exception):
    """图片超过 AI_MAX_IMAGE_BYTES，跳过且不重试。"""


def safe_print(text):
    """安全的打印函数，处理编码错误"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果遇到编码错误，尝试用ASCII编码并忽略无法编码的字符
        try:
            print(text.encode('ascii', errors='ignore').decode('ascii'))
        except:
            # 如果还是失败，打印一个简化的消息
            print("[输出包含无法显示的字符]")


def _build_debug_request_summary(api_mode: str, request_params: dict) -> dict:
    summary = {
        "api_mode": api_mode,
        "model": request_params.get("model"),
    }
    if "temperature" in request_params:
        summary["temperature"] = request_params["temperature"]
    if "max_output_tokens" in request_params:
        summary["max_output_tokens"] = request_params["max_output_tokens"]
    if "max_tokens" in request_params:
        summary["max_tokens"] = request_params["max_tokens"]
    if "max_completion_tokens" in request_params:
        summary["max_completion_tokens"] = request_params["max_completion_tokens"]
    if "text" in request_params:
        summary["text"] = request_params["text"]
    if "response_format" in request_params:
        summary["response_format"] = request_params["response_format"]
    if "input" in request_params:
        summary["input_content_types"] = [
            [item.get("type") for item in message.get("content", [])]
            for message in request_params["input"]
        ]
    if "messages" in request_params:
        summary["message_content_types"] = [
            _extract_message_content_types(message)
            for message in request_params["messages"]
        ]
    return summary


def _extract_message_content_types(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return ["text"]
    if not isinstance(content, list):
        return [type(content).__name__]
    return [str(item.get("type")) for item in content if isinstance(item, dict)]


def _download_image_to_file(url: str, save_path: str, max_bytes: int) -> str:
    """同步下载：建连、读 body、写文件全部在工作线程里完成，不占事件循环。

    先写 ``save_path + ".part"``，完整落盘后再改名，避免半截文件被下次运行当成已下载。
    超过 ``max_bytes``（看 Content-Length 或边读边数）抛 ImageTooLargeError。
    """
    part_path = f"{save_path}.part"
    with requests.get(
        url,
        headers=IMAGE_DOWNLOAD_HEADERS,
        timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        stream=True,
    ) as response:
        response.raise_for_status()
        declared = (response.headers or {}).get("Content-Length")
        if declared and str(declared).strip().isdigit() and int(declared) > max_bytes:
            raise ImageTooLargeError(
                f"Content-Length {int(declared)} 字节超过上限 {max_bytes} 字节"
            )

        written = 0
        try:
            with open(part_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=IMAGE_DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise ImageTooLargeError(
                            f"已读取 {written} 字节，超过上限 {max_bytes} 字节"
                        )
                    handle.write(chunk)
            os.replace(part_path, save_path)
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
    return save_path


async def _download_single_image(url, save_path):
    """异步下载单个图片：整个下载放进一个线程，带重试；超大图直接跳过返回 None。"""
    for attempt in range(1, IMAGE_DOWNLOAD_RETRIES + 1):
        try:
            return await asyncio.to_thread(
                _download_image_to_file, url, save_path, AI_MAX_IMAGE_BYTES
            )
        except ImageTooLargeError as exc:
            safe_print(f"   [图片] 图片过大，已跳过（不重试）: {exc} - {url}")
            return None
        except Exception as exc:
            safe_print(
                f"   [图片] 下载第 {attempt}/{IMAGE_DOWNLOAD_RETRIES} 次失败: "
                f"{type(exc).__name__} - {exc}"
            )
            if attempt < IMAGE_DOWNLOAD_RETRIES:
                if IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS > 0:
                    await asyncio.sleep(IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS)
                continue
    safe_print(f"   [图片] 在 {IMAGE_DOWNLOAD_RETRIES} 次尝试后彻底失败: {url}")
    return None


def _build_image_save_path(
    product_id: str,
    index: int,
    url: str,
    task_image_dir: str,
) -> str:
    clean_url = url.split('.heic')[0] if '.heic' in url else url
    file_name_base = os.path.basename(clean_url).split('?')[0]
    file_name = f"product_{product_id}_{index}_{file_name_base}"
    file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
    if not os.path.splitext(file_name)[1]:
        file_name += ".jpg"
    return os.path.join(task_image_dir, file_name)


async def download_all_images(
    product_id,
    image_urls,
    task_name="default",
    concurrency=None,
    max_images=None,
):
    """异步下载一个商品的图片（最多 AI_MAX_IMAGES_PER_ITEM 张）。已存在则跳过。支持任务隔离。"""
    if not image_urls:
        return []

    # 为每个任务创建独立的图片目录
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    os.makedirs(task_image_dir, exist_ok=True)

    urls = [url.strip() for url in image_urls if url.strip().startswith('http')]
    if not urls:
        return []

    limit = _positive_int(max_images, AI_MAX_IMAGES_PER_ITEM)
    if len(urls) > limit:
        safe_print(
            f"   [图片] 商品共 {len(urls)} 张图，超过上限 {limit} 张，只下载前 {limit} 张。"
        )
        urls = urls[:limit]

    max_concurrency = _positive_int(concurrency, DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrency)
    total_images = len(urls)

    async def _download_one(index: int, url: str):
        save_path = _build_image_save_path(product_id, index, url, task_image_dir)
        if os.path.exists(save_path):
            safe_print(
                f"   [图片] 图片 {index}/{total_images} 已存在，跳过下载: {os.path.basename(save_path)}"
            )
            return save_path
        async with semaphore:
            safe_print(f"   [图片] 正在下载图片 {index}/{total_images}: {url}")
            if await _download_single_image(url, save_path):
                safe_print(
                    f"   [图片] 图片 {index}/{total_images} 已成功下载到: {os.path.basename(save_path)}"
                )
                return save_path
        return None

    tasks = [
        asyncio.create_task(_download_one(index, url))
        for index, url in enumerate(urls, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    saved_paths = []
    for url, result in zip(urls, results):
        try:
            if isinstance(result, Exception):
                raise result
            if result:
                saved_paths.append(result)
        except Exception as e:
            safe_print(f"   [图片] 处理图片 {url} 时发生错误，已跳过此图: {e}")

    return saved_paths


def cleanup_task_images(task_name):
    """清理指定任务的图片目录"""
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    if os.path.exists(task_image_dir):
        try:
            shutil.rmtree(task_image_dir)
            safe_print(f"   [清理] 已删除任务 '{task_name}' 的临时图片目录: {task_image_dir}")
        except Exception as e:
            safe_print(f"   [清理] 删除任务 '{task_name}' 的临时图片目录时出错: {e}")
    else:
        safe_print(f"   [清理] 任务 '{task_name}' 的临时图片目录不存在: {task_image_dir}")


def cleanup_ai_logs(logs_dir: str, keep_days: int = 1) -> None:
    try:
        cutoff = datetime.now() - timedelta(days=keep_days)
        for filename in os.listdir(logs_dir):
            if not filename.endswith(".log"):
                continue
            try:
                timestamp = datetime.strptime(filename[:15], "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            if timestamp < cutoff:
                os.remove(os.path.join(logs_dir, filename))
    except Exception as e:
        safe_print(f"   [日志] 清理AI日志时出错: {e}")


def encode_image_to_base64(image_path):
    """将本地图片文件编码为 Base64 字符串。"""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        safe_print(f"编码图片时出错: {e}")
        return None


def build_image_data_urls(image_paths, max_images=None, max_bytes=None) -> list[str]:
    """把本地图片编码成 data URL：只取前 N 张、跳过超大图、MIME 按文件头判断。"""
    limit = _positive_int(max_images, AI_MAX_IMAGES_PER_ITEM)
    size_limit = _positive_int(max_bytes, AI_MAX_IMAGE_BYTES)
    paths = list(image_paths or [])
    if len(paths) > limit:
        safe_print(f"   [AI分析] 图片 {len(paths)} 张超过上限 {limit} 张，只送前 {limit} 张给模型。")
        paths = paths[:limit]

    data_urls: list[str] = []
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        if size is not None and size > size_limit:
            safe_print(
                f"   [AI分析] 图片 {os.path.basename(path)} 大小 {size} 字节超过上限 {size_limit}，跳过。"
            )
            continue
        base64_image = encode_image_to_base64(path)
        if base64_image:
            data_urls.append(build_image_data_url(base64_image, read_image_mime(path)))
    return data_urls


def validate_ai_response_format(parsed_response):
    """验证AI响应的格式是否符合预期结构"""
    required_fields = [
        "prompt_version",
        "is_recommended",
        "reason",
        "risk_tags",
        "criteria_analysis"
    ]

    # 检查顶层字段
    for field in required_fields:
        if field not in parsed_response:
            safe_print(f"   [AI分析] 警告：响应缺少必需字段 '{field}'")
            return False

    # 检查criteria_analysis是否为字典且不为空
    criteria_analysis = parsed_response.get("criteria_analysis", {})
    if not isinstance(criteria_analysis, dict) or not criteria_analysis:
        safe_print("   [AI分析] 警告：criteria_analysis必须是非空字典")
        return False

    # 检查seller_type字段（所有商品都需要）
    if "seller_type" not in criteria_analysis:
        safe_print("   [AI分析] 警告：criteria_analysis缺少必需字段 'seller_type'")
        return False

    # 检查数据类型
    if not isinstance(parsed_response.get("is_recommended"), bool):
        safe_print("   [AI分析] 警告：is_recommended字段不是布尔类型")
        return False

    if not isinstance(parsed_response.get("risk_tags"), list):
        safe_print("   [AI分析] 警告：risk_tags字段不是列表类型")
        return False

    return True


async def send_ntfy_notification(product_data, reason):
    """兼容旧调用名，内部统一走 NotificationService。

    重试在 NotificationService 里按渠道做；这里不再套 retry_on_failure——
    它只对抛异常生效，而发送失败被封装成 success=False 返回，装饰器从未真正重试过。
    返回各渠道结果，调用方据此记录推送状态。
    """
    service = build_notification_service()
    if not service.clients:
        safe_print(
            "警告：未在 .env 文件中配置任何通知服务，跳过通知。"
        )
        return {}

    results = await service.send_notification(product_data, reason)
    for channel, result in results.items():
        if result["success"]:
            safe_print(f"   -> {channel} 通知发送成功。")
            continue
        safe_print(f"   -> {channel} 通知发送失败: {result['message']}")
    return results


async def _run_ai_analysis_attempts(messages, product_id) -> dict:
    """带分类重试的 AI 调用循环（不含总时限，由 get_ai_analysis 套 wait_for）。

    - 兼容性探测错误：按模块级 CompatProfile 换参数立刻重试，并缓存给后续商品；
    - 空响应 / JSON 解析失败 / 字段缺失：立刻重试（重试时温度更低）；
    - 429 / 5xx / 网络超时：按 AI_RETRY_BACKOFF_SECONDS 退避；
    - 其余 4xx：直接失败；
    - 输出被截断：max_output_tokens 提高到 1.5 倍只再试一次，仍截断则失败。
    """
    profile = _compat_profile
    attempt = 0
    truncation_retries = 0
    max_output_tokens = AI_MAX_OUTPUT_TOKENS
    call_index = 0

    while True:
        call_index += 1
        # 重试时使用更低的温度
        current_temperature = 0.1 if attempt == 0 else 0.05
        request_params = profile.build_request_params(
            model=MODEL_NAME,
            messages=messages,
            temperature=current_temperature,
            max_output_tokens=max_output_tokens,
            enable_json_output=ENABLE_RESPONSE_FORMAT,
        )
        request_params = get_ai_request_params(**request_params)
        api_mode = profile.api_mode

        if AI_DEBUG_MODE:
            safe_print(f"\n--- [AI DEBUG] 第{call_index}次尝试 REQUEST ---")
            safe_print(
                json.dumps(
                    _build_debug_request_summary(api_mode, request_params),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            safe_print("-----------------------------------\n")

        try:
            response = await create_ai_response_async(client, api_mode, request_params)
            ai_response_content = extract_ai_response_content(response)

            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{call_index}次尝试 ---")
                safe_print("--- RAW AI RESPONSE ---")
                safe_print(ai_response_content)
                safe_print("---------------------\n")

            parsed_response = parse_ai_response_json(ai_response_content)
            if not validate_ai_response_format(parsed_response):
                raise AIResponseFormatError("AI响应格式缺少必需字段或字段类型不正确。")
            safe_print(f"   [AI分析] 第{call_index}次尝试成功，响应格式验证通过")
            return parsed_response

        except AIResponseTruncatedError as exc:
            safe_print(f"   [AI分析] 第{call_index}次尝试输出被截断: {exc}")
            if truncation_retries >= 1:
                safe_print("   [AI分析] 提高输出上限后仍被截断，放弃本商品的分析。")
                raise
            truncation_retries += 1
            max_output_tokens = int(max_output_tokens * TRUNCATION_TOKEN_MULTIPLIER)
            safe_print(f"   [AI分析] 将输出上限提高到 {max_output_tokens} 后重试一次...")
            continue

        except Exception as exc:
            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{call_index}次尝试 EXCEPTION ---")
                safe_print(repr(exc))
                safe_print(traceback.format_exc())
                safe_print("-------------------------------------\n")

            changes = profile.learn_from_error(exc)
            if changes:
                safe_print(
                    f"   [AI分析] 第{call_index}次尝试遇到兼容性问题，"
                    f"{describe_compat_changes(changes, profile)}后重试（结果已缓存给后续商品）。"
                )
                continue

            # 走到这里说明 profile 没学到新东西：同样的"兼容性"报错再出现就不是兼容性问题
            kind = classify_ai_error(exc, api_mode, allow_compat_retry=False)
            if kind == FAIL:
                safe_print(
                    f"   [AI分析] 第{call_index}次尝试失败且不可重试（{type(exc).__name__}）: {exc}"
                )
                raise

            attempt += 1
            if attempt >= AI_MAX_ATTEMPTS:
                safe_print(f"   [AI分析] 已达最大尝试次数 {AI_MAX_ATTEMPTS}，放弃: {exc}")
                raise

            if kind == BACKOFF:
                delay = retry_backoff_seconds(attempt, AI_RETRY_BACKOFF_SECONDS)
                safe_print(
                    f"   [AI分析] 第{call_index}次尝试遇到可恢复错误（{type(exc).__name__}: {exc}），"
                    f"{delay:g}s 后进行第{attempt + 1}次尝试..."
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                safe_print(
                    f"   [AI分析] 第{call_index}次尝试响应异常（{exc}），准备第{attempt + 1}次尝试..."
                )


async def get_ai_analysis(product_data, image_paths=None, prompt_text=""):
    """将完整的商品JSON数据和所有图片发送给 AI 进行分析（异步）。"""
    if not client:
        safe_print("   [AI分析] 错误：AI客户端未初始化，跳过分析。")
        return None

    item_info = product_data.get('商品信息', {})
    product_id = item_info.get('商品ID', 'N/A')

    safe_print(f"\n   [AI分析] 开始分析商品 #{product_id} (含 {len(image_paths or [])} 张图片)...")
    safe_print(f"   [AI分析] 标题: {item_info.get('商品标题', '无')}")

    if not prompt_text:
        safe_print("   [AI分析] 错误：未提供AI分析所需的prompt文本。")
        return None

    product_details_json = json.dumps(product_data, ensure_ascii=False, indent=2)
    system_prompt = prompt_text

    if AI_DEBUG_MODE:
        safe_print("\n--- [AI DEBUG] ---")
        safe_print("--- PRODUCT DATA (JSON) ---")
        safe_print(product_details_json)
        safe_print("--- PROMPT TEXT (完整内容) ---")
        safe_print(prompt_text)
        safe_print("-------------------\n")

    image_data_urls = build_image_data_urls(image_paths)

    combined_text_prompt = build_analysis_text_prompt(
        product_details_json,
        system_prompt,
        include_images=bool(image_data_urls),
    )
    user_content = build_user_message_content(combined_text_prompt, image_data_urls)
    messages = [{"role": "user", "content": user_content}]

    # 保存最终传输内容到日志文件
    try:
        # 创建logs文件夹
        logs_dir = os.path.join("logs", "ai")
        os.makedirs(logs_dir, exist_ok=True)
        cleanup_ai_logs(logs_dir, keep_days=1)

        # 生成日志文件名（当前时间）
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"{current_time}.log"
        log_filepath = os.path.join(logs_dir, log_filename)

        task_name = product_data.get("任务名称") or product_data.get("任务名") or "unknown"
        log_payload = {
            "timestamp": current_time,
            "task_name": task_name,
            "product_id": product_id,
            "title": item_info.get("商品标题", "无"),
            "image_count": len(image_data_urls),
        }
        log_content = json.dumps(log_payload, ensure_ascii=False)

        # 写入日志文件
        with open(log_filepath, 'w', encoding='utf-8') as f:
            f.write(log_content)

        safe_print(f"   [日志] AI分析请求已保存到: {log_filepath}")

    except Exception as e:
        safe_print(f"   [日志] 保存AI分析日志时出错: {e}")

    total_timeout = AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS
    if not total_timeout or total_timeout <= 0:
        return await _run_ai_analysis_attempts(messages, product_id)

    try:
        return await asyncio.wait_for(
            _run_ai_analysis_attempts(messages, product_id),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        safe_print(
            f"   [AI分析] 商品 #{product_id} 的分析超过总时限 {total_timeout:g}s（含重试），已放弃。"
        )
        raise AIAnalysisTimeoutError(
            f"商品 #{product_id} 的 AI 分析超过总时限 {total_timeout:g}s（含重试）。"
        ) from None
