import re
import time
import base64
import asyncio
import hashlib
import binascii
from io import BytesIO
from typing import Any, Dict, List, Tuple, Union, Optional
from pathlib import Path
from datetime import datetime, timedelta
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from PIL import Image

from gsuid_core import segment as core_segment
from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from .config import QQBotUIDConfig
from .group_info import QQBotUIDGroupInfo

_MATERIALS: ContextVar[Tuple[str, ...]] = ContextVar("qqbotuid_materials", default=())


class QQTokenManager:
    def __init__(self, config: QQBotUIDConfig):
        self.config = config
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.config.api_base, timeout=30.0)
        return self._client

    async def token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.config.token_base}/app/getAppAccessToken",
                    json={"appId": self.config.appid, "clientSecret": self.config.secret},
                )
            # 响应里有 access_token，不打印原文
            logger.debug(f"[QQBotUID] 获取 access_token -> {resp.status_code}")
            data = resp.json()
            if resp.status_code != 200 or "access_token" not in data:
                raise RuntimeError(f"QQBot token 获取失败: HTTP {resp.status_code} {data}")
            self._token = str(data["access_token"])
            self._expires_at = time.time() + int(data.get("expires_in", 7200))
            return self._token

    async def authorization(self) -> str:
        return f"QQBot {await self.token()}"


@dataclass(slots=True)
class BotProfile:
    """机器人自身资料"""

    uin: str = ""
    bot_id: str = ""
    union_openid: str = ""
    bot_name: str = ""
    avatar: str = ""
    description: str = ""


def robot_uin_from_share_url(share_url: str) -> str:
    matched = re.search(r"robot_uin=(\d+)", share_url or "")
    return matched.group(1) if matched else ""


async def fetch_bot_profile(config: QQBotUIDConfig) -> Optional[BotProfile]:
    tm = QQTokenManager(config)
    try:
        client = await tm.client()
        resp = await client.get("/users/@me", headers={"Authorization": await tm.authorization()})
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError, RuntimeError) as e:
        logger.warning(f"[QQBotUID] 获取机器人自身资料失败（{config.appid}）: {e}")
        return None
    finally:
        await tm.aclose()
    if not isinstance(data, dict):
        return None
    profile = BotProfile(
        uin=robot_uin_from_share_url(str(data.get("share_url") or "")),
        bot_id=str(data.get("id") or "").strip(),
        union_openid=str(data.get("union_openid") or "").strip(),
        bot_name=str(data.get("username") or "").strip(),
        avatar=str(data.get("avatar") or "").strip(),
        description=str(data.get("description") or "").strip(),
    )
    if not profile.uin:
        logger.warning(f"[QQBotUID] /users/@me 未取到机器人 QQ 号（{config.appid}）")
    return profile


async def fetch_group_name(tm: "QQTokenManager", group_openid: str) -> str:
    """查群名，缓存 24 小时。"""
    group_openid = str(group_openid or "").strip()
    if not group_openid:
        return ""
    bot_id = tm.config.platform_bot_id
    cached = await QQBotUIDGroupInfo.get_fresh(bot_id, group_openid)
    if cached is not None:
        return cached.group_name

    status = 0
    data: Any = {}
    try:
        client = await tm.client()
        resp = await client.get(
            f"/v2/groups/{group_openid}/info",
            headers={"Authorization": await tm.authorization()},
        )
        status = resp.status_code
        if status == 200:
            data = resp.json()
    except (httpx.HTTPError, ValueError, RuntimeError) as e:
        logger.warning(f"[QQBotUID] 获取群信息失败（{group_openid}）: {e}")
        await QQBotUIDGroupInfo.upsert(bot_id, group_openid, "")
        return ""

    name = str(data.get("group_name") or "").strip() if isinstance(data, dict) else ""
    members = data.get("group_member_num") if isinstance(data, dict) else 0
    if not name:
        logger.warning(f"[QQBotUID] 群信息接口没给出群名（{group_openid}），HTTP {status}")
    await QQBotUIDGroupInfo.upsert(bot_id, group_openid, name, members if isinstance(members, int) else 0)
    return name


_REFRESH_TASKS: Dict[str, "asyncio.Task[str]"] = {}


