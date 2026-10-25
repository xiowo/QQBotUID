import json
import contextlib
from typing import Any, Literal
from dataclasses import asdict, dataclass

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

BOTS_CONFIG_PATH = get_res_path("QQBotUID") / "bots.json"


@dataclass(slots=True)
class BotRow:
    appid: str
    secret: str = ""
    bot_name: str = ""
    uin: str = ""
    avatar: str = ""
    bot_id: str = ""
    union_openid: str = ""
    description: str = ""
    enabled: bool = True
    mode: str = "WebSocket"

    @property
    def self_id(self) -> str:
        """核心侧区分消息来自哪个机器人的 bot_self_id：机器人 QQ 号"""
        return self.uin or self.appid

    @property
    def avatar_url(self) -> str:
        """机器人头像。"""
        if self.uin:
            return f"https://q1.qlogo.cn/g?b=qq&nk={self.uin}&s=100"
        return self.avatar

    @property
    def transport(self) -> Literal["ws", "wh"]:
        return "wh" if self.mode.strip().lower() in {"webhook", "wh"} else "ws"


def _read_raw() -> list[dict[str, Any]]:
    if not BOTS_CONFIG_PATH.exists():
        return []
    try:
        raw = json.loads(BOTS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        backup = BOTS_CONFIG_PATH.parent / f"{BOTS_CONFIG_PATH.name}.bad"
        with contextlib.suppress(OSError):
            BOTS_CONFIG_PATH.replace(backup)
        logger.warning(f"[QQBotUID] 机器人清单解析失败，已备份到 {backup.name}: {e}")
        return []
    rows = raw.get("bots") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def load_rows() -> list[BotRow]:
    rows: list[BotRow] = []
    for item in _read_raw():
        appid = str(item.get("appid") or "").strip()
        if not appid:
            continue
        rows.append(
            BotRow(
                appid=appid,
                secret=str(item.get("secret") or "").strip(),
                bot_name=str(item.get("bot_name") or "").strip(),
                uin=str(item.get("uin") or "").strip(),
                avatar=str(item.get("avatar") or "").strip(),
                bot_id=str(item.get("bot_id") or "").strip(),
                union_openid=str(item.get("union_openid") or "").strip(),
                description=str(item.get("description") or "").strip(),
                enabled=bool(item.get("enabled", True)),
                mode=str(item.get("mode") or "WebSocket"),
            )
        )
    return rows


def save_rows(rows: list[BotRow]) -> None:
    BOTS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"bots": [asdict(row) for row in rows]}
    tmp = BOTS_CONFIG_PATH.parent / f"{BOTS_CONFIG_PATH.name}.tmp"
    tmp.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BOTS_CONFIG_PATH)


def upsert_bound_bot(appid: str, secret: str) -> bool:
    """扫码绑定写入"""
    rows = load_rows()
    for row in rows:
        if row.appid == appid:
            row.secret = secret
            row.enabled = True
            save_rows(rows)
            return False
    rows.append(BotRow(appid=appid, secret=secret, enabled=True))
    save_rows(rows)
    return True


def save_bot_profile(
    appid: str,
    *,
    uin: str = "",
    bot_name: str = "",
    avatar: str = "",
    bot_id: str = "",
    union_openid: str = "",
    description: str = "",
) -> bool:
    """写入机器人自身资料"""
    values = {
        "uin": str(uin or "").strip(),
        "bot_name": str(bot_name or "").strip(),
        "avatar": str(avatar or "").strip().replace("http://", "https://", 1),
        "bot_id": str(bot_id or "").strip(),
        "union_openid": str(union_openid or "").strip(),
        "description": str(description or "").strip(),
    }
    rows = load_rows()
    changed = False
    for row in rows:
        if row.appid != appid:
            continue
        for field, value in values.items():
            if field == "avatar" and values["uin"]:
                continue
            if value and getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
    if changed:
        save_rows(rows)
    return changed


def set_bot_name(appid: str, bot_name: str) -> None:
    save_bot_profile(appid, bot_name=bot_name)
