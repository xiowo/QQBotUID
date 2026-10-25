import re
import json
import time
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
from .config import QQBotUIDConfig, update_bot_name
from .qq_api import (
    QQSender,
    MaterialImage,
    QQMateriaHost,
    QQTokenManager,
    brief_text,
    image_size,
    image_bytes,
    public_image_url,
    core_upload_image,
)
from .convert import (
    send_endpoint,
    build_keyboard,
    is_bot_message,
    recall_endpoint,
    event_to_receive,
    normalize_buttons,
    is_reference_message,
    messages_to_plain_text,
)
from .webhook import WebhookServer, shared_server
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

INTENTS = (1 << 0) | (1 << 10) | (1 << 12) | (1 << 24) | (1 << 25) | (1 << 26) | (1 << 27) | (1 << 30)
UA_PLUGIN_TAG = "QQBotPlugin/1.7.2"
CLIENT_VERSION = "9.9.9"


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


def _image_segments(content: List[Message]) -> List[Tuple[str, Optional[Tuple[int, int]]]]:
    segments: List[Tuple[str, Optional[Tuple[int, int]]]] = []
    for item in content:
        if item.type == "image" and isinstance(item.data, str):
            segments.append((item.data, None))
        elif item.type == "image_size" and segments and segments[-1][1] is None:
            data = item.data
            if isinstance(data, (tuple, list)) and len(data) == 2:
                segments[-1] = (segments[-1][0], (int(data[0]), int(data[1])))
    return segments


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
    return _MD_AT_PATTERN.sub("", text) if target_type == "direct" else text


def _duration_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _markdown_body(markdown: Optional[Message], text: str, send: MessageSend) -> Dict[str, Any]:
    """markdown 消息"""
    if markdown is not None and isinstance(markdown.data, dict):
        body = dict(markdown.data)
        content = str(markdown.data.get("content") or "")
    else:
        body = {}
        content = str(markdown.data) if markdown is not None and markdown.data is not None else text
    content = _MD_AT_PATTERN.sub("", content)
    mentions = [str(item.data) for item in send.content or [] if item.type == "at" and item.data]
    if mentions and send.target_type == "group":
        content = "".join(f'<qqbot-at-user id="{mention}" />' for mention in mentions) + content
    body["content"] = content
    return body


def _user_agent(client_name: str) -> str:
    runtime = f"Python/{platform.python_version()}; {platform.system().lower()}"
    name = client_name.strip() or "QQBotUID"
    return f"{UA_PLUGIN_TAG} ({runtime}; {name}/{CLIENT_VERSION})"


