from typing import Dict, Tuple, Callable, Optional, Awaitable

from aiohttp import web

from gsuid_core.logger import logger

WebhookHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class WebhookServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._runner: Optional[web.AppRunner] = None
        self._handlers: Dict[str, WebhookHandler] = {}

    async def add(self, path: str, handler: WebhookHandler) -> None:
        if path in self._handlers:
            logger.warning(f"[QQBotUID] Webhook 路径重复注册，后者覆盖前者: {path}")
        self._handlers[path] = handler
        if self._runner is None:
            await self._start()
        logger.info(f"[QQBotUID] Webhook 路由已挂载: {self.host}:{self.port}{path}")

    async def remove(self, path: str) -> None:
        self._handlers.pop(path, None)
        if not self._handlers:
            await self._stop()

    async def _start(self) -> None:
        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.success(f"[QQBotUID] Webhook已启动: {self.host}:{self.port}")

    async def _stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
            logger.info(f"[QQBotUID] Webhook已停止: {self.host}:{self.port}")

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        handler = self._handlers.get(request.path)
        if handler is None:
            logger.debug(f"[QQBotUID] 未知的 Webhook 路径，已返回 404: {request.path}")
            return web.json_response({"error": "not found"}, status=404)
        return await handler(request)


_SERVERS: Dict[Tuple[str, int], WebhookServer] = {}


def shared_server(host: str, port: int) -> WebhookServer:
    """同一组 地址/端口 只留一个实例，多个机器人共用它。"""
    key = (host, port)
    server = _SERVERS.get(key)
    if server is None:
        server = WebhookServer(host, port)
        _SERVERS[key] = server
    return server
