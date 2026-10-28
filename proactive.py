import time
import asyncio
from typing import Any, Dict, Callable, Optional, Awaitable

from gsuid_core.logger import logger

ProactiveJob = Callable[[], Awaitable[Any]]

PASSIVE_WINDOW_SECONDS = 300.0


class PassiveWindow:
    """记住最近收到的入站 msg_id；只有窗口内的才允许走被动回复。"""

    def __init__(self, window: float = PASSIVE_WINDOW_SECONDS, limit: int = 512) -> None:
        self.window = window
        self.limit = limit
        self._seen: Dict[str, float] = {}

    def mark(self, msg_id: str) -> None:
        key = str(msg_id or "")
        if not key:
            return
        now = time.monotonic()
        self._seen[key] = now
        if len(self._seen) > self.limit:
            self._prune(now)

    def usable(self, msg_id: str) -> bool:
        """msg_id过期，固定落到主动队列。"""
        key = str(msg_id or "")
        if not key:
            return False
        marked = self._seen.get(key)
        if marked is None:
            return False
        if time.monotonic() - marked > self.window:
            self._seen.pop(key, None)
            return False
        return True

    def _prune(self, now: float) -> None:
        for key in [k for k, ts in self._seen.items() if now - ts > self.window]:
            self._seen.pop(key, None)


class ProactiveLane:
    def __init__(self, name: str, interval_ms: int) -> None:
        self.name = name
        self.interval = max(int(interval_ms), 0) / 1000
        self._queue: "asyncio.Queue[ProactiveJob]" = asyncio.Queue()
        self._worker: Optional["asyncio.Task[None]"] = None
        self._last_sent: Optional[float] = None

    async def submit(self, job: ProactiveJob) -> Any:
        if self.interval <= 0:
            return await job()
        self._queue.put_nowait(job)
        self._ensure_worker()
        return None

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._loop(), name=f"QQBotUID.proactive.{self.name}")

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job = await self._queue.get()
            try:
                await self._wait_turn(loop)
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[QQBotUID] 主动消息发送异常({self.name}): {e}")
            finally:
                self._last_sent = loop.time()
                self._queue.task_done()

    async def _wait_turn(self, loop: "asyncio.AbstractEventLoop") -> None:
        if self._last_sent is None:
            return
        gap = self._last_sent + self.interval - loop.time()
        if gap > 0:
            await asyncio.sleep(gap)

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


class ProactiveDispatcher:
    def __init__(self, direct_interval_ms: int, group_interval_ms: int) -> None:
        self.direct = ProactiveLane("direct", direct_interval_ms)
        self.group = ProactiveLane("group", group_interval_ms)

    def lane_for(self, target_type: Optional[str]) -> ProactiveLane:
        return self.direct if target_type == "direct" else self.group

    async def submit(self, target_type: Optional[str], job: ProactiveJob) -> Any:
        return await self.lane_for(target_type).submit(job)

    async def close(self) -> None:
        await asyncio.gather(self.direct.close(), self.group.close())
