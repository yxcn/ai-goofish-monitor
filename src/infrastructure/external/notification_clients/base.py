"""
通知客户端基类
定义通知客户端的统一接口
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterable

from src.utils import convert_goofish_link

# AI 把"买前要和卖家确认的事"放在 risk_tags 里，以"需确认"开头；其余标签是单纯的风险提示
_CONFIRM_PREFIX = re.compile(r"^\s*需确认\s*[：:，,、\-—\s]*")


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    price: str
    reason: str
    desktop_link: str
    mobile_link: str | None
    notification_title: str
    content: str
    image_url: str | None
    # 聊卖家前要确认的事项（已去掉"需确认："前缀）
    confirm_items: tuple[str, ...] = ()
    # 其他风险提示
    risk_notes: tuple[str, ...] = ()


def split_risk_tags(tags: Iterable | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把 AI 的 risk_tags 拆成（需确认清单, 其他风险提示），去空去重，保持原顺序。"""
    confirm_items: list[str] = []
    risk_notes: list[str] = []
    for tag in tags or []:
        text = str(tag or "").strip()
        if not text:
            continue
        match = _CONFIRM_PREFIX.match(text)
        if match:
            item = text[match.end():].strip()
            if item and item not in confirm_items:
                confirm_items.append(item)
        elif text not in risk_notes:
            risk_notes.append(text)
    return tuple(confirm_items), tuple(risk_notes)


class NotificationClient(ABC):
    """通知客户端抽象基类"""

    channel_key = "unknown"
    display_name = "未知渠道"

    def __init__(self, enabled: bool = False, pcurl_to_mobile: bool = True):
        self._enabled = enabled
        self._pcurl_to_mobile = pcurl_to_mobile

    def is_enabled(self) -> bool:
        """检查客户端是否启用"""
        return self._enabled

    @abstractmethod
    async def send(self, product_data: Dict, reason: str) -> bool:
        """
        发送通知

        Args:
            product_data: 商品数据（商品信息字段；推荐通知还会带上 ``ai_analysis``）
            reason: 推荐原因

        Returns:
            是否发送成功
        """
        raise NotImplementedError

    def _build_message(self, product_data: Dict, reason: str) -> NotificationMessage:
        """格式化消息内容"""
        title = product_data.get('商品标题', 'N/A')
        price = product_data.get('当前售价', 'N/A')
        desktop_link = product_data.get('商品链接', '#')
        mobile_link = None

        if self._pcurl_to_mobile and desktop_link and desktop_link != "#":
            mobile_link = convert_goofish_link(desktop_link)

        analysis = product_data.get("ai_analysis") or {}
        confirm_items, risk_notes = split_risk_tags(
            analysis.get("risk_tags") if isinstance(analysis, dict) else None
        )

        content_lines = [
            f"价格: {price}",
            f"原因: {reason}",
        ]
        if risk_notes:
            content_lines.append("注意: " + "；".join(risk_notes))
        if confirm_items:
            content_lines.append("聊卖家前需确认:")
            content_lines.extend(f"{index}. {item}" for index, item in enumerate(confirm_items, 1))
        if mobile_link:
            content_lines.append(f"手机端链接: {mobile_link}")
            content_lines.append(f"电脑端链接: {desktop_link}")
        else:
            content_lines.append(f"链接: {desktop_link}")

        short_title = title[:30]
        suffix = "..." if len(title) > 30 else ""
        notification_title = f"🚨 新推荐! {short_title}{suffix}"

        main_image = product_data.get('商品主图链接')
        if not main_image:
            image_list = product_data.get('商品图片列表', [])
            if image_list:
                main_image = image_list[0]

        return NotificationMessage(
            title=title,
            price=price,
            reason=reason,
            desktop_link=desktop_link,
            mobile_link=mobile_link,
            notification_title=notification_title,
            content="\n".join(content_lines),
            image_url=main_image,
            confirm_items=confirm_items,
            risk_notes=risk_notes,
        )
