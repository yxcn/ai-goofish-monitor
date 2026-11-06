import json
import re
from datetime import datetime

from src.config import AI_DEBUG_MODE
from src.utils import safe_get


async def _parse_search_results_json(json_data: dict, source: str) -> list:
    """解析搜索API的JSON数据，返回基础商品信息列表。"""
    page_data = []
    try:
        items = await safe_get(json_data, "data", "resultList", default=[])
        if not items:
            print(f"LOG: ({source}) API响应中未找到商品列表 (resultList)。")
            if AI_DEBUG_MODE:
                print(f"--- [SEARCH DEBUG] RAW JSON RESPONSE from {source} ---")
                print(json.dumps(json_data, ensure_ascii=False, indent=2))
                print("----------------------------------------------------")
            return []

        for item in items:
            main_data = await safe_get(item, "data", "item", "main", "exContent", default={})
            click_params = await safe_get(item, "data", "item", "main", "clickParam", "args", default={})

            title = await safe_get(main_data, "title", default="未知标题")
            price_parts = await safe_get(main_data, "price", default=[])
            price = "".join([str(p.get("text", "")) for p in price_parts if isinstance(p, dict)]).replace("当前价", "").strip() if isinstance(price_parts, list) else "价格异常"
            # 确保price是字符串类型，避免float类型导致的"in"操作错误
            price = str(price) if not isinstance(price, str) else price
            if "万" in price: price = f"¥{float(price.replace('¥', '').replace('万', '')) * 10000:.0f}"
            area = await safe_get(main_data, "area", default="地区未知")
            seller = await safe_get(main_data, "userNickName", default="匿名卖家")
            raw_link = await safe_get(item, "data", "item", "main", "targetUrl", default="")
            image_url = await safe_get(main_data, "picUrl", default="")
            pub_time_ts = click_params.get("publishTime", "")
            item_id = await safe_get(main_data, "itemId", default="未知ID")
            original_price = await safe_get(main_data, "oriPrice", default="暂无")
            wants_count = await safe_get(click_params, "wantNum", default='NaN')


            tags = []
            if await safe_get(click_params, "tag") == "freeship":
                tags.append("包邮")
            r1_tags = await safe_get(main_data, "fishTags", "r1", "tagList", default=[])
            for tag_item in r1_tags:
                content = await safe_get(tag_item, "data", "content", default="")
                if "验货宝" in content:
                    tags.append("验货宝")

            page_data.append({
                "商品标题": title,
                "当前售价": price,
                "商品原价": original_price,
                "“想要”人数": wants_count,
                "商品标签": tags,
                "发货地区": area,
                "卖家昵称": seller,
                "商品链接": raw_link.replace("fleamarket://", "https://www.goofish.com/"),
                "发布时间": datetime.fromtimestamp(int(pub_time_ts)/1000).strftime("%Y-%m-%d %H:%M") if pub_time_ts.isdigit() else "未知时间",
                "商品ID": item_id
            })
        print(f"LOG: ({source}) 成功解析到 {len(page_data)} 条商品基础信息。")
        return page_data
    except Exception as e:
        print(f"LOG: ({source}) JSON数据处理异常: {str(e)}")
        return []


# 商品描述最多保留的字符数，防止个别超长描述撑大每次 AI 请求
MAX_ITEM_DESCRIPTION_CHARS = 3000
ITEM_DESCRIPTION_TRUNCATED_SUFFIX = "…（描述过长，已截断）"


