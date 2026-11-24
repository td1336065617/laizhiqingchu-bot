"""P3 批 15：单图上限、孤儿对账、硬链接去重（干跑默认）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from conftest import import_main


async def _collect(gen):
    return [item async for item in gen]


def _bot(tmp_path, max_image_mb=10):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"admin_users": ["admin-1"], "max_storage_mb": 4096, "max_image_mb": max_image_mb}
    bot.index = {}
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def test_max_image_mb_default_and_clamp(tmp_path):
    main, bot = _bot(tmp_path, max_image_mb=999)
    assert bot._max_image_mb() == 10, "越界值回默认"
    bot.config["max_image_mb"] = 0
    assert bot._max_image_mb() == 0, "0 表示关闭限制"
    bot.config["max_image_mb"] = 5
    assert bot._max_image_mb() == 5


def test_scan_orphans_reports_both_directions(tmp_path):
    main, bot = _bot(tmp_path)
    kept = bot.data_dir / "清楚"
    kept.mkdir()
    (kept / "a.jpg").write_bytes(b"x" * 8)
    ghost = bot.data_dir / "只在磁盘"
    ghost.mkdir()
    (ghost / "b.jpg").write_bytes(b"y" * 8)
    empty = bot.data_dir / "空目录"
    empty.mkdir()
    bot.index = {"清楚": ["a.jpg", "missing.jpg"], "只在索引": ["c.jpg"]}

    report = bot._scan_orphans()

    assert report["disk_only_keywords"] == ["只在磁盘", "空目录"]
    assert report["index_only_keywords"] == ["只在索引"]
    assert "清楚/missing.jpg" in report["missing_files"]
    assert "空目录" in report["empty_dirs"]


def test_dedupe_dry_run_does_not_touch_files(tmp_path):
    main, bot = _bot(tmp_path)
    for kw in ("清楚", "群友"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "same.jpg").write_bytes(b"same-content" * 10)
        bot.index[kw] = ["same.jpg"]
    before = (bot.data_dir / "清楚" / "same.jpg").stat().st_ino

    report = bot._dedupe_by_hardlink(dry_run=True)

    assert report["groups"] == 1
    assert report["files"] == 1
    assert report["reclaimed_mb"] >= 0
    assert (bot.data_dir / "清楚" / "same.jpg").stat().st_ino == before, "干跑不得改动文件"


def test_dedupe_apply_creates_hardlinks_and_keeps_names(tmp_path):
    main, bot = _bot(tmp_path)
    for kw in ("清楚", "群友"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "same.jpg").write_bytes(b"same-content" * 10)
        bot.index[kw] = ["same.jpg"]

    report = bot._dedupe_by_hardlink(dry_run=False)

    assert report["done"] == 1
    a = bot.data_dir / "清楚" / "same.jpg"
    b = bot.data_dir / "群友" / "same.jpg"
    assert a.exists() and b.exists(), "只链不删：两个文件名都要在"
    assert a.stat().st_ino == b.stat().st_ino
    assert bot.index == {"清楚": ["same.jpg"], "群友": ["same.jpg"]}, "索引零改动"


def test_dedupe_skips_already_linked(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    first = folder / "a.jpg"
    first.write_bytes(b"z" * 32)
    os.link(first, folder / "b.jpg")
    bot.index = {"清楚": ["a.jpg", "b.jpg"]}

    report = bot._dedupe_by_hardlink(dry_run=True)

    assert report["groups"] == 0, "已是硬链接的不应再规划一次"