class QQBotUID:
    def __init__(self, config: QQBotUIDConfig, bridge: CoreBridge):
        self.config = config
        self.bridge = bridge
        self.tm = QQTokenManager(config)
        self.sender = QQSender(self.tm)
        self.material = QQMateriaHost(self.tm)
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

    async def start(self) -> None:
        self.config.validate()
        self._closed = False
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
        await self.tm.aclose()

    async def deliver(self, send: MessageSend) -> None:
        """核心下发的一帧：确认收件人是我，再转成 QQ 官方接口调用。"""
        if send.bot_self_id and send.bot_self_id != self.self_id:
            return
        message_id = await self.handle_core_send(send)
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
        receive = event_to_receive(payload, self.config.platform_bot_id, self.config.self_id)
        if receive is not None:
            # debug 下打印原始事件
            logger.debug(
                t("[QQBotUID] WS 事件 {event_type}: {raw}", event_type=event_type, raw=brief_text(payload, 1500))
            )
            if self._skip_other_bot(payload):
                return
            self._warn_unresolved_reference(payload, receive)
            await self.bridge.push(receive)

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
        receive = event_to_receive(body, self.config.platform_bot_id, self.config.self_id)
        if receive is not None:
            logger.debug(
                t("[QQBotUID] Webhook 事件 {event_type}: {raw}", event_type=body.get("t"), raw=brief_text(body, 1500))
            )
            if not self._skip_other_bot(body):
                await self.bridge.push(receive)
        if body.get("t") == "INTERACTION_CREATE":
            return web.json_response({"op": OP_CALLBACK_ACK, "code": 0})
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

    async def handle_core_send(self, send: MessageSend) -> Optional[Union[str, List[str]]]:
        if not send.content:
            return None
        first = send.content[0]
        if send.bot_id not in {self.config.platform_bot_id, self.config.route_bot_id}:
            logger.debug(t("[QQBotUID] 忽略非官机平台下发消息: bot_id={bot_id}", bot_id=send.bot_id))
            return None
        if first.type == "excute_delete_message" and isinstance(first.data, dict):
            mid = str(first.data.get("message_id") or "")
            endpoint = recall_endpoint(send, mid) if mid else None
            return mid if endpoint and await self.sender.delete(endpoint) else None
        if first.type == "excute_ban_user" and isinstance(first.data, dict):
            await self.handle_ban(first.data)
            return None
        node = next((item for item in send.content if item.type == "node" and isinstance(item.data, list)), None)
        if node is not None:
            return await self.send_node_items(send, node.data)
        endpoint = send_endpoint(send)
        if endpoint is None:
            logger.warning(
                t(
                    "[QQBotUID] 无法推断发送目标: {target_type}/{target_id}",
                    target_type=send.target_type,
                    target_id=send.target_id,
                )
            )
            return None
        try:
            payload = await self.build_send_payload(send)
            markdown_body = payload.get("markdown")
            has_markdown = isinstance(markdown_body, dict) and bool(str(markdown_body.get("content") or "").strip())
            if not payload.get("content") and not has_markdown and not payload.get("media"):
                logger.warning(t("[QQBotUID] 没有可发送的内容，已跳过: {target_id}", target_id=send.target_id))
                return None
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
                return None
            return str(data.get("id") or data.get("msg_id") or data.get("message_id") or "") or None
        finally:
            self.material.schedule_cleanup()

    async def send_node_items(self, send: MessageSend, items: Any) -> Optional[List[str]]:
        """node 降级多条消息发送"""
        message_ids: List[str] = []
        for item in normalize_node_items(items):
            part = MessageSend(
                bot_id=send.bot_id,
                bot_self_id=send.bot_self_id,
                msg_id=send.msg_id,
                target_type=send.target_type,
                target_id=send.target_id,
                content=[item],
            )
            message_id = await self.handle_core_send(part)
            if isinstance(message_id, list):
                message_ids.extend(message_id)
            elif message_id:
                message_ids.append(message_id)
        return message_ids or None

    async def build_send_payload(self, send: MessageSend) -> Dict[str, Any]:
        assert send.content is not None
        platform = self.config.platform_bot_id
        use_md = _platform_selected("SendMDPlatform", platform)
        use_buttons = _platform_selected("SendButtonsPlatform", platform)

        markdown = next((m for m in send.content if m.type in {"markdown", "template_markdown"}), None)
        image = next((m for m in send.content if m.type == "image" and isinstance(m.data, str)), None)
        buttons = next((m for m in send.content if m.type in {"buttons", "template_buttons"}), None)
        text = _strip_at(messages_to_plain_text(send.content), send.target_type or "")

        payload: Dict[str, Any] = {"msg_seq": _msg_seq()}
        if send.msg_id:
            payload["msg_id"] = send.msg_id
        reply_id = next((m for m in send.content if m.type == "reply_id" and m.data), None)
        if reply_id is not None:
            payload["message_reference"] = {
                "message_id": str(reply_id.data),
                "ignore_get_message_error": True,
            }

        if use_md:
            payload.update({"msg_type": 2, "markdown": await self._build_markdown(send, markdown, text)})
            self._attach_buttons(payload, buttons, use_buttons)
            return payload

        if image and send.target_id:
            file_info = await self.sender.upload_media(
                send.target_type or "direct",
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
        body = _markdown_body(markdown, text, send)
        content = await self._resolve_markdown_images(str(body.get("content") or ""))
        for image_data, size in _image_segments(send.content or []):
            material = await self.material_image(image_data, size)
            if material is None:
                continue
            alt = _with_size("图片", material.size)
            content = f"{content}\n\n![{alt}]({material.url})" if content else f"![{alt}]({material.url})"
        body["content"] = content
        return body

    async def _resolve_markdown_images(self, content: str) -> str:
        if "![" not in content:
            return content
        parts: List[str] = []
        cursor = 0
        for start, alt_end, end in _iter_md_images(content):
            target = content[alt_end + 2 : end - 1]
            broken = _BROKEN_IMAGE_DATA_PATTERN.search(target)
            if broken is None:
                continue
            alt = content[start + 2 : alt_end] or "图片"
            # alt 里的 `#宽px #高px` 保留，核心没给就用图片本身算出来的
            material = await self.material_image(broken.group("data"), _alt_size(alt))
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
            raw = await image_bytes(image_data)
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
        raw = await image_bytes(image_data)
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