def parse_item_detail_extras(item_do: dict) -> dict:
    """从详情接口的 itemDO 中提取商品描述正文和结构化属性（成色、品牌等）。

    电池健康度、维修史、保修这类关键信息通常只写在描述正文里，搜索结果的标题里没有。
    缺失的字段不会出现在返回值里。
    """
    extras = {}
    if not isinstance(item_do, dict):
        return extras

    description = item_do.get("desc")
    if isinstance(description, str):
        description = description.strip()
        if len(description) > MAX_ITEM_DESCRIPTION_CHARS:
            description = (
                description[:MAX_ITEM_DESCRIPTION_CHARS] + ITEM_DESCRIPTION_TRUNCATED_SUFFIX
            )
        if description:
            extras["商品描述"] = description

    properties = {}
    labels = item_do.get("itemLabelExtList")
    for label in labels if isinstance(labels, list) else []:
        if not isinstance(label, dict):
            continue
        name = str(label.get("propertyText") or "").strip()
        value = str(label.get("text") or "").strip()
        if name and value and name not in properties:
            properties[name] = value
    if properties:
        extras["商品属性"] = properties

    return extras


def _as_count(value):
    """collectCnt 这类计数可能是数字或数字字符串；bool 和占位符返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_item_detail_facts(item_do: dict, seller_do: dict) -> dict:
    """从详情接口的 itemDO / sellerDO 中提取商品页上展示的热度和卖家活跃度信息。

    收藏数、卖家所在城市（比搜索结果里的省份细）、卖家最近活跃时间（"21分钟前来过"）、
    24 小时回复率和平均回复时长。缺失的字段不会出现在返回值里。
    """
    item_do = item_do if isinstance(item_do, dict) else {}
    seller_do = seller_do if isinstance(seller_do, dict) else {}
    facts = {}

    favorites = _as_count(item_do.get("collectCnt"))
    if favorites is not None:
        facts["收藏数"] = favorites

    category_id = item_do.get("categoryId")
    if category_id not in (None, ""):
        facts["品类ID"] = category_id

    text_fields = (
        ("卖家所在城市", seller_do.get("city") or seller_do.get("publishCity")),
        ("卖家最近活跃", seller_do.get("lastVisitTime")),
        ("卖家24小时回复率", seller_do.get("replyRatio24h")),
        ("卖家平均回复时长", seller_do.get("replyInterval")),
    )
    for key, raw in text_fields:
        value = str(raw or "").strip()
        if value:
            facts[key] = value
    return facts


def parse_seller_detail(seller_do: dict) -> dict:
    """详情接口 sellerDO 里的账号级信息：卖出件数、在售数、注册天数/日期、实人认证、交易摘要。

    这些进 卖家信息，和主页接口采到的资料合在一起；缺失的字段不出现。
    """
    seller_do = seller_do if isinstance(seller_do, dict) else {}
    facts = {}
    for key, raw in (
        ("卖家卖出件数", seller_do.get("hasSoldNumInteger")),
        ("卖家在售数量", seller_do.get("itemCount")),
        ("卖家注册天数", seller_do.get("userRegDay")),
    ):
        value = _as_count(raw)
        if value is not None:
            facts[key] = value
    register_ts = _as_count(seller_do.get("registerTime"))
    if register_ts:
        facts["卖家注册日期"] = datetime.fromtimestamp(register_ts / 1000).strftime("%Y-%m-%d")
    identity_tags = seller_do.get("identityTags")
    texts = [str(tag.get("text") or "") for tag in identity_tags if isinstance(tag, dict)] if isinstance(identity_tags, list) else []
    real_person = [text for text in texts if "实人认证" in text]
    if real_person:
        facts["卖家实人认证"] = all("未" not in text for text in real_person)
    summary = str(seller_do.get("xianyuSummary") or "").strip()
    if summary:
        facts["卖家交易摘要"] = summary
    return facts


# 评价列表里的 rateTagList 标的是“写评价的人”的角色：
# 标签为“卖家”的评价是别人卖东西给该用户后留下的（内容多为“很好的买家”），
# 说明该用户在那笔交易里是买家；标签为“买家”则说明该用户是卖家。
RATER_ROLE_TO_USER_ROLE = {"卖家": "买家", "买家": "卖家"}


def _user_role_from_rater_tag(role_tag: str) -> str:
    for rater_role, user_role in RATER_ROLE_TO_USER_ROLE.items():
        if rater_role in (role_tag or ""):
            return user_role
    return "未知"


async def calculate_reputation_from_ratings(ratings_json: list) -> dict:
    """从原始评价API数据列表中，计算该用户作为卖家和作为买家的好评数与好评率。"""
    seller_total = 0
    seller_positive = 0
    buyer_total = 0
    buyer_positive = 0

    for card in ratings_json:
        # 使用 safe_get 保证安全访问
        data = await safe_get(card, 'cardData', default={})
        role_tag = await safe_get(data, 'rateTagList', 0, 'text', default='')
        rate_type = await safe_get(data, 'rate') # 1=好评, 0=中评, -1=差评

        user_role = _user_role_from_rater_tag(role_tag)
        if user_role == "卖家":
            seller_total += 1
            if rate_type == 1:
                seller_positive += 1
        elif user_role == "买家":
            buyer_total += 1
            if rate_type == 1:
                buyer_positive += 1

    # 计算比率，并处理除以零的情况
    seller_rate = f"{(seller_positive / seller_total * 100):.2f}%" if seller_total > 0 else "N/A"
    buyer_rate = f"{(buyer_positive / buyer_total * 100):.2f}%" if buyer_total > 0 else "N/A"

    return {
        "作为卖家的好评数": f"{seller_positive}/{seller_total}",
        "作为卖家的好评率": seller_rate,
        "作为买家的好评数": f"{buyer_positive}/{buyer_total}",
        "作为买家的好评率": buyer_rate
    }


async def _parse_user_items_data(items_json: list) -> list:
    """解析用户主页的商品列表API的JSON数据。"""
    parsed_list = []
    for card in items_json:
        data = card.get('cardData', {})
        status_code = data.get('itemStatus')
        if status_code == 0:
            status_text = "在售"
        elif status_code == 1:
            status_text = "已售"
        else:
            status_text = f"未知状态 ({status_code})"

        entry = {
            "商品ID": data.get('id'),
            "商品标题": data.get('title'),
            "商品价格": data.get('priceInfo', {}).get('price'),
            "商品主图": data.get('picInfo', {}).get('picUrl'),
            "商品状态": status_text
        }
        entry.update(_parse_item_card_labels(data))
        parsed_list.append(entry)
    return parsed_list


_WANT_COUNT_PATTERN = re.compile(r"(\d+)\s*人想要")


def _parse_item_card_labels(card_data: dict) -> dict:
    """主页商品卡片上的标签：划线原价、想要人数、发布时间标签（"48小时内发布"）、包邮/验货宝，以及品类 ID。

    标签在 itemLabelDataVO.labelData 的 r1/r2/r3 几行里，缺失的字段不出现。
    """
    extras = {}
    tags = []
    original_price = None
    wants = None
    publish_label = None
    label_data = (card_data.get("itemLabelDataVO") or {}).get("labelData") or {}
    rows = label_data.values() if isinstance(label_data, dict) else []
    for row in rows:
        tag_list = row.get("tagList") if isinstance(row, dict) else None
        for tag in tag_list if isinstance(tag_list, list) else []:
            tag_data = tag.get("data") if isinstance(tag, dict) else None
            if not isinstance(tag_data, dict):
                continue
            content = str(tag_data.get("content") or tag_data.get("text") or "").strip()
            if not content:
                continue
            if str(tag_data.get("isStrikeThrough")).lower() == "true":
                original_price = original_price or content
                continue
            want_match = _WANT_COUNT_PATTERN.search(content)
            if want_match:
                wants = int(want_match.group(1))
                continue
            if "发布" in content:
                publish_label = publish_label or content
                continue
            if content not in tags:
                tags.append(content)
    post_info = str((card_data.get("detailParams") or {}).get("postInfo") or "").strip()
    if post_info and post_info not in tags:
        tags.append(post_info)
    if tags:
        extras["商品标签"] = tags
    if original_price:
        extras["商品原价"] = original_price
    if wants is not None:
        extras["想要人数"] = wants
    if publish_label:
        extras["发布标签"] = publish_label
    category_id = card_data.get("categoryId")
    if category_id not in (None, ""):
        extras["品类ID"] = category_id
    return extras


# 头部接口 baseInfo.tags 里的账号标识，键名带数字后缀（real_name_certification_77），按前缀识别
ACCOUNT_TAG_LABELS = (
    ("real_name_certification", "实名认证"),
    ("real_person_certification", "实人认证"),
    ("idle_zhima_zheng", "芝麻信用认证"),
)


# “个人档案”（性别、年龄段）目前只在 App 里展示，网页版头部接口抓了 15 个卖家都没有这些字段。
# 这里按可能的键名做一次尽力提取，接口哪天带上了就能直接用；找不到就返回空，画像里写“暂无”。
_PROFILE_GENDER_KEYS = {"gender", "sex", "usergender", "gendertext", "性别"}
_PROFILE_AGE_KEYS = {"age", "agerange", "agegroup", "agetext", "birthyear", "birthday", "年龄", "年龄段"}
_AGE_BAND_PATTERN = re.compile(r"^(\d{2})\s*后$")
_YEAR_PATTERN = re.compile(r"^(19|20)(\d{2})")


def _normalize_gender(value) -> str:
    text = str(value if value is not None else "").strip()
    lowered = text.lower()
    if lowered in {"1", "m", "male", "男", "boy", "man"}:
        return "男"
    if lowered in {"2", "f", "female", "女", "girl", "woman"}:
        return "女"
    if lowered in {"", "0", "unknown", "none", "null", "保密", "未知"}:
        return ""
    return text[:10]


def _normalize_age_band(value) -> str:
    text = str(value if value is not None else "").strip()
    band = _AGE_BAND_PATTERN.match(text)
    if band:
        return f"{band.group(1)}后"
    year = _YEAR_PATTERN.match(text)
    if year:
        return f"{year.group(2)[0]}0后"
    return text[:10] if text and text.lower() not in {"0", "unknown", "none", "null", "保密"} else ""


def extract_personal_profile(payload, depth: int = 0) -> dict:
    """在头部接口返回里找性别 / 年龄段字段（递归，最多 6 层，跳过商品列表那类大数组）。"""
    found = {}
    if depth > 6:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in _PROFILE_GENDER_KEYS and "性别" not in found:
                gender = _normalize_gender(value)
                if gender:
                    found["性别"] = gender
            elif lowered in _PROFILE_AGE_KEYS and "年龄段" not in found:
                age = _normalize_age_band(value)
                if age:
                    found["年龄段"] = age
            elif isinstance(value, (dict, list)):
                for k, v in extract_personal_profile(value, depth + 1).items():
                    found.setdefault(k, v)
    elif isinstance(payload, list):
        for entry in payload[:5]:
            for k, v in extract_personal_profile(entry, depth + 1).items():
                found.setdefault(k, v)
    return found


# 主页头部接口的 module.shop.businessQuality.type：闲鱼给经营账号标的资质。
# 抓到的两种：licensedOperation（持证经营，跳营业执照页）、selfEmployed（个人经营者，跳经营者信息公示页）
BUSINESS_QUALITY_LABELS = {
    "licensedoperation": "持证经营",
    "selfemployed": "个人经营者",
}


def business_quality_label(shop_module) -> str:
    """把 businessQuality.type 转成中文标签；不是经营账号返回空串。"""
    if not isinstance(shop_module, dict):
        return ""
    quality = shop_module.get("businessQuality")
    if not isinstance(quality, dict):
        return ""
    raw = str(quality.get("type") or "").strip()
    if not raw:
        return ""
    return BUSINESS_QUALITY_LABELS.get(raw.lower(), raw)


def account_certifications(tags) -> list:
    """把头部接口的账号标识转成认证名称列表，只保留值为真的。"""
    if not isinstance(tags, dict):
        return []
    labels = []
    for prefix, label in ACCOUNT_TAG_LABELS:
        if any(str(key).startswith(prefix) and value for key, value in tags.items()):
            labels.append(label)
    return labels


async def parse_user_head_data(head_json: dict) -> dict:
    """解析用户头部API的JSON数据。"""
    data = head_json.get('data', {})
    ylz_tags = await safe_get(data, 'module', 'base', 'ylzTags', default=[])
    seller_credit, buyer_credit = {}, {}
    for tag in ylz_tags:
        if await safe_get(tag, 'attributes', 'role') == 'seller':
            seller_credit = {'level': await safe_get(tag, 'attributes', 'level'), 'text': tag.get('text')}
        elif await safe_get(tag, 'attributes', 'role') == 'buyer':
            buyer_credit = {'level': await safe_get(tag, 'attributes', 'level'), 'text': tag.get('text')}
    social = await safe_get(data, 'module', 'social', default={})
    social = social if isinstance(social, dict) else {}
    return {
        "卖家昵称": await safe_get(data, 'module', 'base', 'displayName'),
        "卖家头像链接": await safe_get(data, 'module', 'base', 'avatar', 'avatar'),
        "卖家个性签名": await safe_get(data, 'module', 'base', 'introduction', default=''),
        "卖家在售/已售商品数": await safe_get(data, 'module', 'tabs', 'item', 'number'),
        "卖家收到的评价总数": await safe_get(data, 'module', 'tabs', 'rate', 'number'),
        "卖家信用等级": seller_credit.get('text', '暂无'),
        "买家信用等级": buyer_credit.get('text', '暂无'),
        # 同一个接口里之前没用的几项：IP 属地（可和发货地交叉核对）、粉丝/关注、实名/实人认证
        "卖家IP属地": str(await safe_get(data, 'module', 'base', 'ipLocation', default='') or '').strip(),
        "卖家粉丝数": _as_count(social.get('followers')),
        "卖家关注数": _as_count(social.get('following')),
        "卖家认证": account_certifications(await safe_get(data, 'baseInfo', 'tags', default={})),
        # 个人档案（性别 / 年龄段）：网页版接口目前不返回，找不到就是空 dict，画像里写“暂无”
        "卖家个人档案": extract_personal_profile(data),
        # 闲鱼标注的经营资质（持证经营 / 个人经营者）：平台自己认定的经营账号，不是经营账号为空串
        "卖家经营资质": business_quality_label(await safe_get(data, 'module', 'shop', default={})),
    }


async def parse_ratings_data(ratings_json: list) -> list:
    """解析评价列表API的JSON数据。"""
    parsed_list = []
    for card in ratings_json:
        data = await safe_get(card, 'cardData', default={})
        rate_tag = await safe_get(data, 'rateTagList', 0, 'text', default='未知角色')
        rate_type = await safe_get(data, 'rate')
        if rate_type == 1: rate_text = "好评"
        elif rate_type == 0: rate_text = "中评"
        elif rate_type == -1: rate_text = "差评"
        else: rate_text = "未知"
        parsed_list.append({
            "评价ID": data.get('rateId'),
            "评价内容": data.get('feedback'),
            "评价类型": rate_text,
            "评价来源角色": rate_tag,
            "该用户在此交易中的角色": _user_role_from_rater_tag(rate_tag),
            "评价者昵称": data.get('raterUserNick'),
            "评价时间": data.get('gmtCreate'),
            "评价图片": await safe_get(data, 'pictCdnUrlList', default=[]),
            # 评价者的 IP 属地（"中国湖北省"）：作为卖家的评价里异地买家多，说明有邮寄交易经验
            "评价者IP属地": str(data.get('ipAddress') or '').strip(),
        })
    return parsed_list
