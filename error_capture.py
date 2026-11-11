import re
import time
import asyncio
from typing import TYPE_CHECKING
from collections import deque

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError
from httpx._types import AuthTypes
from httpx._client import USE_CLIENT_DEFAULT, UseClientDefault

from gsuid_core.logger import logger

from .bots_store import load_rows
from .error_store import QQBotUIDAPIError

if TYPE_CHECKING:
    from .config import QQBotUIDConfig

_JSON = TypeAdapter(JsonValue)
_CONTENT_LIMIT = 2000
_PENDING_LIMIT = 256
_BATCH_SIZE = 32
_PRUNE_INTERVAL = 3600.0


def _json_object(raw: bytes) -> dict[str, JsonValue] | None:
    try:
        value = _JSON.validate_json(raw)
    except ValidationError:
        return None
    return value if isinstance(value, dict) else {}


def _text(data: dict[str, JsonValue], key: str) -> str:
    value = data[key] if key in data else None
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _redact(text: str, credentials: tuple[str, ...], limit: int) -> str:
    for value in sorted(credentials, key=len, reverse=True):
        if value:
            text = text.replace(value, "[已隐藏]")
    text = re.sub(
        r'(?i)(access_token|clientSecret|secret|authorization)([\s"\':=]+)([^\s,"\'}]+)',
        r"\1\2[已隐藏]",
        text,
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _message_content(request: httpx.Request) -> str:
    if not request.url.path.endswith(("/messages", "/stream_messages")):
        return ""
    payload = _json_object(request.content)
    if payload is None:
        return ""
    text = _text(payload, "content") or _text(payload, "content_raw")
    markdown = payload["markdown"] if "markdown" in payload else None
    if isinstance(markdown, dict):
        text = text or _text(markdown, "content") or "[Markdown 模板]"
    if "media" in payload or "image" in payload:
        text += " [媒体]"
    if "keyboard" in payload:
        text += " [按钮]"
    return text.strip()


class ErrorRecorder:
    def __init__(self) -> None:
        self._pending: deque[QQBotUIDAPIError] = deque()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_prune: float | None = None

    def start(self) -> None:
        self._closed = False

    def enqueue(self, row: QQBotUIDAPIError) -> None:
        if self._closed:
            return
        if len(self._pending) >= _PENDING_LIMIT:
            logger.warning("[QQBotUID] 错误记录队列已满，本次记录已跳过")
            return
        self._pending.append(row)
        self._start_writer()

    def _start_writer(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._persist(), name="QQBotUID.error-record")
            self._task.add_done_callback(self._record_done)

    async def _persist(self) -> None:
        while self._pending:
            batch = [self._pending.popleft() for _ in range(min(_BATCH_SIZE, len(self._pending)))]
            identities = {bot.appid: (bot.bot_name, bot.self_id) for bot in await asyncio.to_thread(load_rows)}
            for row in batch:
                if row.appid in identities:
                    row.bot_name, row.bot_self_id = identities[row.appid]
            now = time.monotonic()
            prune = self._last_prune is None or now - self._last_prune >= _PRUNE_INTERVAL
            await QQBotUIDAPIError.record_batch(batch, prune=prune)
            if prune:
                self._last_prune = now

    def _record_done(self, task: asyncio.Task[None]) -> None:
        self._task = None
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning(f"[QQBotUID] 错误记录写入失败: {type(error).__name__}")
        if self._pending and not self._closed:
            self._start_writer()

    async def aclose(self) -> None:
        self._closed = True
        task = self._task
        if task is not None:
            _, pending = await asyncio.wait({task}, timeout=5.0)
            if pending:
                logger.warning("[QQBotUID] 错误记录收尾超时，剩余记录已跳过")
                task.cancel()
                await asyncio.wait({task}, timeout=1.0)
        self._pending.clear()


error_recorder = ErrorRecorder()


async def close_error_records() -> None:
    await error_recorder.aclose()


class ErrorRecordingClient(httpx.AsyncClient):
    def __init__(self, config: "QQBotUIDConfig", *, base_url: str, timeout: float) -> None:
        super().__init__(base_url=base_url, timeout=timeout)
        self._error_config = config

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,
        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        try:
            response = await super().send(request, stream=stream, auth=auth, follow_redirects=follow_redirects)
            await response.aread()
        except httpx.HTTPError as error:
            self._record(request, status=0, message=type(error).__name__)
            raise
        parsed = _json_object(response.content) if response.content else {}
        data = parsed if parsed is not None else {}
        code = data["code"] if "code" in data else None
        err_code = data["err_code"] if "err_code" in data else None
        if parsed is not None and response.is_success and code in (None, 0, "0") and err_code in (None, 0, "0"):
            return response
        trace_id = _text(data, "trace_id")
        for header in ("X-Tps-Trace-ID", "X-Trace-ID"):
            if not trace_id and header in response.headers:
                trace_id = response.headers[header]
        message = (
            "QQ 接口返回了非 JSON 响应"
            if parsed is None
            else _text(data, "message") or _text(data, "msg") or f"HTTP {response.status_code}"
        )
        self._record(
            request,
            status=response.status_code,
            code=_text(data, "code"),
            err_code=_text(data, "err_code"),
            message=message,
            trace_id=trace_id,
        )
        return response

    def _record(
        self,
        request: httpx.Request,
        *,
        status: int,
        message: str,
        code: str = "",
        err_code: str = "",
        trace_id: str = "",
    ) -> None:
        authorization = request.headers["Authorization"] if "Authorization" in request.headers else ""
        credentials = (self._error_config.secret, authorization, authorization.removeprefix("QQBot "))
        row = QQBotUIDAPIError(
            appid=self._error_config.appid,
            bot_self_id=self._error_config.self_id,
            method=request.method,
            path=_redact(request.url.path, credentials, 512),
            status=status,
            code=_redact(code, credentials, 64),
            err_code=_redact(err_code, credentials, 64),
            message=_redact(message, credentials, 1000),
            trace_id=_redact(trace_id, credentials, 256),
            content=_redact(_message_content(request), credentials, _CONTENT_LIMIT),
            created_at=time.time(),
        )
        error_recorder.enqueue(row)
