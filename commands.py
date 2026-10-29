import json
from typing import Any, Dict, List, Iterable

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

SNAPSHOT = get_res_path("QQBotUID") / "commands.json"

COMMAND_TYPES = ("command", "prefix", "fullmatch", "suffix")

_CACHE_MTIME = 0.0
_CACHE_DATA: Dict[str, List[str]] = {}


def collect() -> Dict[str, List[str]]:
    """从核心的触发器表里收集 ``{插件目录名: [命令词]}``。"""
    from gsuid_core.sv import SL

    out: Dict[str, List[str]] = {}
    for sv in list(SL.lst.values()):
        name = str(getattr(sv, "self_plugin_name", "") or getattr(sv, "name", "")).strip()
        if not name:
            continue
        words = out.setdefault(name, [])
        for kind, bucket in (getattr(sv, "TL", {}) or {}).items():
            if kind not in COMMAND_TYPES:
                continue
            for keyword in bucket:
                word = str(keyword).strip()
                if word and word not in words:
                    words.append(word)
    return {name: words for name, words in out.items() if words}


def write_snapshot() -> Dict[str, List[str]]:
    """把当前触发器表写成快照。核心启动、退出各写一次。"""
    global _CACHE_MTIME
    data = collect()
    try:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        logger.warning(f"[QQBotUID] 命令快照写入失败: {type(error).__name__}: {error}")
        return data
    _CACHE_MTIME = 0.0
    logger.debug(f"[QQBotUID] 命令快照已更新: {len(data)} 个插件")
    return data


def _parse(raw: Any) -> Dict[str, List[str]]:
    if not isinstance(raw, dict):
        return {}
    return {str(name): [str(word) for word in words] for name, words in raw.items() if isinstance(words, list)}


def load_snapshot() -> Dict[str, List[str]]:
    """读快照。按 mtime 缓存。"""
    global _CACHE_MTIME, _CACHE_DATA
    try:
        mtime = SNAPSHOT.stat().st_mtime
    except OSError:
        return {}
    if _CACHE_MTIME == mtime:
        return _CACHE_DATA
    try:
        raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    _CACHE_DATA = _parse(raw)
    _CACHE_MTIME = mtime
    return _CACHE_DATA


def installed_plugins() -> List[str]:
    from gsuid_core.data_store import core_path

    names: List[str] = []
    for base in (core_path / "plugins", core_path / "buildin_plugins"):
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir(), key=lambda path: path.name.lower()):
            if item.name.startswith(("_", ".")) or item.name == "__pycache__":
                continue
            if item.is_file() and item.suffix != ".py":
                continue
            name = item.stem if item.is_file() else item.name
            if name and name not in names:
                names.append(name)
    return sorted(names, key=str.lower)


def _plugin_key(name: Any) -> str:
    return str(name or "").strip().lower().removesuffix(".py")


def plugin_commands(plugin_name: str) -> List[str]:
    """某个插件注册的命令词"""
    target = _plugin_key(plugin_name)
    if not target:
        return []
    for name, words in load_snapshot().items():
        if _plugin_key(name) == target:
            return words
    return []


def is_plugin_command(text: str, plugins: Iterable[str]) -> bool:
    """这条消息是不是某个插件下的命令（按命令词前缀匹配）。"""
    body = str(text or "").strip().lstrip("/").strip()
    if not body:
        return False
    return any(body.startswith(word) for name in plugins for word in plugin_commands(name))
