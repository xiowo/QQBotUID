import re
import json
import time
import uuid
import random
import asyncio
import tomllib
import platform
import contextlib
from typing import Any, Dict, List, Tuple, Union, Optional
from pathlib import Path

import websockets
from aiohttp import web

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Message, MessageSend, MessageReceive, normalize_node_items
from gsuid_core.utils.plugins_config.gs_config import bm_config

from .sign import verify_and_respond
from .config import (
    QQBotUIDConfig,
    is_master,
    stream_setting,
    update_bot_name,
    maintenance_rule,
    group_name_enabled,
    maintenance_message,
    master_only_enabled,
    input_notify_seconds,
)
from .qq_api import (
    FILE_TYPE_AUDIO,
    FILE_TYPE_VIDEO,
    SHARE_CALLBACK_LIMIT,
    QQSender,
    MaterialImage,
    QQMateriaHost,
    QQTokenManager,
    brief_text,
    file_bytes,
    image_size,
    public_image_url,
    cached_group_name,
    core_upload_image,
    patch_image_fallback,
    cancel_group_refreshes,
)
from .ws_api import ACTIVE_TEST, ADMIN_REQUEST, SHARE_LINK_REQUEST, handle_control
from .convert import (
    at_tag,
    send_endpoint,
    build_keyboard,
    event_reply_id,
    is_bot_message,
    recall_endpoint,
    event_to_receive,
    fallback_nickname,
    normalize_buttons,
    interaction_ack_id,
    is_reference_message,
    messages_to_plain_text,
)
from .webhook import WebhookServer, shared_server
from .proactive import PassiveWindow, ProactiveDispatcher
from .user_info import UserRecorder, ensure_table as ensure_user_table
from .group_info import ensure_table as ensure_group_table
from .core_bridge import CoreBridge

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
# Webhook 回调专用：12 是 ACK，13 是平台发来的回调地址校验
OP_CALLBACK_ACK = 12
OP_CALLBACK_VALIDATION = 13

# 1<<0 频道 / 1<<10 频道表情表态 / 1<<24 群成员 / 1<<25 群@与单聊 / 1<<26 按钮回调 / 1<<30 公域频道消息
INTENTS = (1 << 0) | (1 << 10) | (1 << 24) | (1 << 25) | (1 << 26) | (1 << 30)
UA_PLUGIN_TAG = "QQBotPlugin/1.7.2"
CLIENT_VERSION = "9.9.9"
# 主动消息错误码以这个前缀放在回执 id 位
SEND_ERROR_PREFIX = "__send_error__:"


def _read_version() -> str:
    try:
        with (Path(__file__).with_name("pyproject.toml")).open("rb") as fp:
            return str(tomllib.load(fp)["project"]["version"])
    except (OSError, KeyError, ValueError):
        return "1.0.0"


QQBOTUID_VERSION = _read_version()
STABLE_CONNECT_SECONDS = 5.0
FAST_RETRY_LIMIT = 3
FAST_RETRY_FLOOR_MS = 1000

_BROKEN_IMAGE_DATA_PATTERN = re.compile(r"Message\(type='image', data='(?P<data>[^']*)'")
# markdown 图片尺寸
_MD_SIZE_PATTERN = re.compile(r"#(?P<w>\d+)px\s*#(?P<h>\d+)px")
# 本地绝对路径：Windows 盘符 / UNC / POSIX
_LOCAL_IMAGE_PATH_PATTERN = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _local_image_path(target: str) -> str:
    """markdown 图片地址是本地文件时返回文件系统路径，否则返回空串。"""
    path = target[7:] if target.startswith("file://") else target
    if "://" in path:
        return ""
    if len(path) > 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path if _LOCAL_IMAGE_PATH_PATTERN.match(path) else ""


# 主动消息权限测试
ACTIVE_TEST_TEXT = "✅主动权限确认成功"


def _qq_error_text(data: Any) -> str:
    """把 QQ 开放平台的错误体。"""
    if not isinstance(data, dict):
        return brief_text(data, 200) or "平台拒绝发送"
    message = str(data.get("message") or data.get("msg") or "").strip()
    code = data.get("code") or data.get("err_code") or ""
    if message and code:
        return f"{message}(code={code})"
    if message:
        return message
    if code:
        return f"code={code}"
    return brief_text(data, 200) or "平台拒绝发送"


def _warn_no_target(send: MessageSend) -> None:
    """发送目标推断不出来时统一告警"""
    logger.warning(
        t(
            "[QQBotUID] 无法推断发送目标: {target_type}/{target_id}",
            target_type=send.target_type,
            target_id=send.target_id,
        )
    )


def _alt_size(alt: str) -> Optional[Tuple[int, int]]:
    found = _MD_SIZE_PATTERN.search(alt)
    if found is None:
        return None
    return (int(found.group("w")), int(found.group("h")))


def _with_size(alt: str, size: Optional[Tuple[int, int]]) -> str:
    """`#宽px #高px`"""
    if not size or _MD_SIZE_PATTERN.search(alt):
        return alt
    return f"{alt} #{size[0]}px #{size[1]}px"


_PLAIN_LABELS = {"record": "[语音]", "video": "[视频]", "file": "[文件]"}
# 富媒体段 → 上传时的 file_type
_MEDIA_FILE_TYPES = {"record": FILE_TYPE_AUDIO, "video": FILE_TYPE_VIDEO}
# 这些走流式
_STREAMABLE_TYPES = {"text", "markdown", "at"}


def _pick_media(items: List[Message]) -> Tuple[Optional[Message], int]:
    """挑出第一条要单独发的富媒体段；file_type 为 0 表示走文件上传。"""
    for item in items:
        if not isinstance(item.data, str) or not item.data:
            continue
        if item.type == "file":
            return item, 0
        file_type = _MEDIA_FILE_TYPES.get(str(item.type))
        if file_type is not None:
            return item, file_type
    return None, 0


