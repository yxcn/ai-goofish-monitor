"""
企业微信机器人通知客户端
"""
import asyncio
from typing import Dict

import requests

from .base import NotificationClient, describe_popularity


class WeComBotClient(NotificationClient):
    """企业微信机器人通知客户端"""

    channel_key = "wecom"
    display_name = "企业微信"

    def __init__(self, bot_url: str | None = None, pcurl_to_mobile: bool = True):
        super().__init__(enabled=bool(bot_url), pcurl_to_mobile=pcurl_to_mobile)
        self.bot_url = bot_url

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("企业微信 未启用")

        message = self._build_message(product_data, reason)
        markdown_lines = [f"## {message.notification_title}", ""]
        if message.verdict:
            score = f"（AI 匹配度 {message.match_score}%）" if message.match_score is not None else ""
            markdown_lines.append(f"- 结论: **{message.verdict}**{score}")
        if message.criteria_summary:
            markdown_lines.append(f"- 标准: {message.criteria_summary}")
        price_line = f"- 价格: {message.price}"
        if message.original_price:
            price_line += f"（原价 {message.original_price}）"
        if message.tags:
            price_line += " " + " · ".join(message.tags)
        markdown_lines.append(price_line)
        popularity = describe_popularity(
            {
                "wants_count": message.wants_count,
                "view_count": message.view_count,
                "publish_time": message.publish_time,
                "region": message.region,
            }
        )
        if popularity:
            markdown_lines.append(f"- 热度: {popularity}")
        markdown_lines.append(f"- 原因: {message.reason}")
        if message.value_summary:
            markdown_lines.append(f"- 性价比: {message.value_summary}")
        if message.risk_notes:
            markdown_lines.append("- 注意: " + "；".join(message.risk_notes))
        if message.confirm_items:
            markdown_lines.append("- 聊卖家前需确认:")
            markdown_lines.extend(
                f"  {index}. {item}" for index, item in enumerate(message.confirm_items, 1)
            )
        if message.mobile_link:
            markdown_lines.append(f"- 手机端链接: [{message.mobile_link}]({message.mobile_link})")
        markdown_lines.append(f"- 电脑端链接: [{message.desktop_link}]({message.desktop_link})")
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": "\n".join(markdown_lines)},
        }
        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                self.bot_url,
                json=payload,
                headers=headers,
                timeout=10,
            ),
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode", 0) != 0:
            raise RuntimeError(result.get("errmsg", "企业微信返回未知错误"))
