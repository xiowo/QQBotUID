"""看 QQBotUID 的日志。

日志是每行一条 JSON，直接翻很累，这个脚本负责过滤和排版：

    python logs.py                 最近 50 条本插件的日志
    python logs.py -n 200          最近 200 条
    python logs.py -a              所有插件（不加就只看 QQBotUID / RollPigUID 这类）
    python logs.py -f              实时跟踪（Ctrl+C 退出）
    python logs.py -d 2026-10-28   看某一天
    python logs.py -k 维护         只留事件里含「维护」的行
    python logs.py -l warning      只要 warning 及以上

"""

import io
import sys
import json
import time
import argparse
from typing import Any, Dict, List, Iterator, Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

try:
    from gsuid_core.logger import LOG_PATH
except Exception:  # 核心不在 sys.path 时按默认位置找
    LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "logs"

OWN_PLUGINS = ("QQBotUID",)
LEVEL_ORDER = {"trace": 0, "debug": 1, "info": 2, "success": 2, "warning": 3, "error": 4, "critical": 5}


def _log_file(day: Optional[str]) -> Path:
    name = day or time.strftime("%Y-%m-%d")
    return LOG_PATH / f"{name}.log"


def _parse(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _keep(row: Dict[str, Any], plugins: tuple, level: str, keyword: str) -> bool:
    if plugins and str(row.get("plugin", "")) not in plugins:
        return False
    if LEVEL_ORDER.get(str(row.get("level", "")).lower(), 0) < LEVEL_ORDER.get(level, 0):
        return False
    return keyword.lower() in str(row.get("event", "")).lower() if keyword else True


def _format(row: Dict[str, Any]) -> str:
    level = str(row.get("level", "")).upper()[:7]
    plugin = str(row.get("plugin", ""))[:12]
    return f"{row.get('timestamp', '')}  {level:<7} {plugin:<12} {row.get('event', '')}"


def _iter_file(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            yield line


def show_tail(path: Path, count: int, plugins: tuple, level: str, keyword: str) -> int:
    if not path.exists():
        print(f"日志文件不存在: {path}")
        return 1
    rows: List[str] = []
    for line in _iter_file(path):
        row = _parse(line)
        if row and _keep(row, plugins, level, keyword):
            rows.append(_format(row))
    for item in rows[-count:]:
        print(item)
    print(f"\n—— 共 {len(rows)} 条匹配，显示最近 {min(count, len(rows))} 条（{path.name}）")
    return 0


def follow(path: Path, plugins: tuple, level: str, keyword: str) -> int:
    if not path.exists():
        print(f"日志文件不存在: {path}")
        return 1
    for line in _iter_file(path):
        row = _parse(line)
        if row and _keep(row, plugins, level, keyword):
            print(_format(row), flush=True)
    print(f"—— 跟踪中: {path.name}（Ctrl+C 退出）")
    with path.open("r", encoding="utf-8", errors="replace") as file:
        file.seek(0, io.SEEK_END)
        while True:
            line = file.readline()
            if not line:
                time.sleep(0.4)
                continue
            row = _parse(line)
            if row and _keep(row, plugins, level, keyword):
                print(_format(row), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="看 QQBotUID 日志", add_help=True)
    parser.add_argument("-n", "--lines", type=int, default=50, help="显示最近多少条，默认 50")
    parser.add_argument("-d", "--day", default=None, help="日期，格式 2026-10-28，默认今天")
    parser.add_argument("-a", "--all", action="store_true", help="所有插件，默认只看 QQBotUID")
    parser.add_argument("-f", "--follow", action="store_true", help="实时跟踪")
    parser.add_argument("-k", "--keyword", default="", help="只留事件里含这个关键词的行")
    parser.add_argument("-l", "--level", default="trace", help="最低级别: trace/debug/info/warning/error")
    args = parser.parse_args()

    path = _log_file(args.day)
    plugins = () if args.all else OWN_PLUGINS
    try:
        if args.follow:
            return follow(path, plugins, args.level, args.keyword)
        return show_tail(path, args.lines, plugins, args.level, args.keyword)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
