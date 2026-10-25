import time
import base64
import secrets
from typing import Any
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_BIND_URL = "https://q.qq.com/qqbot/openclaw/connect.html?task_id={task_id}&source=qqbotuid&_wv=2"
_BIND_TASK_TTL = 600

_POLL_MIN_INTERVAL_S = 2.0
_RETCODE_TOO_FREQUENT = 30012
_QQ_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://q.qq.com",
    "Referer": "https://q.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Linux; U; Android 14; zh-cn) AppleWebKit/537.36 Chrome/109.0.5414.118 Mobile Safari/537.36"
    ),
}


@dataclass(slots=True)
class _BindTask:
    key: str
    created_at: float
    last_poll: float = 0.0


_bind_tasks: dict[str, _BindTask] = {}


def _prune_bind_tasks() -> None:
    now = time.time()
    for task_id in [k for k, task in _bind_tasks.items() if now - task.created_at > _BIND_TASK_TTL]:
        _bind_tasks.pop(task_id, None)


def _decrypt_bind_secret(encrypted_b64: str, key_b64: str) -> str:
    raw = base64.b64decode(encrypted_b64)
    key = base64.b64decode(key_b64)
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")


async def _post_q_open(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url="https://q.qq.com", headers=_QQ_HEADERS, timeout=20.0) as client:
        resp = await client.post(path, json=payload)
    if "application/json" not in resp.headers.get("content-type", ""):
        return {"retcode": resp.status_code, "msg": resp.text[:200]}
    data = resp.json()
    return data if isinstance(data, dict) else {"retcode": -1, "msg": str(data)}


async def create_bind_task() -> tuple[str, str]:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    data = await _post_q_open("/lite/create_bind_task", {"key": key})
    task_id = (data.get("data") or {}).get("task_id") if data.get("retcode") == 0 else None
    if not task_id:
        raise RuntimeError(str(data.get("msg") or "创建绑定任务失败"))
    _prune_bind_tasks()
    task_id = str(task_id)
    _bind_tasks[task_id] = _BindTask(key=key, created_at=time.time())
    return task_id, _BIND_URL.format(task_id=quote(task_id))


async def poll_bind_task(task_id: str) -> dict[str, Any]:
    _prune_bind_tasks()
    task = _bind_tasks.get(task_id)
    if task is None:
        return {"status": "not_found", "message": "二维码已过期，请重新生成"}
    now = time.time()
    if now - task.last_poll < _POLL_MIN_INTERVAL_S:
        return {"status": "waiting"}
    task.last_poll = now
    data = await _post_q_open("/lite/poll_bind_result", {"task_id": task_id})
    retcode = data.get("retcode")
    if retcode == _RETCODE_TOO_FREQUENT:
        return {"status": "waiting"}
    if retcode != 0:
        return {"status": "error", "message": str(data.get("msg") or "查询绑定结果失败")}
    d = data.get("data") or {}
    status = d.get("status")
    if status == 2:
        _bind_tasks.pop(task_id, None)
        appid = str(d.get("bot_appid") or "")
        encrypted = str(d.get("bot_encrypt_secret") or "")
        if not appid or not encrypted:
            return {"status": "error", "message": "绑定结果缺少 AppID/Secret"}
        try:
            secret = _decrypt_bind_secret(encrypted, task.key)
        except (InvalidTag, ValueError) as e:
            return {"status": "error", "message": f"解密 Secret 失败: {e}"}
        return {"status": "completed", "appid": appid, "secret": secret}
    if status == 3:
        _bind_tasks.pop(task_id, None)
        return {"status": "expired", "message": "二维码已过期，请重新生成"}
    return {"status": "waiting"}
