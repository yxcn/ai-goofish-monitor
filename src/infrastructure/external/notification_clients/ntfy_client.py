"""
Ntfy 通知客户端
"""
import asyncio
import requests
from typing import Dict
from .base import NotificationClient, url_path_segments


class NtfyClient(NotificationClient):
    """Ntfy 通知客户端"""

    channel_key = "ntfy"
    display_name = "Ntfy"

    def __init__(self, topic_url: str = None, pcurl_to_mobile: bool = True):
        super().__init__(enabled=bool(topic_url), pcurl_to_mobile=pcurl_to_mobile)
        self.topic_url = topic_url

    def _secrets(self) -> tuple[str, ...]:
        # ntfy 没有别的鉴权时 topic 名就是密钥（知道就能订阅）；URL 里的 user:pass 由 _redact 统一处理
        return url_path_segments(self.topic_url)

    async def send(self, product_data: Dict, reason: str) -> None:
        """发送 Ntfy 通知"""
        if not self.is_enabled():
            raise RuntimeError("Ntfy 未启用")

        message = self._build_message(product_data, reason)
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    self.topic_url,
                    data=message.content.encode('utf-8'),
                    headers={
                        "Title": message.notification_title.encode('utf-8'),
                        "Priority": "urgent",
                        "Tags": "bell,vibration"
                    },
                    timeout=10
                )
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # 报错文本里带完整 topic URL，脱敏后再抛
            self._raise_sanitized(exc)
