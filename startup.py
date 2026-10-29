import asyncio
import contextlib

import gsuid_core
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown

from .config import ROUTE_BOT_ID, QQBotUIDConfig, maintenance_rule, get_qqbotuid_configs, refresh_plugin_options
from .runner import QQBotUID
from .commands import write_snapshot as write_command_snapshot
from .user_info import ensure_table as ensure_user_table
from .group_info import ensure_table as ensure_group_table
from .supervisor import DaemonSupervisor
from .core_bridge import CoreBridge

_RUNTIME_SLOT = "_qqbotuid_runtime"
RUNTIME_TAG = "qqbotuid-runtime"


class _Runtime:
    def __init__(self) -> None:
        self._runtime_tag = RUNTIME_TAG
        self.bridge = CoreBridge(ROUTE_BOT_ID)
        self.adapters: list[QQBotUID] = []
        self.supervisor = DaemonSupervisor()

    async def start(self) -> None:
        base = QQBotUIDConfig.from_config()
        if not base.daemon_mode and maintenance_rule() is not None:
            logger.warning(
                "[QQBotUID] 断连维护提示已开启，但『启用独立进程』没开："
                "要让它生效请打开『独立进程模式』里的『启用独立进程』并重启核心。"
            )
        await ensure_group_table()
        await ensure_user_table()
        if base.daemon_mode:
            if not base.enabled:
                logger.info(t("[QQBotUID] 总开关已关闭，跳过启动"))
                return
            await self.supervisor.start()
            return
        configs = get_qqbotuid_configs()
        if not configs:
            logger.info(t("[QQBotUID] WebUI 配置开关已关闭，跳过启动"))
            return
        if self.adapters:
            return
        await self.bridge.attach()
        for cfg in configs:
            try:
                adapter = QQBotUID(cfg, self.bridge)
                await adapter.start()
                self.adapters.append(adapter)
            except Exception as e:
                logger.exception(t("[QQBotUID] Bot 启动失败: appid={appid}, err={err}", appid=cfg.appid, err=e))

    async def stop(self) -> None:
        await self.supervisor.stop()
        adapters, self.adapters = self.adapters, []
        for adapter in adapters:
            with contextlib.suppress(asyncio.CancelledError):
                await adapter.stop()
        await self.bridge.shutdown()

    async def restart(self) -> None:
        if QQBotUIDConfig.from_config().daemon_mode and self.supervisor.running:
            logger.info("[QQBotUID] 独立进程模式，配置改动交给 daemon 自己热重载")
            return
        await self.stop()
        await self.start()


qqbotuid_runtime = _Runtime()


async def _stop_previous() -> None:
    previous = getattr(gsuid_core, _RUNTIME_SLOT, None)
    setattr(gsuid_core, _RUNTIME_SLOT, qqbotuid_runtime)
    if previous is None or previous is qqbotuid_runtime:
        return
    if getattr(previous, "_runtime_tag", "") != RUNTIME_TAG:
        return
    logger.info("[QQBotUID] 检测到重载前遗留的运行时，先停掉再启动")
    await previous.stop()


@on_core_start(priority=20)
async def start_qqbotuid() -> None:
    await _stop_previous()
    write_command_snapshot()
    refresh_plugin_options()
    await qqbotuid_runtime.start()


@on_core_shutdown(priority=20)
async def stop_qqbotuid() -> None:
    await qqbotuid_runtime.stop()
    write_command_snapshot()
