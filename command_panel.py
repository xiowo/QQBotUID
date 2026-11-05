from typing import Literal
from typing_extensions import TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, TypeAdapter, field_validator, model_validator

Scope = Literal["c2c", "group", "channel", "dm"]
SCOPES: tuple[Scope, ...] = ("c2c", "group", "channel", "dm")
JSON_VALUE = TypeAdapter(JsonValue)
HTTPS_URL = TypeAdapter(HttpUrl)


class PanelResult(TypedDict):
    status: int
    msg: str
    data: JsonValue


def success(data: JsonValue) -> PanelResult:
    return {"status": 0, "msg": "ok", "data": data}


def failure(message: str) -> PanelResult:
    return {"status": 1, "msg": message, "data": None}


def panel_text(value: str, limit: int, label: str, required: bool = False) -> str:
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label}不能为空")
    if sum(2 if ord(char) > 127 else 1 for char in value) > limit:
        raise ValueError(f"{label}最多 {limit} 个字符，汉字及非 ASCII 字符计 2 个")
    return value


def normalize_openids(values: list[str]) -> list[str]:
    ids = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if len(ids) > 20:
        raise ValueError("每次最多配置 20 个 OpenID")
    return ids


class PanelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def payload(self) -> JsonValue:
        return JSON_VALUE.validate_json(self.model_dump_json(exclude_none=True))


class PanelItem(PanelModel):
    type: Literal["command", "link"] = "command"
    name: str
    desc: str = ""
    only_admin: bool = False
    link: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return panel_text(value, 15, "元素名称", required=True)

    @field_validator("desc")
    @classmethod
    def validate_desc(cls, value: str) -> str:
        return panel_text(value, 30, "元素描述")

    @model_validator(mode="after")
    def validate_link(self) -> "PanelItem":
        if self.type == "link":
            if self.link is None or not self.link.strip():
                raise ValueError("链接元素必须填写 HTTPS 链接")
            self.link = self.link.strip()
            if len(self.link) > 2048 or HTTPS_URL.validate_python(self.link).scheme != "https":
                raise ValueError("链接必须使用 HTTPS，且不超过 2048 个字符")
        else:
            self.link = None
        return self


class Panel(PanelModel):
    items: list[PanelItem] = Field(min_length=1, max_length=20)
    remark: str = ""
    version: int | None = None

    @field_validator("remark")
    @classmethod
    def validate_remark(cls, value: str) -> str:
        return panel_text(value, 255, "面板备注")


class UpdatePanel(PanelModel):
    panel: Panel


class CreatePanel(UpdatePanel):
    scope: Scope
    target_type: Literal["all", "specific"] = "all"
    user_openids: list[str] | None = None
    group_openids: list[str] | None = None

    @field_validator("user_openids", "group_openids")
    @classmethod
    def validate_openids(cls, value: list[str] | None) -> list[str] | None:
        return normalize_openids(value) if value is not None else None

    @model_validator(mode="after")
    def validate_target(self) -> "CreatePanel":
        if self.target_type == "all":
            self.user_openids = None
            self.group_openids = None
        elif self.scope == "c2c":
            if not self.user_openids or self.group_openids:
                raise ValueError("单聊指定对象时，请只填写用户 OpenID，至少 1 个")
            self.group_openids = None
        elif self.scope == "group":
            if not self.group_openids or self.user_openids:
                raise ValueError("群聊指定对象时，请只填写群 OpenID，至少 1 个")
            self.user_openids = None
        else:
            raise ValueError("文字子频道和频道私信只支持全局生效")
        return self


class UpdateTargets(PanelModel):
    op: Literal["add", "del"]
    user_openids: list[str] | None = None
    group_openids: list[str] | None = None

    @field_validator("user_openids", "group_openids")
    @classmethod
    def validate_openids(cls, value: list[str] | None) -> list[str] | None:
        return normalize_openids(value) if value is not None else None

    @model_validator(mode="after")
    def validate_target(self) -> "UpdateTargets":
        if bool(self.user_openids) == bool(self.group_openids):
            raise ValueError("用户 OpenID 和群 OpenID 必须且只能填写一种，至少 1 个")
        if not self.user_openids:
            self.user_openids = None
        if not self.group_openids:
            self.group_openids = None
        return self


def decode_response(response: httpx.Response) -> PanelResult:
    if response.is_success and not response.content:
        return success(None)
    # QQ 的错误响应可能不是 JSON，不能把 HTML 错误页当成保存成功。
    from pydantic import ValidationError

    try:
        data = JSON_VALUE.validate_json(response.content)
    except ValidationError:
        return failure(f"QQ 接口返回了非 JSON 响应（HTTP {response.status_code}）")
    code: JsonValue = data["code"] if isinstance(data, dict) and "code" in data else None
    if not response.is_success or code not in (None, 0, "0"):
        message = "QQ 接口请求失败"
        if isinstance(data, dict):
            for key in ("message", "msg"):
                if key in data and isinstance(data[key], str) and data[key]:
                    message = data[key]
                    break
        suffix = f"，code={code}" if code is not None else ""
        return failure(f"{message}（HTTP {response.status_code}{suffix}）")
    return success(data)
