"""
Telegram 通知客户端
"""
import asyncio
from html import escape
from typing import Dict

import requests

from src.infrastructure.config.settings import DEFAULT_TELEGRAM_API_BASE_URL

from .base import NotificationClient


class TelegramClient(NotificationClient):
    """Telegram 通知客户端"""

    channel_key = "telegram"
    display_name = "Telegram"

    def __init__(
        self,
        bot_token: str = None,
        chat_id: str = None,
        api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
        pcurl_to_mobile: bool = True,
    ):
        super().__init__(enabled=bool(bot_token and chat_id), pcurl_to_mobile=pcurl_to_mobile)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base_url = (
            (api_base_url or DEFAULT_TELEGRAM_API_BASE_URL).rstrip("/")
        )

    async def send(self, product_data: Dict, reason: str) -> None:
        """发送 Telegram 通知"""
        if not self.is_enabled():
            raise RuntimeError("Telegram 未启用")

        message = self._build_message(product_data, reason)
        telegram_payload = {
            "chat_id": self.chat_id,
            "text": self._render_text(message),
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        telegram_api_url = f"{self.api_base_url}/bot{self.bot_token}/sendMessage"

        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                telegram_api_url,
                json=telegram_payload,
                headers=headers,
                timeout=10
            )
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram 返回未知错误"))

    @staticmethod
    def _render_text(message) -> str:
        """拼 HTML 消息；标题、原因和标签都来自 AI/商品页，必须转义。"""
        short_title = message.title[:50] + ("..." if len(message.title) > 50 else "")
        lines = [
            "🚨 <b>新推荐!</b>",
            "",
            f"<b>{escape(short_title)}</b>",
            "",
            f"💰 价格: {escape(str(message.price))}",
            f"📝 原因: {escape(message.reason)}",
        ]
        if message.risk_notes:
            lines.append("⚠️ 注意:")
            lines.extend(f"• {escape(note)}" for note in message.risk_notes)
        if message.confirm_items:
            lines.append("❓ 聊卖家前需确认:")
            lines.extend(
                f"{index}. {escape(item)}" for index, item in enumerate(message.confirm_items, 1)
            )
        if message.mobile_link:
            lines.append(f"📱 <a href='{escape(message.mobile_link, quote=True)}'>手机端链接</a>")
        lines.append(f"💻 <a href='{escape(message.desktop_link, quote=True)}'>电脑端链接</a>")
        return "\n".join(lines)
