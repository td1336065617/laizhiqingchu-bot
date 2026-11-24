"""图池并集行为：绑定两个真实关键词后，发送/列表/删图/添加都按并集走。"""

from __future__ import annotations

import asyncio
import types
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
    bot.config = {"admin_users": ["admin-1"], "max_storage_mb": 4096}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot._store = None
    bot._md5_cache = {}
    bot.index = {}
    for kw, names in (("蓝色大肥鱼", ["a.jpg", "b.jpg"]), ("大肥鱼", ["c.jpg"])):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x" * 16)
        bot.index[kw] = names
    return main, bot


class _Event:
    def __init__(self, text="", sender="admin-1"):
        self.message_str = text
        self._sender = sender
        self.message_obj = types.SimpleNamespace(message=[])

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official"

    def plain_result(self, text):
        return "TEXT:" + str(text)

    def image_result(self, path):
        return "IMG:" + str(path)


def test_bind_two_real_keywords_shares_pool(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event("绑定大肥鱼到蓝色大肥鱼"), "绑定大肥鱼到蓝色大肥鱼")))

    assert bot._group_of("大肥鱼") == "蓝色大肥鱼"
    assert sorted(bot._group_members("大肥鱼")) == ["大肥鱼", "蓝色大肥鱼"]


def test_send_draws_from_union(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))

    seen = set()
    for _ in range(40):
        out = asyncio.run(_collect(bot._handle_send(_Event("来只大肥鱼"), "来只大肥鱼")))
        assert out and out[0].startswith("IMG:"), out
        seen.add(Path(out[0][4:]).parent.name)

    assert seen == {"大肥鱼", "蓝色大肥鱼"}, f"必须两个关键词都抽到：{seen}"


def test_list_shows_union(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))

    out = asyncio.run(_collect(bot._handle_list(_Event("列表大肥鱼"), "列表大肥鱼")))
    text = "\n".join(out)

    for name in ("a.jpg", "b.jpg", "c.jpg"):
        assert name in text, f"列表应包含并集里的 {name}：{text}"


def test_add_on_member_writes_into_pool_primary(tmp_path):
    main, bot = _bot(tmp_path)
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    new_image = tmp_path / "new.jpg"
    new_image.write_bytes(b"z" * 16)

    class _Img:
        async def convert_to_file_path(self):
            return str(new_image)

    bot._extract_reply_images = lambda event: [_Img()]
    asyncio.run(_collect(bot._handle_add(_Event("添加大肥鱼"), "添加大肥鱼")))

    assert "大肥鱼" in bot.index
    assert any(not name.endswith(("a.jpg", "b.jpg")) for name in bot.index["蓝色大肥鱼"]), bot.index
    assert len(bot.index["大肥鱼"]) == 1, "不应往成员目录里再写一份"


def test_unbind_removes_only_that_member(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "d.jpg").write_bytes(b"x" * 16)
    bot.index["清楚"] = ["d.jpg"]
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定大肥鱼到蓝色大肥鱼")))
    asyncio.run(_collect(bot._handle_bind(_Event(), "绑定清楚到蓝色大肥鱼")))

    out = asyncio.run(_collect(bot._handle_unbind(_Event(), "解绑清楚")))

    assert any("已解绑" in m for m in out), out
    assert not bot._group_of("清楚")
    assert sorted(bot._group_members("蓝色大肥鱼")) == ["大肥鱼", "蓝色大肥鱼"], "其它成员关系必须保留"