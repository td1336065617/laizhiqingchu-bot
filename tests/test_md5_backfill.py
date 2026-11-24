"""P3 续：MD5 回填（迁移后 DB 里 md5 为空，去重/按图删图用不上）。"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import import_main


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"max_storage_mb": 4096, "max_image_mb": 10}
    bot.index = {}
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.blocked_keywords = set()
    bot._md5_cache = {}
    bot._store = None
    return main, bot


def test_backfill_fills_store_md5(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    main, bot = _bot(tmp_path)
    bot._store = Store(bot.db_path)
    for kw in ("清楚", "群友"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "a.jpg").write_bytes(b"content-" + kw.encode())
        bot.index[kw] = ["a.jpg"]
    bot._save_index()
    assert all(v is None for v in bot._store.md5s_of("清楚").values()), "迁移后 md5 应为空"

    report = bot._backfill_md5()

    assert report["images"] == 2
    assert bot._store.md5s_of("清楚")["a.jpg"], "回填后库里应有 md5"
    assert bot._store.delete_by_md5([bot._store.md5s_of("群友")["a.jpg"]])
    bot._store.close()


def test_backfill_is_idempotent(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sticker_store import Store

    main, bot = _bot(tmp_path)
    bot._store = Store(bot.db_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.jpg").write_bytes(b"x" * 16)
    bot.index = {"清楚": ["a.jpg"]}
    bot._save_index()

    first = bot._backfill_md5()
    second = bot._backfill_md5()

    assert first["with_md5"] == second["with_md5"] == 1
    assert bot._store.counts()["images"] == 1, "重复回填不得插入重复行"
    bot._store.close()