import asyncio
from typing import Any, Dict, Optional

import websockets
from msgspec import DecodeError, ValidationError, json as msgjson
from websockets.exceptions import ConnectionClosed

from gsuid_core.logger import logger
from gsuid_core.models import MessageSend, MessageReceive

from .core_bridge import CoreTarget

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
MAX_FRAME_BYTES: Optional[int] = None


class RemoteBridge:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: Optional[Any] = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._targets: Dict[str, CoreTarget] = {}
        self._closed = False
        self._disconnects = 0

    def maintenance_active(self, threshold: int = 1) -> bool:
        if self._closed or self._ws is not None:
            return False
        return self._disconnects >= max(int(threshold), 1)

    async def attach(self) -> None:
        """起后台任务维持连接，不阻塞启动。"""
        if self._reader is not None and not self._reader.done():
            return
        self._closed = False
        self._reader = asyncio.create_task(self._forever(), name="QQBotUID.remote-bridge")

    def add_target(self, target: CoreTarget) -> None:
        self._targets[target.self_id] = target
        logger.info(f"[QQBotUID] 机器人 {target.self_id} 已挂到核心连接 {self.url}")

    def remove_target(self, target: CoreTarget) -> None:
        for key, item in list(self._targets.items()):
            if item is target:
                self._targets.pop(key, None)

    async def push(self, msg: MessageReceive) -> None:
        ws = self._ws
        if ws is None:
            logger.debug("[QQBotUID] 核心还没连上，入站消息已丢弃")
            return
        try:
            await ws.send(msgjson.encode(msg))
        except ConnectionClosed:
            logger.warning("[QQBotUID] 核心连接已断开，入站消息已丢弃")

    async def shutdown(self) -> None:
        self._closed = True
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        self._targets.clear()

    async def _forever(self) -> None:
        delay = RECONNECT_MIN
        while not self._closed:
            try:
                async with websockets.connect(self.url, max_size=MAX_FRAME_BYTES) as ws:
                    self._ws = ws
                    self._disconnects = 0
                    delay = RECONNECT_MIN
                    logger.success(f"[QQBotUID] 已连上核心: {self.url}")
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[QQBotUID] 核心连接断开，{delay:.0f}s 后重试: {e}")
            finally:
                self._ws = None
                if not self._closed:
                    self._disconnects += 1
            if self._closed:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _dispatch(self, raw: Any) -> None:
        body = raw.encode() if isinstance(raw, str) else raw
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
