"""
通知客户端基类
定义通知客户端的统一接口
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from src.utils import convert_goofish_link

VERDICT_STRONG = "强烈推荐"
VERDICT_CONDITIONAL = "有条件推荐"
VERDICT_REJECT = "不建议购买"

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
    # 推荐结论（强烈推荐 / 有条件推荐 / 不建议购买），没有 AI 结果时为 None
    verdict: Optional[str] = None
    # AI 给的匹配度 0-100（网页端的 AI Match）
    match_score: Optional[int] = None
    # 各项标准的通过情况，如 "10 项通过，2 项待确认"
    criteria_summary: Optional[str] = None
    # AI 对性价比的一句话评价
    value_summary: Optional[str] = None
    # 页面上的划线原价（和现价相同或缺失时为 None）
    original_price: Optional[str] = None
    # 商品标签，如 包邮、验货宝
    tags: tuple[str, ...] = ()
    wants_count: Optional[int] = None
    view_count: Optional[int] = None
    publish_time: Optional[str] = None
    region: Optional[str] = None
    favorite_count: Optional[int] = None
    # 卖家所在城市（比发货地区的省份细）
    city: Optional[str] = None
    # 卖家最近活跃，如 "21分钟前来过"
    seller_last_active: Optional[str] = None
    seller_reply_rate: Optional[str] = None
    seller_reply_interval: Optional[str] = None
    # 卖家画像的结论和信号（见 src/seller_profile.py）
    seller_profile_summary: Optional[str] = None
    seller_signals: tuple[str, ...] = ()
    # 画像的身份行：注册时长、个人档案（性别 / 年龄段，没有写暂无）、认证、芝麻信用、经营资质
    seller_identity: Optional[str] = None
    # 行情：比成交参考价/挂牌中位价高低多少、结论（见 price_history_service.build_price_verdict）
    market_line: Optional[str] = None


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


def recommendation_verdict(analysis: dict | None) -> Optional[str]:
    """和网页端结果卡片同一套规则：推荐即“强烈推荐”，reason 以“有条件推荐”开头的单列，否则“不建议购买”。"""
    if not isinstance(analysis, dict) or "is_recommended" not in analysis:
        return None
    if not analysis.get("is_recommended"):
        return VERDICT_REJECT
    reason = str(analysis.get("reason") or "").lstrip()
    if reason.startswith(VERDICT_CONDITIONAL):
        return VERDICT_CONDITIONAL
    return VERDICT_STRONG


def match_score(analysis: dict | None) -> Optional[int]:
    """value_score 可能是数字或字符串，统一成 0-100 的整数；缺失或解析不了返回 None。"""
    if not isinstance(analysis, dict):
        return None
    raw = analysis.get("value_score")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, round(score)))


def summarize_criteria(analysis: dict | None) -> tuple[int, int, int]:
    """统计 criteria_analysis 里 PASS / 待人工确认 / FAIL 的条数。"""
    criteria = analysis.get("criteria_analysis") if isinstance(analysis, dict) else None
    passed = check = failed = 0
    for entry in (criteria or {}).values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().upper()
        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed += 1
        elif "CHECK" in status:
            check += 1
    return passed, check, failed


def _as_int(value) -> Optional[int]:
    """“想要”人数 / 浏览量 可能是数字，也可能是 'NaN'、'-' 这类占位符。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _clean_text(value) -> Optional[str]:
    text = str(value or "").strip()
    return text if text and text not in {"暂无", "-", "N/A", "NaN"} else None


