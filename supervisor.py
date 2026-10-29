import os
import sys
import time
import signal
import asyncio
import contextlib
import subprocess
from typing import Any, Optional, cast
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .config import maintenance_rule
from .daemon import EXIT_LOCKED, lock_pid, pid_alive, lock_started_at

DAEMON_SCRIPT = Path(__file__).with_name("daemon.py")
RESTART_DELAY_MIN = 2.0
RESTART_DELAY_MAX = 30.0
STABLE_SECONDS = 60.0
TERMINATE_TIMEOUT = 8.0


def python_exe() -> str:
    """拉起 daemon 用的解释器。"""
    exe = sys.executable or ""
    if getattr(sys, "frozen", False):
        return ""
    if not Path(exe).name.lower().startswith("python"):
        return ""
    return exe


def _spawn_kwargs() -> dict[str, Any]:
    """让 daemon 脱离核心的进程组和控制台。"""
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
    return {"start_new_session": True}


def _keep_daemon() -> bool:
    return maintenance_rule() is not None


def _plugin_mtime() -> float:
    """插件目录里 ``.py`` 的最新修改时间，用来判断 daemon 是不是跑着旧代码。"""
    newest = 0.0
    for path in DAEMON_SCRIPT.parent.glob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _is_stale() -> bool:
    """锁里记的 daemon 启动时间比插件代码旧 → 它加载的是老代码"""
    started = lock_started_at()
    if not started:
        return False
    return _plugin_mtime() > started + 1.0


def _send_signal(pid: int, name: str) -> None:
    """按名字取信号再发；Windows 没有 SIGKILL，退回 SIGTERM（等价 TerminateProcess）。"""
    with contextlib.suppress(OSError):
        os.kill(pid, getattr(signal, name, signal.SIGTERM))


class DaemonSupervisor:
    """由核心托管 daemon 进程：启动时拉起、崩溃时重启、关核心时终止。"""

    def __init__(self, script: Optional[Path] = None) -> None:
        self._script = script or DAEMON_SCRIPT
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._pid = 0
        self._watch: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._restart_delay = RESTART_DELAY_MIN
        self._spawned_at = 0.0

    @property
    def running(self) -> bool:
        if lock_pid():
            return True
        proc = self._proc
        if proc is not None:
            return proc.poll() is None
        return pid_alive(self._pid)

    async def start(self) -> None:
        self._closed = False
        self._restart_delay = RESTART_DELAY_MIN
        if self._proc is None and not self._pid:
            self._pid = lock_pid()
            if self._pid and _is_stale():
                logger.info(f"[QQBotUID] 插件代码比独立进程新（更新过），重启 pid={self._pid} 以加载新代码")
                await self._terminate_pid(self._pid)
                self._pid = 0
            if self._pid:
                logger.success(t("[QQBotUID] 独立进程 pid={pid} 还在运行，直接接管", pid=self._pid))
            elif not python_exe():
                logger.error(
                    "[QQBotUID] 找不到 python 解释器，无法自动拉起独立进程"
                    "（打包版核心不支持）；请手动执行 python daemon.py，或关掉『启用独立进程』"
                )
                return
            else:
                await self._spawn()
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._loop(), name="QQBotUID.daemon-watch")

    async def _spawn(self) -> None:
        try:
            proc = cast(
                "subprocess.Popen[bytes]",
                subprocess.Popen(  # noqa: ASYNC220
                    [python_exe(), str(self._script)],
                    cwd=str(self._script.parent),
                    **_spawn_kwargs(),
                ),
            )
        except OSError as error:
            logger.error(f"[QQBotUID] 拉起独立进程失败: {type(error).__name__}: {error}")
            return
        self._proc = proc
        self._pid = proc.pid
        self._spawned_at = time.monotonic()
        logger.success(f"[QQBotUID] 已拉起独立进程 daemon pid={self._pid}")

    def _exit_code(self) -> Optional[int]:
        """活着返回 ``None``；已退出返回退出码（接管来的没有退出码，给 -1）。"""
        if lock_pid():
            return None
        proc = self._proc
        if proc is not None:
            return proc.poll()
        if not self._pid:
            return None
        return None if pid_alive(self._pid) else -1

    def _forget(self) -> None:
        self._proc = None
        self._pid = 0

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(3.0)
            code = self._exit_code()
            if code is None:
                if self._proc is not None and time.monotonic() - self._spawned_at > STABLE_SECONDS:
                    self._restart_delay = RESTART_DELAY_MIN
                continue
            self._forget()
            if code == EXIT_LOCKED:
                logger.error(t("[QQBotUID] 已有另一个 daemon 在运行，停止自动拉起；先关掉它或等 daemon.lock 过期"))
                return
            logger.warning(
                t(
                    "[QQBotUID] 独立进程已退出(code={code})，{delay:.0f}s 后重启",
                    code=code,
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
        pid, self._pid = self._pid, 0
        if proc is None and not pid:
            return
        if _keep_daemon():
            logger.info(f"[QQBotUID] 断连维护提示已开启，保留独立进程 pid={pid} 继续值守")
            return
        if proc is not None and proc.poll() is None:
            await self._terminate(proc, pid)
        target = lock_pid() or pid
        if target and pid_alive(target):
            await self._terminate_pid(target)

    async def _terminate(self, proc: "subprocess.Popen[bytes]", pid: int) -> None:
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, TERMINATE_TIMEOUT)
            logger.info(f"[QQBotUID] 独立进程已退出 pid={pid}")
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait)
            logger.warning(f"[QQBotUID] 独立进程没响应终止，已强杀 pid={pid}")

    async def _terminate_pid(self, pid: int) -> None:
        _send_signal(pid, "SIGTERM")
        for _ in range(int(TERMINATE_TIMEOUT * 2)):
            await asyncio.sleep(0.5)
            if not pid_alive(pid):
                logger.info(f"[QQBotUID] 独立进程已退出 pid={pid}")
                return
        _send_signal(pid, "SIGKILL")
        logger.warning(f"[QQBotUID] 独立进程没响应终止，已强杀 pid={pid}")
