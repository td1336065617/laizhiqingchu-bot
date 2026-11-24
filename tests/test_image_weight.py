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


def test_all_zero_falls_back_to_uniform(tmp_path):
    main, bot = _bot(tmp_path)
    for name in ("0.jpg", "1.jpg", "2.jpg"):
        bot._store.set_weight("清楚", name, 0)

    picks = {bot._pick_from_pool("清楚")[1] for _ in range(200)}

    assert len(picks) > 1, "全零时必须退回均匀，而不是发不出图"
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