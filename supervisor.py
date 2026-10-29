import os
import sys
import time
import signal
import asyncio
import logging
import threading
import contextlib
import subprocess
from typing import Any, Optional, cast
from pathlib import Path
from multiprocessing.connection import Client

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .config import QQBotUIDConfig, maintenance_rule
from .daemon import EXIT_LOCKED, lock_pid, pid_alive, lock_started_at, log_pipe_address, log_pipe_authkey

DAEMON_SCRIPT = Path(__file__).with_name("daemon.py")
RESTART_DELAY_MIN = 2.0
RESTART_DELAY_MAX = 30.0
STABLE_SECONDS = 60.0
TERMINATE_TIMEOUT = 8.0
PIPE_RETRY = 1.0
OWN_TAG = "[QQBotUID] "
_RELAY_LEVELS = {
    "trace": "trace",
    "debug": "debug",
    "info": "info",
    "success": "success",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}
_RELAY_FLAG = threading.local()


class _SkipRelayFile(logging.Filter):
    """中继过来的日志 daemon 已经写过盘了，核心这侧只补控制台和内存缓冲，别再存一份。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(_RELAY_FLAG, "active", False)


def _relog(level: str, text: str) -> None:
    """按原级别重打一条；落盘那一步由 ``_SkipRelayFile`` 挡掉。"""
    method = getattr(logger, _RELAY_LEVELS.get(level, "error"))
    _RELAY_FLAG.active = True
    try:
        method(f"[QQBotUID] [独立进程] {text}")
    finally:
        _RELAY_FLAG.active = False


def _install_skip_filter() -> None:
    """把过滤器挂到核心的文件 handler 上；只在核心进程里做，daemon 侧没有这个模块。"""
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        handler.filters[:] = [item for item in handler.filters if type(item).__name__ != "_SkipRelayFile"]
        handler.addFilter(_SkipRelayFile())


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


def _wants_daemon() -> bool:
    return bool(QQBotUIDConfig.from_config().daemon_mode)


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


class LogPipeReader:
    """核心侧接住 daemon 的日志。

    早柚的控制台和网页实时日志只认核心进程内存里的记录，另一个进程写的进不去；
    daemon 的 stdout/stderr 又都是 DEVNULL。跨进程没法直接共享内存，
    所以走本地命名管道：daemon 那边开、这里用一个独立线程连上去读，
    既不碰磁盘，也不占用事件循环。管道由 daemon 开，是因为核心随时可能重启，
    让核心当客户端收摊最干净——关掉连接就行，不用去打断阻塞中的 accept()。
    """

    def __init__(self, address: str) -> None:
        self._address = address
        self._authkey = log_pipe_authkey()
        self._conn: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        _install_skip_filter()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="QQBotUID.log-reader", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        self._close()
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._connect():
                continue
            try:
                self._read()
            finally:
                self._close()

    def _connect(self) -> bool:
        try:
            self._conn = Client(self._address, authkey=self._authkey)
        except Exception:
            # daemon 还没起来，或者管道被上一个进程占着
            self._stop.wait(PIPE_RETRY)
            return False
        return True

    def _read(self) -> None:
        conn = self._conn
        if conn is None:
            return
        while not self._stop.is_set():
            try:
                if not conn.poll(0.5):
                    continue
                payload = conn.recv()
            except (EOFError, OSError):
                return
            for row in payload if isinstance(payload, list) else [payload]:
                self._emit(row)

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def _emit(self, row: Any) -> None:
        if not isinstance(row, dict):
            return
        event = str(row.get("event") or "")
        if not event:
            return
        if event.startswith(OWN_TAG):
            event = event[len(OWN_TAG) :]
        _relog(str(row.get("level") or "error").lower(), event)


class DaemonSupervisor:
    """由核心托管 daemon 进程：启动时拉起、崩溃时重启、关核心时终止。"""

    def __init__(self, script: Optional[Path] = None) -> None:
        self._script = script or DAEMON_SCRIPT
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._pid = 0
        self._watch: Optional[asyncio.Task[None]] = None
        self._log_reader = LogPipeReader(log_pipe_address())
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
        self._log_reader.start()
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
        await self._log_reader.stop()
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        proc, self._proc = self._proc, None
        pid = self._pid or lock_pid()
        self._pid = 0
        if proc is None and not pid:
            return
        if _keep_daemon() and _wants_daemon():
            logger.info(f"[QQBotUID] 断连维护提示已开启，保留独立进程 pid={pid} 继续值守")
            return
        if proc is not None and proc.poll() is None:
            await self._terminate(proc, pid)
        target = lock_pid() or pid
        if target and pid_alive(target):
            await self._terminate_pid(target)

    async def reap(self) -> None:
        """收掉残留的独立进程。"""
        pid = self._pid or lock_pid()
        self._forget()
        if not pid or not pid_alive(pid):
            return
        logger.info(f"[QQBotUID] 结束残留的独立进程 pid={pid}")
        await self._terminate_pid(pid)

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
