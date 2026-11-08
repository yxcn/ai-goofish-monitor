"""
AI 请求消息构造辅助函数
"""
from typing import Dict, List, Union


TEXT_ONLY_ANALYSIS_NOTE = (
    "补充说明：本次未提供商品图片，请仅根据商品文字字段和卖家信息判断，不要推断图片内容。"
)
VALUE_ANALYSIS_NOTE = (
    "\n如果商品 JSON 中包含“价格参考”或 price_insight，请结合价格位置、历史走势、"
    "配置、成色、附件、卖家信息综合判断性价比。"
    "你可以额外输出可选字段 value_score(0-100) 和 value_summary，"
    "但必须保留原有 is_recommended/reason 等字段。\n"
)


def build_analysis_text_prompt(
    product_json: str,
    prompt_text: str,
    *,
    include_images: bool,
) -> str:
    note = "" if include_images else f"\n{TEXT_ONLY_ANALYSIS_NOTE}\n"
    value_note = VALUE_ANALYSIS_NOTE
    return f"""请基于你的专业知识和我的要求，分析以下完整的商品JSON数据：

```json
{product_json}
```

    {prompt_text}
    {value_note}
    {note}"""


def build_product_only_text_prompt(
    product_json: str,
    *,
    include_images: bool,
) -> str:
    """Claude 用：分析标准放在 system 里（每个商品都一样，可缓存），user 里只放商品 JSON 和补充说明。"""
    note = "" if include_images else f"\n{TEXT_ONLY_ANALYSIS_NOTE}\n"
    return f"""请基于你的专业知识和系统提示中的分析标准，分析以下完整的商品JSON数据：

```json
{product_json}
```
{VALUE_ANALYSIS_NOTE}{note}"""


def build_user_message_content(
    text_prompt: str,
    image_data_urls: List[str],
) -> Union[str, List[Dict[str, object]]]:
    if not image_data_urls:
        return text_prompt

    user_content: List[Dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": url}}
        for url in image_data_urls
    ]
    user_content.append({"type": "text", "text": text_prompt})
    return user_content
