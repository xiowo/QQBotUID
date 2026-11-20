import time
import asyncio
import contextlib
from typing import Any, Set, Dict, List, Tuple, Callable, Optional, Awaitable

from gsuid_core.logger import logger

from .config import proactive_interval_ms

ProactiveJob = Callable[[], Awaitable[Any]]

# 群内回复与私聊回复的被动
PASSIVE_WINDOW_SECONDS = 3600.0
PASSIVE_REPLY_LIMIT = 5
PASSIVE_TRACK_LIMIT = 20


class PassiveWindow:
    """按会话缓存最近入站的 msg_id。"""

    def __init__(
        self,
        window: float = PASSIVE_WINDOW_SECONDS,
        limit: int = PASSIVE_TRACK_LIMIT,
        reply_limit: int = PASSIVE_REPLY_LIMIT,
    ) -> None:
        self.window = window
        self.limit = limit
        self.reply_limit = max(int(reply_limit), 1)
        self._seen: Dict[str, Tuple[float, int, str]] = {}

    def mark(self, msg_id: str, scene: str = "") -> None:
        key = str(msg_id or "")
        if not key:
            return
        now = time.monotonic()
        scene_key = str(scene or "")
        used = self._seen.get(key, (now, 0, scene_key))[1]
        self._seen[key] = (now, used, scene_key)
        if len(self._seen) > self.limit:
            self._prune(now)

    def remaining(self, msg_id: str) -> int:
        """还能被动回复几次；0 表示必须降级主动。"""
        key = str(msg_id or "")
        if not key:
            return 0
        entry = self._seen.get(key)
        if entry is None:
            return 0
        marked, used, _ = entry
        if time.monotonic() - marked > self.window:
            self._seen.pop(key, None)
            return 0
        return max(self.reply_limit - used, 0)

    def take(self, msg_id: str, count: int = 1) -> bool:
        """预占被动额度；额度用完就从缓存池移出。"""
        need = max(int(count), 1)
        key = str(msg_id or "")
        if self.remaining(key) < need:
            return False
        marked, used, scene = self._seen[key]
        used += need
        if used >= self.reply_limit:
            self._seen.pop(key, None)
        else:
            self._seen[key] = (marked, used, scene)
        return True

    def pick_available(self, msg_id: str, scene: str) -> str:
        """从同会话缓存池里取入站时间最新且仍有额度的 ID。"""
        key = str(msg_id or "")
        if not key or not scene:
            return ""
        now = time.monotonic()
        best_key, best_ts = "", -1.0
        for candidate, (marked, used, candidate_scene) in self._seen.items():
            if candidate == key or candidate_scene != scene:
                continue
            if now - marked > self.window or used >= self.reply_limit:
                continue
            if marked > best_ts:
                best_key, best_ts = candidate, marked
        return best_key

    def acquire(self, msg_id: str, scene: str = "") -> str:
        """优先用原 ID；不可用时换同会话最近可用的 ID；都没有返回空串。"""
        key = str(msg_id or "")
        if not key:
            return ""
        if self.take(key):
            return key
        fallback = self.pick_available(key, scene)
        if fallback and self.take(fallback):
            return fallback
        return ""

    def _prune(self, now: float) -> None:
        for key in [k for k, (ts, _, _) in self._seen.items() if now - ts > self.window]:
            self._seen.pop(key, None)
        overflow = len(self._seen) - self.limit
        if overflow > 0:
            oldest = sorted(self._seen, key=lambda k: self._seen[k][0])[:overflow]
            for key in oldest:
                self._seen.pop(key, None)


class ProactiveLane:
    def __init__(self, scene: str, interval_ms: Optional[int] = None) -> None:
        self.scene = scene
        self._interval_ms = interval_ms
        self._queue: List[ProactiveJob] = []
        self._worker: Optional["asyncio.Task[None]"] = None
        self._tasks: Set["asyncio.Task[Any]"] = set()
        self._last_sent = 0.0

    def submit(self, job: ProactiveJob, front: bool = False) -> None:
        if front:
            self._queue.insert(0, job)
        else:
            self._queue.append(job)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain(), name=f"QQBotUID.proactive.{self.scene}")

    def interval_ms(self) -> int:
        """本车道的放行间隔；构造时传了就用传的，否则读配置。"""
        if self._interval_ms is not None:
            return max(int(self._interval_ms), 0)
        return proactive_interval_ms(self.scene)

    async def _drain(self) -> None:
        try:
            while self._queue:
                job = self._queue.pop(0)
                wait = self.interval_ms() / 1000 - (time.monotonic() - self._last_sent)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_sent = time.monotonic()
                task = asyncio.create_task(self._run(job))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            if not self._queue:
                self._worker = None

    async def _run(self, job: ProactiveJob) -> None:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[QQBotUID] 主动消息发送异常({self.scene}): {e}")

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        dropped = len(self._queue)
        self._queue.clear()
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if dropped:
            logger.warning(f"[QQBotUID] 主动消息队列({self.scene})关闭，丢弃 {dropped} 条未放行的消息")
        tasks, self._tasks = self._tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class ProactiveDispatcher:
    def __init__(self) -> None:
        self.direct = ProactiveLane("direct")
        self.group = ProactiveLane("group")
        # 频道先不做主动消息间隔：间隔固定 0，提交即发，不提供配置项
        self.channel = ProactiveLane("channel", 0)

    def lane_for(self, target_type: Optional[str]) -> ProactiveLane:
        if target_type == "direct":
            return self.direct
        if target_type in {"channel", "sub_channel"}:
            return self.channel
        return self.group

    def submit(self, target_type: Optional[str], job: ProactiveJob, front: bool = False) -> None:
        self.lane_for(target_type).submit(job, front)

    async def close(self) -> None:
        await asyncio.gather(self.direct.close(), self.group.close(), self.channel.close())
