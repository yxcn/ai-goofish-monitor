from datetime import datetime

from src.seller_profile import build_seller_profile, category_mix, describe_category_mix

NOW = datetime(2026, 11, 6, 15, 0)


def _rating(role: str, when: str, rate: str = "好评", ip: str = "中国湖北省"):
    return {"该用户在此交易中的角色": role, "评价类型": rate, "评价时间": when, "评价者IP属地": ip}


def _listing(item_id: str, title: str, status: str = "在售", category=50025387, **extra):
    return {"商品ID": item_id, "商品标题": title, "商品状态": status, "品类ID": category, **extra}


def _thin_personal_seller_record():
    # 和 ¥1494 那条 ThinkPad T14 的真实数据一致
    return {
        "搜索关键字": "ThinkPad T14",
        "商品信息": {
            "商品ID": "1097788657985",
            "商品标题": "ThinkPad T14笔记本电脑95成新无任何维修",
            "发货地区": "山东",
            "卖家所在城市": "淄博",
            "卖家最近活跃": "21分钟前来过",
            "卖家24小时回复率": "100%",
            "卖家平均回复时长": "3分钟",
            "品类ID": 50025387,
            "商品属性": {"分类": "笔记本电脑"},
        },
        "卖家信息": {
            "卖家昵称": "x***7",
            "卖家IP属地": "山东省",
            "卖家粉丝数": 0,
            "卖家认证": ["实名认证", "实人认证"],
            "卖家芝麻信用": "暂无",
            "卖家信用等级": "暂无",
            "卖家注册时长": "来闲鱼2年7个月",
            "卖家注册日期": "2024-04-18",
            "卖家注册天数": 932,
            "卖家卖出件数": 0,
            "卖家发布的商品列表": [_listing("1097788657985", "ThinkPad T14笔记本电脑95成新无任何维修", 发布标签="48小时内发布")],
            "卖家收到的评价列表": [_rating("买家", "2026-10-26 21:06:48")],
        },
    }


def test_thin_personal_seller_is_not_called_a_dealer():
    profile = build_seller_profile(_thin_personal_seller_record(), now=NOW)

    assert profile["摘要"] == "x***7（山东 淄博 · IP 属地山东省，与发货地一致）"
    assert profile["身份"] == "来闲鱼2年7个月（2024-04-18 注册） · 个人档案：暂无 · 实名认证、实人认证 · 芝麻信用未公开"
    assert profile["活跃"] == "21分钟前来过 · 24 小时回复率 100% · 平均 3分钟回复 · 粉丝 0"
    assert profile["交易"] == "在售 1 / 已售 0 · 卖出过 0 件 · 同类在售 1、已售 0（笔记本电脑） · 在售只有本商品 · 本商品48小时内发布"
    assert profile["历史"] == "作为卖家没有收到过评价 · 作为买家 1 条评价（2026-10-26）"
    assert profile["口碑"] == "作为卖家暂无评价 · 作为买家 1/1 好评 · 最近一条 2026-10-26（评价者来自湖北）"
    assert profile["结论"] == "可见记录无商家迹象，按个人卖家对待；没有任何出售记录，按新人卖家对待，建议验货宝或担保交易；卖家在线且回复及时"
    assert profile["商家判断"] == "个人"
    assert profile["商家迹象"] == []
    assert profile["新人卖家"] is True
    assert profile["个人档案"] == {"性别": "暂无", "年龄段": "暂无"}
    assert profile["经营资质"] == "无"
    assert profile["信号"] == ["⚠️ 新人卖家（无出售记录）", "⚠️ 卖家零评价", "✅ 认证齐全", "✅ IP 属地与发货地一致", "✅ 回复快"]
    assert profile["指标"]["同类在售"] == 1 and profile["指标"]["注册天数"] == 932


