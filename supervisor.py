import sys
import time
import asyncio
import contextlib
from typing import Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

DAEMON_SCRIPT = Path(__file__).with_name("daemon.py")
DAEMON_EXIT_LOCKED = 3
RESTART_DELAY_MIN = 2.0
RESTART_DELAY_MAX = 30.0
STABLE_SECONDS = 60.0


def python_exe() -> str:
    exe = sys.executable or ""
    if getattr(sys, "frozen", False):
        return ""
    if not Path(exe).name.lower().startswith("python"):
        return ""
    return exe


class DaemonSupervisor:
    """由核心托管 daemon 进程：启动时拉起、崩溃时重启、关核心时终止。"""

    def __init__(self, script: Optional[Path] = None) -> None:
        self._script = script or DAEMON_SCRIPT
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._watch: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._restart_delay = RESTART_DELAY_MIN
        self._spawned_at = 0.0

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.returncode is None

    async def start(self) -> None:
        self._closed = False
        self._restart_delay = RESTART_DELAY_MIN
        if not python_exe():
            logger.error(
                "[QQBotUID] 找不到 python 解释器，无法自动拉起独立进程"
                "（打包版核心不支持）；请手动执行 python daemon.py，或关掉『启用独立进程』"
            )
            return
        await self._spawn()
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._loop(), name="QQBotUID.daemon-watch")

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            python_exe(),
            str(self._script),
            cwd=str(self._script.parent),
        )
        self._spawned_at = time.monotonic()
        logger.success(f"[QQBotUID] 已拉起独立进程 daemon pid={self._proc.pid}")

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(3.0)
            proc = self._proc
            if proc is None:
                continue
            if proc.returncode is None:
                if time.monotonic() - self._spawned_at > STABLE_SECONDS:
                    self._restart_delay = RESTART_DELAY_MIN
                continue
            if proc.returncode == DAEMON_EXIT_LOCKED:
                logger.error(t("[QQBotUID] 已有另一个 daemon 在运行，停止自动拉起；先关掉它或等 daemon.lock 过期"))
                return
            logger.warning(
                t(
                    "[QQBotUID] 独立进程已退出(code={code})，{delay:.0f}s 后重启",
                    code=proc.returncode,
                    delay=self._restart_delay,
                )
            )
            await asyncio.sleep(self._restart_delay)
            if self._closed:
                return
            self._restart_delay = min(self._restart_delay * 2, RESTART_DELAY_MAX)
            await self._spawn()

    async def stop(self) -> None:
        self._closed = True
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=8.0)
            logger.info(f"[QQBotUID] 独立进程已退出 pid={proc.pid}")
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"[QQBotUID] 独立进程没响应终止，已强杀 pid={proc.pid}")
