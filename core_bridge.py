import asyncio
from typing import Dict, Callable, Optional, Protocol, Awaitable

from msgspec import DecodeError, ValidationError, json as msgjson
from starlette.types import Scope, Message
from starlette.websockets import WebSocket

from gsuid_core.bot import _Bot
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.models import MessageSend, MessageReceive
from gsuid_core.shutdown import shutdown_event


class CoreTarget(Protocol):
    @property
    def self_id(self) -> str: ...

    async def deliver(self, send: MessageSend) -> None: ...


class TargetBridge:
    def __init__(self) -> None:
        self._targets: Dict[str, CoreTarget] = {}

    @property
    def _peer(self) -> str:
        raise NotImplementedError

    def add_target(self, target: CoreTarget) -> None:
        self._targets[target.self_id] = target
        logger.info(f"[QQBotUID] 机器人 {target.self_id} 已挂到核心连接 {self._peer}")

    def remove_target(self, target: CoreTarget) -> None:
        for key, item in list(self._targets.items()):
            if item is target:
                self._targets.pop(key, None)

    async def _deliver(self, body: bytes) -> None:
        try:
            send = msgjson.decode(body, type=MessageSend)
        except (DecodeError, ValidationError) as e:
            logger.warning(f"[QQBotUID] 核心下发的帧无法解析，已跳过: {e}")
            return
        if send.bot_self_id:
            target = self._targets.get(send.bot_self_id)
            if target is None:
                logger.debug(f"[QQBotUID] 找不到 bot_self_id={send.bot_self_id} 的机器人，已丢弃")
                return
            await target.deliver(send)
            return
        for item in list(self._targets.values()):
            await item.deliver(send)


def _build_scope(bot_id: str) -> Scope:
    path = f"/ws/{bot_id}"
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 0),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
        "state": {},
    }


class CoreSocket:
    def __init__(self, bot_id: str, on_send: Callable[[bytes], Awaitable[None]]) -> None:
        self._on_send = on_send
        self._closed = asyncio.Event()
        self._handshaked = False
        self.websocket = WebSocket(_build_scope(bot_id), self._receive, self._send)

    async def _receive(self) -> Message:
        if not self._handshaked:
            self._handshaked = True
            return {"type": "websocket.connect"}
        await self._closed.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def _send(self, message: Message) -> None:
        if message["type"] != "websocket.send":
            return
        payload = message.get("bytes")
        if isinstance(payload, (bytes, bytearray)):
            await self._on_send(bytes(payload))
            return
        text = message.get("text")
        if isinstance(text, str):
            await self._on_send(text.encode())

    def shutdown(self) -> None:
        self._closed.set()


class CoreBridge(TargetBridge):
    def __init__(self, route_bot_id: str) -> None:
        from gsuid_core.handler import _INBOUND_SLOTS

        super().__init__()
        self.route_bot_id = route_bot_id
        self._slots = asyncio.Semaphore(_INBOUND_SLOTS)
        self._bot: Optional[_Bot] = None
        self._socket: Optional[CoreSocket] = None
        self._process: Optional[asyncio.Task[None]] = None
        self._process_bot: Optional[_Bot] = None

    @property
    def _peer(self) -> str:
        return self.route_bot_id

    async def attach(self) -> CoreSocket:
        socket = CoreSocket(self.route_bot_id, self._deliver)
        self._bot = await gss.connect(socket.websocket, self.route_bot_id)
        self._socket = socket
        self._ensure_process(self._bot)
        logger.info(f"[QQBotUID] 已注册到核心，连接标识 {self.route_bot_id}")
        return socket

    async def detach(self, socket: CoreSocket) -> None:
        if self._socket is not socket:
            return
        self._socket = None
        socket.shutdown()
        await gss.disconnect(self.route_bot_id, socket.websocket)
        logger.info("[QQBotUID] 已从核心注销该连接")

    async def shutdown(self) -> None:
        self._targets.clear()
        socket = self._socket
        if socket is not None:
            await self.detach(socket)
        process = self._process
        self._process = None
        self._process_bot = None
        if process is not None and not process.done():
            process.cancel()
            try:
                await process
            except asyncio.CancelledError:
                pass
        self._bot = None

    async def push(self, msg: MessageReceive) -> None:
        bot = self._bot
        if bot is None:
            return
        if bot.resolve_recall(msg):
            return

        from gsuid_core.handler import run_inbound_event

        bot._add_bg_task(asyncio.create_task(run_inbound_event(bot, msg, self._slots)))

    def _ensure_process(self, bot: _Bot) -> None:
        if self._process is not None and not self._process.done() and self._process_bot is bot:
            return
        if self._process is not None and not self._process.done():
            self._process.cancel()
        self._process_bot = bot
        self._process = asyncio.create_task(
            bot._process(shutdown_event),
            name=f"QQBotUID.process.{bot.bot_id}",
        )
