import os
import sys
import time
import types
import signal
import asyncio
import hashlib
import importlib
import contextlib
from typing import Any, Optional
from pathlib import Path
from urllib.parse import quote

_PLUGIN_DIR = Path(__file__).resolve().parent
_PLUGINS_ROOT = _PLUGIN_DIR.parent
_CORE_ROOT = _PLUGIN_DIR.parents[2]
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.data_store import CONFIGS_PATH, get_res_path  # noqa: E402

_MODULES = (
    "bots_store",
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

EXIT_LOCKED = 3


def _pid_alive(pid: int) -> bool:
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


def _lock_alive() -> bool:
    if not _LOCK.exists():
        return False
    try:
        if time.time() - _LOCK.stat().st_mtime >= LOCK_TTL:
            return False
        pid = int(_LOCK.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _touch_lock() -> None:
    with contextlib.suppress(OSError):
        _LOCK.write_text(str(os.getpid()), encoding="utf-8")


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
        if _lock_alive():
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
    daemon = Daemon()
    try:
        return asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("[QQBotUID] 收到中断信号，已退出")
        return 0
    except Exception as e:
        logger.exception(f"[QQBotUID] 独立进程异常退出: {e}")
        return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
