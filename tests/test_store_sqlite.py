"""P1 批 8/9：SQLite 存储层（双写 + 迁移幂等 + 回落 + MD5 索引复用）。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from conftest import import_main


def _bot(tmp_path, monkeypatch, mode=None):
    if mode is not None:
        monkeypatch.setenv("STICKER_STORAGE", mode)
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"max_storage_mb": 200, "backup_keep": 2, "backup_total_mb": 1024}
    bot.index = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def test_store_schema_and_roundtrip(tmp_path):
    main, bot = _bot(tmp_path, monkeypatch=None) if False else (None, None)
    sys_path = Path(__file__).resolve().parent.parent
    import sys
    if str(sys_path) not in sys.path:
        sys.path.insert(0, str(sys_path))
    from sticker_store import Store

    store = Store(tmp_path / "index.db")
    assert store.schema_version() == 1
    assert store.integrity() == "ok"
    store.sync_index({"清楚": ["a.jpg", "b.jpg"]})
    assert store.counts() == {"keywords": 1, "images": 2}
    assert store.load_index() == {"清楚": ["a.jpg", "b.jpg"]}
    store.set_md5s("清楚", {"a.jpg": "m1"})
    assert store.md5s_of("清楚") == {"a.jpg": "m1"}
    assert store.delete_by_md5(["m1"]) == [("清楚", "a.jpg")]
    store.close()


def test_storage_mode_env(monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import storage_mode

    monkeypatch.delenv("STICKER_STORAGE", raising=False)
    assert storage_mode() == "sqlite"
    monkeypatch.setenv("STICKER_STORAGE", "json")
    assert storage_mode() == "json"
    monkeypatch.setenv("STICKER_STORAGE", "SQLITE")
    assert storage_mode() == "sqlite"


def test_migrate_is_idempotent(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    index = {"清楚": ["a.jpg", "b.jpg"], "群友": ["c.jpg"]}
    store = Store(tmp_path / "index.db")
    first = store.migrate(index)
    second = store.migrate(index)
    third = store.migrate(index)
    assert first == second == third == {"keywords": 2, "images": 3}
    store.close()


def test_clear_content_keeps_meta(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    store = Store(tmp_path / "index.db")
    store.sync_index({"清楚": ["a.jpg"]})
    store.clear_content()
    assert store.counts() == {"keywords": 0, "images": 0}
    assert store.schema_version() == 1, "清空内容不得清掉 schema 版本"
    store.close()


def test_save_index_writes_both_json_and_db(tmp_path, monkeypatch):
    monkeypatch.delenv("STICKER_STORAGE", raising=False)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    main, bot = _bot(tmp_path, monkeypatch)
    bot._store = Store(bot.db_path)
    bot.index = {"清楚": ["a.jpg"]}

    bot._save_index()

    assert json.loads(bot.index_path.read_text(encoding="utf-8")) == {"清楚": ["a.jpg"]}
    assert bot._store.load_index() == {"清楚": ["a.jpg"]}
    bot._store.close()


def test_json_mode_does_not_create_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STICKER_STORAGE", "json")
    main, bot = _bot(tmp_path, monkeypatch)
    bot._init_storage()

    assert bot._store is None
    assert not bot.db_path.exists(), "json 模式不得创建数据库文件"
    bot.index = {"清楚": ["a.jpg"]}
    bot._save_index()
    assert json.loads(bot.index_path.read_text(encoding="utf-8")) == {"清楚": ["a.jpg"]}


def test_md5_cache_seeded_from_store(tmp_path, monkeypatch):
    """冷启动时 MD5 从库里取，不再重算磁盘。"""
    monkeypatch.delenv("STICKER_STORAGE", raising=False)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    main, bot = _bot(tmp_path, monkeypatch)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"清楚": ["a.jpg"]}
    bot._store = Store(bot.db_path)
    bot._store.sync_index(bot.index)
    bot._store.set_md5s("清楚", {"a.jpg": "known-md5"})

    calls = []
    real = main.StickerPlugin._md5
    monkeypatch.setattr(
        main.StickerPlugin,
        "_md5",
        staticmethod(lambda path, chunk_size=1 << 20: calls.append(path) or real(path, chunk_size)),
    )

    md5s = bot._folder_md5s("清楚")

    assert md5s["a.jpg"] == "known-md5", "应复用库里的 MD5"
    assert not calls, "库里有 MD5 时不应再读磁盘"
    bot._store.close()