async def cached_group_name(tm: "QQTokenManager", group_openid: str) -> str:
    """只读缓存：命中直接给名字，没命中给空并丢后台回填。"""
    group_openid = str(group_openid or "").strip()
    if not group_openid:
        return ""
    row = await QQBotUIDGroupInfo.get_fresh(tm.config.platform_bot_id, group_openid)
    if row is not None:
        return row.group_name
    refresh_group_name(tm, group_openid)
    return ""


def refresh_group_name(tm: "QQTokenManager", group_openid: str) -> "asyncio.Task[str]":
    """后台回填群名；同群并发只打一次接口。"""
    key = f"{tm.config.platform_bot_id}:{group_openid}"
    running = _REFRESH_TASKS.get(key)
    if running is not None and not running.done():
        return running
    task = asyncio.create_task(fetch_group_name(tm, group_openid), name=f"QQBotUID.group_info.{group_openid}")
    _REFRESH_TASKS[key] = task
    task.add_done_callback(lambda done: _refresh_done(key, group_openid, done))
    return task


def _refresh_done(key: str, group_openid: str, task: "asyncio.Task[str]") -> None:
    if _REFRESH_TASKS.get(key) is task:
        _REFRESH_TASKS.pop(key, None)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(f"[QQBotUID] 群信息回填失败（{group_openid}）: {error}")


def cancel_group_refreshes() -> None:
    for task in list(_REFRESH_TASKS.values()):
        task.cancel()
    _REFRESH_TASKS.clear()


def brief_text(value: Any, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}...(共 {len(text)} 字符)"


def _kb(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f}KB"


def _files_endpoint(target_type: str, target_id: str) -> str:
    """富媒体上传端点：群/单聊走 /v2，频道走 /channels。"""
    if not target_id:
        return ""
    if target_type == "group":
        return f"/v2/groups/{target_id}/files"
    if target_type in {"channel", "sub_channel"}:
        return f"/channels/{target_id}/files"
    return f"/v2/users/{target_id}/files"


def public_image_url(image_data: str) -> str:
    url = image_data.removeprefix("link://")
    return url if url.startswith(("http://", "https://")) else ""


def _b64decode(payload: str) -> Optional[bytes]:
    if not payload:
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, binascii.Error):
        return None


_FILE_TYPE_FILE = 4


def split_file_data(data: str) -> Tuple[str, str]:
    name, sep, payload = data.partition("|")
    if not sep:
        return "", name.strip()
    return name.strip(), payload.strip()


async def file_bytes(payload: str) -> Optional[bytes]:
    if payload.startswith("link://"):
        payload = payload[7:]
    if payload.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(payload)
        except httpx.HTTPError as e:
            logger.warning(f"[QQBotUID] 文件下载失败: {payload[:64]} {e}")
            return None
        return resp.content if resp.status_code == 200 else None
    if payload.startswith("data:"):
        return _b64decode(payload.partition(",")[2])
    if payload.startswith("file://"):
        path = Path(payload[7:])
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_bytes)
    if not payload.startswith("base64://"):
        path = Path(payload)
        if await asyncio.to_thread(path.is_file):
            return await asyncio.to_thread(path.read_bytes)
    return _b64decode(payload.removeprefix("base64://"))


