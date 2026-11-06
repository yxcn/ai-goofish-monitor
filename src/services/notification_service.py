"""
通知服务
统一管理所有通知渠道
"""
import asyncio
from typing import Dict, List, Sequence

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.factory import build_notification_clients
from src.services.notification_config_service import load_notification_settings
from src.infrastructure.config.settings import NotificationSettings


# 单个渠道失败后的重试间隔（秒）：共 3 次尝试。推送失败以前只打一行日志，推荐就丢了。
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)


class NotificationService:
    """通知服务"""

    def __init__(
        self,
        clients: List[NotificationClient],
        *,
        retry_delays: Sequence[float] | None = None,
    ):
        self.clients = [client for client in clients if client.is_enabled()]
        self.retry_delays = tuple(DEFAULT_RETRY_DELAYS if retry_delays is None else retry_delays)

    async def send_notification(
        self,
        product_data: Dict,
        reason: str,
        *,
        retry_delays: Sequence[float] | None = None,
    ) -> Dict[str, Dict[str, str | bool]]:
        """
        发送通知到所有启用的渠道，每个渠道独立重试。

        Returns:
            各渠道发送结果，包含成功状态、消息和尝试次数
        """
        if not self.clients:
            return {}

        delays = self.retry_delays if retry_delays is None else tuple(retry_delays)
        tasks = [
            self._send_with_result(client, product_data, reason, delays)
            for client in self.clients
        ]
        results = await asyncio.gather(*tasks)
        return {result["channel"]: result for result in results}

    async def send_test_notification(self) -> Dict[str, Dict[str, str | bool]]:
        test_product = {
            "商品标题": "[测试通知] 闲鱼智能监控",
            "当前售价": "0",
            "商品链接": "https://www.goofish.com/",
        }
        # 设置页的"测试"按钮要立刻给反馈，不重试
        return await self.send_notification(
            test_product,
            "这是一条测试通知，用于验证推送渠道是否可用。",
            retry_delays=(),
        )

    async def _send_with_result(
        self,
        client: NotificationClient,
        product_data: Dict,
        reason: str,
        retry_delays: Sequence[float] = (),
    ) -> Dict[str, str | bool | int]:
        attempts = len(retry_delays) + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                await client.send(product_data, reason)
                return {
                    "channel": client.channel_key,
                    "label": client.display_name,
                    "success": True,
                    "message": "发送成功" if attempt == 1 else f"第 {attempt} 次尝试发送成功",
                    "attempts": attempt,
                }
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    delay = retry_delays[attempt - 1]
                    print(f"   -> {client.display_name} 第 {attempt} 次发送失败，{delay:g} 秒后重试: {last_error}")
                    if delay > 0:
                        await asyncio.sleep(delay)
        return {
            "channel": client.channel_key,
            "label": client.display_name,
            "success": False,
            "message": last_error if attempts == 1 else f"{attempts} 次尝试均失败: {last_error}",
            "attempts": attempts,
        }


def build_notification_service(
    settings: NotificationSettings | None = None,
) -> NotificationService:
    notification_settings = settings or load_notification_settings()
    return NotificationService(build_notification_clients(notification_settings))
