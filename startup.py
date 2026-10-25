import asyncio
import contextlib

import gsuid_core
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown

from .config import ROUTE_BOT_ID, get_qqbotuid_configs
from .runner import QQBotUID
from .core_bridge import CoreBridge

_RUNTIME_SLOT = "_qqbotuid_runtime"
RUNTIME_TAG = "qqbotuid-runtime"


class _Runtime:
    def __init__(self) -> None:
        self._runtime_tag = RUNTIME_TAG
        self.bridge = CoreBridge(ROUTE_BOT_ID)
        self.adapters: list[QQBotUID] = []

    async def start(self) -> None:
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
        adapters, self.adapters = self.adapters, []
        for adapter in adapters:
            with contextlib.suppress(asyncio.CancelledError):
                await adapter.stop()
        await self.bridge.shutdown()

    async def restart(self) -> None:
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
    await qqbotuid_runtime.start()


@on_core_shutdown(priority=20)
async def stop_qqbotuid() -> None:
    await qqbotuid_runtime.stop()
