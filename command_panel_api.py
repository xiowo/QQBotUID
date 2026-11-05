from __future__ import annotations

import re
import asyncio
from typing import TYPE_CHECKING, Literal

import httpx
from fastapi import Request
from pydantic import ValidationError

if TYPE_CHECKING:
    from gsuid_core.webconsole.plugin_page import PluginAPI

    from .config import QQBotUIDConfig
    from .bots_store import BotRow

from .command_panel import (
    SCOPES,
    PanelModel,
    PanelResult,
    CreatePanel,
    UpdatePanel,
    UpdateTargets,
    decode_response,
    failure,
)
from .global_menu import UpdateMenu
from .command_panel_client import PanelClientKey, PanelClientPoolClosed


def register_routes(api: PluginAPI) -> None:
    api.get("/bots/{appid}/panels")(list_panels)
    api.post("/bots/{appid}/panels")(create_panel)
    api.get("/bots/{appid}/panels/{panel_id}")(get_panel)
    api.put("/bots/{appid}/panels/{panel_id}")(update_panel)
    api.delete("/bots/{appid}/panels/{panel_id}")(delete_panel)
    api.put("/bots/{appid}/panels/{panel_id}/target")(update_panel_targets)
    api.get("/bots/{appid}/menu")(get_menu)
    api.put("/bots/{appid}/menu")(update_menu)


def panel_path(panel_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", panel_id):
        return None
    return f"/v2/panels/{panel_id}"


def redact_panel_response(text: str, secret: str, authorization: str) -> str:
    credentials = (secret, authorization, authorization.removeprefix("QQBot "))
    for credential in sorted(credentials, key=len, reverse=True):
        if credential:
            text = text.replace(credential, "[已隐藏]")
    return text


def _panel_client_keys(rows: list[BotRow], config: QQBotUIDConfig) -> dict[str, PanelClientKey]:
    return {
        row.appid: PanelClientKey(row.appid, row.secret, config.api_base, config.token_base)
        for row in rows
        if row.secret
    }


async def prune_panel_clients() -> None:
    from .config import QQBotUIDConfig
    from .startup import qqbotuid_runtime
    from .bots_store import load_rows

    rows = await asyncio.to_thread(load_rows)
    config = await asyncio.to_thread(QQBotUIDConfig.from_config)
    await qqbotuid_runtime.panel_clients.prune(_panel_client_keys(rows, config))


async def panel_request(
    appid: str,
    method: Literal["GET", "POST", "PUT", "DELETE"],
    path: str,
    *,
    params: dict[str, str | int] | None = None,
    body: PanelModel | None = None,
) -> PanelResult:
    from .config import QQBotUIDConfig
    from .qq_api import QQTokenManager
    from .startup import qqbotuid_runtime
    from .bots_store import load_rows

    rows = await asyncio.to_thread(load_rows)
    row = next((row for row in rows if row.appid == appid), None)
    if row is None:
        return failure("机器人不存在，请刷新 Bot 列表")
    if not row.secret:
        return failure("机器人未设置 Secret，请先扫码绑定")
    config = await asyncio.to_thread(QQBotUIDConfig.from_config)
    config.appid = row.appid
    config.secret = row.secret
    pool = qqbotuid_runtime.panel_clients
    keys = _panel_client_keys(rows, config)
    try:
        await pool.prune(keys)
        async with pool.acquire(keys[appid], lambda: QQTokenManager(config)) as manager:
            client = await manager.client()
            authorization = await manager.authorization()
            response = await client.request(
                method,
                path,
                params=params,
                headers={"Authorization": authorization},
                json=body.payload() if body is not None else None,
            )
            if response.status_code == 401:
                await pool.invalidate(keys[appid])
            result = decode_response(response)
            result["msg"] = redact_panel_response(result["msg"], row.secret, authorization)
            return result
    except PanelClientPoolClosed:
        return failure("面板服务正在重载，请稍后重试")
    except httpx.TimeoutException:
        return failure("QQ 接口请求超时，请刷新确认远端状态后再操作")
    except httpx.HTTPError:
        return failure("无法连接 QQ 接口，请检查网络和接口地址")
    except (RuntimeError, ValueError):
        return failure("QQ 接口认证或响应解析失败，请检查机器人凭据和接口地址")


async def mutate_panel(
    appid: str,
    method: Literal["POST", "PUT"],
    path: str,
    request: Request,
    model: type[PanelModel],
) -> PanelResult:
    try:
        body = model.model_validate_json(await request.body())
    except ValidationError as error:
        first = error.errors(include_input=False, include_context=False, include_url=False)[0]
        message: str = first["msg"]
        location = ".".join(str(part) for part in first["loc"])
        return failure(f"配置不合法（{location or '请求体'}）：{message.removeprefix('Value error, ')}")
    return await panel_request(appid, method, path, body=body)


async def list_panels(appid: str, scope: str = "", cursor: str = "", limit: str = "20") -> PanelResult:
    if scope not in SCOPES:
        return failure("请选择单聊、群聊、文字子频道或频道私信")
    if len(limit) > 2 or not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 50:
        return failure("每页数量必须为 1 到 50")
    if len(cursor) > 2048:
        return failure("分页游标过长")
    params: dict[str, str | int] = {"scope": scope, "limit": int(limit)}
    if cursor:
        params["cursor"] = cursor
    return await panel_request(appid, "GET", "/v2/panels", params=params)


async def create_panel(appid: str, request: Request) -> PanelResult:
    return await mutate_panel(appid, "POST", "/v2/panels", request, CreatePanel)


async def get_panel(appid: str, panel_id: str) -> PanelResult:
    path = panel_path(panel_id)
    if path is None:
        return failure("面板 ID 不合法")
    return await panel_request(appid, "GET", path)


async def update_panel(appid: str, panel_id: str, request: Request) -> PanelResult:
    path = panel_path(panel_id)
    if path is None:
        return failure("面板 ID 不合法")
    return await mutate_panel(appid, "PUT", path, request, UpdatePanel)


async def delete_panel(appid: str, panel_id: str) -> PanelResult:
    path = panel_path(panel_id)
    if path is None:
        return failure("面板 ID 不合法")
    return await panel_request(appid, "DELETE", path)


async def update_panel_targets(appid: str, panel_id: str, request: Request) -> PanelResult:
    path = panel_path(panel_id)
    if path is None:
        return failure("面板 ID 不合法")
    return await mutate_panel(appid, "PUT", path + "/target", request, UpdateTargets)


async def get_menu(appid: str) -> PanelResult:
    return await panel_request(appid, "GET", "/v2/menu")


async def update_menu(appid: str, request: Request) -> PanelResult:
    return await mutate_panel(appid, "PUT", "/v2/menu", request, UpdateMenu)
