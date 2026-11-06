"""
任务领域模型
定义任务实体及其业务逻辑
"""
import os
import re
from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.cron_utils import validate_cron_expression
from src.services.account_strategy_service import (
    clean_account_state_file,
    normalize_account_strategy,
)
from src.attribute_rules import normalize_attribute_rules
from src.title_filter import normalize_title_keywords


class TaskStatus(str, Enum):
    """任务状态枚举"""

    STOPPED = "stopped"
    RUNNING = "running"
    SCHEDULED = "scheduled"


def _normalize_keyword_values(value) -> List[str]:
    if value is None:
        return []

    raw_values = []
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    elif isinstance(value, str):
        raw_values = re.split(r"[\n,]+", value)
    else:
        raw_values = [value]

    normalized: List[str] = []
    seen = set()
    for item in raw_values:
        text = str(item).strip()
        if not text:
            continue
        dedup_key = text.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        normalized.append(text)
    return normalized


def _extract_keywords_from_legacy_groups(groups) -> List[str]:
    if not groups:
        return []

    merged: List[str] = []
    for group in groups:
        include_keywords = []
        if isinstance(group, dict):
            include_keywords = group.get("include_keywords") or []
        else:
            include_keywords = getattr(group, "include_keywords", []) or []
        merged.extend(_normalize_keyword_values(include_keywords))
    return _normalize_keyword_values(merged)


def _normalize_payload_keywords(payload: Any, *, partial: bool = False) -> Any:
    """归一化账号字段和旧版关键词分组。

    partial=True 用于 TaskUpdate：只在请求里带了账号字段时才归一化，
    否则 pydantic 会把这两个字段算作"已设置"，任何局部更新（包括每次启动/结束写 is_running）
    都会把任务的账号绑定抹成 auto/None。
    """
    if payload is None or not isinstance(payload, dict):
        return payload
    values = dict(payload)
    has_state_file = "account_state_file" in values
    has_strategy = "account_strategy" in values
    if not partial or has_state_file:
        values["account_state_file"] = clean_account_state_file(values.get("account_state_file"))
    if not partial or has_strategy:
        values["account_strategy"] = normalize_account_strategy(
            values.get("account_strategy"),
            values.get("account_state_file"),
        )
    if "keyword_rules" in values:
        values["keyword_rules"] = _normalize_keyword_values(values.get("keyword_rules"))
    elif "keyword_rule_groups" in values:
        values["keyword_rules"] = _extract_keywords_from_legacy_groups(
            values.get("keyword_rule_groups")
        )
    return values


def _has_keyword_rules(keyword_rules: List[str]) -> bool:
    return bool(keyword_rules and len(keyword_rules) > 0)


def _normalize_optional_string(value):
    if value == "" or value == "null" or value == "undefined" or value is None:
        return None
    return value


def _validate_cron_expression(value: Optional[str]) -> Optional[str]:
    return validate_cron_expression(value)


def _normalize_price_value(value):
    if _normalize_optional_string(value) is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return value


PROMPT_FILE_DIR = "prompts"