def _size_after(items: List[Message], index: int) -> Optional[Tuple[int, int]]:
    for item in items[index + 1 :]:
        if item.type == "image_size":
            data = item.data
            if isinstance(data, (tuple, list)) and len(data) == 2:
                return (int(data[0]), int(data[1]))
            return None
        if item.type == "image":
            return None
    return None


def _join_pieces(pieces: List[Tuple[str, bool]]) -> str:
    out = ""
    for text, is_image in pieces:
        if not text:
            continue
        if is_image:
            out = out.rstrip("\n")
            if out:
                out += "\n\n"
            out += f"{text.strip()}\n\n"
            continue
        if out.endswith("\n\n"):
            text = text.lstrip("\n")
        out += text
    return out.rstrip("\n")


def _iter_md_images(content: str) -> List[Tuple[int, int, int]]:
    spans: List[Tuple[int, int, int]] = []
    cursor = 0
    while True:
        start = content.find("![", cursor)
        if start < 0:
            return spans
        alt_end = content.find("](", start + 2)
        if alt_end < 0:
            cursor = start + 2
            continue
        depth = 1
        pos = alt_end + 2
        while pos < len(content) and depth:
            char = content[pos]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            pos += 1
        if depth:
            return spans
        spans.append((start, alt_end, pos))
        cursor = pos


_MD_AT_PATTERN = re.compile(r"<@!?[A-Za-z0-9_\-]+>")
_AT_TAG_PATTERN = re.compile(r'<@!?[A-Za-z0-9_\-]+>|<qqbot-at-user id="[^"]*"\s*/>|<qqbot-at-everyone />')
_MD_SOFT_BREAK = re.compile(r"(?<!\n)\n(?!\n)")
_MD_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def _md_hard_break(content: str) -> str:
    return _MD_SOFT_BREAK.sub("  \n", content)


def _msg_seq() -> int:
    return random.randint(1000000, 9999999)


def _platform_selected(config_key: str, platform: str) -> bool:
    """读核心「按钮与Markdown配置」里的平台列表"""
    if not platform:
        return False
    selected = bm_config.get_config(config_key).data
    return isinstance(selected, list) and platform in selected


def _strip_at(text: str, target_type: str) -> str:
    """过滤私聊@"""
    return _AT_TAG_PATTERN.sub("", text) if target_type == "direct" else text


_REPLY_TARGETS = {"group": "group", "direct": "direct", "channel": "channel", "sub_channel": "sub_channel"}


def _reply_target_type(user_type: str) -> Optional[str]:
    return _REPLY_TARGETS.get(str(user_type or ""))


def _is_lifecycle(receive: MessageReceive) -> bool:
    content = receive.content or []
    return bool(content) and str(content[0].type or "").startswith("meta-")


def _duration_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _md_text(item: Message) -> str:
    data = item.data
    if isinstance(data, dict):
        content = str(data.get("content") or "")
    else:
        content = "" if data is None else str(data)
    return _MD_AT_PATTERN.sub("", content)


def _has_image(send: MessageSend) -> bool:
    for item in send.content or []:
        if item.type == "image":
            return True
        if item.type in {"markdown", "text"} and _MD_IMAGE_PATTERN.search(_md_text(item)):
            return True
    return False


def _markdown_body(markdown: Optional[Message]) -> Dict[str, Any]:
    if markdown is not None and isinstance(markdown.data, dict):
        return dict(markdown.data)
    return {}


def _user_agent(client_name: str) -> str:
    runtime = f"Python/{platform.python_version()}; {platform.system().lower()}"
    name = client_name.strip() or "QQBotUID"
    return f"{UA_PLUGIN_TAG} ({runtime}; {name}/{CLIENT_VERSION})"


def _admin_str(data: Dict[str, Any], *keys: str) -> str:
    """按顺序取第一个非空字符串字段。"""
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _admin_list(data: Dict[str, Any], *keys: str) -> List[str]:
    """取 openid 列表，单个字符串也当一项。"""
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        items = [str(item).strip() for item in raw if str(item).strip()]
        if items:
            return items
    return []


