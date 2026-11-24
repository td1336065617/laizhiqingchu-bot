"""audit-weight / audit-pool / audit-ui 的修复回归（1.10.2）。"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

from conftest import import_main


def _status(resp):
    if isinstance(resp, dict):
        return resp.get("status")
    raw = getattr(resp, "body", b"{}")
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw).get("status")


async def _collect(gen):
    return [item async for item in gen]


class _Event:
    def __init__(self, text="", sender="admin-1"):
        self.message_str = text
        self._sender = sender
        self.message_obj = types.SimpleNamespace(message=[])
        self.unified_msg_origin = "g1"

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official"

    def get_platform_name(self):
        return "qq_official"

    def get_message_type(self):
        return "GROUP_MESSAGE"

    def get_group_id(self):
        return "g1"

    def get_self_id(self):
        return "bot-1"

    def plain_result(self, text):
        return "TEXT:" + str(text)

    def image_result(self, path):
        return "IMG:" + str(path)

    async def send(self, chain):
        return None


def _bot(tmp_path, kw_files):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.config = {"admin_users": ["admin-1"], "max_storage_mb": 4096}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.pending_delete = {}
    bot._md5_cache = {}
    bot._store = None
    bot.index = {}
    for keyword, names in kw_files.items():
        folder = bot.data_dir / keyword
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x" * 16)
        bot.index[keyword] = list(names)
    return main, bot


def test_delete_one_index_matches_list_order(tmp_path):
    """审计 S1：删图{词}{序号} 必须按列表显示的并集序号，不能按单词索引。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg", "a2.jpg"], "B": ["b1.jpg", "b2.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定B到A")))

    listed = asyncio.run(_collect(bot._handle_list(_Event(), "列表A")))
    text = "\n".join(listed)
    first_listed = "a1.jpg"
    assert text.index(first_listed) < text.index("b1.jpg"), text

    asyncio.run(_collect(bot._handle_delete_one(_Event(), "删图B1")))

    assert not (bot.data_dir / "A" / first_listed).exists(), "序号 1 应对应列表第一张（a1.jpg）"
    assert (bot.data_dir / "B" / "b1.jpg").exists(), "不能误删 b1.jpg"


def test_bind_member_merges_whole_pool(tmp_path):
    """审计 S2：把某池的"组员"绑到别处时，必须把它所在的整池一起并过去。"""
    main, bot = _bot(tmp_path, {"P": ["p.jpg"], "A": ["a.jpg"], "A2": ["a2.jpg"], "B": ["b.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定A到P")))
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定A2到A")))
    assert sorted(bot._group_members("P")) == ["A", "A2", "P"]

    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定A到B")))

    assert sorted(bot._group_members("B")) == sorted(["A", "A2", "B", "P"]), bot.alias_groups
    assert not [g for g, m in bot.alias_groups.items() if g not in m], "不允许出现组名不在成员里的孤儿组"


def test_ai_send_respects_pool_block(tmp_path):
    """审计 S3：屏蔽池内任一成员后，AI 工具也不能通过其它成员发图。"""
    main, bot = _bot(tmp_path, {"A": ["a.jpg"], "B": ["b.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定B到A")))
    bot.blocked_keywords = {"B"}

    out = asyncio.run(bot.sticker_send(_Event(), keyword="A"))

    assert "disabled" in out, out


def test_deleting_one_image_keeps_pool(tmp_path):
    """审计 M4：删掉一张图不该把整个图池解散。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg", "a2.jpg"], "B": ["b1.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定B到A")))

    asyncio.run(_collect(bot._handle_delete_one(_Event(), "删图A1")))

    assert bot._group_of("B") == "A", "A 还有图，池子不该解散"
    assert sorted(bot._group_members("A")) == ["A", "B"]


def test_deleting_keyword_contracts_pool(tmp_path):
    """审计 M5：删除整个关键词后要把它从池里摘掉。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"], "B": ["b1.jpg"], "C": ["c1.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定B到A")))
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定C到A")))
    assert sorted(bot._group_members("A")) == ["A", "B", "C"]

    asyncio.run(_collect(bot._handle_delete_all(_Event(), "删除A")))
    asyncio.run(_collect(bot._handle_delete_all(_Event(), "删除A")))   # 二次确认

    assert "A" not in bot.index
    assert sorted(bot._group_members("B")) == ["B", "C"], bot.alias_groups


def test_web_clear_wipes_groups_and_needs_confirm(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"], "B": ["b1.jpg"]})
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定B到A")))

    class _Req:
        @staticmethod
        def json(default=None):
            return {}

    monkeypatch.setattr(main, "request", _Req)
    resp = asyncio.run(bot._web_clear())
    assert _status(resp) == "error", "缺确认令牌必须拒绝"
    assert bot.index, "被拒绝时不得清空"

    class _Req2:
        @staticmethod
        def json(default=None):
            return {"confirm": "clear"}

    monkeypatch.setattr(main, "request", _Req2)
    asyncio.run(bot._web_clear())
    assert bot.index == {}
    assert bot.alias_groups == {}, "清空后绑定表也要清掉"


def test_download_marks_aborted_when_client_closes(tmp_path):
    """审计 G1：客户端中途断开必须记 aborted，不能永远停在 downloading。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"]})
    target = bot.data_dir / "A" / "a1.jpg"
    target.write_bytes(b"z" * (3 * 1024 * 1024))
    state = {"filename": "a1.jpg", "total": target.stat().st_size, "sent": 0,
             "percent": 0, "state": "downloading"}

    async def consume_one():
        gen = bot._iter_backup_chunks(target, state)
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(consume_one())

    assert state["state"] == "aborted", state

def test_clear_rejects_empty_body(tmp_path, monkeypatch):
    """审计 S-A：页面空 body 调 clear 时也必须被拒绝（旧写法会直接放行）。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"]})

    class _Req:
        username = "tester"

        @staticmethod
        def json(default=None):
            return None

    monkeypatch.setattr(main, "request", _Req)
    resp = asyncio.run(bot._web_clear())

    assert _status(resp) == "error", "空 body 必须拒绝"
    assert bot.index, "被拒绝时不得清空"


def test_restore_requires_prepare_token(tmp_path, monkeypatch):
    """恢复是 multipart，带不了 JSON 令牌：必须"先预确认"，否则拒绝。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"]})

    class _Upload:
        async def save(self, path):
            Path(path).write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    class _Req:
        username = "tester"
        files = staticmethod(lambda: {"file": _Upload()})

        @staticmethod
        def json(default=None):
            return None

    class _ReqFiles:
        username = "tester"

        @staticmethod
        async def files():
            return {"file": _Upload()}

        @staticmethod
        def json(default=None):
            return None

    called = []
    monkeypatch.setattr(bot, "_restore_from_zip", lambda p: called.append(p) or {"added": 0})
    monkeypatch.setattr(main, "request", _ReqFiles)

    resp = asyncio.run(bot._web_restore())
    assert _status(resp) == "error", "没有预确认必须拒绝"
    assert not called, "被拒绝时不得真正恢复"

    asyncio.run(bot._web_restore_prepare())
    asyncio.run(bot._web_restore())
    assert called, "预确认后应当恢复"


def test_pool_pairs_rejects_path_traversal(tmp_path):
    """索引里出现 ../ 文件名时，图池必须跳过（否则 来只/删图/AI 都会越权）。"""
    main, bot = _bot(tmp_path, {"A": ["a1.jpg"]})
    bot.index["A"].append("../secret.txt")

    names = [name for _member, name in bot._pool_pairs("A")]

    assert names == ["a1.jpg"], names
