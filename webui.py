import base64
from io import BytesIO
from typing import Any
from pathlib import Path
from dataclasses import replace

import qrcode
from qrcode.image.pil import PilImage

from gsuid_core.logger import logger
from gsuid_core.webconsole.plugin_page import PluginAPI, register_plugin_page

from .config import QQBotUIDConfig, display_mode
from .qq_api import fetch_bot_profile
from .qr_bind import poll_bind_task, create_bind_task
from .startup import qqbotuid_runtime
from .bots_store import BotRow, load_rows, save_rows, save_bot_profile, upsert_bound_bot

WEB_DIR = Path(__file__).with_name("web")

# 群聊 / 频道添加扫码
GROUP_SHARE_URL = "https://qun.qq.com/qunpro/robot/qunshare?robot_uin={uin}"
CHANNEL_SHARE_URL = "https://qun.qq.com/qunpro/robot/share?robot_appid={appid}"

register_plugin_page(
    title="Bot 管理",
    static_dir=WEB_DIR,
    page_id="console",
    description="管理 QQ 官方机器人的启用状态与运行模式，扫码添加机器人后自动写入 AppID / Secret。",
    confirm_message="即将打开 QQBotUID 提供的 Bot 管理页面。确认后侧边栏会收起，页面以嵌入方式打开。",
    title_i18n={"zh-CN": "Bot 管理", "en-US": "Bot Manager", "ja-JP": "Bot 管理"},
    description_i18n={
        "zh-CN": "管理 QQ 官方机器人的启用状态与运行模式，扫码添加机器人后自动写入 AppID / Secret。",
        "en-US": "Manage QQ official bots: enable state and transport mode, with QR binding for AppID / Secret.",
        "ja-JP": "QQ 公式 Bot の有効状態・接続方式を管理し、QR 連携で AppID / Secret を書き込みます。",
    },
    confirm_message_i18n={
        "zh-CN": "即将打开 QQBotUID 提供的 Bot 管理页面。确认后侧边栏会收起，页面以嵌入方式打开。",
        "en-US": "Open the QQBotUID bot manager. The sidebar will collapse and the page will be embedded.",
        "ja-JP": "QQBotUID の Bot 管理を開きます。サイドバーは折りたたまれ埋め込み表示されます。",
    },
    icon="bot",
)

api = PluginAPI()


def _rows_payload() -> list[dict[str, Any]]:
    out = []
    for row in load_rows():
        out.append(
            {
                "enabled": row.enabled,
                "bot_name": row.bot_name,
                "appid": row.appid,
                "secret_set": bool(row.secret),
                "mode": row.mode,
                "uin": row.uin,
                "avatar": row.avatar_url,
            }
        )
    return out


def _replace_rows(raw_rows: list[dict[str, Any]]) -> None:
    """保存列表"""
    old = {row.appid: row for row in load_rows()}
    rows: list[BotRow] = []
    for raw in raw_rows:
        appid = str(raw.get("appid") or "").strip()
        if not appid:
            continue
        mode = display_mode(raw.get("mode"))
        enabled = bool(raw.get("enabled", True))
        prev = old.get(appid)
        if prev is None:
            rows.append(BotRow(appid=appid, enabled=enabled, mode=mode))
        else:
            rows.append(replace(prev, enabled=enabled, mode=mode))
    save_rows(rows)


def _qr_data_url(url: str) -> str:
    img = qrcode.make(url, image_factory=PilImage)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def _refresh_profile(appid: str, secret: str) -> bool:
    """扫码绑定成功时取一次机器人自身资料"""
    config = QQBotUIDConfig.from_config()
    config.appid = appid
    config.secret = secret
    profile = await fetch_bot_profile(config)
    if profile is None:
        return False
    return save_bot_profile(
        appid,
        uin=profile.uin,
        bot_name=profile.bot_name,
        avatar=profile.avatar,
        bot_id=profile.bot_id,
        union_openid=profile.union_openid,
        description=profile.description,
    )


@api.get("/bots")
async def qqbotuid_bots():
    return {"status": 0, "msg": "ok", "data": {"bots": _rows_payload()}}


def _find_row(appid: str) -> BotRow | None:
    return next((row for row in load_rows() if row.appid == appid), None)


@api.get("/bots/{appid}")
async def qqbotuid_bot_detail(appid: str):
    """机器人详情：身份信息 + 群聊 / 频道添加二维码。"""
    row = _find_row(appid)
    if row is None:
        return {"status": 1, "msg": "机器人不存在", "data": None}
    if row.secret and not (row.uin and row.bot_id and row.union_openid):
        await _refresh_profile(row.appid, row.secret)
        row = _find_row(appid) or row
    group_url = GROUP_SHARE_URL.format(uin=row.uin) if row.uin else ""
    channel_url = CHANNEL_SHARE_URL.format(appid=row.appid)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "appid": row.appid,
            "uin": row.uin,
            "bot_id": row.bot_id,
            "union_openid": row.union_openid,
            "bot_name": row.bot_name,
            "description": row.description,
            "avatar": row.avatar_url,
            "qrcodes": [
                {
                    "key": "group",
                    "label": "群聊添加",
                    "url": group_url,
                    "qr": _qr_data_url(group_url) if group_url else "",
                },
                {
                    "key": "channel",
                    "label": "频道添加",
                    "url": channel_url,
                    "qr": _qr_data_url(channel_url),
                },
            ],
        },
    }


@api.post("/bots")
async def qqbotuid_save_bots(data: dict[str, Any]):
    _replace_rows([r for r in data.get("bots", []) if isinstance(r, dict)])
    reloaded = await _reload_runtime()
    return {"status": 0, "msg": "ok", "data": {"bots": _rows_payload(), "reloaded": reloaded}}


@api.post("/bind/create")
async def qqbotuid_bind_create():
    task_id, url = await create_bind_task()
    return {"status": 0, "msg": "ok", "data": {"task_id": task_id, "url": url, "qr_data_url": _qr_data_url(url)}}


async def _reload_runtime() -> bool:
    try:
        await qqbotuid_runtime.restart()
    except Exception as e:
        logger.warning(f"[QQBotUID] 扫码绑定后重载运行时失败，需手动重启核心: {e}")
        return False
    return True


@api.post("/bind/poll")
async def qqbotuid_bind_poll(data: dict[str, Any]):
    result = await poll_bind_task(str(data.get("task_id") or ""))
    if result.get("status") != "completed":
        return {"status": 0, "msg": "ok", "data": result}
    created = upsert_bound_bot(str(result["appid"]), str(result["secret"]))
    await _refresh_profile(str(result["appid"]), str(result["secret"]))
    reloaded = await _reload_runtime()
    payload = {
        "status": "completed",
        "appid": result["appid"],
        "created": created,
        "reloaded": reloaded,
        "bots": _rows_payload(),
    }
    return {"status": 0, "msg": "ok", "data": payload}
