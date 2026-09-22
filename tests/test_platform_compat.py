"""platform_compat 双通道解析测试。"""
from __future__ import annotations

import asyncio

from astrbot.api.message_components import Forward, Image, Node, Nodes

import platform_compat as pc


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _MsgObj:
    def __init__(self, chain=None, raw=None):
        self.message = chain or []
        self.raw_message = raw


class _Event:
    def __init__(self, name, *, chain=None, raw=None, group_id="", sender_id="", platform_id="p1", bot=None, message_str=""):
        self._name = name
        self.message_obj = _MsgObj(chain, raw)
        self.group_id = group_id
        self.sender_id = sender_id
        self.platform_id = platform_id
        self.bot = bot
        self.message_str = message_str

    def get_platform_name(self):
        return self._name

    def get_platform_id(self):
        return self.platform_id

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id


class _FakeBot:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self._exc is not None:
            raise self._exc
        return self._resp


def test_resolve_channel():
    assert pc.resolve_channel(_Event("qq_official")) == "official"
    assert pc.resolve_channel(_Event("qq_official_webhook")) == "official"
    assert pc.resolve_channel(_Event("aiocqhttp")) == "onebot"
    assert pc.resolve_channel(_Event("telegram")) == ""


def test_scoped_key():
    assert pc.scoped_key("p1", "u1") == "p1:u1"
    assert pc.scoped_key("", "u1") == "u1"


def test_admin_entry_matches():
    assert pc.admin_entry_matches("u1", "p1", "u1") is True
    assert pc.admin_entry_matches("p1:u1", "p1", "u1") is True
    assert pc.admin_entry_matches("p2:u1", "p1", "u1") is False
    assert pc.admin_entry_matches("p1:u2", "p1", "u1") is False
    assert pc.admin_entry_matches("", "p1", "u1") is False


def test_is_forward_event_official():
    assert pc.is_forward_event(_Event("qq_official", raw=_Obj(message_type=102))) is True
    assert pc.is_forward_event(_Event("qq_official", raw=_Obj(message_type=0))) is False


def test_is_forward_event_onebot():
    assert pc.is_forward_event(_Event("aiocqhttp", chain=[Forward(id="x")])) is True
    assert pc.is_forward_event(_Event("aiocqhttp", chain=[])) is False


def test_extract_forward_official_summary():
    content = "[附件1] 类型:图片 文件名:a.jpg 尺寸:100 URL:https://multimedia.nt.qq.com.cn/download?x=1"
    raw = _Obj(message_type=102, msg_elements=[], attachments=[], content=content)
    payload = asyncio.run(pc.extract_forward(_Event("qq_official", raw=raw)))
    assert payload is not None
    assert payload.saw_body is True
    assert len(payload.images) == 1
    assert "multimedia.nt.qq.com.cn" in str(payload.images[0].url or payload.images[0].file)


def test_extract_forward_onebot_get_forward_msg():
    resp = {
        "messages": [
            {"message": [
                {"type": "image", "data": {"url": "https://example.com/a.jpg"}},
                {"type": "text", "data": {"text": "hi"}},
            ]}
        ]
    }
    bot = _FakeBot(resp=resp)
    ev = _Event("aiocqhttp", chain=[Forward(id="fwd1")], bot=bot)
    payload = asyncio.run(pc.extract_forward(ev))
    assert payload is not None
    assert payload.saw_body is True
    assert len(payload.images) == 1
    assert payload.text == "hi"
    assert bot.calls[0][0] == "get_forward_msg"
    assert bot.calls[0][1].get("message_id") == "fwd1"


def test_extract_forward_onebot_nodes():
    node = Node(content=[Image.fromURL("https://example.com/b.jpg")])
    ev = _Event("aiocqhttp", chain=[Nodes([node])])
    payload = asyncio.run(pc.extract_forward(ev))
    assert payload is not None
    assert len(payload.images) == 1


def test_extract_forward_onebot_get_forward_msg_fallback_id():
    resp = {"data": {"messages": [{"message": [{"type": "image", "data": {"file": "https://example.com/c.jpg"}}]}]}}
    # 第一次用 message_id 调用失败，第二次用 id 调用成功
    class _Bot2:
        def __init__(self):
            self.calls = []

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if "message_id" in params:
                raise RuntimeError("unsupported")
            return resp

    b2 = _Bot2()
    ev = _Event("aiocqhttp", chain=[Forward(id="fwd2")], bot=b2)
    payload = asyncio.run(pc.extract_forward(ev))
    assert payload is not None
    assert len(payload.images) == 1
    assert b2.calls[-1][1].get("id") == "fwd2"


def test_extract_forward_non_forward_returns_none():
    assert asyncio.run(pc.extract_forward(_Event("telegram"))) is None
