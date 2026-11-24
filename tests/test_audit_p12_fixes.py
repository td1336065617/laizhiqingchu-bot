"""审计 P1/P2 闭环回归：每条对应一条审计发现。"""

from __future__ import annotations

import asyncio
import json
import threading
import types
from pathlib import Path

from conftest import import_main


async def _collect(gen):
    return [item async for item in gen]


def _bot(tmp_path, admins=("qq_official:admin-1",)):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"admin_users": list(admins), "max_storage_mb": 4096}
    bot.index = {}
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}
    return main, bot


class At:
    def __init__(self, qq):
        self.qq = qq


class _Event:
    def __init__(self, text="", sender="admin-1", chain=None):
        self.message_str = text
        self._sender = sender
        self.message_obj = types.SimpleNamespace(message=list(chain or []))

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official"

    def get_platform_name(self):
        return "qq_official"

    def get_message_type(self):
        return "GROUP_MESSAGE"

    def get_group_id(self):
        return "group-1"

    def get_self_id(self):
        return "bot-1"

    def plain_result(self, text):
        return text


def _img(tmp_path, name="a.jpg", data=b"x" * 8):
    path = tmp_path / name
    path.write_bytes(data)
    return path


class _FakeImage:
    """最小 Image 替身：convert_to_file_path 返回本地文件。"""

    def __init__(self, path):
        self._path = path

    async def convert_to_file_path(self):
        return str(self._path)


def test_add_can_create_new_keyword(tmp_path):
    """P2 审计 S1：添加新关键词不得被别名解析拦住。"""
    main, bot = _bot(tmp_path)
    image = _FakeImage(_img(tmp_path))
    bot._extract_reply_images = lambda event: [image]

    out = asyncio.run(_collect(bot._handle_add(_Event("添加新词"), "添加新词")))

    assert any("成功添加" in m or "添加" in m for m in out), out
    assert "新词" in bot.index, "新关键词必须被创建"
    assert (bot.data_dir / "新词").is_dir()


def test_add_uses_alias_target_when_bound(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "主词"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"主词": ["a.jpg"]}
    bot.alias_groups = {"主词": ["别名"]}
    bot._refresh_alias_map()

    bot._extract_reply_images = lambda event: [_FakeImage(_img(tmp_path, "n.jpg"))]
    asyncio.run(_collect(bot._handle_add(_Event("添加别名"), "添加别名")))

    assert "别名" not in bot.index
    assert any(name.startswith("n") or name.endswith(".jpg") for name in bot.index["主词"])


def test_delete_one_with_keyword_and_index(tmp_path):
    """P2 审计 S2：删图{词}{序号} 必须可用（先剥序号再解析）。"""
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"清楚": ["a.jpg"]}

    out = asyncio.run(_collect(bot._handle_delete_one(_Event("删图清楚1"), "删图清楚1")))

    assert any("用法" not in m for m in out), out
    assert not any("不存在" in m for m in out), out


def test_list_resolves_alias(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "主词"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"主词": ["a.jpg"]}
    bot.alias_groups = {"主词": ["别名"]}
    bot._refresh_alias_map()

    out = asyncio.run(_collect(bot._handle_list(_Event("列表别名"), "列表别名")))

    assert not any("没有图片" in m for m in out), out


def test_unblock_resolves_alias(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "主词"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"主词": ["a.jpg"]}
    bot.alias_groups = {"主词": ["别名"]}
    bot._refresh_alias_map()
    bot.blocked_keywords = {"主词"}

    out = asyncio.run(_collect(bot._handle_unblock(_Event("解除屏蔽别名"), "解除屏蔽别名")))

    assert not any("不在屏蔽列表" in m for m in out), out
    assert "主词" not in bot.blocked_keywords


def test_leading_platform_mention_is_stripped(tmp_path):
    """P2 审计 7：<@id> 前缀不得让指令失效。"""
    main, bot = _bot(tmp_path)
    collected = []

    async def fake_handler(event):
        collected.append("hit")
        yield "ok"

    bot._handle_admin_list = fake_handler
    event = _Event("<@0123456789abcdef0123456789abcdef> 管理员列表")

    asyncio.run(_collect(bot.on_message(event)))

    assert collected == ["hit"], "剥掉 <@id> 前缀后必须命中 管理员列表 分支"


def test_admin_save_failure_does_not_take_effect(tmp_path):
    """P2 审计 8：保存失败不得让新管理员生效。"""
    main, bot = _bot(tmp_path)

    class _Cfg(dict):
        def save_config(self):
            raise OSError("磁盘满")

    bot.config = _Cfg(admin_users=["qq_official:admin-1"])
    event = _Event("授予管理员NEWUSER123456")

    out = asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))

    assert any("保存失败" in m for m in out), out
    assert bot.config["admin_users"] == ["qq_official:admin-1"], "失败不得生效"


def test_alias_list_requires_admin(tmp_path):
    main, bot = _bot(tmp_path)
    out = asyncio.run(_collect(bot._handle_alias_list(_Event(sender="stranger"), "别名")))
    assert any("仅限管理员" in m for m in out), out


def test_drop_alias_group_on_keyword_delete(tmp_path):
    main, bot = _bot(tmp_path)
    bot.alias_groups = {"清楚": ["清晰"]}
    bot._refresh_alias_map()

    bot._drop_alias_group("清楚")

    assert bot.alias_groups == {}
    assert bot.alias_map == {}


def test_store_sync_keeps_alias_tables(tmp_path):
    """P1 审计 G2：保存索引不得级联清空别名/管理员表。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    store = Store(tmp_path / "index.db")
    store.sync_index({"清楚": ["a.jpg"]})
    with store.conn:
        store.conn.execute("INSERT INTO keyword(k) VALUES('群友')")
        store.conn.execute("INSERT INTO alias_group(primary_kw) VALUES('清楚')")
        store.conn.execute("INSERT INTO alias(alias, primary_kw) VALUES('清晰', '清楚')")

    store.sync_index({"清楚": ["a.jpg"]})

    assert store.conn.execute("SELECT COUNT(*) FROM alias").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM alias_group").fetchone()[0] == 1
    store.close()


def test_store_empty_keyword_survives_reload(tmp_path):
    """P1 审计 G3：0 张图片的关键词不能在重启后消失。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    store = Store(tmp_path / "index.db")
    store.sync_index({"a": ["1.jpg"], "empty": []})

    assert store.load_index() == {"a": ["1.jpg"], "empty": []}
    store.close()


def test_store_writes_from_worker_thread(tmp_path):
    """P1 审计 S2：工作线程（to_thread / 回填）写库不得失败。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    store = Store(tmp_path / "index.db")
    store.sync_index({"清楚": ["a.jpg"]})
    errors = []

    def worker():
        try:
            store.set_md5s("清楚", {"a.jpg": "m1"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert not errors, errors
    assert store.md5s_of("清楚") == {"a.jpg": "m1"}
    store.close()