def image_meta(raw: bytes) -> Tuple[str, str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "", ""


def image_size(raw: bytes) -> Optional[Tuple[int, int]]:
    try:
        with Image.open(BytesIO(raw)) as img:
            return img.size
    except Exception:
        return None


@dataclass(slots=True)
class MaterialImage:
    url: str
    size: Optional[Tuple[int, int]] = None


_FALLBACK_TAG = "_qqbotuid_fallback"
_PLAIN_URL_TAG = "_qqbotuid_plain_url"


def patch_plain_url_image() -> None:
    current = getattr(core_segment, "_convert_message_to_image", None)
    if current is None or getattr(current, _PLAIN_URL_TAG, False):
        return

    async def _convert(message: Message, bot_id: str, bot_self_id: str) -> List[Message]:
        data = message.data
        if isinstance(data, str) and data.startswith(("http://", "https://")):
            message = Message(type="image", data=f"link://{data}")
        return await current(message, bot_id, bot_self_id)

    setattr(_convert, _PLAIN_URL_TAG, True)
    core_segment._convert_message_to_image = _convert


def patch_image_fallback() -> None:
    """接管核心的 _image_to_url，给没有图床的情况补一条 base64 退路。"""
    patch_plain_url_image()
    current = getattr(core_segment, "_image_to_url", None)
    if current is None or getattr(current, _FALLBACK_TAG, False):
        return

    async def _fallback(image: Union[str, bytes], send_type: str, message: Message) -> List[Message]:
        has_host = core_segment.IS_UPLOAD and core_segment.pclient is not None
        no_host = (send_type == "link_remote" and not has_host) or (
            send_type == "link_local" and not core_segment.pic_srv
        )
        if not no_host:
            return await current(image, send_type, message)
        fallback = [core_segment.MessageSegment.image(image)]
        size = image_size(image) if isinstance(image, bytes) else None
        if size is not None:
            fallback.append(core_segment.MessageSegment.image_size(size))
        return fallback

    setattr(_fallback, _FALLBACK_TAG, True)
    core_segment._image_to_url = _fallback


async def core_upload_image(raw: bytes) -> Optional[MaterialImage]:
    if not pic_upload_config.get_config("EnablePicSrv").data:
        return None
    content_type, _ = image_meta(raw)
    if not content_type:
        logger.warning("[QQBotUID] 图床只支持 png/jpeg/gif/webp，已跳过该图片")
        return None
    if core_segment.pclient is not None:
        send_type = "link_remote"
    elif core_segment.SERVER == "local" and core_segment.pic_srv:
        send_type = "link_local"
    else:
        logger.debug("[QQBotUID] 图片转链接已开启，但没拿到图床客户端（检查 PicUploader 是否配好）")
        return None
    try:
        messages = await core_segment._image_to_url(raw, send_type, Message(type="image", data=raw))
    except Exception as e:
        logger.warning(f"[QQBotUID] 核心图床上传失败，改用素材管理: {e}")
        return None
    for message in messages:
        if message.type == "image" and isinstance(message.data, str):
            url = public_image_url(message.data)
            if url:
                logger.debug(f"[QQBotUID] 图片已转链接(核心图床): {url[:64]}")
                return MaterialImage(url=url, size=image_size(raw))
    return None


async def _post_json(
    tm: QQTokenManager,
    endpoint: str,
    payload: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    client = await tm.client()
    resp = await client.post(endpoint, json=payload, headers={"Authorization": await tm.authorization()})
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not data and resp.text:
        data = {"text": resp.text}
    logger.debug(f"[QQBotUID] POST {endpoint} -> {resp.status_code} {brief_text(data)}")
    return resp.status_code, data


class QQMateriaHost:
    MAX_BYTES = 10 * 1024 * 1024
    QUOTA_EXCEEDED = 40035007
    RELEASE_COUNT = 10
    CLEANUP_DELAY = 10

    def __init__(self, token_manager: QQTokenManager):
        self.tm = token_manager
        self._tasks: set[asyncio.Task[Any]] = set()

    async def upload(self, raw: bytes) -> Optional[MaterialImage]:
        if len(raw) > self.MAX_BYTES:
            logger.warning(f"[QQBotUID] 图片 {len(raw)} 字节超过素材管理 10MB 上限，跳过")
            return None
        content_type, suffix = image_meta(raw)
        if not content_type:
            logger.warning("[QQBotUID] 素材管理只支持 png/jpeg/gif/webp，已跳过该图片")
            return None
        digest = hashlib.sha1(raw).hexdigest()
        md5 = hashlib.md5(raw).hexdigest()
        request = {
            "file_name": f"qqbotuid_{digest[:12]}.{suffix}",
            "file_size": len(raw),
            "content_type": content_type,
            "md5": md5,
            "sha1": digest,
        }
        status, prepared = await self._post("/v2/media-images/prepare", request)
        if status == self.QUOTA_EXCEEDED and await self._release_quota():
            status, prepared = await self._post("/v2/media-images/prepare", request)
        upload_id = str(prepared.get("upload_id") or "")
        presigned_url = str(prepared.get("presigned_url") or "")
        if not upload_id or not presigned_url:
            return None
        if not await self._put(presigned_url, raw, content_type):
            return None
        _, completed = await self._post("/v2/media-images/complete", {"upload_id": upload_id, "md5": md5})
        image_id = str(completed.get("image_id") or "")
        url = str(completed.get("url") or "")
        if not url:
            return None
        if image_id:
            _MATERIALS.set(_MATERIALS.get() + (image_id,))
        logger.info(
            f"[QQBotUID] 素材上传成功: {_kb(len(raw))} {suffix} -> {image_id or '-'} "
            f"{url[:48]}{'...' if len(url) > 48 else ''}"
        )
        return MaterialImage(url=url, size=image_size(raw))

    def schedule_cleanup(self) -> None:
        pending = _MATERIALS.get()
        _MATERIALS.set(())
        for image_id in pending:
            task = asyncio.create_task(self._delete_later(image_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if pending:
            logger.debug(f"[QQBotUID] 素材排队删除({self.CLEANUP_DELAY}s): {', '.join(pending)}")

    async def _delete_later(self, image_id: str) -> None:
        await asyncio.sleep(self.CLEANUP_DELAY)
        client = await self.tm.client()
        resp = await client.delete(
            f"/v2/media-images/{image_id}/delete",
            headers={"Authorization": await self.tm.authorization()},
        )
        if resp.is_success:
            logger.info(f"[QQBotUID] 素材已删除: {image_id}")
            return
        logger.warning(f"[QQBotUID] 素材删除失败: {image_id} HTTP {resp.status_code} {brief_text(resp.text)}")

    async def _release_quota(self) -> bool:
        client = await self.tm.client()
        headers = {"Authorization": await self.tm.authorization()}
        resp = await client.get("/v2/media-images", headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}
        images = data.get("images") if isinstance(data, dict) else None
        if not resp.is_success or not isinstance(images, list) or not images:
            logger.warning(f"[QQBotUID] 素材配额已满且取不到旧图列表: HTTP {resp.status_code} {str(data)[:200]}")
            return False
        ordered = sorted(
            [item for item in images if isinstance(item, dict) and item.get("image_id")],
            key=lambda item: int(item.get("created_at") or 0),
        )
        for item in ordered[: self.RELEASE_COUNT]:
            await client.delete(f"/v2/media-images/{item['image_id']}/delete", headers=headers)
        released = [str(item["image_id"]) for item in ordered[: self.RELEASE_COUNT]]
        logger.info(f"[QQBotUID] 素材配额已满，删掉最旧的 {len(released)} 张后重试: {', '.join(released)}")
        return True

    async def _post(self, endpoint: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        logger.debug(f"[QQBotUID] 素材 POST {endpoint} {brief_text(payload)}")
        status, data = await _post_json(self.tm, endpoint, payload)
        if 200 <= status < 300:
            raw_nested = data.get("data")
            nested = raw_nested if isinstance(raw_nested, dict) else {}
            return status, {**nested, **data}
        logger.warning(f"[QQBotUID] 素材管理 {endpoint} 失败: HTTP {status} {brief_text(data)}")
        return status, data

    async def _put(self, url: str, raw: bytes, content_type: str) -> bool:
        """预签名 URL 是 COS 地址，不带 Authorization；Content-Type 必须和 prepare 时一致。"""
        logger.debug(f"[QQBotUID] 素材 PUT {url[:80]} {_kb(len(raw))} {content_type}")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.put(url, content=raw, headers={"Content-Type": content_type})
        if 200 <= resp.status_code < 300:
            logger.debug(f"[QQBotUID] 素材 PUT -> {resp.status_code} etag={resp.headers.get('etag', '')}")
            return True
        logger.warning(f"[QQBotUID] 素材直传失败: HTTP {resp.status_code} {brief_text(resp.text)}")
        return False


class QQSender:
    def __init__(self, token_manager: QQTokenManager):
        self.tm = token_manager

    async def post_json(self, endpoint: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        logger.debug(f"[QQBotUID] POST {endpoint} {brief_text(payload, 500)}")
        status, data = await _post_json(self.tm, endpoint, payload)
        return 200 <= status < 300, data

    async def delete(self, endpoint: str) -> bool:
        client = await self.tm.client()
        logger.debug(f"[QQBotUID] DELETE {endpoint}")
        resp = await client.delete(endpoint, headers={"Authorization": await self.tm.authorization()})
        logger.debug(f"[QQBotUID] DELETE {endpoint} -> {resp.status_code} {brief_text(resp.text)}")
        return 200 <= resp.status_code < 300

    async def set_group_member_mute(self, group_id: str, member_openid: str, seconds: int) -> bool:
        if not group_id or not member_openid:
            return False
        endpoint = f"/v2/groups/{group_id}/restrict_chat_setting"
        if seconds <= 0:
            ok, data = await self.post_json(endpoint, {"members": [{"op": "del", "member_openid": member_openid}]})
            if not ok:
                logger.warning(f"[QQBotUID] 解除禁言失败: {member_openid} {brief_text(data)}")
            return ok
        expire_at = (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        member = {"member_openid": member_openid, "mute_expire_at": expire_at}
        ok, data = await self.post_json(endpoint, {"members": [{"op": "add", **member}]})
        if ok:
            return True
        ok, data = await self.post_json(endpoint, {"members": [{"op": "update", **member}]})
        if not ok:
            logger.warning(f"[QQBotUID] 禁言失败: {member_openid} {seconds}s {brief_text(data)}")
        return ok

    async def upload_media(
        self,
        target_type: str,
        target_id: str,
        image_data: str,
        file_type: int = 1,
    ) -> Optional[str]:
        endpoint = _files_endpoint(target_type, target_id)
        if not endpoint:
            return None
        url = public_image_url(image_data)
        if url:
            file_info = await self._upload_json(endpoint, {"srv_send_msg": False, "file_type": file_type, "url": url})
            if file_info:
                return file_info
        raw = await file_bytes(image_data)
        if raw is None:
            logger.warning(f"[QQBotUID] 取不到图片内容，跳过上传: {image_data[:64]}")
            return None
        return await self._upload_json(
            endpoint,
            {
                "srv_send_msg": False,
                "file_type": file_type,
                "file_data": base64.b64encode(raw).decode(),
            },
        )

    async def upload_file(self, target_type: str, target_id: str, file_data: str) -> Optional[str]:
        """file 段走 /files 上传，返回 file_info（发送时塞进 media）。"""
        endpoint = _files_endpoint(target_type, target_id)
        if not endpoint:
            return None
        name, payload = split_file_data(file_data)
        if not payload:
            logger.warning(f"[QQBotUID] 文件内容为空，跳过发送: {name or '-'}")
            return None
        url = payload.removeprefix("link://")
        if url.startswith(("http://", "https://")):
            body: Dict[str, Any] = {"srv_send_msg": False, "file_type": _FILE_TYPE_FILE, "url": url}
            if name:
                body["file_name"] = name
            file_info = await self._upload_json(endpoint, body)
            if file_info:
                logger.info(f"[QQBotUID] 文件上传成功: {name or url[:48]}")
                return file_info
        raw = await file_bytes(payload)
        if raw is None:
            logger.warning(f"[QQBotUID] 取不到文件内容，跳过发送: {name or payload[:64]}")
            return None
        file_info = await self._upload_json(
            endpoint,
            {
                "srv_send_msg": False,
                "file_type": _FILE_TYPE_FILE,
                "file_name": name or "file",
                "file_data": base64.b64encode(raw).decode(),
            },
        )
        if file_info:
            logger.info(f"[QQBotUID] 文件上传成功: {name or 'file'} {_kb(len(raw))}")
        return file_info

    async def _upload_json(self, endpoint: str, payload: Dict[str, Any]) -> Optional[str]:
        logger.debug(f"[QQBotUID] 富媒体 POST {endpoint} {brief_text(payload)}")
        status, data = await _post_json(self.tm, endpoint, payload)
        if 200 <= status < 300:
            raw_nested = data.get("data")
            nested = raw_nested if isinstance(raw_nested, dict) else {}
            return data.get("file_info") or nested.get("file_info")
        logger.warning(f"[QQBotUID] 富媒体上传失败: HTTP {status} {brief_text(data)}")
        return None
