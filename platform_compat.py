"""QQ 双通道兼容层（表情包插件）。

官方族：qq_official / qq_official_webhook（raw 为 botpy 消息对象）
非官方：aiocqhttp（OneBot v11，raw 为 aiocqhttp Event）

只处理插件真正需要的差异：平台判定、作用域键、管理员匹配、合并转发解析。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Forward, Image, Node, Nodes, Plain

OFFICIAL_NAMES = {"qq_official", "qq_official_webhook"}
ONEBOT_NAMES = {"aiocqhttp"}

QQ_FORWARD_MESSAGE_TYPE = 102
FORWARD_URL_RE = re.compile(r"URL:\s*(https?://\S+)")
FORWARD_SUMMARY_MARKERS = ("[附件", "[图片]")
FORWARD_NESTED_KEYS = ("msg_elements", "elements", "messages", "nodes", "records", "record")
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)


def resolve_channel(event: AstrMessageEvent) -> str:
    """按平台适配器名解析通道：official / onebot / 其他空串。"""
    try:
        name = event.get_platform_name()
    except Exception:  # noqa: BLE001
        return ""
    if name in OFFICIAL_NAMES:
        return "official"
    if name in ONEBOT_NAMES:
        return "onebot"
    return ""


def scoped_key(platform_id: str, raw_id: str) -> str:
    """把原始 ID 归一到 platform_id:raw_id 作用域键。"""
    raw_id = str(raw_id or "")
    platform_id = str(platform_id or "")
    return f"{platform_id}:{raw_id}" if platform_id else raw_id


def admin_entry_matches(entry: str, platform_id: str, user_id: str) -> bool:
    """管理员条目匹配。

    - platform_id:user_id：精确匹配平台实例与用户；
    - 无前缀：按当前平台匹配用户（兼容旧配置）。
    """
    entry = str(entry or "").strip()
    if not entry:
        return False
    if ":" in entry:
        pid, uid = entry.split(":", 1)
        return pid == str(platform_id or "") and uid == str(user_id or "")
    return entry == str(user_id or "")


def _message_chain(event) -> list:
    message_obj = getattr(event, "message_obj", None)
    return list(getattr(message_obj, "message", None) or [])


def _raw_message(event):
    return getattr(getattr(event, "message_obj", None), "raw_message", None)


@dataclass
class ForwardPayload:
    """统一的合并转发内容。"""

    text: str = ""
    images: list[Image] = field(default_factory=list)
    saw_body: bool = False
    raw: Any = None


def is_forward_event(event: AstrMessageEvent) -> bool:
    """是否为合并转发消息（官方 102 / OneBot Forward|Node|Nodes）。"""
    channel = resolve_channel(event)
    if channel == "official":
        raw = _raw_message(event)
        if raw is None:
            return False
        try:
            return (
                int(getattr(raw, "message_type", None) or 0)
                == QQ_FORWARD_MESSAGE_TYPE
            )
        except (TypeError, ValueError):
            return False
    if channel == "onebot":
        for comp in _message_chain(event):
            if isinstance(comp, (Forward, Node, Nodes)):
                return True
        return False
    return False


# --------------------------------------------------------------------------
# 官方族：102 摘要 / msg_elements / attachments
# --------------------------------------------------------------------------
def extract_forward_summary_urls(content: str) -> list[str]:
    """从 QQ 官方 102 摘要文本中提取图片 URL。"""
    if not content:
        return []
    urls: list[str] = []
    seen: set = set()

    def add_url(url: str) -> None:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    for line in content.splitlines():
        if "URL:" not in line or not any(
            marker in line for marker in FORWARD_SUMMARY_MARKERS
        ):
            continue
        type_match = re.search(r"类型:\s*([^\s]+)", line)
        if type_match and type_match.group(1) != "图片":
            continue
        for m in FORWARD_URL_RE.finditer(line):
            add_url(m.group(1))
    if not urls:
        for m in re.finditer(
            r"https://multimedia\.nt\.qq\.com\.cn/download\?\S+", content
        ):
            add_url(m.group(0))
    return urls


def collect_forward_urls(elements) -> list[str]:
    """递归提取官方合并转发元素中的图片 URL（去重）。"""
    urls: list[str] = []
    seen: set = set()

    def get_attr(element, key):
        if isinstance(element, dict):
            return element.get(key)
        return getattr(element, key, None)

    def add_url(url: str) -> None:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    def add_attachments(attachments) -> None:
        if not isinstance(attachments, list):
            return
        for att in attachments:
            url = str(get_attr(att, "url") or "").strip()
            if not url:
                continue
            ctype = str(get_attr(att, "content_type") or "").lower()
            filename = str(
                get_attr(att, "filename") or get_attr(att, "name") or ""
            )
            ext = Path(filename).suffix.lower()
            if (
                ctype.startswith("image")
                or ext in ALLOWED_EXTS
                or "multimedia.nt.qq.com.cn" in url
            ):
                add_url(url)

    def handle_element(element) -> None:
        if element is None:
            return
        if isinstance(element, str):
            for url in extract_forward_summary_urls(element):
                add_url(url)
            return
        add_attachments(get_attr(element, "attachments"))
        content = get_attr(element, "content")
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    for item in parsed:
                        handle_element(item)
                elif isinstance(parsed, dict):
                    handle_element(parsed)
                else:
                    for url in extract_forward_summary_urls(content):
                        add_url(url)
            else:
                for url in extract_forward_summary_urls(content):
                    add_url(url)
        for key in FORWARD_NESTED_KEYS:
            nested = get_attr(element, key)
            if isinstance(nested, list):
                for item in nested:
                    handle_element(item)

    if isinstance(elements, list):
        for element in elements:
            handle_element(element)
    else:
        handle_element(elements)
    return urls


def describe_raw_structure(raw) -> str:
    """简要描述原始消息结构（只打印键名与短文本，避免刷爆日志）。"""
    if raw is None:
        return "raw=None"
    if isinstance(raw, dict):
        return "keys=" + ",".join(str(k) for k in raw)
    attrs = [
        name
        for name in ("content", "message_type", "attachments", "msg_elements")
        if hasattr(raw, name)
    ]
    text = getattr(raw, "content", None)
    raw_data = getattr(raw, "raw_data", None)
    desc = "attrs=" + ",".join(attrs)
    if isinstance(raw_data, dict):
        desc += "; raw_data_keys=" + ",".join(str(k) for k in raw_data)
    if isinstance(text, str) and text:
        desc += "; content_head=" + repr(text[:300])
    return desc


def _extract_official_forward(event) -> ForwardPayload:
    raw = _raw_message(event)
    urls: list[str] = []
    saw_body = False
    if raw is not None:
        elements = getattr(raw, "msg_elements", None)
        if isinstance(elements, list) and elements:
            saw_body = True
            urls.extend(collect_forward_urls(elements))
        top_attachments = getattr(raw, "attachments", None) or []
        if top_attachments:
            saw_body = True
            urls.extend(collect_forward_urls([{"attachments": top_attachments}]))
        summary_text = getattr(raw, "content", None) or ""
        if isinstance(summary_text, str) and summary_text.strip():
            saw_body = True
            urls.extend(extract_forward_summary_urls(summary_text))
    else:
        summary_text = getattr(event, "message_str", "") or ""
        if summary_text.strip():
            saw_body = True
            urls.extend(extract_forward_summary_urls(summary_text))
    return ForwardPayload(
        text="",
        images=_images_from_urls(_normalize_urls(urls)),
        saw_body=saw_body,
        raw=raw,
    )


# --------------------------------------------------------------------------
# OneBot：forward 段 → get_forward_msg；Node/Nodes 直接展开
# --------------------------------------------------------------------------
def _normalize_urls(raw_urls) -> list[str]:
    seen: set = set()
    out: list[str] = []
    for url in raw_urls:
        url = str(url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _images_from_urls(urls) -> list[Image]:
    images: list[Image] = []
    for url in urls:
        if _HTTP_RE.match(url):
            images.append(Image.fromURL(url))
        else:
            images.append(Image(file=url, url=url))
    return images


def _segments_of(node) -> list:
    """取出 OneBot 节点/消息的段列表（message / content）。"""
    if isinstance(node, dict):
        msg = node.get("message")
        if msg is None:
            msg = node.get("content")
        return list(msg) if isinstance(msg, list) else []
    msg = getattr(node, "content", None)
    return list(msg) if isinstance(msg, list) else []


def _collect_segment_media(segments, urls: list, texts: list) -> None:
    for seg in segments or []:
        if isinstance(seg, Image):
            url = getattr(seg, "url", None) or getattr(seg, "file", None)
            if url:
                urls.append(str(url))
            continue
        if isinstance(seg, Plain):
            texts.append(seg.text or "")
            continue
        if isinstance(seg, (Node, Nodes)):
            _collect_component_media(seg, urls, texts)
            continue
        if isinstance(seg, dict):
            stype = str(seg.get("type") or "").lower()
            data = seg.get("data") or {}
            if stype == "image":
                url = data.get("url") or data.get("file")
                if url:
                    urls.append(str(url))
            elif stype == "text":
                texts.append(str(data.get("text") or ""))
            elif stype in ("node", "nodes", "forward"):
                nested = _segments_of(data) or _segments_of(seg)
                _collect_segment_media(nested, urls, texts)


def _collect_component_media(comp, urls: list, texts: list) -> None:
    if isinstance(comp, Image):
        url = getattr(comp, "url", None) or getattr(comp, "file", None)
        if url:
            urls.append(str(url))
        return
    if isinstance(comp, Plain):
        texts.append(comp.text or "")
        return
    if isinstance(comp, Node):
        _collect_segment_media(getattr(comp, "content", None) or [], urls, texts)
        return
    if isinstance(comp, Nodes):
        for node in getattr(comp, "nodes", None) or []:
            _collect_component_media(node, urls, texts)


def _iter_forward_nodes(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "nodes", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _iter_forward_nodes(value)
                if nested:
                    return nested
    return []


async def _call_get_forward(event, msg_id):
    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    action = getattr(bot, "call_action", None)
    if not callable(action):
        api = getattr(bot, "api", None)
        action = getattr(api, "call_action", None)
    if not callable(action):
        return None
    for params in ({"message_id": msg_id}, {"id": msg_id}):
        try:
            return await action("get_forward_msg", **params)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_forward_msg 失败(%s): %s", params, exc)
    return None


async def _extract_onebot_forward(event) -> ForwardPayload:
    chain = _message_chain(event)
    urls: list[str] = []
    texts: list[str] = []
    saw_body = False
    for comp in chain:
        if isinstance(comp, Forward):
            saw_body = True
            msg_id = getattr(comp, "id", None)
            if msg_id in (None, "", 0):
                continue
            data = await _call_get_forward(event, msg_id)
            if not data:
                continue
            for node in _iter_forward_nodes(data):
                _collect_segment_media(_segments_of(node), urls, texts)
        elif isinstance(comp, (Node, Nodes)):
            saw_body = True
            _collect_component_media(comp, urls, texts)
    return ForwardPayload(
        text="\n".join(t for t in texts if t),
        images=_images_from_urls(_normalize_urls(urls)),
        saw_body=saw_body,
        raw=None,
    )


async def extract_forward(event: AstrMessageEvent) -> ForwardPayload | None:
    """统一解析合并转发内容；非转发或无法解析时返回 None。"""
    channel = resolve_channel(event)
    if channel == "official":
        return _extract_official_forward(event)
    if channel == "onebot":
        return await _extract_onebot_forward(event)
    return None
