from typing import Any, Literal, Optional
from dataclasses import dataclass

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .bots_store import load_rows, set_bot_name

PLATFORM_BOT_ID = "qqgroup"
ROUTE_BOT_ID = "QQBotUID"


CONFIG_DEFAULT: dict[str, GSC] = {
    "_DividerRuntime": GsDivider("运行控制", ""),
    "enabled": GsBoolConfig(
        title="启用 QQBotUID",
        desc="总开关，关闭后所有机器人都不会启动。",
        data=True,
    ),
    "webhook_enabled": GsBoolConfig(
        title="启用 Webhook",
        desc="开启后才会监听 Webhook 回调端口；关闭时 Webhook 模式的机器人收不到消息。",
        data=False,
    ),
    "ignore_bot_message": GsBoolConfig(
        title="忽略其他机器人消息",
        desc="开启后发送者是机器人的消息直接丢弃，不交给核心处理。",
        data=True,
    ),
    "reconnect_interval": GsIntConfig(
        title="重连间隔 (ms)",
        desc="QQ 官方 WebSocket 断开后等多少毫秒重连；0 表示断开立刻重连。",
        data=0,
        max_value=60000,
    ),
    "_DividerWebhook": GsDivider("Webhook", ""),
    "webhook_host": GsStrConfig(
        title="Webhook 监听地址",
        desc="Webhook 模式下的监听网卡，0.0.0.0 表示所有网卡；核心在 Docker 里时留 0.0.0.0 并映射端口。",
        data="0.0.0.0",
    ),
    "webhook_port": GsIntConfig(
        title="Webhook 监听端口",
        desc="Webhook 模式下的监听端口；所有 Webhook 机器人共用这一个端口，按回调路径里的 AppID 区分。",
        data=8766,
        max_value=65535,
    ),
    "webhook_path": GsStrConfig(
        title="Webhook 路径",
        desc="官方后台填的回调路径，插件会在后面追加每个机器人的 AppID。",
        data="/qqbotuid/webhook",
    ),
}


ADVANCED_CONFIG_DEFAULT: dict[str, GSC] = {
    "_DividerApi": GsDivider("接口地址", ""),
    "api_base": GsStrConfig(
        title="QQ API Base",
        desc="QQ 官方开放平台接口地址，一般不用改。",
        data="https://api.bot.qq.com",
    ),
    "token_base": GsStrConfig(
        title="QQ Token Base",
        desc="获取 access_token 的地址，一般不用改。",
        data="https://api.bot.qq.com",
    ),
    "_DividerLink": GsDivider("连接细节", ""),
    "gateway_url": GsStrConfig(
        title="固定 Gateway URL",
        desc="留空时由 /gateway/bot 自动获取；只在自建网关时才需要填。",
        data="",
    ),
    "client_name": GsStrConfig(
        title="客户端名称",
        desc="写进官方 WebSocket User-Agent 的客户端名。",
        data="QQBotUID",
    ),
    "_DividerProactive": GsDivider("主动消息队列", ""),
    "proactive_direct_interval": GsIntConfig(
        title="私聊主动间隔 (ms)",
        desc="私聊车道上两条主动消息之间的最小间隔；订阅推送等被动窗口外的消息都走这条车道。",
        data=110,
        max_value=60000,
    ),
    "proactive_group_interval": GsIntConfig(
        title="群聊主动间隔 (ms)",
        desc="群聊车道上两条主动消息之间的最小间隔；频道消息也走这条车道。",
        data=1010,
        max_value=60000,
    ),
}


QQBOTUID_CONFIG_PATH = get_res_path("QQBotUID") / "config.json"
ADVANCED_CONFIG_PATH = get_res_path("QQBotUID") / "advanced_config.json"

qqbotuid_config = StringConfig("QQBotUID配置", QQBOTUID_CONFIG_PATH, CONFIG_DEFAULT)
advanced_config = StringConfig("高级配置", ADVANCED_CONFIG_PATH, ADVANCED_CONFIG_DEFAULT)


def _cfg_str(config: StringConfig, name: str, default: str = "") -> str:
    return str(config.get_config(name).data or "").strip() or default


def _cfg_int(config: StringConfig, name: str, default: int) -> int:
    try:
        return int(config.get_config(name).data)
    except (TypeError, ValueError):
        return default


def _normalize_mode(mode: Any) -> Literal["ws", "wh"]:
    value = str(mode or "ws").strip().lower()
    if value in {"websocket", "ws"}:
        return "ws"
    if value in {"webhook", "wh"}:
        return "wh"
    return "ws"