def _validate_project_relative_file(value, *, field_label: str, allowed_dir: Optional[str] = None,
                                    required_suffix: Optional[str] = None):
    """任务里的文件路径只接受项目内的相对路径。

    这些字段会被 spider_v2 直接 open() 并把内容拼进发给 AI 的 prompt，
    或者被 Playwright 当作登录态加载；放开绝对路径和 `..` 等于任意文件读取。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    normalized = text.replace("\\", "/")
    if os.path.isabs(normalized) or normalized.startswith("~") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{field_label} 只能是项目内的相对路径，不接受绝对路径：{value}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"{field_label} 不能包含 '..'：{value}")
    if allowed_dir and parts[0] != allowed_dir:
        raise ValueError(f"{field_label} 必须位于 {allowed_dir}/ 目录下：{value}")
    if required_suffix and not parts[-1].lower().endswith(required_suffix):
        raise ValueError(f"{field_label} 必须是 {required_suffix} 文件：{value}")
    return "/".join(parts)


def _validate_prompt_file(value):
    return _validate_project_relative_file(value, field_label="Prompt 文件", allowed_dir=PROMPT_FILE_DIR)


def _validate_account_state_file(value):
    return _validate_project_relative_file(value, field_label="登录态文件", required_suffix=".json")


class Task(BaseModel):
    """任务实体"""

    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    id: Optional[int] = None
    task_name: str
    enabled: bool
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    max_pages: int
    personal_only: bool
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: str
    ai_prompt_criteria_file: str
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Literal["ai", "keyword"] = "ai"
    keyword_rules: List[str] = Field(default_factory=list)
    # 附加搜索词：和 keyword 一起依次搜索，结果共用同一个结果集和判断标准
    extra_keywords: List[str] = Field(default_factory=list)
    # 标题预筛选：标题不含任一关键词、或含任一排除词的商品，在抓详情和 AI 分析前直接跳过
    title_include_keywords: List[str] = Field(default_factory=list)
    title_exclude_keywords: List[str] = Field(default_factory=list)
    # 属性硬规则：用详情接口的结构化属性在调 AI 之前挡掉明显不合格的商品
    attribute_rules: List[str] = Field(default_factory=list)
    is_running: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("ai_prompt_base_file", "ai_prompt_criteria_file", mode="before")
    @classmethod
    def validate_prompt_file_paths(cls, value):
        return _validate_prompt_file(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def validate_account_state_file_path(cls, value):
        return _validate_account_state_file(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @field_validator("extra_keywords", "title_include_keywords", "title_exclude_keywords", mode="before")
    @classmethod
    def normalize_title_keyword_lists(cls, value):
        return normalize_title_keywords(value)

    @field_validator("attribute_rules", mode="before")
    @classmethod
    def normalize_attribute_rule_lines(cls, value):
        return normalize_attribute_rules(value)

    def can_start(self) -> bool:
        """检查任务是否可以启动"""
        return self.enabled and not self.is_running

    def can_stop(self) -> bool:
        """检查任务是否可以停止"""
        return self.is_running

    def apply_update(self, update: "TaskUpdate") -> "Task":
        """应用更新并返回新的任务实例"""
        update_data = update.model_dump(exclude_unset=True)
        return self.model_copy(update=update_data)


class TaskCreate(BaseModel):
    """创建任务的DTO"""

    model_config = ConfigDict(extra="ignore")

    task_name: str
    enabled: bool = True
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    max_pages: int = 3
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: str = "prompts/base_prompt.txt"
    ai_prompt_criteria_file: str = ""
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Literal["ai", "keyword"] = "ai"
    keyword_rules: List[str] = Field(default_factory=list)
    extra_keywords: List[str] = Field(default_factory=list)
    title_include_keywords: List[str] = Field(default_factory=list)
    title_exclude_keywords: List[str] = Field(default_factory=list)
    attribute_rules: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("ai_prompt_base_file", "ai_prompt_criteria_file", mode="before")
    @classmethod
    def validate_prompt_file_paths(cls, value):
        return _validate_prompt_file(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def validate_account_state_file_path(cls, value):
        return _validate_account_state_file(value)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_state_file(cls, value):
        return clean_account_state_file(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @field_validator("extra_keywords", "title_include_keywords", "title_exclude_keywords", mode="before")
    @classmethod
    def normalize_title_keyword_lists(cls, value):
        return normalize_title_keywords(value)

    @field_validator("attribute_rules", mode="before")
    @classmethod
    def normalize_attribute_rule_lines(cls, value):
        return normalize_attribute_rules(value)

    @model_validator(mode="after")
    def validate_decision_mode_payload(self):
        description = str(self.description or "").strip()
        if self.decision_mode == "ai" and not description:
            raise ValueError("AI 判断模式下，详细需求(description)不能为空。")
        if self.decision_mode == "keyword" and not _has_keyword_rules(self.keyword_rules):
            raise ValueError("关键词判断模式下，至少需要一个关键词。")
        if self.account_strategy == "fixed" and not self.account_state_file:
            raise ValueError("固定账号模式下必须选择账号。")
        return self


class TaskUpdate(BaseModel):
    """更新任务的DTO"""

    model_config = ConfigDict(extra="ignore")

    task_name: Optional[str] = None
    enabled: Optional[bool] = None
    keyword: Optional[str] = None
    description: Optional[str] = None
    analyze_images: Optional[bool] = None
    max_pages: Optional[int] = None
    personal_only: Optional[bool] = None
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: Optional[str] = None
    ai_prompt_criteria_file: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Optional[Literal["auto", "fixed", "rotate"]] = None
    free_shipping: Optional[bool] = None
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Optional[Literal["ai", "keyword"]] = None
    keyword_rules: Optional[List[str]] = None
    extra_keywords: Optional[List[str]] = None
    title_include_keywords: Optional[List[str]] = None
    title_exclude_keywords: Optional[List[str]] = None
    attribute_rules: Optional[List[str]] = None
    is_running: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values, partial=True)

    @field_validator("ai_prompt_base_file", "ai_prompt_criteria_file", mode="before")
    @classmethod
    def validate_prompt_file_paths(cls, value):
        return _validate_prompt_file(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def validate_account_state_file_path(cls, value):
        return _validate_account_state_file(value)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_state_file(cls, value):
        return clean_account_state_file(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @field_validator("extra_keywords", "title_include_keywords", "title_exclude_keywords", mode="before")
    @classmethod
    def normalize_title_keyword_lists(cls, value):
        # None 表示本次不改这个字段；空字符串/空列表表示清空
        if value is None:
            return None
        return normalize_title_keywords(value)

    @field_validator("attribute_rules", mode="before")
    @classmethod
    def normalize_attribute_rule_lines(cls, value):
        if value is None:
            return None
        return normalize_attribute_rules(value)

    @model_validator(mode="after")
    def validate_partial_keyword_payload(self):
        if self.decision_mode == "keyword" and self.keyword_rules is not None:
            if not _has_keyword_rules(self.keyword_rules):
                raise ValueError("关键词判断模式下，至少需要一个关键词。")
        if self.decision_mode == "ai" and self.description is not None:
            if not str(self.description).strip():
                raise ValueError("AI 判断模式下，详细需求(description)不能为空。")
        return self


class TaskGenerateRequest(BaseModel):
    """任务创建请求DTO（AI模式支持自动生成标准）"""

    model_config = ConfigDict(extra="ignore")

    task_name: str
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    max_pages: int = 3
    cron: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Literal["ai", "keyword"] = "ai"
    keyword_rules: List[str] = Field(default_factory=list)
    extra_keywords: List[str] = Field(default_factory=list)
    title_include_keywords: List[str] = Field(default_factory=list)
    title_exclude_keywords: List[str] = Field(default_factory=list)
    attribute_rules: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def validate_account_state_file_path(cls, value):
        return _validate_account_state_file(value)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("extra_keywords", "title_include_keywords", "title_exclude_keywords", mode="before")
    @classmethod
    def normalize_title_keyword_lists(cls, value):
        return normalize_title_keywords(value)

    @field_validator("attribute_rules", mode="before")
    @classmethod
    def normalize_attribute_rule_lines(cls, value):
        return normalize_attribute_rules(value)

    @field_validator("cron", mode="before")
    @classmethod
    def empty_str_to_none(cls, value):
        return _normalize_optional_string(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def empty_account_to_none(cls, value):
        return _normalize_optional_string(value)

    @field_validator("new_publish_option", "region", mode="before")
    @classmethod
    def empty_str_to_none_for_strings(cls, value):
        return _normalize_optional_string(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @model_validator(mode="after")
    def validate_decision_mode_payload(self):
        description = str(self.description or "").strip()
        if self.decision_mode == "ai" and not description:
            raise ValueError("AI 判断模式下，详细需求(description)不能为空。")
        if self.decision_mode == "keyword" and not _has_keyword_rules(self.keyword_rules):
            raise ValueError("关键词判断模式下，至少需要一个关键词。")
        if self.account_strategy == "fixed" and not self.account_state_file:
            raise ValueError("固定账号模式下必须选择账号。")
        return self
