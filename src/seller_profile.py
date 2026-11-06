"""
卖家画像：按固定规则把卖家资料压成几行可读的摘要。

输入是一条完整记录（商品信息 + 卖家信息），输出放在记录的 ``卖家画像`` 字段里，
AI 分析、推送消息和结果卡片共用。所有结论只基于可见记录：买卖历史里同类商品卖得多、
在售同款多、近期卖家评价密集，才算商家迹象；拿不到的信息写"暂无"，不臆测。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_FRACTION = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_PLACE_SUFFIXES = ("特别行政区", "自治区", "省", "市")

# 商家迹象的阈值
SIMILAR_ON_SALE_STRONG = 3   # 同时在售 N 件同类（含本商品）
SIMILAR_SOLD_WEAK = 2        # 此前已售 N 件同类：2 件留意，3 件起算明确迹象
SIMILAR_SOLD_STRONG = 3
SOLD_TOTAL_STRONG = 20       # 卖出过 N 件
RECENT_SELLER_RATINGS_STRONG = 5   # 近 30 天收到的买家评价
SELLER_RATINGS_WEAK = 30
ON_SALE_WEAK = 8
NEW_ACCOUNT_DAYS = 90

# 按标题把卖家发布过的商品归到粗粒度品类，用来描述买卖历史的构成（"12 件里 9 件电脑数码"）。
# 只描述交易内容，不据此推断性别、年龄、职业。
CATEGORY_GROUPS = (
    ("电脑数码", ("笔记本", "thinkpad", "macbook", "电脑", "显示器", "手机", "iphone", "ipad", "平板", "相机", "镜头",
               "耳机", "键盘", "鼠标", "硬盘", "内存", "显卡", "cpu", "主板", "路由", "switch", "ps5", "xbox", "游戏机",
               "手表", "watch", "充电", "数据线")),
    ("图书", ("书", "教材", "小说", "漫画", "教程")),
    ("母婴童装", ("童", "婴", "宝宝", "儿童", "奶粉", "纸尿裤", "玩具", "推车")),
    ("服饰鞋包", ("衣", "裙", "裤", "外套", "鞋", "包", "帽", "t恤", "卫衣", "羽绒", "大衣", "毛衣")),
    ("家居家电", ("家具", "沙发", "桌", "椅", "床", "冰箱", "洗衣机", "空调", "电饭", "吸尘", "灯", "锅", "净化")),
    ("美妆", ("口红", "面膜", "精华", "粉底", "香水", "护肤", "眼霜")),
    ("运动户外", ("健身", "跑步", "自行车", "瑜伽", "球", "露营", "滑雪", "哑铃")),
)
CATEGORY_FOCUS_SHARE = 0.7
CATEGORY_FOCUS_MIN_ITEMS = 3


def category_mix(items: Iterable[dict]) -> dict:
    """统计卖家发布过的商品落在哪些品类，按数量降序返回 {品类: 件数}。"""
    counts: dict[str, int] = {}
    for entry in items:
        title = str(entry.get("商品标题") or "").lower()
        if not title:
            continue
        group = next((name for name, words in CATEGORY_GROUPS if any(word in title for word in words)), "其他")
        counts[group] = counts.get(group, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def describe_category_mix(mix: dict) -> tuple[str, Optional[str]]:
    """返回（构成文字, 集中品类或 None），如 ('电脑数码 9、童装 2、其他 1', '电脑数码')。"""
    total = sum(mix.values())
    if not total:
        return "", None
    text = "、".join(f"{name} {count}" for name, count in mix.items())
    top_name, top_count = next(iter(mix.items()))
    focus = top_name if top_name != "其他" and top_count >= CATEGORY_FOCUS_MIN_ITEMS and top_count / total >= CATEGORY_FOCUS_SHARE else None
    return text, focus


def build_seller_profile(record: dict, now: Optional[datetime] = None) -> dict:
    """生成卖家画像。record 缺卖家资料时也会返回一份只有结论的画像，不抛异常。"""
    item = record.get("商品信息") if isinstance(record.get("商品信息"), dict) else {}
    seller = record.get("卖家信息") if isinstance(record.get("卖家信息"), dict) else {}
    now = now or datetime.now()

    items = [x for x in (seller.get("卖家发布的商品列表") or []) if isinstance(x, dict)]
    ratings = [x for x in (seller.get("卖家收到的评价列表") or []) if isinstance(x, dict)]
    has_profile = bool(seller.get("卖家昵称")) or bool(items) or bool(ratings)

    this_id = str(item.get("商品ID") or "")
    tokens = _keyword_tokens(record.get("搜索关键字"), record.get("实际搜索词"))
    category_label = str((item.get("商品属性") or {}).get("分类") or "").strip() if isinstance(item.get("商品属性"), dict) else ""
    category_id = item.get("品类ID")

    on_sale = [x for x in items if x.get("商品状态") == "在售"]
    sold = [x for x in items if x.get("商品状态") == "已售"]
    similar_on_sale = [x for x in on_sale if _is_similar(x, category_id, tokens, category_label)]
    similar_sold = [x for x in sold if _is_similar(x, category_id, tokens, category_label)]
    this_entry = next((x for x in items if str(x.get("商品ID") or "") == this_id), None)

    seller_ratings = [r for r in ratings if _user_role(r) == "卖家"]
    buyer_ratings = [r for r in ratings if _user_role(r) == "买家"]
    seller_positive = sum(1 for r in seller_ratings if r.get("评价类型") == "好评")
    seller_negative = sum(1 for r in seller_ratings if r.get("评价类型") == "差评")
    seller_dates = sorted(d for d in (_rating_date(r.get("评价时间")) for r in seller_ratings) if d)
    recent_seller = [d for d in seller_dates if (now.date() - d).days <= 30]
    rater_places = {p for p in (_place(r.get("评价者IP属地")) for r in seller_ratings) if p}
    buyer_dates = sorted(d for d in (_rating_date(r.get("评价时间")) for r in buyer_ratings) if d)

    sold_total = _count(seller.get("卖家卖出件数"))
    reg_days = _count(seller.get("卖家注册天数"))
    followers = _count(seller.get("卖家粉丝数"))
    # 主页头部给的总数；列表为空但总数不为零，说明列表没采到（翻页超时、风控），不能当成“干净”
    rating_total = _count(seller.get("卖家收到的评价总数"))
    item_total = _count(seller.get("卖家在售/已售商品数"))
    ratings_missing = not ratings and bool(rating_total)
    items_missing = not items and bool(item_total)
    # 闲鱼标注的经营资质（持证经营 / 个人经营者）：平台自己认定的经营账号，比任何行为推断都硬
    business = _clean(seller.get("卖家经营资质"), {"无"})

    # 个人档案：性别 / 年龄段，接口没给就写“暂无”
    archive = seller.get("卖家个人档案") if isinstance(seller.get("卖家个人档案"), dict) else {}
    gender = _clean(archive.get("性别"))
    age_band = _clean(archive.get("年龄段"))

    # 新人卖家：历史很干净——作为卖家没有评价、没有已售、没卖出过东西（买家评价最多 1 条）
    newcomer = (
        has_profile
        and not business
        and not ratings_missing
        and not items_missing
        and not seller_ratings
        and not sold
        and (sold_total or 0) == 0
        and len(buyer_ratings) <= 1
    )

    # --- 商家迹象 ---
    strong: list[str] = []
    weak: list[str] = []
    similar_label = category_label or (record.get("搜索关键字") or "同类")
    if business:
        strong.append(f"闲鱼标注为{business}")
    if len(similar_on_sale) >= SIMILAR_ON_SALE_STRONG:
        strong.append(f"同时在售 {len(similar_on_sale)} 件同类商品（{similar_label}，含本商品）")
    if len(similar_sold) >= SIMILAR_SOLD_STRONG:
        strong.append(f"此前已售 {len(similar_sold)} 件同类商品（{similar_label}）")
    elif len(similar_sold) >= SIMILAR_SOLD_WEAK:
        weak.append(f"此前已售 {len(similar_sold)} 件同类商品（{similar_label}）")
    if sold_total is not None and sold_total >= SOLD_TOTAL_STRONG:
        strong.append(f"卖出过 {sold_total} 件")
    if len(recent_seller) >= RECENT_SELLER_RATINGS_STRONG:
        strong.append(f"近 30 天收到 {len(recent_seller)} 条买家评价")
    if len(seller_ratings) >= SELLER_RATINGS_WEAK:
        weak.append(f"作为卖家累计 {len(seller_ratings)} 条评价")
    if len(on_sale) >= ON_SALE_WEAK:
        weak.append(f"同时在售 {len(on_sale)} 件商品")
    evidence = strong + weak

    if not has_profile:
        identity_verdict = "卖家资料未采集，无法判断是否商家"
        dealer_level = "未知"
    elif business:
        identity_verdict = f"商家（闲鱼标注为{business}）" + ("：" + "；".join(evidence[1:]) if len(evidence) > 1 else "")
        dealer_level = "商家"
    elif len(strong) >= 2:
        identity_verdict = "商家特征明显：" + "；".join(evidence)
        dealer_level = "商家"
    elif strong or len(weak) >= 2:
        identity_verdict = "疑似商家：" + "；".join(evidence)
        dealer_level = "疑似商家"
    elif weak:
        identity_verdict = "有商家迹象，需留意：" + "；".join(evidence)
        dealer_level = "留意"
    else:
        identity_verdict = "可见记录无商家迹象，按个人卖家对待"
        dealer_level = "个人"

    # --- 地点 / 认证 / 活跃 ---
    region = _clean(item.get("发货地区"), {"地区未知"})
    city = _clean(item.get("卖家所在城市"))
    ip_location = _clean(seller.get("卖家IP属地"))
    ip_match = _places_match(ip_location, region, city) if ip_location and (region or city) else None

    certs = list(seller.get("卖家认证") or []) if isinstance(seller.get("卖家认证"), list) else None
    if seller.get("卖家实人认证") is True and certs is not None and "实人认证" not in certs:
        certs.append("实人认证")
    elif seller.get("卖家实人认证") is True and certs is None:
        certs = ["实人认证"]
    fully_certified = bool(certs) and "实名认证" in certs and "实人认证" in certs

    reply_rate = _first_number(item.get("卖家24小时回复率"))
    reply_minutes = _minutes(item.get("卖家平均回复时长"))
    last_active = _clean(item.get("卖家最近活跃"))

    # --- 信号 ---
    signals: list[str] = []
    if business:
        signals.append(f"⚠️ 闲鱼标注{business}")
    elif dealer_level == "商家":
        signals.append("⚠️ 商家特征明显")
    elif dealer_level == "疑似商家":
        signals.append("⚠️ 疑似商家")
    if len(similar_sold) >= SIMILAR_SOLD_WEAK:
        signals.append(f"⚠️ 此前已售 {len(similar_sold)} 件同类")
    if reg_days is not None and reg_days < NEW_ACCOUNT_DAYS:
        signals.append(f"⚠️ 新号（注册 {reg_days} 天）")
    if newcomer:
        signals.append("⚠️ 新人卖家（无出售记录）")
    if ratings_missing or items_missing:
        signals.append("⚠️ 卖家资料未采全")
    elif has_profile and not seller_ratings:
        signals.append("⚠️ 卖家零评价")
    if seller_negative:
        signals.append(f"⚠️ 有 {seller_negative} 条差评")
    if ip_match is False:
        signals.append(f"⚠️ IP 属地（{ip_location}）与发货地（{city or region}）不一致")
    if certs is not None and not certs:
        signals.append("⚠️ 未见实名/实人认证")
    if reply_rate is not None and reply_rate < 50:
        signals.append(f"⚠️ 24 小时回复率仅 {item.get('卖家24小时回复率')}")
    if fully_certified:
        signals.append("✅ 认证齐全")
    if ip_match is True:
        signals.append("✅ IP 属地与发货地一致")
    if reply_rate is not None and reply_rate >= 80 and reply_minutes is not None and reply_minutes <= 10:
        signals.append("✅ 回复快")
    if len(seller_ratings) >= 5 and seller_negative == 0 and seller_positive / len(seller_ratings) >= 0.98:
        signals.append(f"✅ 卖家好评率 {seller_positive / len(seller_ratings):.0%}（{len(seller_ratings)} 条）")

    # --- 各行文字 ---
    summary_parts = [str(seller.get("卖家昵称") or item.get("卖家昵称") or "匿名卖家")]
    if gender or age_band:
        summary_parts.append(" · ".join(part for part in (gender, age_band) if part))
    if city or region:
        summary_parts.append(_join_place(region, city))
    if ip_location:
        suffix = "" if ip_match is None else ("，与发货地一致" if ip_match else "，与发货地不一致")
        summary_parts.append(f"IP 属地{ip_location}{suffix}")
    summary = summary_parts[0] + (f"（{' · '.join(summary_parts[1:])}）" if len(summary_parts) > 1 else "")

    identity_parts = []
    reg_text = _clean(seller.get("卖家注册时长"), {"未知"})
    reg_date = _clean(seller.get("卖家注册日期"))
    if reg_text or reg_date:
        identity_parts.append((reg_text or "注册时间未知") + (f"（{reg_date} 注册）" if reg_date else ""))
    if gender or age_band:
        identity_parts.append(f"个人档案：{gender or '性别暂无'} · {age_band or '年龄段暂无'}")
    elif has_profile:
        identity_parts.append("个人档案：暂无")
    if business:
        identity_parts.append(f"经营资质：{business}")
    if certs:
        identity_parts.append("、".join(certs))
    elif certs is not None:
        identity_parts.append("未见实名/实人认证")
    zhima = _clean(seller.get("卖家芝麻信用"), {"暂无"})
    identity_parts.append(f"芝麻信用{zhima}" if zhima else "芝麻信用未公开")
    credit = _clean(seller.get("卖家信用等级"), {"暂无"})
    if credit:
        identity_parts.append(credit)

    activity_parts = []
    if last_active:
        activity_parts.append(last_active)
    if item.get("卖家24小时回复率"):
        activity_parts.append(f"24 小时回复率 {item['卖家24小时回复率']}")
    if item.get("卖家平均回复时长"):
        activity_parts.append(f"平均 {item['卖家平均回复时长']}回复")
    if followers is not None:
        activity_parts.append(f"粉丝 {followers}")

    trade_parts = []
    if items or sold_total is not None:
        trade_parts.append(f"在售 {len(on_sale)} / 已售 {len(sold)}")
    if sold_total is not None:
        trade_parts.append(f"卖出过 {sold_total} 件")
    if similar_on_sale or similar_sold:
        trade_parts.append(f"同类在售 {len(similar_on_sale)}、已售 {len(similar_sold)}（{similar_label}）")
    if items and len(on_sale) == 1 and this_entry is not None and this_entry.get("商品状态") == "在售":
        trade_parts.append("在售只有本商品")
    if this_entry is not None and this_entry.get("发布标签"):
        trade_parts.append(f"本商品{this_entry['发布标签']}")

    history_parts = []
    if seller_ratings:
        span = _span(seller_dates)
        places = f"，评价者来自 {len(rater_places)} 个省" if len(rater_places) >= 2 else ""
        history_parts.append(f"作为卖家收到 {len(seller_ratings)} 条评价" + (f"（{span}{places}）" if span else ""))
    elif ratings_missing:
        history_parts.append(f"评价列表未采集（主页显示 {rating_total} 条评价）")
    elif has_profile:
        history_parts.append("作为卖家没有收到过评价")
    if items_missing:
        history_parts.append(f"商品列表未采集（主页显示 {item_total} 件）")
    if buyer_ratings:
        span = _span(buyer_dates)
        history_parts.append(f"作为买家 {len(buyer_ratings)} 条评价" + (f"（{span}）" if span else ""))
    if similar_sold:
        history_parts.append(f"此前卖出过 {len(similar_sold)} 件同类商品")
    elif sold:
        history_parts.append(f"已售的 {len(sold)} 件都不是同类商品")
    mix = category_mix(items)
    mix_text, focus = describe_category_mix(mix)
    if len(items) >= 2 and mix_text:
        history_parts.append(f"发布过 {len(items)} 件：{mix_text}" + (f"，交易集中在{focus}" if focus else ""))

    reputation_parts = []
    if seller_ratings:
        rate = seller_positive / len(seller_ratings)
        reputation_parts.append(f"作为卖家 {seller_positive}/{len(seller_ratings)} 好评（{rate:.0%}）")
        if seller_negative:
            reputation_parts.append(f"{seller_negative} 条差评")
        if rating_total and rating_total > len(ratings):
            reputation_parts.append(f"已采集 {len(ratings)}/{rating_total} 条")
    elif ratings_missing:
        reputation_parts.append(f"评价列表未采集（主页显示 {rating_total} 条）")
    elif has_profile:
        reputation_parts.append("作为卖家暂无评价")
    if buyer_ratings:
        buyer_positive = sum(1 for r in buyer_ratings if r.get("评价类型") == "好评")
        reputation_parts.append(f"作为买家 {buyer_positive}/{len(buyer_ratings)} 好评")
    latest = max(seller_dates + buyer_dates) if (seller_dates or buyer_dates) else None
    if latest:
        latest_rating = max(ratings, key=lambda r: _rating_date(r.get("评价时间")) or datetime.min.date())
        place = _place(latest_rating.get("评价者IP属地"))
        reputation_parts.append(f"最近一条 {latest.isoformat()}" + (f"（评价者来自{place}）" if place else ""))

    # --- 结论 ---
    conclusion = [identity_verdict]
    if reg_days is not None and reg_days < NEW_ACCOUNT_DAYS:
        conclusion.append(f"注册仅 {reg_days} 天的新号")
    if newcomer:
        conclusion.append("没有任何出售记录，按新人卖家对待，建议验货宝或担保交易")
    elif ratings_missing or items_missing:
        conclusion.append("卖家资料未采全，口碑待下次抓取补齐")
    elif has_profile and not seller_ratings:
        conclusion.append("无卖家口碑，建议验货宝或担保交易")
    elif seller_negative:
        conclusion.append(f"有 {seller_negative} 条差评，先看评价内容")
    elif len(seller_ratings) >= 5 and seller_positive / len(seller_ratings) >= 0.98:
        conclusion.append(f"卖家口碑好（{len(seller_ratings)} 条评价，{seller_positive / len(seller_ratings):.0%} 好评）")
    if ip_match is False:
        conclusion.append("IP 属地与发货地不一致，下单前确认所在地")
    if last_active and reply_rate is not None and reply_rate >= 80:
        conclusion.append("卖家在线且回复及时")

    return {
        "摘要": summary,
        "身份": " · ".join(identity_parts) if identity_parts else "暂无",
        "活跃": " · ".join(activity_parts) if activity_parts else "暂无",
        "交易": " · ".join(trade_parts) if trade_parts else "暂无",
        "历史": " · ".join(history_parts) if history_parts else "暂无",
        "口碑": " · ".join(reputation_parts) if reputation_parts else "暂无",
        "结论": "；".join(conclusion),
        "商家判断": dealer_level,
        "商家迹象": evidence,
        "信号": signals,
        "新人卖家": newcomer,
        "经营资质": business or "无",
        "个人档案": {"性别": gender or "暂无", "年龄段": age_band or "暂无"},
        "品类分布": mix,
        "指标": {
            "在售": len(on_sale),
            "已售": len(sold),
            "同类在售": len(similar_on_sale),
            "同类已售": len(similar_sold),
            "卖出件数": sold_total,
            "卖家评价数": len(seller_ratings),
            "卖家好评数": seller_positive,
            "卖家差评数": seller_negative,
            "近30天卖家评价": len(recent_seller),
            "买家评价数": len(buyer_ratings),
            "评价者省份数": len(rater_places),
            "注册天数": reg_days,
            "粉丝数": followers,
        },
    }


def _user_role(rating: dict) -> str:
    """该用户在这笔交易里的角色。老记录没有派生字段时，从“评价来源角色”反推：写评价的是卖家 → 该用户是买家。"""
    role = str(rating.get("该用户在此交易中的角色") or "").strip()
    if role in ("卖家", "买家"):
        return role
    rater = str(rating.get("评价来源角色") or "")
    if "卖家" in rater:
        return "买家"
    if "买家" in rater:
        return "卖家"
    return "未知"


def _is_similar(entry: dict, category_id, tokens: tuple[str, ...], category_label: str) -> bool:
    """同类 = 品类 ID 相同，或标题命中搜索关键词 / 分类名。"""
    entry_category = entry.get("品类ID")
    if category_id not in (None, "") and entry_category not in (None, "") and str(entry_category) == str(category_id):
        return True
    title = str(entry.get("商品标题") or "").lower()
    if not title:
        return False
    if category_label and category_label.lower() in title:
        return True
    return any(token in title for token in tokens)


def _keyword_tokens(*keywords) -> tuple[str, ...]:
    tokens = []
    for keyword in keywords:
        for token in re.split(r"[\s,，、/|]+", str(keyword or "").lower()):
            if len(token) >= 2 and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _count(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _first_number(value) -> Optional[float]:
    match = _NUMBER.search(str(value or ""))
    return float(match.group()) if match else None


def _minutes(value) -> Optional[int]:
    """'3分钟' -> 3, '2小时' -> 120, '1天' -> 1440。"""
    text = str(value or "")
    number = _first_number(text)
    if number is None:
        return None
    if "天" in text:
        return int(number * 1440)
    if "小时" in text:
        return int(number * 60)
    if "秒" in text:
        return 0
    return int(number)


def _clean(value, placeholders: Iterable[str] = ()) -> str:
    text = str(value or "").strip()
    return "" if not text or text in set(placeholders) | {"暂无", "-", "N/A", "NaN", "None"} else text


def _place(value) -> str:
    """'中国湖北省' -> '湖北'，'山东省' -> '山东'。"""
    text = _clean(value)
    if text.startswith("中国"):
        text = text[2:]
    for suffix in _PLACE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def _places_match(ip_location: str, region: str, city: str) -> Optional[bool]:
    """IP 属地和发货地是否一致；发货地只有区/县（如“海淀区”）时和省级 IP 属地比不出来，返回 None。"""
    ip = _place(ip_location)
    raw_candidates = [p for p in ((region or "").strip(), (city or "").strip()) if p]
    candidates = [_place(p) for p in raw_candidates]
    if not ip or not candidates:
        return None
    if any(ip in c or c in ip for c in candidates if c):
        return True
    if all(p.endswith(("区", "县", "镇")) for p in raw_candidates):
        return None
    return False


def _join_place(region: str, city: str) -> str:
    if region and city and city not in region and region not in city:
        return f"{region} {city}"
    return city or region


def _rating_date(value):
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _span(dates: list) -> str:
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return first.isoformat()
    return f"{first.strftime('%Y-%m')} ~ {last.strftime('%Y-%m')}"