def display_mode(mode: Any) -> str:
    return "Webhook" if _normalize_mode(mode) == "wh" else "WebSocket"


def update_bot_name(appid: str, bot_name: str) -> None:
    """READY 后把官方机器人名称回写到清单，供 WebUI 列表标题展示。"""
    appid = str(appid or "").strip()
    bot_name = str(bot_name or "").strip()
    if not appid or not bot_name:
        return
    set_bot_name(appid, bot_name)


def get_qqbotuid_configs() -> list["QQBotUIDConfig"]:
    base = QQBotUIDConfig.from_config()
    rows = load_rows()
    if not base.enabled or not rows:
        return []

    configs: list[QQBotUIDConfig] = []
    for row in rows:
        if not row.enabled:
            continue
        configs.append(
            QQBotUIDConfig(
                enabled=True,
                appid=row.appid,
                secret=row.secret,
                route_bot_id=ROUTE_BOT_ID,
                platform_bot_id=PLATFORM_BOT_ID,
                bot_self_id=row.self_id,
                mode=row.transport,
                webhook_enabled=base.webhook_enabled,
                webhook_host=base.webhook_host,
                webhook_port=base.webhook_port,
                webhook_path=f"{base.webhook_path.rstrip('/')}/{row.appid}",
                api_base=base.api_base,
                token_base=base.token_base,
                gateway_url=base.gateway_url,
                reconnect_interval=base.reconnect_interval,
                client_name=base.client_name,
                ignore_bot_message=base.ignore_bot_message,
                proactive_direct_interval=base.proactive_direct_interval,
                proactive_group_interval=base.proactive_group_interval,
            )
        )
    return configs


@dataclass(slots=True)
class QQBotUIDConfig:
    enabled: bool
    appid: str
    secret: str
    route_bot_id: str = "QQBotUID"
    platform_bot_id: str = PLATFORM_BOT_ID
    bot_self_id: Optional[str] = None
    mode: Literal["ws", "wh"] = "ws"
    webhook_enabled: bool = False
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8766
    webhook_path: str = "/qqbotuid/webhook"
    api_base: str = "https://api.bot.qq.com"
    token_base: str = "https://api.bot.qq.com"
    gateway_url: str = ""
    reconnect_interval: int = 0
    client_name: str = "QQBotUID"
    ignore_bot_message: bool = True
    proactive_direct_interval: int = 110
    proactive_group_interval: int = 1010

    @property
    def self_id(self) -> str:
        return self.bot_self_id or self.appid

    @classmethod
    def from_config(cls) -> "QQBotUIDConfig":
        """读一份全局默认值；AppID/Secret 和收发模式由每个机器人的清单行单独提供。"""
        return cls(
            enabled=bool(qqbotuid_config.get_config("enabled").data),
            appid="",
            secret="",
            route_bot_id="QQBotUID",
            platform_bot_id=PLATFORM_BOT_ID,
            bot_self_id=None,
            webhook_enabled=bool(qqbotuid_config.get_config("webhook_enabled").data),
            webhook_host=_cfg_str(qqbotuid_config, "webhook_host", "0.0.0.0"),
            webhook_port=_cfg_int(qqbotuid_config, "webhook_port", 8766),
            webhook_path=_cfg_str(qqbotuid_config, "webhook_path", "/qqbotuid/webhook"),
            api_base=_cfg_str(advanced_config, "api_base", "https://api.bot.qq.com").rstrip("/"),
            token_base=_cfg_str(advanced_config, "token_base", "https://api.bot.qq.com").rstrip("/"),
            gateway_url=_cfg_str(advanced_config, "gateway_url"),
            reconnect_interval=_cfg_int(qqbotuid_config, "reconnect_interval", 0),
            client_name=_cfg_str(advanced_config, "client_name", "QQBotUID"),
            ignore_bot_message=bool(qqbotuid_config.get_config("ignore_bot_message").data),
            proactive_direct_interval=_cfg_int(advanced_config, "proactive_direct_interval", 110),
            proactive_group_interval=_cfg_int(advanced_config, "proactive_group_interval", 1010),
        )

    def validate(self) -> None:
        if not self.appid:
            raise RuntimeError("QQBOTUID_APPID 未配置")
        if not self.secret:
            raise RuntimeError("QQBOTUID_SECRET 未配置")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = f"/{self.webhook_path}"
