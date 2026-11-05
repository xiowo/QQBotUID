from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator

from .command_panel import HTTPS_URL, PanelModel


def menu_text(value: str, limit: int, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    if len(value) > limit:
        raise ValueError(f"{label}最多 {limit} 个字符")
    return value


class MenuAction(PanelModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return menu_text(value, 14, "菜单名称")


class MessageMenuItem(MenuAction):
    type: Literal["send_message"]
    send_message: str

    @field_validator("send_message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return menu_text(value, 2000, "发送内容")


class LinkMenuItem(MenuAction):
    type: Literal["link"]
    link: str

    @field_validator("link")
    @classmethod
    def validate_link(cls, value: str) -> str:
        value = menu_text(value, 2048, "菜单链接")
        if HTTPS_URL.validate_python(value).scheme != "https":
            raise ValueError("菜单链接必须使用 HTTPS")
        return value


class MenuSwitch(PanelModel):
    switch_id: str
    default: bool = False

    @field_validator("switch_id")
    @classmethod
    def validate_switch_id(cls, value: str) -> str:
        return menu_text(value, 128, "开关 ID")


class SwitchMenuItem(MenuAction):
    type: Literal["switch"]
    switch: MenuSwitch


SubMenuItem = Annotated[MessageMenuItem | LinkMenuItem, Field(discriminator="type")]


class FoldMenuItem(MenuAction):
    type: Literal["menu"]
    sub_menu_items: list[SubMenuItem] = Field(min_length=1, max_length=5)


MenuItem = Annotated[MessageMenuItem | LinkMenuItem | SwitchMenuItem | FoldMenuItem, Field(discriminator="type")]


class GlobalMenu(PanelModel):
    items: list[MenuItem] = Field(max_length=10)

    @field_validator("items")
    @classmethod
    def validate_top_names(cls, values: list[MenuItem]) -> list[MenuItem]:
        for item in values:
            if len(item.name) > 10:
                raise ValueError("一级菜单名称最多 10 个字符")
        return values


class UpdateMenu(PanelModel):
    menu: GlobalMenu | None

    def payload(self) -> JsonValue:
        # QQ 用显式 null 删除菜单，不能随其它可选字段一起剥掉。
        return {"menu": self.menu.payload() if self.menu is not None else None}
