"""图片权重：加权抽取、0=永不发送、全零兜底、同步不冲权重、备份往返。"""

from __future__ import annotations

import asyncio
import collections
import json
import sys
import types
import zipfile
from pathlib import Path

from conftest import import_main

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sticker_store import Store  # noqa: E402


def _bot(tmp_path, with_store=True):
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
    bot._md5_cache = {}
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"{i}.jpg" for i in range(3)]
    for name in names:
        (folder / name).write_bytes(b"x" * 16)
    bot.index = {"清楚": names}
    bot._store = Store(bot.db_path) if with_store else None
    if bot._store is not None:
        bot._store.sync_index(bot.index)
    return main, bot


def test_weight_defaults_to_one(tmp_path):
    main, bot = _bot(tmp_path)
    assert bot._weight_of("清楚", "0.jpg") == 1
    bot._store.close()


def test_weighted_pick_follows_ratio(tmp_path):
    main, bot = _bot(tmp_path)
    bot._store.set_weight("清楚", "0.jpg", 9)
    bot._store.set_weight("清楚", "1.jpg", 1)
    bot._store.set_weight("清楚", "2.jpg", 1)

    counter = collections.Counter()
    for _ in range(3000):
        member, name = bot._pick_from_pool("清楚")
        counter[name] += 1

    share = counter["0.jpg"] / sum(counter.values())
    assert 0.6 < share < 0.85, f"权重 9/(9+1+1)≈0.82，实测 {share:.3f}"
    bot._store.close()


def test_zero_weight_never_sent(tmp_path):
    main, bot = _bot(tmp_path)
    bot._store.set_weight("清楚", "0.jpg", 0)

    for _ in range(2000):
        _member, name = bot._pick_from_pool("清楚")
        assert name != "0.jpg", "权重 0 的图绝不能出现"
    bot._store.close()


def test_all_zero_means_nothing_sendable(tmp_path):
    """0 = 永不发送：整池都是 0 时应当"无可发送图片"，不能硬发一张。"""
    main, bot = _bot(tmp_path)
    for name in ("0.jpg", "1.jpg", "2.jpg"):
        bot._store.set_weight("清楚", name, 0)

    assert bot._pick_from_pool("清楚") is None
    assert bot._pool_weights("清楚") == ([], [])
    bot._store.close()


def test_sync_index_keeps_weights(tmp_path):
    main, bot = _bot(tmp_path)
    bot._store.set_weight("清楚", "1.jpg", 7)

    bot._store.sync_index(bot.index)   # 再存一次索引

    assert bot._store.weight_of("清楚", "1.jpg") == 7, "同步索引不得把权重冲回默认"
    bot._store.close()


def test_json_mode_has_no_weights(tmp_path):
    main, bot = _bot(tmp_path, with_store=False)
    assert bot._weight_of("清楚", "0.jpg") == 1
    pairs, weights = bot._pool_weights("清楚")
    assert weights == [1, 1, 1]


def test_backup_carries_weights_and_restore_applies(tmp_path):
    main, bot = _bot(tmp_path)
    bot._store.set_weight("清楚", "2.jpg", 5)

    info = bot._backup_to_zip()
    archive = bot.backup_root / info["filename"]
    with zipfile.ZipFile(archive) as zf:
        weights = json.loads(zf.read("weights.json"))["weights"]
    assert weights == {"清楚/2.jpg": 5}

    fresh_main, fresh = _bot(tmp_path / "fresh")
    fresh._restore_from_zip(archive)
    assert fresh._store.weight_of("清楚", "2.jpg") == 5, "恢复后权重必须跟着回来"
    bot._store.close()
    fresh._store.close()


def test_set_weight_needs_existing_row(tmp_path):
    main, bot = _bot(tmp_path)
    assert bot._store.set_weight("清楚", "不存在.jpg", 3) is False
    bot._store.close()

def test_schema_version_is_upgraded_on_open(tmp_path):
    """老库（meta 里写着 1）打开后版本必须升到 2，否则每次启动都以为没迁移。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import SCHEMA_VERSION, Store

    db = Store(tmp_path / "index.db")
    with db.conn:
        db.conn.execute("UPDATE meta SET v=? WHERE k=?", ("1", "schema_version"))
    assert db.schema_version() == 1
    db.close()

    again = Store(tmp_path / "index.db")
    assert again.schema_version() == SCHEMA_VERSION == 2
    assert "weight" in [r[1] for r in again.conn.execute("PRAGMA table_info(image)")]
    again.close()


def test_weight_survives_reopen(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    db = Store(tmp_path / "index.db")
    db.sync_index({"清楚": ["a.jpg"]})
    db.set_weight("清楚", "a.jpg", 0)
    db.close()

    again = Store(tmp_path / "index.db")
    assert again.weight_of("清楚", "a.jpg") == 0, "0 权重必须能落盘并读回"
    again.close()