def test_repeat_laptop_seller_with_dense_recent_ratings_is_flagged_as_dealer():
    record = _thin_personal_seller_record()
    seller = record["卖家信息"]
    seller["卖家卖出件数"] = 35
    seller["卖家发布的商品列表"] = [
        _listing("1097788657985", "ThinkPad T14 i5 16G"),
        _listing("2", "ThinkPad T14s 锐龙版"),
        _listing("3", "ThinkPad X1 Carbon 2021"),
        _listing("4", "ThinkPad T480 已售", status="已售"),
        _listing("5", "联想笔记本 T490", status="已售"),
        _listing("6", "ThinkPad E14", status="已售"),
        _listing("7", "儿童滑板车", status="已售", category=1),
    ]
    seller["卖家收到的评价列表"] = [
        _rating("卖家", f"2026-10-{day:02d} 10:00:00", ip=ip)
        for day, ip in zip(range(10, 18), ["中国广东省", "中国浙江省", "中国江苏省", "中国北京市", "中国湖北省", "中国四川省", "中国广东省", "中国山东省"])
    ] + [_rating("卖家", "2025-03-02 09:00:00", rate="差评", ip="中国河南省")]

    profile = build_seller_profile(record, now=NOW)

    assert profile["商家判断"] == "商家"
    assert profile["商家迹象"] == [
        "同时在售 3 件同类商品（笔记本电脑，含本商品）",
        "此前已售 3 件同类商品（笔记本电脑）",
        "卖出过 35 件",
        "近 30 天收到 8 条买家评价",
    ]
    assert profile["结论"].startswith("商家特征明显：同时在售 3 件同类商品")
    assert "有 1 条差评，先看评价内容" in profile["结论"]
    assert profile["信号"][:3] == ["⚠️ 商家特征明显", "⚠️ 此前已售 3 件同类", "⚠️ 有 1 条差评"]
    assert profile["历史"] == (
        "作为卖家收到 9 条评价（2025-03 ~ 2026-10，评价者来自 8 个省） · 此前卖出过 3 件同类商品 · "
        "发布过 7 件：电脑数码 6、母婴童装 1，交易集中在电脑数码"
    )
    assert profile["口碑"].startswith("作为卖家 8/9 好评（89%） · 1 条差评")
    assert profile["品类分布"] == {"电脑数码": 6, "母婴童装": 1}


def test_two_similar_sales_only_raise_a_caution():
    record = _thin_personal_seller_record()
    record["卖家信息"]["卖家发布的商品列表"] += [
        _listing("8", "ThinkPad T490 自用", status="已售"),
        _listing("9", "MacBook Air M1", status="已售"),  # 品类 ID 相同即同类，不看标题
    ]
    profile = build_seller_profile(record, now=NOW)

    assert profile["商家判断"] == "留意"
    assert profile["结论"].startswith("有商家迹象，需留意：此前已售 2 件同类商品（笔记本电脑）")
    assert "⚠️ 此前已售 2 件同类" in profile["信号"]
    assert "⚠️ 疑似商家" not in profile["信号"]


def test_new_account_with_mismatched_ip_and_no_certification_is_flagged():
    record = _thin_personal_seller_record()
    record["卖家信息"].update({"卖家IP属地": "广东省", "卖家注册天数": 20, "卖家注册时长": "来闲鱼20天", "卖家认证": []})
    record["商品信息"].update({"卖家24小时回复率": "40%", "卖家平均回复时长": "5小时"})

    profile = build_seller_profile(record, now=NOW)

    assert profile["摘要"] == "x***7（山东 淄博 · IP 属地广东省，与发货地不一致）"
    assert "未见实名/实人认证" in profile["身份"]
    assert profile["信号"] == [
        "⚠️ 新号（注册 20 天）",
        "⚠️ 新人卖家（无出售记录）",
        "⚠️ 卖家零评价",
        "⚠️ IP 属地（广东省）与发货地（淄博）不一致",
        "⚠️ 未见实名/实人认证",
        "⚠️ 24 小时回复率仅 40%",
    ]
    assert "注册仅 20 天的新号" in profile["结论"]
    assert "IP 属地与发货地不一致，下单前确认所在地" in profile["结论"]


def test_profile_degrades_without_seller_data():
    profile = build_seller_profile({"商品信息": {"商品ID": "1", "商品标题": "T14"}, "卖家信息": {}}, now=NOW)

    assert profile["结论"] == "卖家资料未采集，无法判断是否商家"
    assert profile["商家判断"] == "未知"
    assert profile["信号"] == []
    assert profile["身份"] == "芝麻信用未公开"
    assert build_seller_profile({}, now=NOW)["商家判断"] == "未知"


