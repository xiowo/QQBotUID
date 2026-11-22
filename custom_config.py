"""插件的「消息追加配置」。"""

from __future__ import annotations

import json
import contextlib
from typing import Any
from dataclasses import field, asdict, dataclass

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

CUSTOM_CONFIG_PATH = get_res_path("QQBotUID") / "custom_config.json"

DEFAULT_FOOTER_TEXT = (
    "> 点击[支持一下](mqqapi://aio/inlinecmd?command=支持沫狐&enter=false&reply=false)"
)

MAX_ROWS = 10
MAX_BUTTONS_PER_ROW = 5
MAX_FOOTER_CHARS = 4000

BUTTON_KEYS = {
    "text",
    "data",
    "pressed_text",
    "style",
    "action",
    "permisson",
    "specify_role_ids",
    "specify_user_ids",
    "unsupport_tips",
    "enter",
    "reply",
    "group_id",
    "id",
    "smallbtn",
    "content",
    "confirm_text",
    "cancel_text",
}
BOOL_KEYS = {"enter", "reply", "smallbtn"}
INT_KEYS = {"action", "style", "permisson"}
STR_KEYS = {
    "text",
    "data",
    "pressed_text",
    "group_id",
    "id",
    "content",
    "confirm_text",
    "cancel_text",
    "unsupport_tips",
}


