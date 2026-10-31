from typing import Any, List, Literal, Optional
from dataclasses import dataclass

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .commands import installed_plugins, is_plugin_command
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
    "group_name_enabled": GsBoolConfig(
        title="获取群名称",
        desc=(
            "开启后调 /v2/groups/{group_openid}/info 查群名，结果缓存 24 小时；"
            "关闭则群名统一用「群聊-xxxxxxxx」兜底，不请求接口。"
        ),
        data=True,
    ),
    "_DividerDaemon": GsDivider("独立进程模式", ""),
    "daemon_mode": GsBoolConfig(
        title="启用独立进程",
        desc="开启后由核心拉起独立的 daemon 进程承载适配器，核心启动/关闭时一并启停；关掉即回到同进程。",
        data=False,
    ),
    "core_host": GsStrConfig(
        title="核心地址",
        desc="daemon 连接核心 WS 端点用的地址；核心和 daemon 同机时保持 127.0.0.1。",
        data="127.0.0.1",
    ),
    "core_port": GsIntConfig(
        title="核心端口",
        desc="核心 Web 服务端口，要和核心配置里的 PORT 一致。",
        data=8765,
        max_value=65535,
    ),
    "core_token": GsStrConfig(
        title="核心 WS Token",
        desc="核心配置里的 WS_TOKEN；127.0.0.1 已在默认信任列表里，同机部署留空即可。",
        data="",
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


DEFAULT_MAINTENANCE_MESSAGE = "机器人维护中~\n正在更新框架(꒪⌓꒪)"
MAINTENANCE_COUNT_DEFAULT = 3


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
        desc="私聊车道两条主动消息之间的放行间隔；被动窗口外的发送都走这条车道。填 0 表示不限速，立刻放行。",
        data=110,
        max_value=60000,
    ),
    "proactive_group_interval": GsIntConfig(
        title="群聊主动间隔 (ms)",
        desc="群聊车道两条主动消息之间的放行间隔。填 0 表示不限速，立刻放行。",
        data=1010,
        max_value=60000,
    ),
    "_DividerMessage": GsDivider("消息类型", ""),
    "stream_enabled": GsBoolConfig(
        title="流式发送",
        desc=(
            "开启后私聊的纯文本 / markdown 回复按分片流式下发，像打字机一样逐段出现；"
            "发送失败会自动回退成普通消息。群聊没有流式接口，不受影响。"
        ),
        data=False,
    ),
    "stream_chunk_size": GsIntConfig(
        title="流式分片字符数",
        desc="每次下发的字符数，越小越像打字机，但请求次数也越多。内容比这个值短时会自动对半分，保证至少两片。",
        data=200,
        max_value=2000,
    ),
    "stream_delay_ms": GsIntConfig(
        title="流式分片间隔 (ms)",
        desc="两次分片之间等多久再发下一段。",
        data=100,
        max_value=5000,
    ),
    "input_notify_enabled": GsBoolConfig(
        title="私聊正在输入",
        desc=(
            "收到私聊后先发一条「正在输入」状态再等核心回复。"
            "会占用 1 次被动回复额度，剩最后 1 次时不再发，避免真回复被挤到主动通道。"
        ),
        data=False,
    ),
    "input_notify_seconds": GsIntConfig(
        title="正在输入时长 (s)",
        desc="「正在输入」状态持续多少秒。",
        data=30,
        max_value=300,
    ),
    "_DividerMaintenance": GsDivider("维护提示", ""),
    "disconnect_reply_enabled": GsBoolConfig(
        title="启用断连维护提示",
        desc=(
            "【必须先打开上面〔独立进程模式〕里的『启用独立进程』，否则不会生效】"
            "开了独立进程之后：核心重启 / 重载插件导致断开，用户发命令时由 daemon 直接回下面这条提示，"
        ),
        data=False,
    ),
    "disconnect_reply_count": GsIntConfig(
        title="断连次数",
        desc="连续这么多次没连上核心才判定为断连、开始回提示；填 1 表示一断开就提示。",
        data=3,
        max_value=100,
    ),
    "disconnect_reply_plugins": GsListStrConfig(
        title="触发的插件",
        desc=(
            "从已安装的插件里多选；只有这些插件下的命令才回提示。"
            "留空表示任何消息都回提示。装了新插件要重启核心才会出现在列表里。"
        ),
        data=[],
        options=installed_plugins(),
    ),
    "master_only_enabled": GsBoolConfig(
        title="启用主人维护",
        desc=(
            "开启后只有主人（核心配置里的 masters）的消息会转给核心，"
            "其他人一律回下面的『提示内容』。不看核心连接状态，同进程 / 独立进程都生效。"
        ),
        data=False,
    ),
    "disconnect_reply_message": GsStrConfig(
        title="提示内容",
        desc="断连维护和主人维护共用这条文案，留空用默认文案。",
        data=DEFAULT_MAINTENANCE_MESSAGE,
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


def _cfg_list(config: StringConfig, name: str) -> List[str]:
    """字符串列表配置。"""
    data = config.get_config(name).data
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def group_name_enabled() -> bool:
    return bool(qqbotuid_config.get_config("group_name_enabled").data)


PROACTIVE_DEFAULT_MS = {"direct": 110, "group": 1010}


def proactive_interval_ms(scene: str) -> int:
    key = f"proactive_{scene}_interval"
    return max(_cfg_int(advanced_config, key, PROACTIVE_DEFAULT_MS.get(scene, 0)), 0)


def maintenance_message() -> str:
    """维护提示文案（断连维护和主人维护共用）。"""
    return _cfg_str(advanced_config, "disconnect_reply_message", DEFAULT_MAINTENANCE_MESSAGE)


def master_only_enabled() -> bool:
    return bool(advanced_config.get_config("master_only_enabled").data)


@dataclass(slots=True)
class StreamSetting:
    """流式发送参数。"""

    chunk_size: int
    delay_ms: int


def stream_setting() -> Optional[StreamSetting]:
    """没开流式返回None。"""
    if not bool(advanced_config.get_config("stream_enabled").data):
        return None
    return StreamSetting(
        chunk_size=max(_cfg_int(advanced_config, "stream_chunk_size", 200), 1),
        delay_ms=max(_cfg_int(advanced_config, "stream_delay_ms", 100), 0),
    )


def input_notify_seconds() -> int:
    """「私聊正在输入」的持续秒数。"""
    if not bool(advanced_config.get_config("input_notify_enabled").data):
        return 0
    return max(_cfg_int(advanced_config, "input_notify_seconds", 30), 1)


def master_ids() -> set[str]:
    """核心配置里的主人名单"""
    from gsuid_core.config import core_config

    masters = core_config.get_config("masters") or []
    return {str(item).strip() for item in masters if str(item).strip()}


def is_master(user_id: str) -> bool:
    """是否机器人主人。"""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    masters = master_ids()
    if uid in masters:
        return True
    return any(item.rsplit(":", 1)[-1] == uid for item in masters if ":" in item)


@dataclass(slots=True)
class MaintenanceRule:
    """核心断连时的兜底回复规则。"""

    threshold: int
    plugins: tuple[str, ...]
    message: str

    def matches(self, text: str) -> bool:
        """消息属于配置的插件就命中；没配插件就一律回提示。"""
        if not self.plugins:
            return True
        return is_plugin_command(text, self.plugins)


def refresh_plugin_options() -> None:
    options = installed_plugins()
    changed = False
    default = ADVANCED_CONFIG_DEFAULT.get("disconnect_reply_plugins")
    current = advanced_config.get_config("disconnect_reply_plugins")
    for item in (default, current):
        if isinstance(item, GsListStrConfig) and item.options != options:
            item.options = options
            changed = True
    if changed:
        advanced_config.write_config()


def maintenance_rule() -> Optional[MaintenanceRule]:
    """读高级配置里的断连维护提示；没开启返回 ``None``。"""
    if not bool(advanced_config.get_config("disconnect_reply_enabled").data):
        return None
    return MaintenanceRule(
        threshold=max(_cfg_int(advanced_config, "disconnect_reply_count", MAINTENANCE_COUNT_DEFAULT), 1),
        plugins=tuple(_cfg_list(advanced_config, "disconnect_reply_plugins")),
        message=maintenance_message(),
    )


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
    daemon_mode: bool = False
    core_host: str = "127.0.0.1"
    core_port: int = 8765
    core_token: str = ""

    @property
    def self_id(self) -> str:
        return self.bot_self_id or self.appid

    @property
    def connection_key(self) -> tuple:
        """决定「要不要重连」的那部分配置。"""
        return (
            self.appid,
            self.secret,
            self.mode,
            self.bot_self_id,
            self.api_base,
            self.token_base,
            self.gateway_url,
            self.client_name,
            self.reconnect_interval,
            self.webhook_enabled,
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
        )

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
            daemon_mode=bool(qqbotuid_config.get_config("daemon_mode").data),
            core_host=_cfg_str(qqbotuid_config, "core_host", "127.0.0.1"),
            core_port=_cfg_int(qqbotuid_config, "core_port", 8765),
            core_token=_cfg_str(qqbotuid_config, "core_token"),
        )

    def validate(self) -> None:
        if not self.appid:
            raise RuntimeError("QQBOTUID_APPID 未配置")
        if not self.secret:
            raise RuntimeError("QQBOTUID_SECRET 未配置")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = f"/{self.webhook_path}"
