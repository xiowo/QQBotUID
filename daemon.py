import os
import sys
import io
import time
import types
import signal
import asyncio
import hashlib
import logging
import tempfile
import threading
import traceback
import importlib
import contextlib
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional
from pathlib import Path
from urllib.parse import quote
from multiprocessing.connection import Listener

_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGINS_ROOT = _PLUGIN_DIR.parent
_CORE_ROOT = _PLUGIN_DIR.parents[2]
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.data_store import CONFIGS_PATH, get_res_path  # noqa: E402

_MODULES = (
    "bots_store",
    "commands",
    "config",
    "sign",
    "qq_api",
    "convert",
    "webhook",
    "proactive",
    "core_bridge",
    "remote_bridge",
    "runner",
)

_UPLOAD_HINT = "pclient"
_EXTRA_UPLOAD_PLUGINS: tuple[str, ...] = ()
_ACTIVE_UPLOAD_PLUGINS: list[str] = []
_UPLOAD_CONFIGS: list[Any] = []
_PLUGIN_CONFIGS: list[Any] = []

_DATA_DIR = get_res_path("QQBotUID")
_WATCHED = ("config.json", "advanced_config.json", "bots.json")
WATCH_INTERVAL = 3.0

_PIC_CONFIG = CONFIGS_PATH / "pic_upload_config.json"

_LOCK = _DATA_DIR / "daemon.lock"
LOCK_TTL = 30.0
_STARTED_AT = 0.0

EXIT_LOCKED = 3

# 独立进程的日志要能被核心看到：daemon 自己开一条本地命名管道，核心连上来取。
# 不走文件是因为文件要落盘、核心还得轮询，两样都是白白多出来的阻塞。
LOG_QUEUE_MAX = 500
LOG_BATCH_MAX = 64
LOG_RETRY = 1.0
RELAY_LEVEL = 5  # TRACE，全量转发


def log_pipe_address() -> str:
    """管道名按核心安装目录算，核心重启、daemon 不重启也能接上同一条。"""
    digest = hashlib.blake2b(str(_CORE_ROOT).encode("utf-8"), digest_size=6).hexdigest()
    if os.name == "nt":
        return rf"\\.\pipe\QQBotUID-{digest}"
    return str(Path(tempfile.gettempdir()) / f"QQBotUID-{digest}.sock")


def log_pipe_authkey() -> bytes:
    """两边各算一份，免得别的本地进程往管道里塞日志。"""
    return hashlib.blake2b(str(_CORE_ROOT).encode("utf-8"), digest_size=16).digest()