def listing_facts(product_data: Dict) -> dict:
    """从商品信息里取页面上那几个展示字段：原价、标签、想要、浏览、收藏、发布时间、发货地、卖家活跃度。"""
    price = _clean_text(product_data.get("当前售价"))
    original_price = _clean_text(product_data.get("商品原价"))
    if original_price and price and _normalize_price(original_price) == _normalize_price(price):
        original_price = None
    tags = tuple(
        dict.fromkeys(t for t in (_clean_text(tag) for tag in (product_data.get("商品标签") or [])) if t)
    )
    return {
        "original_price": original_price,
        "tags": tags,
        "wants_count": _as_int(product_data.get("“想要”人数")),
        "view_count": _as_int(product_data.get("浏览量")),
        "publish_time": _clean_text(product_data.get("发布时间")),
        "region": _clean_text(product_data.get("发货地区")),
        "favorite_count": _as_int(product_data.get("收藏数")),
        "city": _clean_text(product_data.get("卖家所在城市")),
        "seller_last_active": _clean_text(product_data.get("卖家最近活跃")),
        "seller_reply_rate": _clean_text(product_data.get("卖家24小时回复率")),
        "seller_reply_interval": _clean_text(product_data.get("卖家平均回复时长")),
    }


def message_facts(message: "NotificationMessage") -> dict:
    """把 NotificationMessage 上的商品页字段还原成 describe_* 用的 dict，各渠道渲染时共用。"""
    return {
        "wants_count": message.wants_count,
        "view_count": message.view_count,
        "favorite_count": message.favorite_count,
        "publish_time": message.publish_time,
        "region": message.region,
        "city": message.city,
        "seller_last_active": message.seller_last_active,
        "seller_reply_rate": message.seller_reply_rate,
        "seller_reply_interval": message.seller_reply_interval,
    }


def describe_popularity(facts: dict) -> Optional[str]:
    """如 '8 人想要 · 208 次浏览 · 6 人收藏 · 11-05 12:29 发布 · 山东 淄博'。"""
    parts = []
    if facts.get("wants_count") is not None:
        parts.append(f"{facts['wants_count']} 人想要")
    if facts.get("view_count") is not None:
        parts.append(f"{facts['view_count']} 次浏览")
    if facts.get("favorite_count") is not None:
        parts.append(f"{facts['favorite_count']} 人收藏")
    if facts.get("publish_time"):
        parts.append(f"{_short_time(facts['publish_time'])} 发布")
    location = describe_location(facts.get("region"), facts.get("city"))
    if location:
        parts.append(location)
    return " · ".join(parts) if parts else None


def describe_location(region: Optional[str], city: Optional[str]) -> Optional[str]:
    """省份和城市都有时合成 '山东 淄博'；城市已经写在地区里（如 '上海'）就不重复。"""
    region = (region or "").strip()
    city = (city or "").strip()
    if region and city and city not in region and region not in city:
        return f"{region} {city}"
    return city or region or None


def seller_profile_lines(profile) -> tuple[Optional[str], tuple[str, ...], Optional[str]]:
    """从 卖家画像 里取推送要用的三样：一句结论、信号列表、身份行（个人档案 / 认证 / 经营资质）。"""
    if not isinstance(profile, dict):
        return None, (), None
    summary = str(profile.get("结论") or "").strip() or None
    signals = tuple(s for s in (str(x or "").strip() for x in (profile.get("信号") or [])) if s)
    identity = str(profile.get("身份") or "").strip()
    identity = identity if identity and identity != "暂无" else None
    return summary, signals, identity


def describe_market(insight) -> Optional[str]:
    """如 '比成交参考价低 11%（成交参考价 ¥1049，5 条）→ 性价比高 · 挂牌中位 ¥1688（8 条）'。"""
    if not isinstance(insight, dict):
        return None
    text = str(insight.get("price_verdict_text") or "").strip()
    if not text or insight.get("price_verdict_key") == "insufficient":
        return None
    parts = [text]
    basis = insight.get("price_verdict_basis")
    sold = insight.get("sold_reference") if isinstance(insight.get("sold_reference"), dict) else {}
    if basis == "sold" and insight.get("market_median_price"):
        parts.append(f"挂牌中位 ¥{float(insight['market_median_price']):.0f}（{insight.get('market_sample_count') or 0} 条）")
    elif basis == "listing" and sold.get("median_price"):
        parts.append(f"成交参考 ¥{float(sold['median_price']):.0f}（{sold.get('sample_count') or 0} 条，样本不足）")
    return " · ".join(parts)