async def _admin_kick(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    openids = _admin_list(data, "member_openids", "member_openid")
    if not group_id or not openids:
        return {"ok": False, "error": "缺少 group_openid 或 member_openids"}
    ok = await bot.sender.kick_group_members(group_id, openids, bool(data.get("add_to_blacklist")))
    return {"ok": ok}


async def _admin_mute(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    openid = _admin_str(data, "member_openid", "user_id")
    seconds = _duration_seconds(data.get("seconds", data.get("duration")))
    if not group_id or not openid or seconds is None:
        return {"ok": False, "error": "缺少 group_openid / member_openid / seconds"}
    ok = await bot.sender.set_group_member_mute(group_id, openid, seconds)
    return {"ok": ok}


async def _admin_mute_state(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    if not group_id:
        return {"ok": False, "error": "缺少 group_openid"}
    return {"ok": True, "data": await bot.sender.get_group_mute_state(group_id)}


async def _admin_bot_state(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    if not group_id:
        return {"ok": False, "error": "缺少 group_openid"}
    return {"ok": True, "data": await bot.sender.get_group_bot_state(group_id)}


async def _admin_group_list(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": await bot.sender.get_group_list()}


async def _admin_join_requests(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    if not group_id:
        return {"ok": False, "error": "缺少 group_openid"}
    return {"ok": True, "data": await bot.sender.list_join_requests(group_id)}


async def _admin_approve(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    openid = _admin_str(data, "member_openid", "user_id")
    if not group_id or not openid:
        return {"ok": False, "error": "缺少 group_openid 或 member_openid"}
    ok = await bot.sender.approve_join_request(group_id, openid, _admin_str(data, "join_request_id"))
    return {"ok": ok}


async def _admin_decline(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    openid = _admin_str(data, "member_openid", "user_id")
    if not group_id or not openid:
        return {"ok": False, "error": "缺少 group_openid 或 member_openid"}
    ok = await bot.sender.decline_join_request(
        group_id,
        openid,
        _admin_str(data, "join_request_id"),
        _admin_str(data, "reason", "reject_reason"),
        bool(data.get("add_to_blacklist")),
    )
    return {"ok": ok}


async def _admin_blacklist(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    if not group_id:
        return {"ok": False, "error": "缺少 group_openid"}
    return {"ok": True, "data": await bot.sender.get_member_blacklist(group_id)}


async def _admin_blacklist_change(bot: "QQBotUID", data: Dict[str, Any], op: str) -> Dict[str, Any]:
    group_id = _admin_str(data, "group_openid", "group_id")
    openids = _admin_list(data, "member_openids", "member_openid")
    if not group_id or not openids:
        return {"ok": False, "error": "缺少 group_openid 或 member_openids"}
    ok = await bot.sender.change_member_blacklist(group_id, op, openids)
    return {"ok": ok}


async def _admin_blacklist_add(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    return await _admin_blacklist_change(bot, data, "add")


async def _admin_blacklist_del(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    return await _admin_blacklist_change(bot, data, "del")


async def _admin_menu_get(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": await bot.sender.get_menu()}


async def _admin_menu_set(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    menu = data.get("menu")
    if not isinstance(menu, dict) or not menu:
        return {"ok": False, "error": "menu 必须是非空 dict"}
    return {"ok": await bot.sender.change_menu(menu)}


async def _admin_panel_list(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    scope = _admin_str(data, "scope") or "group"
    return {"ok": True, "data": await bot.sender.list_panels(scope)}


async def _admin_panel_create(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    panel = data.get("panel")
    if not isinstance(panel, dict) or not panel:
        return {"ok": False, "error": "panel 必须是非空 dict"}
    scope = _admin_str(data, "scope") or "group"
    target_type = _admin_str(data, "target_type") or "all"
    result = await bot.sender.create_panel(
        scope,
        target_type,
        panel,
        _admin_list(data, "user_openids"),
        _admin_list(data, "group_openids"),
    )
    return {"ok": bool(result), "data": result}


async def _admin_panel_delete(bot: "QQBotUID", data: Dict[str, Any]) -> Dict[str, Any]:
    panel_id = _admin_str(data, "panel_id")
    if not panel_id:
        return {"ok": False, "error": "缺少 panel_id"}
    return {"ok": await bot.sender.delete_panel(panel_id)}


_ADMIN_ACTIONS = {
    "kick": _admin_kick,
    "mute": _admin_mute,
    "mute_state": _admin_mute_state,
    "bot_state": _admin_bot_state,
    "group_list": _admin_group_list,
    "join_requests": _admin_join_requests,
    "approve": _admin_approve,
    "decline": _admin_decline,
    "blacklist": _admin_blacklist,
    "blacklist_add": _admin_blacklist_add,
    "blacklist_del": _admin_blacklist_del,
    "menu_get": _admin_menu_get,
    "menu_set": _admin_menu_set,
    "panel_list": _admin_panel_list,
    "panel_create": _admin_panel_create,
    "panel_delete": _admin_panel_delete,
}


class _Deferred:
    def __bool__(self) -> bool:
        return False


class QQBotUID:
    def __init__(self, config: QQBotUIDConfig, bridge: CoreBridge):
        self.config = config
        self.bridge = bridge
        self.tm = QQTokenManager(config)
        self.sender = QQSender(self.tm)
        self.material = QQMateriaHost(self.tm)
        self.proactive = ProactiveDispatcher()
        self.passive = PassiveWindow()
        self.users = UserRecorder()
        self._tasks: List[asyncio.Task[Any]] = []
        self._webhook: Optional[WebhookServer] = None
        self._ws = None
        self._session_id: Optional[str] = None
        self._seq: Optional[int] = None
        self._heartbeat_interval = 45.0
        self._heartbeat_task: Optional[asyncio.Task[Any]] = None
        self._closed = False
        self._gateway_url = ""
        self._bot_name = ""

    @property
    def self_id(self) -> str:
        return self.config.self_id

    def apply_config(self, config: QQBotUIDConfig) -> None:
        self.config = config
        self.tm.config = config

    async def start(self) -> None:
        self.config.validate()
        self._closed = False
        patch_image_fallback()
        await ensure_group_table()
        await ensure_user_table()
        self.bridge.add_target(self)
        try:
            if self.config.mode == "wh":
                if self.config.webhook_enabled:
                    await self.start_webhook()
                else:
                    logger.warning(
                        t(
                            "[QQBotUID] {appid} 是 Webhook 模式但未启用「启用 Webhook」，已跳过监听",
                            appid=self.config.appid,
                        )
                    )
            if self.config.mode == "ws":
                self._tasks.append(asyncio.create_task(self.qq_ws_forever()))
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._closed = True
        self.bridge.remove_target(self)
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._webhook is not None:
            await self._webhook.remove(self.config.webhook_path)
            self._webhook = None
        await self.proactive.close()
        await self.users.close()
        cancel_group_refreshes()
        await self.tm.aclose()

    async def deliver(self, send: MessageSend) -> None:
        """核心下发的一帧：确认收件人是我，再转成 QQ 官方接口调用。"""
        if send.bot_self_id and send.bot_self_id != self.self_id:
            return
        message_id = await self.handle_core_send(send)
        if isinstance(message_id, _Deferred):
            return
        await self.send_recall_ack(send, message_id)

    async def qq_ws_forever(self) -> None:
        fails = 0
        while not self._closed:
            started = time.monotonic()
            try:
                url = await self.get_gateway_url()
                headers = {"User-Agent": _user_agent(self.config.client_name)}
                async with websockets.connect(url, additional_headers=headers) as ws:
                    self._ws = ws
                    async for message in ws:
                        await self.handle_qq_ws_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closed:
                    continue
                self._gateway_url = ""
                fails = 0 if time.monotonic() - started >= STABLE_CONNECT_SECONDS else fails + 1
                delay_ms = self._reconnect_delay_ms(fails)
                when = f"{delay_ms}ms 后重连" if delay_ms else "立刻重连"
                logger.warning(t("[QQBotUID] QQ官方WS断开，{when}: {err}", when=when, err=e))
                await asyncio.sleep(delay_ms / 1000)
            finally:
                self._ws = None

    def _reconnect_delay_ms(self, fails: int) -> int:
        """重连间隔"""
        interval = max(int(self.config.reconnect_interval), 0)
        if interval:
            return interval
        return FAST_RETRY_FLOOR_MS if fails > FAST_RETRY_LIMIT else 0

    async def get_gateway_url(self) -> str:
        if self.config.gateway_url:
            return self.config.gateway_url
        if self._gateway_url:
            return self._gateway_url
        client = await self.tm.client()
        resp = await client.get("/gateway/bot", headers={"Authorization": await self.tm.authorization()})
        data = resp.json()
        self._gateway_url = str(data.get("url") or "")
        if not self._gateway_url:
            raise RuntimeError(f"获取 QQBot 网关失败: {data}")
        return self._gateway_url

    async def handle_qq_ws_message(self, message: Union[str, bytes]) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        op = payload.get("op")
        if payload.get("s") is not None:
            self._seq = int(payload["s"])
        if op == OP_HELLO:
            self._heartbeat_interval = float(payload.get("d", {}).get("heartbeat_interval", 45000)) / 1000
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self.heartbeat_loop())
            if self._session_id and self._seq is not None:
                await self.send_op(
                    OP_RESUME,
                    {"token": f"QQBot {await self.tm.token()}", "session_id": self._session_id, "seq": self._seq},
                )
            else:
                await self.send_op(
                    OP_IDENTIFY, {"token": f"QQBot {await self.tm.token()}", "intents": INTENTS, "shard": [0, 1]}
                )
        elif op == OP_DISPATCH:
            await self.handle_dispatch(payload)
        elif op == OP_RECONNECT:
            raise RuntimeError("QQBot gateway requested reconnect")
        elif op == OP_INVALID_SESSION:
            self._session_id = None
            self._seq = None
            self._gateway_url = ""
            raise RuntimeError("QQBot invalid session")

    async def send_op(self, op: int, data: Any) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({"op": op, "d": data}))

    async def heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._heartbeat_interval)
            with contextlib.suppress(Exception):
                await self.send_op(OP_HEARTBEAT, self._seq)

    async def group_name(self, payload: Dict[str, Any]) -> str:
        if not group_name_enabled():
            return ""
        raw = payload.get("d")
        d = raw if isinstance(raw, dict) else {}
        group_id = str(d.get("group_openid") or d.get("group_id") or "")
        if not group_id:
            return ""
        return await cached_group_name(self.tm, group_id)

    async def handle_dispatch(self, payload: Dict[str, Any]) -> None:
        event_type = payload.get("t")
        if event_type == "READY":
            data = payload.get("d", {}) if isinstance(payload.get("d"), dict) else {}
            self._session_id = data.get("session_id")
            raw_user = data.get("user")
            user = raw_user if isinstance(raw_user, dict) else {}
            self._bot_name = str(
                user.get("username")
                or user.get("name")
                or self.config.client_name
                or self.config.self_id
                or self.config.route_bot_id
            )
            update_bot_name(self.config.appid, self._bot_name)
            logger.success(t("QQBotUID v{version} {name} 已连接", version=QQBOTUID_VERSION, name=self._bot_name))
            return
        if event_type == "RESUMED":
            logger.success(t("[QQBotUID] QQ官方WS会话已恢复"))
            return
        await self.ack_interaction(payload)
        receive = event_to_receive(
            payload,
            self.config.platform_bot_id,
            self.config.self_id,
            self.config.appid,
            await self.group_name(payload),
            await fallback_nickname(payload.get("d") or {}, self.config.platform_bot_id),
        )
        if receive is not None:
            # debug 下打印原始事件
            logger.debug(
                t("[QQBotUID] WS 事件 {event_type}: {raw}", event_type=event_type, raw=brief_text(payload, 1500))
            )
            if self._skip_other_bot(payload):
                return
            self._warn_unresolved_reference(payload, receive)
            self.passive.mark(receive.msg_id)
            self.users.observe(receive)
            await self.maybe_input_notify(receive)
            if await self.maintenance_reply(receive):
                return
            await self.bridge.push(receive)

    async def maybe_input_notify(self, receive: MessageReceive) -> None:
        """私聊先发「正在输入」；剩最后一次被动额度时不发，留给真回复。"""
        seconds = input_notify_seconds()
        if not seconds or receive.user_type != "direct" or not receive.user_id:
            return
        if self.passive.remaining(receive.msg_id) <= 1 or not self.passive.take(receive.msg_id):
            return
        await self.sender.send_input_notify(receive.user_id, seconds, receive.msg_id)

    async def ack_interaction(self, payload: Dict[str, Any]) -> None:
        """按钮回调立刻回应"""
        interaction_id = interaction_ack_id(payload)
        if not interaction_id:
            return
        try:
            ok = await self.sender.reply_interaction(interaction_id)
        except Exception as error:
            logger.warning(t("[QQBotUID] 按钮回调回应失败: {error}", error=error))
            return
        if not ok:
            logger.debug(t("[QQBotUID] 按钮回调回应未成功: {id}", id=interaction_id))

    def _skip_other_bot(self, payload: Dict[str, Any]) -> bool:
        """开了「忽略其他机器人消息」时，别的 bot 发的消息不进核心。"""
        if not self.config.ignore_bot_message or not is_bot_message(payload):
            return False
        logger.debug(t("[QQBotUID] 已忽略其他机器人消息: {event}", event=str(payload.get("t") or "")))
        return True

    def _warn_unresolved_reference(self, payload: Dict[str, Any], receive: MessageReceive) -> None:
        if any(item.type == "reply_id" for item in receive.content):
            return
        if not is_reference_message(payload):
            return
        raw_d = payload.get("d")
        d = raw_d if isinstance(raw_d, dict) else {}
        elements = [
            {key: item.get(key) for key in ("msg_idx", "type")}
            for item in d.get("msg_elements") or []
            if isinstance(item, dict)
        ]
        logger.warning(
            t(
                "[QQBotUID] 引用消息没解析出引用 ID: message_type={message_type} scene={scene} elements={elements}",
                message_type=d.get("message_type"),
                scene=d.get("message_scene"),
                elements=elements,
            )
        )

    async def maintenance_reply(self, receive: MessageReceive) -> bool:
        """维护期间用提示顶替核心的回复，返回 True 表示已回提示。"""
        message = self._maintenance_message(receive)
        if not message:
            return False
        target_type = _reply_target_type(receive.user_type)
        if target_type is None:
            return False
        target_id = receive.user_id if target_type == "direct" else receive.group_id
        if not target_id:
            return False
        send = MessageSend(
            bot_id=receive.bot_id,
            bot_self_id=receive.bot_self_id,
            msg_id=receive.msg_id,
            target_type=target_type,
            target_id=target_id,
            content=[Message(type="text", data=message)],
        )
        endpoint = send_endpoint(send)
        if endpoint is None:
            return False
        logger.info(f"[QQBotUID] 维护中，已回提示: {target_type}/{target_id}")
        await self.post_message(send, endpoint, receive.msg_id)
        return True

    def _maintenance_message(self, receive: MessageReceive) -> str:
        """该回维护提示就返回文案；放行返回空串。"""
        if _is_lifecycle(receive):
            # 进群 / 退群这类事件不是「有人在说话」，别往群里刷维护提示
            return ""
        if master_only_enabled() and not is_master(receive.user_id):
            return maintenance_message()
        rule = maintenance_rule()
        if rule is None:
            return ""
        active = getattr(self.bridge, "maintenance_active", None)
        if not callable(active) or not active(rule.threshold):
            return ""
        if not rule.matches(messages_to_plain_text(receive.content or [], receive.user_type)):
            return ""
        return rule.message

    async def start_webhook(self) -> None:
        self._webhook = shared_server(self.config.webhook_host, self.config.webhook_port)
        await self._webhook.add(self.config.webhook_path, self.handle_webhook)

    async def handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if body.get("op") == OP_CALLBACK_VALIDATION:
            resp = verify_and_respond(body, self.config.secret)
            if resp is None:
                return web.json_response({"error": "invalid validation"}, status=400)
            return web.json_response(resp)
        await self.ack_interaction(body)
        receive = event_to_receive(
            body,
            self.config.platform_bot_id,
            self.config.self_id,
            self.config.appid,
            await self.group_name(body),
            await fallback_nickname(body.get("d") or {}, self.config.platform_bot_id),
        )
        if receive is not None:
            logger.debug(
                t("[QQBotUID] Webhook 事件 {event_type}: {raw}", event_type=body.get("t"), raw=brief_text(body, 1500))
            )
            if not self._skip_other_bot(body):
                self.passive.mark(receive.msg_id)
                self.users.observe(receive)
                await self.maybe_input_notify(receive)
                if not await self.maintenance_reply(receive):
                    await self.bridge.push(receive)
        if body.get("t") == "INTERACTION_CREATE":
            return web.json_response({"op": OP_CALLBACK_ACK, "d": {}})
        return web.json_response({})

    async def send_recall_ack(self, send: MessageSend, message_id: Optional[Union[str, List[str]]]) -> None:
        if not send.echo:
            return
        ack = event_to_receive(
            {
                "t": "QQBOTUID_RECALL_ACK",
                "d": {
                    "id": "",
                    "content": "",
                    "author": {"id": ""},
                },
            },
            self.config.platform_bot_id,
            self.config.self_id,
            self.config.appid,
        )
        if ack is None:
            return
        ack.content = [Message(type="recall_message_id", data={"echo": send.echo, "id": message_id})]
        await self.bridge.push(ack)

    async def handle_ban(self, data: Dict[str, Any]) -> None:
        """核心 bot.ban() 下发的禁言"""
        user_id = str(data.get("user_id") or "")
        group_id = str(data.get("group_id") or "")
        seconds = _duration_seconds(data.get("duration"))
        if not user_id or not group_id or seconds is None:
            logger.warning(
                t(
                    "[QQBotUID] 禁言参数不完整，已忽略: user={user_id} group={group_id} duration={duration}",
                    user_id=user_id,
                    group_id=group_id,
                    duration=data.get("duration"),
                )
            )
            return
        if await self.sender.set_group_member_mute(group_id, user_id, seconds):
            if seconds:
                logger.info(t("[QQBotUID] 已禁言 {user_id} {sec} 秒", user_id=user_id, sec=seconds))
            else:
                logger.info(t("[QQBotUID] 已解除禁言 {user_id}", user_id=user_id))

    async def handle_core_send(self, send: MessageSend) -> Optional[Union[str, List[str], _Deferred]]:
        if not send.content:
            return None
        first = send.content[0]
        if send.bot_id not in {self.config.platform_bot_id, self.config.route_bot_id}:
            logger.debug(t("[QQBotUID] 忽略非官机平台下发消息: bot_id={bot_id}", bot_id=send.bot_id))
            return None
        control = await handle_control(first.type, first.data, self.config.platform_bot_id)
        if control is not None:
            return control
        if first.type == ACTIVE_TEST:
            return await self.active_test_reply(send, first.data)
        if first.type == ADMIN_REQUEST:
            return await self.admin_reply(first.data)
        if first.type == SHARE_LINK_REQUEST:
            return await self.share_link_reply(first.data)
        if first.type == "excute_delete_message" and isinstance(first.data, dict):
            mid = str(first.data.get("message_id") or "")
            endpoint = recall_endpoint(send, mid) if mid else None
            return mid if endpoint and await self.sender.delete(endpoint) else None
        if first.type == "excute_ban_user" and isinstance(first.data, dict):
            await self.handle_ban(first.data)
            return None
        node = next((item for item in send.content if item.type == "node" and isinstance(item.data, list)), None)
        if node is not None:
            message_ids = await self.send_node_items(send, node.data, send.msg_id)
            return message_ids or _Deferred()
        endpoint = send_endpoint(send)
        if endpoint is None:
            _warn_no_target(send)
            return None
        if not self.passive.take(send.msg_id):
            self._log_passive_fallback(send)
            self.proactive.submit(send.target_type, lambda: self.send_proactive(send, endpoint))
            return _Deferred()
        streamed = await self.try_stream(send, send.msg_id)
        if streamed is not None:
            return streamed
        return await self.post_message(send, endpoint, send.msg_id)

    async def try_stream(self, send: MessageSend, msg_id: str) -> Optional[str]:
        """私聊纯文本走流式；返回 message_id 表示已发出，``None`` 表示该回退普通发送。"""
        setting = stream_setting()
        if setting is None or send.target_type != "direct" or not send.target_id:
            return None
        if _has_image(send):
            logger.debug("[QQBotUID] 回复里带图片，不走流式，直接普通发送")
            return None
        if any(str(item.type or "") not in _STREAMABLE_TYPES for item in send.content or []):
            return None
        content = await self._segments_markdown(send)
        if not content.strip():
            return None
        if not any(item.type == "markdown" for item in send.content or []):
            content = _md_hard_break(content)
        chunk_size = setting.chunk_size
        if chunk_size >= len(content):
            chunk_size = max(1, (len(content) + 1) // 2)
        event_id = event_reply_id(msg_id)
        ok, stream_id = await self.sender.send_stream(
            send.target_id,
            content,
            msg_id="" if event_id else msg_id,
            event_id=event_id,
            chunk_size=chunk_size,
            delay_ms=setting.delay_ms,
        )
        if ok:
            return stream_id or None
        if stream_id:
            return stream_id
        logger.warning("[QQBotUID] 流式发送失败，回退普通消息")
        return None

    def _log_passive_fallback(self, send: MessageSend) -> None:
        """被动额度用完（5 次）或已过期，改走主动通道。"""
        if not send.msg_id:
            return
        logger.debug(
            f"[QQBotUID] 被动回复不可用（剩余 {self.passive.remaining(send.msg_id)} 次），"
            f"转主动发送: {send.target_type}/{send.target_id} msg_id={send.msg_id}"
        )

    async def active_test_reply(self, send: MessageSend, data: Any) -> str:
        """核心通用协议 gscore_qqbot_active_test"""
        payload = data if isinstance(data, dict) else {}
        target_type = "direct" if str(payload.get("target_type") or "") == "direct" else "group"
        target_id = str(payload.get("target_id") or "")
        probe = MessageSend(
            bot_id=send.bot_id,
            bot_self_id=send.bot_self_id,
            target_type=target_type,
            target_id=target_id,
            content=[Message(type="text", data=ACTIVE_TEST_TEXT)],
        )
        endpoint = send_endpoint(probe)
        if endpoint is None:
            return json.dumps({"ok": False, "message_id": None, "error": "缺少有效的发送目标"}, ensure_ascii=False)
        message_id, error = await self.post_message_detail(probe, endpoint, "")
        logger.debug(
            f"[QQBotUID] 主动消息权限测试: target={target_type}/{target_id} "
            f"ok={bool(message_id)} error={error!r}"
        )
        if message_id:
            return json.dumps({"ok": True, "message_id": message_id, "error": ""}, ensure_ascii=False)
        return json.dumps(
            {"ok": False, "message_id": None, "error": error or "主动发送失败，可能是权限不足或频次限制"},
            ensure_ascii=False,
        )

    async def share_link_reply(self, data: Any) -> str:
        """核心通用协议 gscore_qqbot_share_link_request：生成机器人分享链接。"""
        payload = data if isinstance(data, dict) else {}
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            return json.dumps({"ok": False, "error": "missing request_id"}, ensure_ascii=False)
        callback = str(payload.get("callback_data") or "").strip()
        if not callback:
            callback = uuid.uuid4().hex
        elif len(callback) > SHARE_CALLBACK_LIMIT:
            return json.dumps(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": f"callback_data 最长 {SHARE_CALLBACK_LIMIT} 字符",
                },
                ensure_ascii=False,
            )
        url = await self.sender.generate_url_link(callback)
        if not url:
            return json.dumps(
                {"request_id": request_id, "ok": False, "error": "生成分享链接失败"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "request_id": request_id,
                "ok": True,
                "data": {"url": url, "callback_data": callback},
            },
            ensure_ascii=False,
        )

    async def admin_reply(self, data: Any) -> str:
        """核心通用协议 gscore_qqbot_admin_request：按 action 调群管 / 菜单接口。"""
        payload = data if isinstance(data, dict) else {}
        request_id = str(payload.get("request_id") or "")
        action = str(payload.get("action") or "").strip().lower()
        if not request_id:
            return json.dumps({"ok": False, "error": "missing request_id"}, ensure_ascii=False)
        handler = _ADMIN_ACTIONS.get(action)
        if handler is None:
            logger.warning(f"[QQBotUID] 未知的群管 action: {action or '-'}")
            return json.dumps(
                {"request_id": request_id, "action": action, "ok": False, "error": f"未知 action: {action or '-'}"},
                ensure_ascii=False,
            )
        try:
            body = await handler(self, payload)
        except Exception as error:
            logger.warning(f"[QQBotUID] 群管动作失败（{action}）: {error}")
            body = {"ok": False, "error": str(error)}
        result: Dict[str, Any] = {"request_id": request_id, "action": action}
        result.update(body)
        return json.dumps(result, ensure_ascii=False)

    async def send_proactive(self, send: MessageSend, endpoint: str) -> None:
        """主动消息"""
        message_id: Optional[str] = None
        try:
            message_id, error = await self.post_message_detail(send, endpoint, "")
            if message_id is None and error:
                message_id = f"{SEND_ERROR_PREFIX}{error}"
        finally:
            await self.send_recall_ack(send, message_id)

    async def post_message(self, send: MessageSend, endpoint: str, msg_id: str) -> Optional[str]:
        message_id, _ = await self.post_message_detail(send, endpoint, msg_id)
        return message_id

    async def post_message_detail(self, send: MessageSend, endpoint: str, msg_id: str) -> Tuple[Optional[str], str]:
        """发送一帧，返回 (message_id, error)；error 为空串表示成功。"""
        try:
            payload = await self.build_send_payload(send, msg_id)
            markdown_body = payload.get("markdown")
            has_markdown = isinstance(markdown_body, dict) and bool(str(markdown_body.get("content") or "").strip())
            has_card = bool(payload.get("card"))
            has_media = bool(payload.get("media"))
            has_text = bool(payload.get("content"))
            if not has_text and not has_markdown and not has_media and not has_card:
                logger.warning(t("[QQBotUID] 没有可发送的内容，已跳过: {target_id}", target_id=send.target_id))
                return None, "没有可发送的内容"
            ok, data = await self.sender.post_json(endpoint, payload)
            if not ok:
                logger.warning(
                    t(
                        "[QQBotUID] 消息发送失败: {data} endpoint={endpoint} payload={payload}",
                        data=data,
                        endpoint=endpoint,
                        payload=brief_text(payload, 400),
                    )
                )
                return None, _qq_error_text(data)
            # 进审核的消息平台只回 audit_id，直接当 id 回传：撤回会失败，但不至于判成发送失败
            message_id = str(
                data.get("id") or data.get("msg_id") or data.get("message_id") or data.get("audit_id") or ""
            )
            return message_id or None, ""
        finally:
            self.material.schedule_cleanup()

    async def send_node_items(
        self, send: MessageSend, items: Any, msg_id: str = ""
    ) -> Optional[List[str]]:
        """node 降级多条消息发送"""
        endpoint = send_endpoint(send)
        if endpoint is None:
            _warn_no_target(send)
            return None
        message_ids: List[str] = []
        for item in normalize_node_items(items):
            if msg_id and self.passive.take(msg_id):
                part = MessageSend(
                    bot_id=send.bot_id,
                    bot_self_id=send.bot_self_id,
                    msg_id=send.msg_id,
                    target_type=send.target_type,
                    target_id=send.target_id,
                    content=[item],
                )
                message_id = await self.post_message(part, endpoint, msg_id)
                if message_id:
                    message_ids.append(message_id)
                continue
            self.proactive.submit(send.target_type, lambda it=item: self._send_node_part(send, it))
        return message_ids or None

    async def _send_node_part(self, send: MessageSend, item: Message) -> None:
        endpoint = send_endpoint(send)
        if endpoint is None:
            return
        part = MessageSend(
            bot_id=send.bot_id,
            bot_self_id=send.bot_self_id,
            msg_id="",
            target_type=send.target_type,
            target_id=send.target_id,
            content=[item],
        )
        await self.post_message(part, endpoint, "")

    async def build_send_payload(self, send: MessageSend, msg_id: str) -> Dict[str, Any]:
        assert send.content is not None
        platform = self.config.platform_bot_id
        use_buttons = _platform_selected("SendButtonsPlatform", platform)
        if any(item.type == "template_markdown" for item in send.content):
            logger.debug("[QQBotUID] 不支持 markdown 模板消息，该段已跳过")

        markdown = next((m for m in send.content if m.type == "markdown"), None)
        image = next((m for m in send.content if m.type == "image" and isinstance(m.data, str)), None)
        buttons = next((m for m in send.content if m.type in {"buttons", "template_buttons"}), None)
        card = next((m for m in send.content if m.type == "card" and isinstance(m.data, dict)), None)
        media, media_type = _pick_media(send.content)
        target_type = send.target_type or "direct"
        text = _strip_at(messages_to_plain_text(send.content, target_type), target_type)
        use_md = markdown is not None or _platform_selected("SendMDPlatform", platform)

        payload: Dict[str, Any] = {"msg_seq": _msg_seq()}
        event_id = event_reply_id(msg_id)
        if event_id:
            payload["event_id"] = event_id
        elif msg_id:
            payload["msg_id"] = msg_id
        reply_id = next((m for m in send.content if m.type == "reply_id" and m.data), None)
        if reply_id is not None:
            payload["message_reference"] = {
                "message_id": str(reply_id.data),
                "ignore_get_message_error": True,
            }

        if card is not None and isinstance(card.data, dict):
            card_type = str(card.data.get("type") or "tuwen").strip().lower()
            content = card.data.get("content")
            if card_type == "ark" and isinstance(content, dict):
                payload.update({"msg_type": 3, "ark": content})
                return payload
            payload.update({"msg_type": 8, "card": card.data})
            return payload

        if media is not None and send.target_id:
            if media_type == FILE_TYPE_AUDIO or media_type == FILE_TYPE_VIDEO:
                file_info = await self.sender.upload_media(
                    target_type,
                    send.target_id,
                    str(media.data),
                    media_type,
                )
            else:
                file_info = await self.sender.upload_file(target_type, send.target_id, str(media.data))
            if file_info:
                payload.update({"msg_type": 7, "media": {"file_info": file_info}})
                rest = _strip_at(
                    messages_to_plain_text((item for item in send.content if item is not media), target_type),
                    target_type,
                ).strip()
                if rest:
                    payload["content"] = rest
                self._attach_buttons(payload, buttons, use_buttons)
                return payload

        if use_md:
            payload.update({"msg_type": 2, "markdown": await self._build_markdown(send, markdown, text)})
            self._attach_buttons(payload, buttons, use_buttons)
            return payload

        if image and send.target_id:
            file_info = await self.sender.upload_media(
                target_type,
                send.target_id,
                str(image.data),
            )
            if file_info:
                payload.update({"msg_type": 7, "media": {"file_info": file_info}, "content": text})
                self._attach_buttons(payload, buttons, use_buttons)
                return payload

        payload.update({"msg_type": 0, "content": text})
        self._attach_buttons(payload, buttons, use_buttons)
        return payload

    async def _build_markdown(self, send: MessageSend, markdown: Optional[Message], text: str) -> Dict[str, Any]:
        body = _markdown_body(markdown)
        body["content"] = await self._segments_markdown(send) or text
        return body

    async def _segments_markdown(self, send: MessageSend) -> str:
        """把消息段按原顺序拼成 markdown，正文和图片都就地插入。"""
        items = send.content or []
        pieces: List[Tuple[str, bool]] = []
        mentions = [str(item.data) for item in items if item.type == "at" and item.data]
        if mentions and send.target_type in {"group", "channel", "sub_channel"}:
            pieces.append(("".join(at_tag(mention, send.target_type or "") for mention in mentions), False))
        for index, item in enumerate(items):
            if item.type == "markdown":
                text_piece = _md_text(item)
                if text_piece:
                    pieces.append((await self._resolve_markdown_images(text_piece), False))
            elif item.type == "text" and item.data is not None:
                text_piece = _MD_AT_PATTERN.sub("", str(item.data))
                pieces.append((await self._resolve_markdown_images(text_piece), False))
            elif item.type == "image" and isinstance(item.data, str):
                material = await self.material_image(item.data, _size_after(items, index))
                if material is not None:
                    pieces.append((f"![{_with_size('图片', material.size)}]({material.url})", True))
            elif item.type in _PLAIN_LABELS:
                pieces.append((_PLAIN_LABELS[item.type], False))
        return _join_pieces(pieces)

    async def _resolve_markdown_images(self, content: str) -> str:
        if "![" not in content:
            return content
        parts: List[str] = []
        cursor = 0
        for start, alt_end, end in _iter_md_images(content):
            target = content[alt_end + 2 : end - 1]
            broken = _BROKEN_IMAGE_DATA_PATTERN.search(target)
            if broken is not None:
                image_data = broken.group("data")
            else:
                local_path = _local_image_path(target)
                if not local_path:
                    continue
                image_data = f"file://{local_path}"
            alt = content[start + 2 : alt_end] or "图片"
            # alt 里的 `#宽px #高px` 保留，核心没给就用图片本身算出来的
            material = await self.material_image(image_data, _alt_size(alt))
            parts.append(content[cursor:start])
            if material is not None:
                parts.append(f"![{_with_size(alt, material.size)}]({material.url})")
            cursor = end
        if not parts:
            return content
        parts.append(content[cursor:])
        return "".join(parts)

    async def material_image(
        self,
        image_data: str,
        size: Optional[Tuple[int, int]] = None,
    ) -> Optional[MaterialImage]:
        public = public_image_url(image_data)
        if not public:
            raw = await file_bytes(image_data)
            if raw is None:
                logger.warning(f"[QQBotUID] 取不到图片内容，MD 里跳过这张图: {image_data[:64]}")
                return None
            linked = await core_upload_image(raw)
            if linked is not None:
                return MaterialImage(url=linked.url, size=size or linked.size)
            uploaded = await self.material.upload(raw)
            if uploaded is not None and size:
                return MaterialImage(url=uploaded.url, size=size)
            return uploaded
        if size:
            return MaterialImage(url=public, size=size)
        raw = await file_bytes(image_data)
        return MaterialImage(url=public, size=image_size(raw) if raw else None)

    @staticmethod
    def _attach_buttons(payload: Dict[str, Any], buttons: Optional[Message], use_buttons: bool) -> None:
        """按钮只在核心「默认发送按钮的平台列表」选中本平台时才带。"""
        if not use_buttons or buttons is None or not buttons.data:
            return
        data = buttons.data
        if isinstance(data, dict) and isinstance(data.get("content"), dict):
            payload["keyboard"] = data
            return
        rows = normalize_buttons(data)
        if rows:
            payload["keyboard"] = build_keyboard(rows)
