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
    assert bot.alias_map == {"大肥鱼": "蓝色大肥鱼"}
    assert bot._resolve_keyword("大肥鱼") == ("蓝色大肥鱼", "alias")
    assert bot._resolve_keyword("蓝色大肥鱼")[1] == "exact"
    assert json.loads(bot.alias_path.read_text(encoding="utf-8"))["groups"]["蓝色大肥鱼"]["aliases"] == ["大肥鱼"]


def test_bind_rejects_unknown_primary(tmp_path):
    main, bot = _bot(tmp_path)
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到不存在")))
    assert any("不存在图片" in m for m in msgs), msgs
    assert bot.alias_map == {}


def test_bind_rejects_alias_same_as_real_keyword(tmp_path):
    main, bot = _bot(tmp_path)
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定蓝色大肥鱼到蓝色大肥鱼")))
    assert any("相同" in m for m in msgs), msgs


def test_bind_is_single_level_no_chain(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "b.jpg").write_bytes(b"y" * 8)
    bot.index["清楚"] = ["b.jpg"]
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    # 目标本身是别名 → 拒绝（否则会出现链式/循环）
    msgs = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定清晰到大肥鱼")))
    assert any("别名不能再作为主词" in m for m in msgs), msgs
    # 别名不能劫持已存在的真关键词
    hijack = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定清楚到蓝色大肥鱼")))
    assert any("已经是真实关键词" in m for m in hijack), hijack
    assert bot.alias_map == {"大肥鱼": "蓝色大肥鱼"}


def test_bind_and_unbind_are_idempotent(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    again = asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    assert any("已经绑定" in m for m in again), again
    asyncio.run(_collect(bot._handle_unbind(_Event(), "解绑大肥鱼")))
    none_left = asyncio.run(_collect(bot._handle_unbind(_Event(), "解绑大肥鱼")))
    assert any("不是任何关键词的别名" in m for m in none_left), none_left
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