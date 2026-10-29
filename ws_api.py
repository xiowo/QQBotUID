import json
from typing import Dict, Tuple, Optional

from gsuid_core.logger import logger

from .user_info import QQBotUIDUser

MEMBERS_REQUEST = "gscore_group_members_request"
ACTIVE_TEST = "gscore_qqbot_active_test"
CONTROL_TYPES: Tuple[str, ...] = (MEMBERS_REQUEST,)


def _payload(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


async def handle_control(
    frame_type: Optional[str],
    raw: object,
    platform_bot_id: str = "",
) -> Optional[str]:
    """处理一帧控制请求，返回回执要带的 JSON；不是控制帧则返回 None。"""
    if frame_type not in CONTROL_TYPES:
        return None
    data = _payload(raw)
    request_id = _as_str(data.get("request_id"))
    if not request_id:
        logger.warning(f"[QQBotUID] 控制帧缺少 request_id: {frame_type}")
        return json.dumps({"ok": False, "error": "missing request_id"}, ensure_ascii=False)
    try:
        body = await _members_body(data, platform_bot_id)
    except Exception as error:
        logger.warning(f"[QQBotUID] 控制帧处理失败（{frame_type}）: {error}")
        body = {"ok": False, "error": str(error)}
    result: Dict[str, object] = {"request_id": request_id}
    result.update(body)
    return json.dumps(result, ensure_ascii=False)


async def _members_body(data: Dict[str, object], platform_bot_id: str) -> Dict[str, object]:
    """核心通用协议 ``gscore_group_members_request``：获取缓存群成员列表"""
    group_openid = _as_str(data.get("group_openid") or data.get("group_id"))
    if not group_openid:
        return {"ok": False, "error": "missing group_openid", "count": 0, "member_ids": []}
    member_ids = await QQBotUIDUser.list_members(
        bot_id=_as_str(data.get("bot_id")) or platform_bot_id,
        group_openid=group_openid,
    )
    return {
        "ok": True,
        "group_openid": group_openid,
        "count": len(member_ids),
        "member_ids": member_ids,
    }
