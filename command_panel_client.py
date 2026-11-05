import asyncio
from typing import Protocol
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable

import httpx


@dataclass(frozen=True, slots=True)
class PanelClientKey:
    appid: str
    secret: str = field(repr=False)
    api_base: str
    token_base: str


class PanelTokenClient(Protocol):
    async def client(self) -> httpx.AsyncClient: ...

    async def authorization(self) -> str: ...

    async def aclose(self) -> None: ...


class PanelClientPoolClosed(RuntimeError):
    pass


@dataclass(eq=False, slots=True)
class _ClientEntry:
    key: PanelClientKey
    client: PanelTokenClient
    users: int = 0
    retired: bool = False
    closing: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class PanelClientPool:
    def __init__(self) -> None:
        self._clients: dict[str, _ClientEntry] = {}
        self._retired: set[_ClientEntry] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _retire(self, entry: _ClientEntry) -> None:
        entry.retired = True
        self._retired.add(entry)

    async def _close_entry(self, entry: _ClientEntry) -> None:
        if entry.closing:
            await entry.closed.wait()
            return
        entry.closing = True
        try:
            await entry.client.aclose()
        finally:
            self._retired.discard(entry)
            entry.closed.set()

    @asynccontextmanager
    async def acquire(
        self, key: PanelClientKey, factory: Callable[[], PanelTokenClient]
    ) -> AsyncIterator[PanelTokenClient]:
        if self._closed:
            raise PanelClientPoolClosed("面板连接池已关闭")
        entry = self._clients[key.appid] if key.appid in self._clients else None
        stale: _ClientEntry | None = None
        if entry is None or entry.key != key:
            replacement = _ClientEntry(key, factory())
            if entry is not None:
                stale = entry
                self._retire(stale)
            entry = replacement
            self._clients[key.appid] = entry
        # 主事件循环内先登记使用者再 await，避免并发建池或关掉正在使用的连接。
        entry.users += 1
        try:
            if stale is not None and stale.users == 0:
                await self._close_entry(stale)
            yield entry.client
        finally:
            entry.users -= 1
            if entry.retired and entry.users == 0:
                await self._close_entry(entry)

    async def prune(self, valid: dict[str, PanelClientKey]) -> None:
        idle: list[_ClientEntry] = []
        for appid, entry in tuple(self._clients.items()):
            if appid in valid and entry.key == valid[appid]:
                continue
            del self._clients[appid]
            self._retire(entry)
            if entry.users == 0:
                idle.append(entry)
        await asyncio.gather(*(self._close_entry(entry) for entry in idle))

    async def invalidate(self, key: PanelClientKey) -> None:
        entry = self._clients[key.appid] if key.appid in self._clients else None
        if entry is None or entry.key != key:
            return
        del self._clients[key.appid]
        self._retire(entry)
        if entry.users == 0:
            await self._close_entry(entry)

    async def aclose(self) -> None:
        """停止借出新连接；在途请求释放后再关闭对应连接。"""
        self._closed = True
        for entry in self._clients.values():
            self._retire(entry)
        self._clients.clear()
        await asyncio.gather(*(self._close_entry(entry) for entry in tuple(self._retired) if entry.users == 0))