def describe_seller(facts: dict) -> Optional[str]:
    """如 '21分钟前来过 · 24 小时回复率 100% · 平均 3分钟回复'。"""
    parts = []
    if facts.get("seller_last_active"):
        parts.append(facts["seller_last_active"])
    if facts.get("seller_reply_rate"):
        parts.append(f"24 小时回复率 {facts['seller_reply_rate']}")
    if facts.get("seller_reply_interval"):
        parts.append(f"平均 {facts['seller_reply_interval']}回复")
    return " · ".join(parts) if parts else None


def _normalize_price(text: str) -> str:
    return "".join(ch for ch in str(text) if ch.isdigit() or ch == ".")


def _short_time(text: str) -> str:
    # '2026-11-05 12:29' -> '11-05 12:29'
    return text[5:] if len(text) >= 16 and text[4] == "-" and text[:4].isdigit() else text


def describe_criteria(counts: tuple[int, int, int]) -> Optional[str]:
    passed, check, failed = counts
    parts = []
    if passed:
        parts.append(f"{passed} 项通过")
    if check:
        parts.append(f"{check} 项待确认")
    if failed:
        parts.append(f"{failed} 项不符")
    return "，".join(parts) if parts else None


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

        analysis = product_data.get("ai_analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        confirm_items, risk_notes = split_risk_tags(analysis.get("risk_tags"))
        verdict = recommendation_verdict(analysis)
        score = match_score(analysis)
        criteria_summary = describe_criteria(summarize_criteria(analysis))
        value_summary = str(analysis.get("value_summary") or "").strip() or None
        facts = listing_facts(product_data)
        popularity = describe_popularity(facts)
        seller_line = describe_seller(facts)
        profile_summary, signals, identity = seller_profile_lines(product_data.get("卖家画像"))
        market_line = describe_market(product_data.get("price_insight"))

        price_line = f"价格: {price}"
        if facts["original_price"]:
            price_line += f"（原价 {facts['original_price']}）"
        if facts["tags"]:
            price_line += " " + " · ".join(facts["tags"])

        content_lines = []
        if verdict:
            content_lines.append(
                f"结论: {verdict}（AI 匹配度 {score}%）" if score is not None else f"结论: {verdict}"
            )
        if criteria_summary:
            content_lines.append(f"标准: {criteria_summary}")
        content_lines.append(price_line)
        if market_line:
            content_lines.append(f"行情: {market_line}")
        if popularity:
            content_lines.append(f"热度: {popularity}")
        if seller_line:
            content_lines.append(f"卖家: {seller_line}")
        if identity:
            content_lines.append(f"身份: {identity}")
        if profile_summary:
            content_lines.append(f"画像: {profile_summary}")
        if signals:
            content_lines.append("信号: " + " · ".join(signals))
        content_lines.append(f"原因: {reason}")
        if value_summary:
            content_lines.append(f"性价比: {value_summary}")
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
        notification_title = f"🚨 {verdict or '新推荐'}! {short_title}{suffix}"

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
            verdict=verdict,
            match_score=score,
            criteria_summary=criteria_summary,
            value_summary=value_summary,
            original_price=facts["original_price"],
            tags=facts["tags"],
            wants_count=facts["wants_count"],
            view_count=facts["view_count"],
            publish_time=facts["publish_time"],
            region=facts["region"],
            favorite_count=facts["favorite_count"],
            city=facts["city"],
            seller_last_active=facts["seller_last_active"],
            seller_reply_rate=facts["seller_reply_rate"],
            seller_reply_interval=facts["seller_reply_interval"],
            seller_profile_summary=profile_summary,
            seller_signals=signals,
            seller_identity=identity,
            market_line=market_line,
        )
