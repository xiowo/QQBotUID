from typing import Callable, Awaitable

from aiohttp import web

from gsuid_core.i18n import t
from gsuid_core.logger import logger

WebhookHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class WebhookServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._handlers: dict[tuple[str, str], WebhookHandler] = {}

    async def add(self, path: str, appid: str, handler: WebhookHandler) -> None:
        key = (path, appid)
        if key in self._handlers:
            logger.warning(
                t("[QQBotUID] Webhook 机器人重复注册，后者覆盖前者: {path} ({appid})", path=path, appid=appid)
            )
        self._handlers[key] = handler
        if self._runner is None:
            await self._start()
        logger.info(
            t(
                "[QQBotUID] Webhook 路由已挂载: {host}:{port}{path} ({appid})",
                host=self.host,
                port=self.port,
                path=path,
                appid=appid,
            )
        )

    async def remove(self, path: str, appid: str) -> None:
        key = (path, appid)
        if key in self._handlers:
            del self._handlers[key]
        if not self._handlers:
            await self._stop()

    async def _start(self) -> None:
        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.success(t("[QQBotUID] Webhook已启动: {host}:{port}", host=self.host, port=self.port))

    async def _stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
            logger.info(t("[QQBotUID] Webhook已停止: {host}:{port}", host=self.host, port=self.port))

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        if "X-Bot-Appid" not in request.headers or not request.headers["X-Bot-Appid"].strip():
            return web.json_response({"error": "missing X-Bot-Appid"}, status=400)
        key = (request.path, request.headers["X-Bot-Appid"].strip())
        if key not in self._handlers:
            logger.debug(
                t("[QQBotUID] 未知的 Webhook 路径或机器人，已返回 404: {path} ({appid})", path=key[0], appid=key[1])
            )
            return web.json_response({"error": "not found"}, status=404)
        return await self._handlers[key](request)


_SERVERS: dict[tuple[str, int], WebhookServer] = {}


def shared_server(host: str, port: int) -> WebhookServer:
    """同一组地址和端口只留一个实例，多个机器人共用它。"""
    key = (host, port)
    if key not in _SERVERS:
        _SERVERS[key] = WebhookServer(host, port)
    return _SERVERS[key]
