"""P0 批 3：用量口径（贴纸库 + 备份分开统计）与超限拦截。"""

from __future__ import annotations

import asyncio
import logging
import os
import types
from pathlib import Path

from conftest import import_main


def _bot(tmp_path, limit_mb=200):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.config = {"max_storage_mb": limit_mb}
    bot.index = {}
    return main, bot


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_usage_counts_stickers_and_backups_separately(tmp_path):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "清楚" / "1.jpg", 1024 * 1024)
    _write(bot.backup_root / "sticker_backup_1.zip", 2 * 1024 * 1024)

    usage = bot._storage_usage()

    assert usage["files"] == 1
    assert usage["used_mb"] == 1.0
    assert usage["backup_count"] == 1
    assert usage["backup_mb"] == 2.0
    assert usage["limit_mb"] == 200


def test_usage_dedupes_hardlinks(tmp_path):
    main, bot = _bot(tmp_path)
    first = _write(bot.data_dir / "清楚" / "1.jpg", 1024)
    os.link(first, bot.data_dir / "清楚" / "1-link.jpg")

    usage = bot._storage_usage()

    assert usage["files"] == 1, "硬链接同一 inode 只算一次，否则用量虚高"
    assert usage["used_mb"] == round(1024 / 1048576, 1)


def test_enforce_storage_under_limit_returns_none(tmp_path):
    main, bot = _bot(tmp_path, limit_mb=10)
    _write(bot.data_dir / "清楚" / "1.jpg", 1024)

    assert bot._enforce_storage() is None


def test_enforce_storage_over_limit_returns_actionable_text(tmp_path):
    main, bot = _bot(tmp_path, limit_mb=1)
    _write(bot.data_dir / "清楚" / "1.jpg", 2 * 1024 * 1024)

    reason = bot._enforce_storage()

    assert reason and "存储已满" in reason and "上限" in reason


def test_add_images_blocked_leaves_no_trace(tmp_path):
    main, bot = _bot(tmp_path, limit_mb=1)
    _write(bot.data_dir / "清楚" / "1.jpg", 2 * 1024 * 1024)

    result = asyncio.run(bot._add_images("新词", []))

    assert result == (0, 0, 0)
    assert not (bot.data_dir / "新词").exists(), "被拒时不得创建目录"
    assert "新词" not in bot.index
    assert "存储已满" in bot._last_storage_blocked


def test_check_storage_warns_with_backup_info(tmp_path, caplog):
    main, bot = _bot(tmp_path, limit_mb=1)
    _write(bot.data_dir / "清楚" / "1.jpg", 2 * 1024 * 1024)
    _write(bot.backup_root / "sticker_backup_1.zip", 1024 * 1024)

    with caplog.at_level(logging.WARNING):
        bot._check_storage()

    assert "备份另占" in caplog.text