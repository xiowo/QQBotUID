import re
import html
from typing import Any, Dict, List, Tuple, Iterable, Optional
from urllib.parse import unquote

from gsuid_core.logger import logger
from gsuid_core.models import NODE_MARK, Message, MessageSend, MessageReceive, format_node_preview

_AT_PATTERN = re.compile(r"<@!?[A-Za-z0-9_\-]+>")
_FACE_PATTERN = re.compile(r'<faceType=\d+,faceId="([^"]+)",ext="[^"]*">')
_INLINE_ATTACHMENT_PATTERN = re.compile(
    r'<attachmentType="(?P<content_type>[^"]+)"[^>]*>\s*<(?P<url>https?://[^>\s]+)>',
    re.IGNORECASE,
)
# 文本附件，形如 <attachmentType="image/jpeg",attachmentIndex=0>
_ATTACHMENT_MARKER_PATTERN = re.compile(r"<attachmentType=[^>]*>", re.IGNORECASE)
_IMAGE_MARKER_PATTERN = re.compile(r"<attachmentType=[\"']?image/", re.IGNORECASE)
# 引用和合并转发塞进 msg_elements[].content 的文本里，按格式反解析
_BLOCK_SPLIT_PATTERN = re.compile(r"(?m)^\s*(?:===\s*消息\s*\d+\s*===|---\s*第\d+条\s*---)\s*$")
_BLOCK_CONTENT_PATTERN = re.compile(
    r"(?ms)^\s*\[消息内容\]\s*(.*?)(?=^\s*\[(?:发送者|消息类型|关联消息)\]|^\s*---\s*第\d+条\s*---|^\s*--\s*$|\Z)"
)
_BLOCK_SENDER_PATTERN = re.compile(r"(?m)^\s*\[发送者\]\s*(.*?)\s*$")
_BLOCK_IMAGE_PATTERN = re.compile(r"(?im)^\s*\[附件\d*\].*?类型\s*:\s*图片\b.*?\bURL\s*:\s*(https?://\S+)")

_BLOCK_ATTACHMENT_LINE_PATTERN = re.compile(r"(?im)^[ \t]*\[附件\d*\][^\n]*$")
_BARE_URL_PATTERN = re.compile(r"<?https?://\S+>?")
_BIDI_CONTROL_PATTERN = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_SCENE_REF_KEYS = ("ref_msg_idx", "ref_msg_id", "reference_id", "message_reference_id")

_SCENE_OWN_IDX_KEY = "msg_idx"
REFERENCE_MESSAGE_TYPE = 103

