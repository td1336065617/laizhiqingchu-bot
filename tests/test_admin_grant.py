"""P2 批 13：@人授予管理员（三路兜底 + 占位黑名单 + 自举守卫 + 审计）。"""

from __future__ import annotations

import asyncio
import json
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
    bot.config = {"admin_users": list(admins)}
    bot.index = {}
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}

    saved = {}

    def save_config():
        saved["admin_users"] = list(bot.config.get("admin_users") or [])

    bot.config = type("Cfg", (dict,), {"save_config": staticmethod(save_config)})(bot.config)
    bot._saved = saved
    return main, bot


class At:
    def __init__(self, qq):
        self.qq = qq


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class _Event:
    def __init__(self, *, sender="admin-1", text="", chain=None, self_id="bot-1", reply_to=None):
        self._sender = sender
        self.message_str = text
        components = list(chain or [])
        if reply_to is not None:
            components.append(Reply(reply_to))
        self.message_obj = types.SimpleNamespace(message=components)
        self._self = self_id

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official"

    def get_self_id(self):
        return self._self

    def plain_result(self, text):
        return text


def _at(qq):
    return At(qq)


def test_extract_from_at_component(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(chain=[At("123456789")])
    assert bot._extract_admin_targets(event) == ["123456789"]


def test_extract_from_text_mention(tmp_path):
    """官方通道 @他人不产生 At，只留 <@openid> 字面量。"""
    main, bot = _bot(tmp_path)
    event = _Event(text="<@0123456789abcdef0123456789abcdef> 授予管理员")
    assert bot._extract_admin_targets(event) == ["0123456789abcdef0123456789abcdef"]


def test_extract_from_reply(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(reply_to="ABCDEF123456")
    assert bot._extract_admin_targets(event) == ["ABCDEF123456"]


def test_extract_skips_placeholders_and_self(tmp_path):
    main, bot = _bot(tmp_path)
    chain = []
    for value in ("all", "qq_official", "unknown_selfid", "bot-1"):
        chain.append(At(value))
    event = _Event(chain=chain)
    assert bot._extract_admin_targets(event) == []


def test_grant_admin_scoped_and_idempotent(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(text="授予管理员NEWUSER123456")
    out = asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))
    assert any("已授予" in m for m in out), out
    assert "qq_official:NEWUSER123456" in bot.config["admin_users"]
    again = asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))
    assert any("已是管理员" in m for m in again), again
    assert bot.config["admin_users"].count("qq_official:NEWUSER123456") == 1


def test_grant_blocked_when_no_admin_bootstrap(tmp_path):
    """决策 A：没有管理员时只能去 WebUI 配，不能靠指令自举。"""
    main, bot = _bot(tmp_path, admins=())
    event = _Event(text="授予管理员NEWUSER123456")
    out = asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))
    assert any("WebUI" in m for m in out), out
    assert bot.config["admin_users"] == []


def test_non_admin_cannot_grant(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(sender="stranger", text="授予管理员NEWUSER123456")
    out = asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))
    assert any("仅限管理员" in m for m in out), out
    assert "qq_official:NEWUSER123456" not in bot.config["admin_users"]


def test_cannot_revoke_last_admin(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(text="撤销管理员admin-1")
    out = asyncio.run(_collect(bot._handle_revoke_admin(event, event.message_str)))
    assert any("最后一个管理员" in m for m in out), out
    assert bot.config["admin_users"] == ["qq_official:admin-1"]


def test_revoke_removes_target(tmp_path):
    main, bot = _bot(tmp_path, admins=("qq_official:admin-1", "qq_official:other-2"))
    event = _Event(text="撤销管理员other-2")
    out = asyncio.run(_collect(bot._handle_revoke_admin(event, event.message_str)))
    assert any("已撤销" in m for m in out), out
    assert bot.config["admin_users"] == ["qq_official:admin-1"]


def test_admin_audit_written(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event(text="授予管理员NEWUSER123456")
    asyncio.run(_collect(bot._handle_grant_admin(event, event.message_str)))

    path = bot.data_dir / "admin_audit.jsonl"
    assert path.exists()
    line = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["action"] == "grant" and line["result"] == "ok"


def test_admin_list_masks_ids(tmp_path):
    main, bot = _bot(tmp_path)
    event = _Event()
    out = asyncio.run(_collect(bot._handle_admin_list(event)))
    assert any("管理员（1）" in m for m in out), out
    assert not any("qq_official:admin-1" in m for m in out), "列表必须脱敏"