def test_old_records_without_new_fields_still_get_a_profile():
    # 新字段（IP 属地、认证、注册天数、品类 ID）缺失时按旧字段算，不报错
    record = {
        "搜索关键字": "ThinkPad T14",
        "商品信息": {"商品ID": "1", "商品标题": "ThinkPad T14", "发货地区": "山东"},
        "卖家信息": {
            "卖家昵称": "老记录",
            "卖家注册时长": "来闲鱼1年",
            "卖家发布的商品列表": [{"商品ID": "1", "商品标题": "ThinkPad T14", "商品状态": "在售"}],
            "卖家收到的评价列表": [{"该用户在此交易中的角色": "卖家", "评价类型": "好评", "评价时间": "2026-01-01 00:00:00"}],
        },
    }
    profile = build_seller_profile(record, now=NOW)

    assert profile["摘要"] == "老记录（山东）"
    assert profile["商家判断"] == "个人"
    assert profile["口碑"] == "作为卖家 1/1 好评（100%） · 最近一条 2026-01-01"
    assert profile["信号"] == []
    assert profile["新人卖家"] is False


def test_platform_business_label_overrides_everything_else():
    # “本本匠数码”这类：2 件在售、0 评价，但闲鱼标注了个人经营者 → 不是新人，是商家
    record = _thin_personal_seller_record()
    record["卖家信息"].update({"卖家经营资质": "个人经营者", "卖家收到的评价列表": []})
    profile = build_seller_profile(record, now=NOW)

    assert profile["商家判断"] == "商家" and profile["新人卖家"] is False
    assert profile["结论"].startswith("商家（闲鱼标注为个人经营者）；无卖家口碑")
    assert profile["商家迹象"] == ["闲鱼标注为个人经营者"]
    assert profile["信号"][0] == "⚠️ 闲鱼标注个人经营者"
    assert "经营资质：个人经营者" in profile["身份"]
    assert profile["经营资质"] == "个人经营者"


def test_personal_archive_shows_up_in_summary_and_identity():
    record = _thin_personal_seller_record()
    record["卖家信息"]["卖家个人档案"] = {"性别": "女", "年龄段": "90后"}
    profile = build_seller_profile(record, now=NOW)
    assert profile["摘要"].startswith("x***7（女 · 90后 · 山东 淄博")
    assert "个人档案：女 · 90后" in profile["身份"]
    assert profile["个人档案"] == {"性别": "女", "年龄段": "90后"}

    record["卖家信息"]["卖家个人档案"] = {"年龄段": "00后"}
    assert "个人档案：性别暂无 · 00后" in build_seller_profile(record, now=NOW)["身份"]


def test_newcomer_needs_a_clean_history_not_just_a_young_account():
    record = _thin_personal_seller_record()
    record["卖家信息"]["卖家收到的评价列表"] = [_rating("买家", "2026-10-01"), _rating("买家", "2026-10-02")]
    assert build_seller_profile(record, now=NOW)["新人卖家"] is False  # 买过两次以上就不算干净
    record["卖家信息"]["卖家收到的评价列表"] = []
    record["卖家信息"]["卖家卖出件数"] = 3
    assert build_seller_profile(record, now=NOW)["新人卖家"] is False  # 卖出过东西


def test_category_mix_groups_titles_and_reports_focus():
    mix = category_mix([
        {"商品标题": "ThinkPad T14"}, {"商品标题": "iPhone 13"}, {"商品标题": "机械键盘"},
        {"商品标题": "儿童自行车"}, {"商品标题": "一本高数教材"}, {"商品标题": ""},
    ])
    assert mix == {"电脑数码": 3, "图书": 1, "母婴童装": 1}
    assert describe_category_mix(mix) == ("电脑数码 3、图书 1、母婴童装 1", None)
    assert describe_category_mix({"电脑数码": 7, "其他": 1}) == ("电脑数码 7、其他 1", "电脑数码")
    assert describe_category_mix({"其他": 5}) == ("其他 5", None)
    assert describe_category_mix({}) == ("", None)