@dataclass(slots=True)
class MessageDecoration:
    """消息发送时自动追加的内容。"""

    auto_buttons_enabled: bool = False
    auto_buttons_override: bool = False
    buttons: list[list[dict[str, Any]]] = field(default_factory=list)
    footer_enabled: bool = False
    footer_text: str = DEFAULT_FOOTER_TEXT

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_button(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"text": raw, "data": raw}
    if not isinstance(raw, dict):
        return {}
    button: dict[str, Any] = {}
    for key in BUTTON_KEYS & raw.keys():
        value = raw[key]
        if key in BOOL_KEYS:
            button[key] = bool(value)
        elif key in INT_KEYS:
            if value in (None, ""):
                continue
            try:
                button[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif key in STR_KEYS:
            if value is None:
                continue
            button[key] = str(value).strip()
        elif key in {"specify_role_ids", "specify_user_ids"} and isinstance(value, list):
            button[key] = [str(item).strip() for item in value if str(item).strip()]
    text = str(button.get("text") or "").strip()
    data = str(button.get("data") or text).strip()
    if not text and not data:
        return {}
    button["text"] = text or data
    button["data"] = data or text
    return button


def normalize_payload(raw: Any) -> tuple[MessageDecoration | None, str]:
    """校验 WebUI 提交的数据，返回 (配置, 错误信息)。"""
    if not isinstance(raw, dict):
        return None, "配置必须是一个对象"

    rows_raw = raw.get("buttons", [])
    if rows_raw is None:
        rows_raw = []
    if not isinstance(rows_raw, list) or len(rows_raw) > MAX_ROWS:
        return None, f"按钮最多 {MAX_ROWS} 行"

    buttons: list[list[dict[str, Any]]] = []
    for row_index, row_raw in enumerate(rows_raw, 1):
        if isinstance(row_raw, dict):
            row_raw = row_raw.get("buttons") or []
        if not isinstance(row_raw, list) or len(row_raw) > MAX_BUTTONS_PER_ROW:
            return None, f"第 {row_index} 行按钮最多 {MAX_BUTTONS_PER_ROW} 个"
        row = [_normalize_button(item) for item in row_raw]
        row = [item for item in row if item]
        if row:
            buttons.append(row)

    footer_raw = raw.get("footer_text")
    footer_text = DEFAULT_FOOTER_TEXT if footer_raw is None else str(footer_raw).strip()
    if len(footer_text) > MAX_FOOTER_CHARS:
        return None, f"底部内容最多 {MAX_FOOTER_CHARS} 个字符"

    return (
        MessageDecoration(
            auto_buttons_enabled=bool(raw.get("auto_buttons_enabled")),
            auto_buttons_override=bool(raw.get("auto_buttons_override")),
            buttons=buttons,
            footer_enabled=bool(raw.get("footer_enabled")),
            footer_text=footer_text,
        ),
        "",
    )


def _empty_store() -> dict[str, Any]:
    return {"default": {}, "bots": {}}


_cache: dict[str, Any] | None = None
_cache_key: tuple[str, int, int] | None = None


def _invalidate_cache() -> None:
    global _cache, _cache_key
    _cache = None
    _cache_key = None


def _file_key() -> tuple[str, int, int] | None:
    try:
        stat = CUSTOM_CONFIG_PATH.stat()
    except OSError:
        return None
    return (str(CUSTOM_CONFIG_PATH), stat.st_mtime_ns, stat.st_size)


def load_store() -> dict[str, Any]:
    """读整份配置"""
    global _cache, _cache_key
    key = _file_key()
    if key is not None and key == _cache_key and _cache is not None:
        return _cache

    result = _empty_store()
    if key is not None:
        try:
            raw = json.loads(CUSTOM_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            backup = CUSTOM_CONFIG_PATH.parent / f"{CUSTOM_CONFIG_PATH.name}.bad"
            with contextlib.suppress(OSError):
                CUSTOM_CONFIG_PATH.replace(backup)
            logger.warning(f"[QQBotUID] 消息追加配置解析失败，已备份到 {backup.name}: {error}")
            key = None
        else:
            if not isinstance(raw, dict):
                raw = {}
            default = raw.get("default") if isinstance(raw.get("default"), dict) else {}
            bots = raw.get("bots") if isinstance(raw.get("bots"), dict) else {}
            clean_bots = {
                str(name): value for name, value in bots.items() if isinstance(value, dict)
            }
            result = {"default": default, "bots": clean_bots}

    _cache, _cache_key = result, key
    return result


def _write_store(store: dict[str, Any]) -> None:
    CUSTOM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_CONFIG_PATH.parent / f"{CUSTOM_CONFIG_PATH.name}.tmp"
    tmp.write_text(
        json.dumps(store, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(CUSTOM_CONFIG_PATH)
    _invalidate_cache()


def _decorate(raw: Any) -> MessageDecoration:
    config, _ = normalize_payload(raw)
    return config or MessageDecoration()


def load_custom_config(appid: str = "") -> MessageDecoration:
    """读某个机器人的配置；没单独配过就用全局默认。

    文件没变时走内存缓存，WebUI 保存或外部改动后会立刻生效，不用重启核心。
    """
    store = load_store()
    if appid and appid in store["bots"]:
        return _decorate(store["bots"][appid])
    return _decorate(store["default"])


def message_decoration(appid: str = "") -> MessageDecoration:
    return load_custom_config(appid)


def decoration_payload(appid: str = "") -> dict[str, Any]:
    """给 WebUI 用：配置本体 + 是否单独配置 + 全局默认。"""
    store = load_store()
    own = store["bots"].get(appid) if appid else None
    default_raw = store["default"]
    payload = own if own is not None else default_raw
    return {
        "appid": appid,
        "config": _decorate(payload).payload(),
        "has_own": own is not None,
        "default": _decorate(default_raw).payload(),
    }


def save_custom_config(raw: Any, appid: str = "") -> tuple[bool, str]:
    """保存配置。appid 为空表示写全局默认，否则只写这个机器人。"""
    config, error = normalize_payload(raw)
    if config is None:
        return False, error
    store = load_store()
    if appid:
        store["bots"][str(appid)] = config.payload()
    else:
        store["default"] = config.payload()
    _write_store(store)
    return True, ""


def delete_custom_config(appid: str) -> bool:
    """删掉某个机器人的单独配置，让它重新继承全局默认。"""
    if not appid:
        return False
    store = load_store()
    if appid not in store["bots"]:
        return False
    del store["bots"][appid]
    _write_store(store)
    return True
