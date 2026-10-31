import asyncio
from typing import Any, Optional

import websockets
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosed

from gsuid_core.logger import logger
from gsuid_core.models import MessageReceive

from .core_bridge import TargetBridge

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
MAX_FRAME_BYTES: Optional[int] = None


class RemoteBridge(TargetBridge):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self._ws: Optional[Any] = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._worker: Optional[asyncio.Task[None]] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._disconnects = 0

    @property
    def _peer(self) -> str:
        return self.url

    def maintenance_active(self, threshold: int = 1) -> bool:
        if self._closed or self._ws is not None:
            return False
        return self._disconnects >= max(int(threshold), 1)

    async def attach(self) -> None:
        """起后台任务维持连接，不阻塞启动。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain_forever(), name="QQBotUID.remote-dispatch"
            )
        if self._reader is not None and not self._reader.done():
            return
        self._closed = False
        self._reader = asyncio.create_task(self._forever(), name="QQBotUID.remote-bridge")

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
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
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
                        self._queue.put_nowait(raw)
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

    async def _drain_forever(self) -> None:
        while True:
            raw = await self._queue.get()
            try:
                await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[QQBotUID] 下发帧处理异常，已跳过: {e}")
            finally:
                self._queue.task_done()

    async def _dispatch(self, raw: Any) -> None:
        await self._deliver(raw.encode() if isinstance(raw, str) else raw)