class LogPipe:
    """daemon 侧的日志出口：自己开一条本地命名管道，核心连上来取。

    ``put()`` 只往内存队列塞一条，绝不阻塞调用方（logging handler 和 stderr 都在调它）；
    真正走管道的发送在后台线程里做，核心没连上时队列自己攒着，攒满丢最旧的。
    管道由 daemon 开、核心连，是因为核心那边随时可能重启，让核心当客户端才好收摊。
    """

    def __init__(self, address: str, capacity: int = LOG_QUEUE_MAX) -> None:
        self._address = address
        self._authkey = log_pipe_authkey()
        self._capacity = capacity
        self._queue: Deque[Dict[str, str]] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._pending: Optional[Callable[[], None]] = None

    def set_pending_flush(self, hook: Callable[[], None]) -> None:
        """队列空下来时顺手调一下，用来把 stderr 攒着的半截刷出去。"""
        self._pending = hook

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="QQBotUID.log-pipe", daemon=True)
            self._thread.start()

    def put(self, level: str, event: str) -> None:
        with self._cond:
            if len(self._queue) >= self._capacity:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append({"level": level, "event": event})
            self._cond.notify()

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(1.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            listener = self._listen()
            if listener is None:
                self._stop.wait(LOG_RETRY)
                continue
            try:
                self._accept(listener)
            finally:
                with contextlib.suppress(Exception):
                    listener.close()

    def _listen(self) -> Optional[Any]:
        try:
            return Listener(self._address, authkey=self._authkey)
        except OSError:
            # 上一个进程的管道还没释放干净，等一会儿再试
            return None

    def _accept(self, listener: Any) -> None:
        while not self._stop.is_set():
            try:
                conn = listener.accept()
            except (OSError, EOFError):
                return
            try:
                self._serve(conn)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()

    def _serve(self, conn: Any) -> None:
        self._flush_dropped(conn)
        while not self._stop.is_set():
            batch = self._take()
            if not batch:
                if self._peer_gone(conn):
                    return
                continue
            try:
                conn.send(batch)
            except Exception:
                # 核心断了，这批塞回去下次一起发
                with self._cond:
                    self._queue.extendleft(reversed(batch))
                return

    def _peer_gone(self, conn: Any) -> bool:
        try:
            return bool(conn.poll(0))
        except (OSError, EOFError):
            return True

    def _take(self) -> List[Dict[str, str]]:
        with self._cond:
            if not self._queue:
                self._cond.wait(0.5)
            batch = self._pop()
        if batch:
            return batch
        # 队列空下来了：把 stderr 攒着的半截刷出来，再去拿一次
        hook = self._pending
        if hook is not None:
            hook()
        with self._cond:
            return self._pop()

    def _pop(self) -> List[Dict[str, str]]:
        if not self._queue:
            return []
        batch = [self._queue.popleft()]
        while self._queue and len(batch) < LOG_BATCH_MAX:
            batch.append(self._queue.popleft())
        return batch

    def _flush_dropped(self, conn: Any) -> None:
        with self._cond:
            dropped, self._dropped = self._dropped, 0
        if not dropped:
            return
        with contextlib.suppress(Exception):
            conn.send([{"level": "warning", "event": f"[QQBotUID] 核心不在线期间丢弃了 {dropped} 条日志"}])


def _event_text(record: logging.LogRecord) -> str:
    """structlog 把事件塞在 record.msg 这个 dict 里，普通 logging 才是模板串。"""
    msg = record.msg
    if isinstance(msg, dict):
        return str(msg.get("event", ""))
    try:
        return record.getMessage()
    except Exception:
        return str(msg)


class MemoryRelay(logging.Handler):
    """日志进内存队列，由 LogPipe 的后台线程发给核心；本进程不额外落盘、不阻塞。"""

    def __init__(self, pipe: LogPipe, level: int = RELAY_LEVEL) -> None:
        super().__init__(level)
        self._pipe = pipe

    def emit(self, record: logging.LogRecord) -> None:
        text = _event_text(record)
        if record.exc_info:
            text = f"{text}\n{''.join(traceback.format_exception(*record.exc_info)).rstrip()}"
        self._pipe.put(record.levelname.lower(), text)


class _StderrRelay(io.TextIOBase):
    """接住 stderr：stdout/stderr 都是 DEVNULL，traceback 不接就彻底没了。

    这里只攒不发 —— traceback 是一行一行写出来的，逐行发就成一堆碎日志了；
    管道空闲时（约 0.5s）会回来调 ``flush()``，整段一次发出去。
    """

    def __init__(self, pipe: LogPipe) -> None:
        self._pipe = pipe
        self._buffer = ""
        self._lock = threading.Lock()
        pipe.set_pending_flush(self.flush)

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            big = len(self._buffer) >= 8192
        if big:
            self.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            text, self._buffer = self._buffer.strip(), ""
        if text:
            self._pipe.put("error", text)


def _utf8_stdio() -> None:
    """stdout/stderr 被重定向到 DEVNULL 时是 GBK，写 emoji 会抛 UnicodeEncodeError，
    logging 就把「--- Logging error ---」刷到 stderr。只把错误处理改成 replace 就够，
    编码保持不变，免得手动跑 daemon.py 时在 GBK 控制台上变成乱码。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")


def redirect_stderr(pipe: LogPipe) -> None:
    """supervisor 把 stderr 丢进 DEVNULL，不接住的话 traceback 会彻底消失。"""
    sys.stderr = _StderrRelay(pipe)


def install_thread_hook() -> None:
    """线程里没人接的异常默认只写 stderr，这里收回到日志。"""

    def _hook(args: Any) -> None:
        name = getattr(args.thread, "name", "")
        logger.critical(
            f"[QQBotUID] 线程 {name} 未捕获异常",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _hook


def install_error_relay(pipe: LogPipe) -> None:
    """daemon 侧唯一的出口：全量日志都往管道里送一份。"""
    logging.getLogger("GsCore").addHandler(MemoryRelay(pipe))


def log_loop_exception(loop: Any, context: Dict[str, Any]) -> None:
    """asyncio 里没人接的异常默认只写 asyncio logger，这里收回来。"""
    error = context.get("exception")
    message = str(context.get("message") or "")
    if isinstance(error, BaseException):
        logger.critical(f"[QQBotUID] 未捕获的异步异常: {message}", exc_info=error)
    else:
        logger.critical(f"[QQBotUID] 未捕获的异步异常: {message}")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_lines() -> list[str]:
    try:
        return _LOCK.read_text(encoding="utf-8").split()
    except OSError:
        return []


def lock_pid() -> int:
    if not _LOCK.exists():
        return 0
    try:
        if time.time() - _LOCK.stat().st_mtime >= LOCK_TTL:
            return 0
        pid = int(_lock_lines()[0])
    except (OSError, ValueError, IndexError):
        return 0
    return pid if pid_alive(pid) else 0


def lock_started_at() -> float:
    try:
        return float(_lock_lines()[1])
    except (ValueError, IndexError):
        return 0.0


def lock_alive() -> bool:
    return lock_pid() > 0


def _touch_lock() -> None:
    global _STARTED_AT
    if not _STARTED_AT:
        _STARTED_AT = time.time()
    with contextlib.suppress(OSError):
        _LOCK.write_text(f"{os.getpid()}\n{_STARTED_AT:.0f}", encoding="utf-8")


def _release_lock() -> None:
    with contextlib.suppress(OSError):
        _LOCK.unlink(missing_ok=True)


def core_ws_url(cfg: Any) -> str:
    host = str(cfg.core_host or "127.0.0.1")
    port = int(cfg.core_port or 8765)
    token = str(cfg.core_token or "")
    query = f"?token={quote(token, safe='')}" if token else ""
    return f"ws://{host}:{port}/ws/{cfg.route_bot_id}{query}"


def _install_plugin_package() -> None:
    """伪造 plugins.QQBotUID 包，绕开插件 __init__ 里的核心注册逻辑。"""
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(_PLUGINS_ROOT)]
        sys.modules["plugins"] = pkg
    if "plugins.QQBotUID" not in sys.modules:
        pkg = types.ModuleType("plugins.QQBotUID")
        pkg.__path__ = [str(_PLUGIN_DIR)]
        sys.modules["plugins.QQBotUID"] = pkg


def _plugin_dirs() -> list[Path]:
    root = _PLUGINS_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists())


def _patches_pclient(plugin_dir: Path) -> bool:
    """扫插件顶层源码里有没有 pclient，避免把无关插件 import 进来。"""
    for path in plugin_dir.glob("*.py"):
        try:
            if _UPLOAD_HINT in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def upload_plugin_names() -> list[str]:
    """候选图床插件：手工指定的优先，其余靠扫源码自动发现。"""
    names = [name for name in _EXTRA_UPLOAD_PLUGINS if name != _PLUGIN_DIR.name]
    for plugin_dir in _plugin_dirs():
        name = plugin_dir.name
        if name in names or name == _PLUGIN_DIR.name:
            continue
        if _patches_pclient(plugin_dir):
            names.append(name)
    return names


def _upload_hooks(registry: str) -> list[Any]:
    """从核心的钩子表里挑出图床插件注册的启动/关闭钩子。"""
    server = sys.modules.get("gsuid_core.server")
    if server is None or not _ACTIVE_UPLOAD_PLUGINS:
        return []
    prefixes = tuple(f"gsuid_core.plugins.{name}" for name in _ACTIVE_UPLOAD_PLUGINS)
    picked = [
        hook
        for hook in getattr(server, registry, ())
        if (getattr(hook.func, "__module__", "") or "").startswith(prefixes)
    ]
    return [hook.func for hook in sorted(picked, key=lambda h: h.priority)]


def load_upload_plugins() -> list[str]:
    """加载会接管图片上传的插件。"""
    for name in upload_plugin_names():
        if name in _ACTIVE_UPLOAD_PLUGINS:
            continue
        segment = sys.modules.get("gsuid_core.segment")
        before = getattr(segment, "pclient", None)
        try:
            importlib.import_module(f"gsuid_core.plugins.{name}")
        except Exception as e:
            logger.warning(f"[QQBotUID] 插件 {name} 加载失败，跳过: {e}")
            continue
        after = getattr(sys.modules.get("gsuid_core.segment"), "pclient", None)
        if after is before:
            logger.debug(f"[QQBotUID] 插件 {name} 没接管图片上传，忽略")
            continue
        _ACTIVE_UPLOAD_PLUGINS.append(name)
        logger.info(f"[QQBotUID] 图床插件 {name} 已接管上传，pclient={type(after).__name__}")
    collect_upload_configs()
    return list(_ACTIVE_UPLOAD_PLUGINS)


def collect_upload_configs() -> list[Any]:
    """收集图床相关配置单例，框架一份，插件各自一份。"""
    from gsuid_core.utils.plugins_config.gs_config import StringConfig, pic_upload_config

    found: dict[int, Any] = {}
    for name in _ACTIVE_UPLOAD_PLUGINS:
        prefix = f"gsuid_core.plugins.{name}"
        for key, module in list(sys.modules.items()):
            if key != prefix and not key.startswith(f"{prefix}."):
                continue
            for value in list(vars(module).values()):
                if isinstance(value, StringConfig):
                    found.setdefault(id(value), value)
    _PLUGIN_CONFIGS[:] = list(found.values())
    _UPLOAD_CONFIGS[:] = [pic_upload_config, *found.values()]
    return list(_UPLOAD_CONFIGS)


def refresh_upload_configs() -> None:
    """把配置从磁盘读回内存。"""
    for config in _UPLOAD_CONFIGS:
        config.update_config()


def restart_upload_plugin(name: str) -> None:
    """整个重新 import 一遍插件。"""
    prefix = f"gsuid_core.plugins.{name}"
    drop_upload_hooks(name)
    for key in [k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")]:
        del sys.modules[key]
    importlib.import_module(prefix)


def drop_upload_hooks(name: str) -> None:
    """丢掉插件上一轮注册的钩子。"""
    server = sys.modules.get("gsuid_core.server")
    if server is None:
        return
    prefix = f"gsuid_core.plugins.{name}"
    for registry in ("core_start_def", "core_shutdown_def"):
        hooks = getattr(server, registry, None)
        if hooks is None:
            continue
        for hook in list(hooks):
            if (getattr(hook.func, "__module__", "") or "").startswith(prefix):
                hooks.discard(hook)


def repatch_upload_plugins() -> None:
    """segment 被 reload 后 pclient 会退回原始客户端，补丁重新打一次。"""
    for name in _ACTIVE_UPLOAD_PLUGINS:
        key = f"gsuid_core.plugins.{name}"
        module = sys.modules.get(key)
        patch = getattr(module, "_do_patch", None) if module is not None else None
        if callable(patch):
            patch()
            continue
        if module is not None:
            del sys.modules[key]
            importlib.import_module(key)


async def start_upload_plugins() -> None:
    for hook in _upload_hooks("core_start_def"):
        await hook()


async def stop_upload_plugins() -> None:
    for hook in _upload_hooks("core_shutdown_def"):
        await hook()


def load_plugin_modules() -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    _install_plugin_package()
    for name in _MODULES:
        key = f"plugins.QQBotUID.{name}"
        if key in sys.modules:
            continue
        spec = spec_from_file_location(key, _PLUGIN_DIR / f"{name}.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"加载插件模块失败: {name}")
        module = module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)


def _digest(path: Path) -> bytes:
    try:
        return hashlib.blake2b(path.read_bytes(), digest_size=16).digest()
    except OSError:
        return b""


def _config_stamp() -> tuple[bytes, ...]:
    return tuple(_digest(_DATA_DIR / name) for name in _WATCHED)


def _pic_stamp() -> bytes:
    return _digest(_PIC_CONFIG)


def _plugin_cfg_stamp() -> bytes:
    digest = hashlib.blake2b(digest_size=16)
    for config in _PLUGIN_CONFIGS:
        digest.update(_digest(Path(config.CONFIG_PATH)))
    return digest.digest()


def _module(name: str) -> Any:
    return sys.modules[f"plugins.QQBotUID.{name}"]


class Daemon:
    def __init__(self) -> None:
        self.bridge: Optional[Any] = None
        self.adapters: list[Any] = []
        self._url = ""
        self._stamp: tuple[bytes, ...] = ()
        self._pic = _pic_stamp()
        self._plugin_cfg = _plugin_cfg_stamp()

    async def run(self) -> int:
        asyncio.get_running_loop().set_exception_handler(log_loop_exception)
        if lock_alive():
            logger.error(
                f"[QQBotUID] 检测到另一个 daemon 还在运行（{_LOCK.name} 有新鲜心跳），本次启动退出；"
                f"确认没有残留进程后删掉 {_LOCK} 再试"
            )
            return EXIT_LOCKED
        _touch_lock()
        try:
            load_plugin_modules()
            load_upload_plugins()
            await start_upload_plugins()
            self._pic = _pic_stamp()
            self._plugin_cfg = _plugin_cfg_stamp()
            base = _module("config").QQBotUIDConfig.from_config()
            url = core_ws_url(base)
            logger.success(f"[QQBotUID] 独立进程启动，核心地址 {url}")
            await self._reconnect(url)
            await self.reload()
            self._stamp = _config_stamp()
            await self._watch()
        finally:
            await self.shutdown()
            _release_lock()
        return 0

    async def _refresh_config(self) -> None:
        config_mod = _module("config")
        config_mod.qqbotuid_config.update_config()
        config_mod.advanced_config.update_config()
        await self._refresh_image_host()

    async def _refresh_image_host(self) -> None:
        pic = _pic_stamp()
        plugin_cfg = _plugin_cfg_stamp()
        if pic == self._pic and plugin_cfg == self._plugin_cfg:
            return
        segment = sys.modules.get("gsuid_core.segment")
        if segment is None:
            self._pic = pic
            self._plugin_cfg = plugin_cfg
            return
        refresh_upload_configs()
        importlib.reload(segment)
        _module("qq_api").patch_image_fallback()
        if plugin_cfg != self._plugin_cfg:
            for name in _ACTIVE_UPLOAD_PLUGINS:
                restart_upload_plugin(name)
            collect_upload_configs()
        else:
            repatch_upload_plugins()
        await start_upload_plugins()
        self._pic = _pic_stamp()
        self._plugin_cfg = _plugin_cfg_stamp()
        logger.info(f"[QQBotUID] 图床配置有变动，已重建客户端 pclient={type(segment.pclient).__name__}")

    async def _reconnect(self, url: str) -> None:
        if self.bridge is not None:
            await self.bridge.shutdown()
        bridge = _module("remote_bridge").RemoteBridge(url)
        self.bridge = bridge
        self._url = url
        await bridge.attach()

    async def reload(self) -> None:
        await self._refresh_config()
        config_mod = _module("config")
        url = core_ws_url(config_mod.QQBotUIDConfig.from_config())
        await self.stop_adapters()
        if url != self._url:
            logger.info(f"[QQBotUID] 核心地址变成 {url}，重连")
            await self._reconnect(url)
        configs = config_mod.get_qqbotuid_configs()
        if not configs:
            logger.warning("[QQBotUID] 没有启用中的机器人，等配置变更")
            return
        runner_mod = _module("runner")
        for cfg in configs:
            try:
                adapter = runner_mod.QQBotUID(cfg, self.bridge)
                await adapter.start()
                self.adapters.append(adapter)
            except Exception as e:
                logger.exception(f"[QQBotUID] Bot 启动失败: appid={cfg.appid}, err={e}")
        logger.success(f"[QQBotUID] 已启动 {len(self.adapters)} 个机器人")

    async def stop_adapters(self) -> None:
        adapters, self.adapters = self.adapters, []
        for adapter in adapters:
            with contextlib.suppress(asyncio.CancelledError):
                await adapter.stop()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            _touch_lock()
            await self._refresh_image_host()
            if _config_stamp() == self._stamp:
                continue
            logger.info("[QQBotUID] 配置文件有变动，重载机器人")
            await self.reload()
            self._stamp = _config_stamp()

    async def shutdown(self) -> None:
        await self.stop_adapters()
        if self.bridge is not None:
            await self.bridge.shutdown()
            self.bridge = None
        await stop_upload_plugins()


def main() -> int:
    _utf8_stdio()
    pipe = LogPipe(log_pipe_address())
    pipe.start()
    install_error_relay(pipe)
    redirect_stderr(pipe)
    install_thread_hook()
    daemon = Daemon()
    try:
        return asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("[QQBotUID] 收到中断信号，已退出")
        return 0
    except Exception as e:
        logger.exception(f"[QQBotUID] 独立进程异常退出: {e}")
        return 1
    finally:
        pipe.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
