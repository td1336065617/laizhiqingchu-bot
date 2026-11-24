"""P2 批 12：多关键词同义绑定（别名组）。

语义刻意做窄：单层多对一、别名不得当组名、真关键词优先于别名。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import import_main


async def _collect(gen):
    return [item async for item in gen]


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"admin_users": ["admin-1"], "max_storage_mb": 200}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot._store = None
    bot._md5_cache = {}
    folder = bot.data_dir / "蓝色大肥鱼"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"蓝色大肥鱼": ["a.jpg"]}
    return main, bot


class _Event:
    def __init__(self, sender="admin-1"):
        self._sender = sender

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official"

    def plain_result(self, text):
        return text


def test_bind_then_resolve_hits_same_pool(tmp_path):
    main, bot = _bot(tmp_path)

    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    assert any("已绑定" in m for m in msgs), msgs
    assert bot._group_of("大肥鱼") == "蓝色大肥鱼"
    assert bot._group_of("蓝色大肥鱼") == "蓝色大肥鱼"
    assert bot._resolve_keyword("大肥鱼") == ("蓝色大肥鱼", "alias")
    assert bot._resolve_keyword("蓝色大肥鱼")[1] == "exact"
    stored = json.loads(bot.alias_path.read_text(encoding="utf-8"))
    assert stored["version"] == 3
    assert stored["groups"]["蓝色大肥鱼"] == ["蓝色大肥鱼", "大肥鱼"]


def test_bind_rejects_unknown_primary(tmp_path):
    main, bot = _bot(tmp_path)
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到不存在")))
    assert any("不存在图片" in m for m in msgs), msgs
    assert bot.alias_map == {}


def test_bind_two_real_keywords_merges_pools(tmp_path):
    """需求变更：两个真实关键词可以绑在一个图池里（并集）。"""
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "大肥鱼"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "b.jpg").write_bytes(b"y" * 8)
    bot.index["大肥鱼"] = ["b.jpg"]

    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))

    assert any("已绑定" in m for m in msgs), msgs
    assert sorted(bot._group_members("大肥鱼")) == ["大肥鱼", "蓝色大肥鱼"]
    assert sorted(bot._group_members("蓝色大肥鱼")) == ["大肥鱼", "蓝色大肥鱼"]
    pairs = bot._pool_pairs("大肥鱼")
    assert ("大肥鱼", "b.jpg") in pairs and ("蓝色大肥鱼", "a.jpg") in pairs


def test_bind_same_keyword_is_noop(tmp_path):
    main, bot = _bot(tmp_path)
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定蓝色大肥鱼到蓝色大肥鱼")))
    assert any("无需绑定" in m for m in msgs), msgs


def test_bind_merges_two_existing_groups(tmp_path):
    main, bot = _bot(tmp_path)
    for kw in ("清楚", "清晰", "大肥鱼"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "x.jpg").write_bytes(b"y" * 8)
        bot.index[kw] = ["x.jpg"]
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定清晰到清楚")))

    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定清楚到蓝色大肥鱼")))

    assert any("已绑定" in m for m in msgs), msgs
    members = sorted(bot._group_members("蓝色大肥鱼"))
    assert members == sorted(["大肥鱼", "清楚", "清晰", "蓝色大肥鱼"]), members


def test_bind_and_unbind_are_idempotent(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    again = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    assert any("已经在" in m for m in again), again
    asyncio.run(_collect(bot._handle_unbind(_Event(), "解绑大肥鱼")))
    none_left = asyncio.run(_collect(bot._handle_unbind(_Event(), "解绑大肥鱼")))
    assert any("没有绑定" in m for m in none_left), none_left
    assert bot.alias_map == {}


def test_non_admin_cannot_bind(tmp_path):
    main, bot = _bot(tmp_path)
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(sender="stranger"), "绑定大肥鱼到蓝色大肥鱼")))
    assert any("仅限管理员" in m for m in msgs), msgs
    assert bot.alias_map == {}


def test_reserved_words_include_alias_commands():
    main = import_main()
    for word in ("绑定", "解绑", "别名"):
        assert word in main.RESERVED_KEYWORDS


def test_alias_list_shows_members(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    msgs = asyncio.run(_collect(bot._handle_alias_list(_Event(), "别名蓝色大肥鱼")))
    assert any("大肥鱼" in m for m in msgs), msgs