GROUP_EVENTS = {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
DIRECT_EVENTS = {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}
CHANNEL_EVENTS = {"AT_MESSAGE_CREATE", "MESSAGE_CREATE"}
LIFECYCLE_EVENTS = {"GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE", "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"}
FORWARD_KINDS = {"node", "nodes", "forward", "forward_msg", "forward_message"}


def _strip_attachment_markers(value: str) -> str:
    return _ATTACHMENT_MARKER_PATTERN.sub("", value)


def _clean_text(content: Any) -> str:
    text = "" if content is None else str(content)
    text = _strip_attachment_markers(text)
    text = _FACE_PATTERN.sub(r"[face id=\1]", text)
    text = _AT_PATTERN.sub("", text)
    return html.unescape(text).strip()


def _extract_inline_attachment_images(text: Any) -> Tuple[str, List[str]]:
    if not text:
        return "", []
    raw_text = str(text)
    image_urls: List[str] = []

    def _replace(match: re.Match[str]) -> str:
        content_type = str(match.group("content_type") or "").lower()
        url = html.unescape(str(match.group("url") or "").strip())
        if url and "image" in content_type and url not in image_urls:
            image_urls.append(url)
        return ""

    cleaned = _INLINE_ATTACHMENT_PATTERN.sub(_replace, raw_text)
    return _clean_text(cleaned), image_urls


def _attachment_to_message(attachment: Dict[str, Any]) -> Optional[Message]:
    url = attachment.get("url")
    if not url:
        return None
    url = html.unescape(str(url))
    content_type = str(attachment.get("content_type") or "").lower()
    file_name = str(attachment.get("filename") or "file")
    if "image" in content_type:
        return Message(type="image", data=url)
    if "audio" in content_type or "voice" in content_type:
        return Message(type="record", data=url)
    if "video" in content_type:
        return Message(type="video", data=url)
    return Message(type="file", data=f"{file_name}|{url}")


def _append_unique_image(content: List[Message], seen: set[str], image_url: str) -> None:
    image_url = html.unescape(str(image_url or "").strip())
    if not image_url or image_url in seen:
        return
    content.append(_image_message(image_url))
    seen.add(image_url)


def _attachment_image_urls(attachments: Any) -> List[str]:
    """从 attachments 里按顺序取图片 URL 并去重"""
    urls: List[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        content_type = str(attachment.get("content_type") or "").lower()
        url = html.unescape(str(attachment.get("url") or "").strip())
        if url and "image" in content_type and url not in urls:
            urls.append(url)
    return urls


def _extract_msg_element_image_urls(msg_elements: Any) -> List[str]:
    image_urls: List[str] = []
    if not isinstance(msg_elements, list):
        return image_urls
    for element in msg_elements:
        if not isinstance(element, dict):
            continue
        for url in _attachment_image_urls(element.get("attachments")):
            if url not in image_urls:
                image_urls.append(url)
    return image_urls


def _sanitize_display_text(value: Any) -> str:
    return _BIDI_CONTROL_PATTERN.sub("", str(value or "")).strip()


def _normalize_node_content(value: Any) -> str:
    content = _sanitize_display_text(value)
    has_image_marker = bool(_IMAGE_MARKER_PATTERN.search(content))
    cleaned, image_urls = _extract_inline_attachment_images(content)
    cleaned = _strip_attachment_markers(cleaned).strip()
    if not cleaned and (has_image_marker or image_urls):
        return "[图片]"
    return cleaned


def _extract_block_image_url(block: str) -> str:
    matched = _BLOCK_IMAGE_PATTERN.search(block)
    return matched.group(1).rstrip("，,。") if matched else ""


def _image_message(image_url: str) -> Message:
    image_url = html.unescape(str(image_url or "").strip())
    return Message(type="image", data=image_url)


def _extract_associated_node_items(value: Any) -> List[Message]:
    text = str(value or "")
    if "[关联消息]" in text:
        associated = text.split("[关联消息]", 1)[1]
    elif "[群聊的聊天记录]" in text:
        associated = text.split("[群聊的聊天记录]", 1)[1]
    else:
        return []

    items: List[Message] = []
    for block in _BLOCK_SPLIT_PATTERN.split(associated)[1:]:
        content_match = _BLOCK_CONTENT_PATTERN.search(block)
        sender_match = _BLOCK_SENDER_PATTERN.search(block)
        content = content_match.group(1).strip() if content_match else ""
        sender = _sanitize_display_text(sender_match.group(1)) if sender_match else ""
        prefix = f"{sender}:" if sender else ""
        _, inline_image_urls = _extract_inline_attachment_images(content)
        image_urls = list(inline_image_urls)
        attachment_url = _extract_block_image_url(block)
        if attachment_url and attachment_url not in image_urls:
            image_urls.append(attachment_url)
        if image_urls:
            if prefix:
                items.append(Message(type="text", data=prefix))
            items.extend(_image_message(url) for url in image_urls)
            continue
        content = _normalize_node_content(content)
        if not content:
            continue
        if prefix:
            items.append(Message(type="text", data=prefix))
        items.append(Message(type="text", data=content))
    return items


def _node_entries_to_messages(entries: List[Any]) -> List[Message]:
    """结构化 node：每条取昵称 + 内容分段。"""
    result: List[Message] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sender = entry.get("sender") or {}
        nickname = sender.get("nickname") if isinstance(sender, dict) else entry.get("name")
        if nickname:
            result.append(Message(type="text", data=f"{_sanitize_display_text(nickname)}:"))
        segments = entry.get("content") or entry.get("message") or entry.get("data")
        if isinstance(segments, str):
            text = _strip_attachment_markers(_sanitize_display_text(segments)).strip()
            if text:
                result.append(Message(type="text", data=text))
            continue
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            data = segment.get("data")
            if segment_type == "text":
                data = data.get("text", "") if isinstance(data, dict) else data
            elif segment_type == "image" and isinstance(data, dict):
                data = data.get("url") or data.get("file")
            if not data:
                continue
            if segment_type == "image":
                result.append(_image_message(str(data)))
            else:
                result.append(Message(type=segment_type, data=str(data)))
    return result or [Message(type="text", data=NODE_MARK)]


def _extract_node_items(elements: Any) -> List[Message]:
    items: List[Message] = []
    has_forward = False
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        kind = str(element.get("type") or element.get("message_type") or "").lower()
        raw_data = element.get("data")
        payload = raw_data if isinstance(raw_data, dict) else element
        structured = isinstance(payload.get("nodes"), list) or isinstance(payload.get("messages"), list)
        has_forward = has_forward or kind in FORWARD_KINDS or structured
        entries = payload.get("nodes") or payload.get("messages") or payload.get("content")
        if isinstance(entries, list):
            items.extend(_node_entries_to_messages(entries))
            continue
        items.extend(_extract_associated_node_items(element.get("content")))
    return items or ([Message(type="text", data=NODE_MARK)] if has_forward else [])


def _query_value(item: str, keys: Tuple[str, ...]) -> str:
    query = item.split("?", 1)[-1]
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if value and unquote(key) in keys:
            return unquote(value).strip()
    return ""


def _scene_reference_id(scene: Any, allow_own_idx: bool) -> str:
    if not isinstance(scene, dict):
        return ""
    ext = scene.get("ext")
    items = [ext] if isinstance(ext, str) else (ext if isinstance(ext, list) else [])
    keys = (_SCENE_REF_KEYS + (_SCENE_OWN_IDX_KEY,)) if allow_own_idx else _SCENE_REF_KEYS
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        found = _query_value(item, keys)
        if found:
            return found
    return ""


def _extract_reference_id(d: Dict[str, Any]) -> str:
    """引用消息 ID：显式字段 → message_scene.ext → 引用消息的 msg_elements[].msg_idx。"""
    reference = d.get("message_reference")
    if isinstance(reference, dict):
        reply_id = str(reference.get("message_id") or reference.get("id") or "").strip()
        if reply_id:
            return reply_id
    for key in ("message_reference_id", "reference_message_id"):
        value = str(d.get(key) or "").strip()
        if value:
            return value

    scene = d.get("message_scene")
    explicit = _scene_reference_id(scene, allow_own_idx=False)
    if explicit:
        return explicit

    element_ids = [
        str(item.get("msg_idx") or "").strip()
        for item in d.get("msg_elements") or []
        if isinstance(item, dict) and str(item.get("msg_idx") or "").strip()
    ]
    own_idx = _scene_reference_id(scene, allow_own_idx=True)
    if own_idx and own_idx in element_ids:
        return own_idx
    if str(d.get("message_type") or "") != str(REFERENCE_MESSAGE_TYPE):
        return ""
    return own_idx or (element_ids[0] if element_ids else "")


def is_reference_message(payload: Dict[str, Any]) -> bool:
    d = payload.get("d")
    if not isinstance(d, dict):
        return False
    return str(d.get("message_type") or "") == str(REFERENCE_MESSAGE_TYPE)


def is_bot_message(payload: Dict[str, Any]) -> bool:
    d = payload.get("d")
    if not isinstance(d, dict):
        return False
    author = d.get("author")
    if not isinstance(author, dict):
        return False
    flag = author.get("bot")
    if isinstance(flag, bool):
        return flag
    return str(flag or "").strip().lower() in {"1", "true", "yes"}


def _reference_elements(elements: Any, reply_id: str) -> List[Dict[str, Any]]:
    items = [item for item in elements if isinstance(item, dict)] if isinstance(elements, list) else []
    if not reply_id:
        return items
    matched = [item for item in items if str(item.get("msg_idx") or "").strip() == reply_id]
    return matched or items


def _normalize_reference_text(value: str) -> str:
    raw = str(value or "").strip()
    text = raw
    if "[关联消息]" in text:
        text = text.split("[关联消息]", 1)[1]
    contents = [item.strip() for item in _BLOCK_CONTENT_PATTERN.findall(text) if item.strip()]
    text = "\n".join(contents) if contents else text
    cleaned = _strip_attachment_markers(text)
    cleaned = _BLOCK_ATTACHMENT_LINE_PATTERN.sub("", cleaned).strip()
    return cleaned


def _extract_reference_text(elements: Any) -> str:
    texts: List[str] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        value = element.get("content") or ""
        if isinstance(value, str) and value.strip():
            normalized = _normalize_reference_text(value)
            if normalized:
                texts.append(normalized)
    return "\n".join(texts)


def _reference_image_urls(elements: Any) -> List[str]:
    urls: List[str] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        for url in _attachment_image_urls(element.get("attachments")):
            if url not in urls:
                urls.append(url)
        value = element.get("content")
        if not isinstance(value, str) or not value.strip():
            continue
        _, inline_urls = _extract_inline_attachment_images(value)
        for url in inline_urls:
            if url not in urls:
                urls.append(url)
        for url in _BLOCK_IMAGE_PATTERN.findall(value):
            url = url.rstrip("，,。")
            if url and url not in urls:
                urls.append(url)
    return urls


def _build_reference_content(elements: Any, seen_images: set[str], reply_id: str = "") -> List[Message]:
    """引用转换"""
    picked = _reference_elements(elements, reply_id)
    node_items = _extract_node_items(picked)
    if node_items:
        return [
            Message(type="reply", data=format_node_preview(node_items)),
            Message(type="node", data=node_items),
        ]
    result: List[Message] = []
    image_urls = _reference_image_urls(picked)
    reply_text = _extract_reference_text(picked)
    if image_urls and _BARE_URL_PATTERN.fullmatch(reply_text):
        reply_text = ""
    if not reply_text and not image_urls:
        return []
    result.append(Message(type="reply", data=reply_text))
    for image_url in image_urls:
        _append_unique_image(result, seen_images, image_url)
    return result


def _author_id(author: Dict[str, Any]) -> str:
    return str(author.get("member_openid") or author.get("id") or author.get("union_openid") or "")


def _event_user_id(d: Dict[str, Any]) -> str:
    raw_author = d.get("author")
    author = raw_author if isinstance(raw_author, dict) else {}
    return str(
        _author_id(author)
        or d.get("op_member_openid")
        or d.get("user_openid")
        or d.get("member_openid")
        or d.get("group_member_openid")
        or ""
    )


def _build_qqbot_member_avatar(appid: str, member_openid: str) -> str:
    """官机用户头像，appid 用配置里存的那个。"""
    member_openid = str(member_openid or "").strip()
    if appid and member_openid:
        return f"https://q.qlogo.cn/qqapp/{appid}/{member_openid}/0"
    return "qqbot://user-avatar/unknown"


def _sender(
    d: Dict[str, Any],
    user_id: str,
    group_id: Optional[str],
    appid: str,
    group_name: str = "",
    nickname: str = "",
) -> Dict[str, Any]:
    raw_author = d.get("author")
    author = raw_author if isinstance(raw_author, dict) else {}
    name = str(author.get("username") or "").strip() or nickname
    avatar = _build_qqbot_member_avatar(appid, user_id)
    sender = {
        "nickname": name,
        "user_name": name,
        "avatar": avatar,
        "user_icon": avatar,
    }
    if group_id:
        sender["group_name"] = group_name or f"群聊-{str(group_id)[:8]}"
    return sender


async def fallback_nickname(d: Any, bot_id: str) -> str:
    if not isinstance(d, dict):
        return ""
    raw_author = d.get("author")
    author = raw_author if isinstance(raw_author, dict) else {}
    if str(author.get("username") or "").strip():
        return ""
    user_id = _event_user_id(d)
    if not bot_id or not user_id:
        return ""
    try:
        from gsuid_core.utils.database.models import CoreUser

        rows = await CoreUser.select_rows(bot_id=bot_id, user_id=user_id)
    except Exception as error:
        logger.debug(f"[QQBotUID] 查核心用户昵称失败: {type(error).__name__}: {error}")
        return ""
    for row in sorted(rows or [], key=lambda item: getattr(item, "id", 0) or 0, reverse=True):
        name = str(getattr(row, "user_name", "") or "").strip()
        if not name or name == "1" or name == user_id:
            continue
        return name
    return ""


def _meta_message(
    event_type: str,
    d: Dict[str, Any],
    user_id: str,
    group_id: Optional[str],
) -> Optional[Message]:
    """生命周期事件统一成核心认的 meta 名称，方便核心侧做统一处理。"""
    if event_type == "GROUP_MEMBER_ADD":
        return Message(type="meta-user_join_group", data={"user_id": user_id, "group_id": group_id})
    if event_type == "GROUP_MEMBER_REMOVE":
        return Message(type="meta-user_exit_group", data={"user_id": user_id, "group_id": group_id})
    if event_type in {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"}:
        operator = str(d.get("op_member_openid") or user_id or "")
        data: Dict[str, Any] = {"group_id": group_id}
        if operator:
            data["user_id"] = operator
            data["operator_id"] = operator
        if d.get("timestamp") is not None:
            data["timestamp"] = d.get("timestamp")
        name = "bot_join_group" if event_type == "GROUP_ADD_ROBOT" else "bot_exit_group"
        return Message(type=f"meta-{name}", data=data)
    return None


def event_to_receive(
    payload: Dict[str, Any],
    bot_id: str,
    bot_self_id: str,
    appid: str,
    group_name: str = "",
    nickname: str = "",
) -> Optional[MessageReceive]:
    event_type = str(payload.get("t") or "")
    d = payload.get("d") or {}
    if not isinstance(d, dict):
        return None

    if event_type in GROUP_EVENTS or event_type in LIFECYCLE_EVENTS:
        user_type = "group"
        target_id = str(d.get("group_openid") or d.get("group_id") or "")
    elif event_type in DIRECT_EVENTS:
        user_type = "direct"
        target_id = None
    elif event_type in CHANNEL_EVENTS:
        user_type = "channel"
        target_id = str(d.get("channel_id") or "")
    else:
        user_id = _event_user_id(d)
        group_id = str(d.get("group_openid") or d.get("group_id") or "") or None
        return MessageReceive(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            msg_id=str(payload.get("id") or d.get("id") or ""),
            user_type="group",
            group_id=group_id,
            user_id=user_id,
            sender=_sender(d, user_id, group_id, appid, group_name, nickname),
            content=[Message(type=f"meta-{event_type}", data=d)],
        )

    user_id = _event_user_id(d)
    content: List[Message] = []
    seen_images: set[str] = set()

    meta = _meta_message(event_type, d, user_id, target_id)
    if meta:
        return MessageReceive(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            msg_id=str(payload.get("id") or d.get("id") or ""),
            user_type=user_type,  # type: ignore[arg-type]
            group_id=target_id,
            user_id=user_id,
            sender=_sender(d, user_id, target_id, appid, group_name, nickname),
            content=[meta],
        )
    reply_id = _extract_reference_id(d)
    if reply_id:
        content.append(Message(type="reply_id", data=reply_id))

    elements = d.get("msg_elements")
    node_items = _extract_node_items(elements)

    raw_text = d.get("content")
    text, inline_image_urls = _extract_inline_attachment_images(raw_text)
    text_node_items = _extract_associated_node_items(raw_text)

    if text_node_items:
        content.append(Message(type="node", data=text_node_items))
    elif text:
        content.append(Message(type="text", data=text))

    for image_url in inline_image_urls:
        _append_unique_image(content, seen_images, image_url)

    for attachment in d.get("attachments") or []:
        if isinstance(attachment, dict):
            converted = _attachment_to_message(attachment)
            if converted:
                if converted.type == "image":
                    _append_unique_image(content, seen_images, str(converted.data or ""))
                else:
                    content.append(converted)

    if not node_items and not reply_id:
        for image_url in _extract_msg_element_image_urls(elements):
            _append_unique_image(content, seen_images, image_url)

    if reply_id:
        content.extend(_build_reference_content(elements, seen_images, reply_id))
    elif node_items and not text_node_items:
        content.append(Message(type="node", data=node_items))

    mentions = d.get("mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mid = mention.get("member_openid") or mention.get("id")
            if mention.get("is_you"):
                content.append(Message(type="at", data=bot_self_id))
            elif mid:
                content.append(Message(type="at", data=str(mid)))

    if event_type in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"} and not any(
        msg.type == "at" and str(msg.data) == str(bot_self_id) for msg in content
    ):
        content.insert(0, Message(type="at", data=bot_self_id))

    if not content:
        content.append(Message(type="text", data=""))

    return MessageReceive(
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        msg_id=str(d.get("id") or payload.get("id") or ""),
        user_type=user_type,  # type: ignore[arg-type]
        group_id=target_id,
        user_id=user_id,
        sender=_sender(d, user_id, target_id, appid, group_name, nickname),
        content=content,
    )


def messages_to_plain_text(messages: Iterable[Message]) -> str:
    parts: List[str] = []
    for msg in messages:
        if msg.type == "text" and msg.data is not None:
            parts.append(str(msg.data))
        elif msg.type == "at" and msg.data is not None:
            parts.append(f"<@{msg.data}>")
        elif msg.type == "record":
            parts.append("[语音]")
        elif msg.type == "video":
            parts.append("[视频]")
        elif msg.type == "file":
            parts.append("[文件]")
    return "".join(parts)


def normalize_buttons(buttons: Any) -> Optional[List[List[Dict[str, Any]]]]:
    if not buttons:
        return None
    rows = buttons
    if isinstance(buttons, dict):
        rows = buttons.get("rows") or buttons.get("buttons") or buttons.get("btns") or []
    if not isinstance(rows, list):
        return None

    normalized_rows: List[List[Dict[str, Any]]] = []
    for row in rows:
        if isinstance(row, dict) and ("buttons" in row or "btns" in row):
            row_buttons = row.get("buttons") or row.get("btns") or []
        elif isinstance(row, list):
            row_buttons = row
        else:
            row_buttons = [row]

        normalized_buttons: List[Dict[str, Any]] = []
        for button in row_buttons:
            normalized = normalize_button(button)
            if normalized:
                normalized_buttons.append(normalized)
        if normalized_buttons:
            normalized_rows.append(normalized_buttons)
    return normalized_rows or None


def normalize_button(button: Any) -> Optional[Dict[str, Any]]:
    if isinstance(button, str):
        return {"text": button, "data": button}
    if not isinstance(button, dict):
        names = (
            "text",
            "data",
            "pressed_text",
            "style",
            "action",
            "permisson",
            "specify_role_ids",
            "specify_user_ids",
            "unsupport_tips",
        )
        attrs = {name: getattr(button, name, None) for name in names}
        if not any(value is not None for value in attrs.values()):
            return None
        button = attrs

    text = str(button.get("text") or "").strip()
    data = str(button.get("data") or text).strip()
    if not text and not data:
        return None

    action_type = button.get("action")
    if action_type == -1:
        action_type = button.get("type", 2)
    elif not isinstance(action_type, int):
        action_type = button.get("type", 2)

    normalized: Dict[str, Any] = {
        "text": text or data,
        "show": str(button.get("pressed_text") or text or data),
        "style": button.get("style", 1),
        "type": action_type,
        "data": data,
        "tips": str(button.get("unsupport_tips") or "您的客户端暂不支持该功能, 请升级后适配..."),
    }

    permission_type = button.get("permisson")
    if permission_type is not None:
        permission: Dict[str, Any] = {"type": permission_type}
        specify_role_ids = button.get("specify_role_ids")
        specify_user_ids = button.get("specify_user_ids")
        if specify_role_ids:
            permission["specify_role_ids"] = specify_role_ids
        if specify_user_ids:
            permission["specify_user_ids"] = specify_user_ids
        normalized["permission"] = permission

    return normalized


def build_keyboard(rows: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """核心的按钮行 → QQ 官方 keyboard"""
    built_rows: List[Dict[str, Any]] = []
    index = 0
    for row in rows:
        built_buttons: List[Dict[str, Any]] = []
        for button in row:
            label = str(button.get("text") or "")
            built_buttons.append(
                {
                    "id": str(index),
                    "render_data": {
                        "label": label,
                        "visited_label": str(button.get("show") or label),
                        "style": button.get("style", 1),
                    },
                    "action": {
                        "type": button.get("type", 2),
                        "data": str(button.get("data") or label),
                        "permission": button.get("permission") or {"type": 2},
                        "unsupport_tips": button.get("tips") or "您的客户端暂不支持该功能, 请升级后适配...",
                    },
                }
            )
            index += 1
        if built_buttons:
            built_rows.append({"buttons": built_buttons})
    return {"content": {"rows": built_rows}}


def _message_path(send: MessageSend) -> Optional[str]:
    if not send.target_id:
        return None
    if send.target_type == "group":
        return f"/v2/groups/{send.target_id}/messages"
    if send.target_type == "direct":
        return f"/v2/users/{send.target_id}/messages"
    if send.target_type in {"channel", "sub_channel"}:
        return f"/channels/{send.target_id}/messages"
    return None


def send_endpoint(send: MessageSend) -> Optional[str]:
    return _message_path(send)


def recall_endpoint(send: MessageSend, message_id: str) -> Optional[str]:
    path = _message_path(send)
    if path is None:
        return None
    suffix = "?hidetip=true" if send.target_type in {"channel", "sub_channel"} else ""
    return f"{path}/{message_id}{suffix}"
