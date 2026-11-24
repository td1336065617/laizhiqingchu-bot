"""P0 审计闭环：S1 备份并发、S3 半截包、M2 拒绝原因、M3/M4 保留边界、keep_name 保护。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

from conftest import import_main


def _bot(tmp_path, keep=2, total_mb=1024):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.config = {"backup_keep": keep, "backup_total_mb": total_mb}
    bot.blocked_keywords = set()
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(4):
        name = f"{i}.jpg"
        (folder / name).write_bytes(b"x" * 64)
        names.append(name)
    bot.index = {"清楚": names}
    return main, bot


def _zip(folder: Path, name: str, size: int, mtime: float) -> Path:
    path = folder / name
    path.write_bytes(b"z" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_backup_survives_concurrent_index_mutation(tmp_path):
    """S1：备份遍历期间有新关键词入库，不得 RuntimeError。"""
    main, bot = _bot(tmp_path)
    stop = threading.Event()
    errors = []

    def mutate():
        i = 0
        while not stop.is_set():
            bot.index[f"new{i}"] = []
            i += 1
            time.sleep(0.001)

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _ in range(5):
            try:
                info = bot._backup_to_zip()
                assert info["images"] == 4
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    finally:
        stop.set()
        thread.join()
    assert not errors, f"备份与入库并发时必须成功：{errors[0]!r}"


def test_failed_backup_leaves_no_half_zip(tmp_path, monkeypatch):
    """S3：中途失败不得留下 .zip（会被列表/下载/用量当成有效包）。"""
    main, bot = _bot(tmp_path)

    def boom(path, chunk_size=1 << 20):
        raise OSError("读文件失败")

    monkeypatch.setattr(main.StickerPlugin, "_md5", staticmethod(boom))
    with pytest.raises(OSError):
        bot._backup_to_zip()

    assert not list(bot.backup_root.glob("*.zip")), "不得残留半截 zip"
    assert not list(bot.backup_root.glob("*.part")), "临时文件也必须清掉"


def test_keep_name_protected_even_when_oldest(tmp_path):
    """keep_name 的保护必须能单独判定：让新包的 mtime 最旧。"""
    main, bot = _bot(tmp_path, keep=1)
    now = time.time()
    fresh = _zip(bot.backup_root, "sticker_backup_fresh.zip", 100, now - 100)
    _zip(bot.backup_root, "sticker_backup_old.zip", 100, now - 10)

    removed = bot._prune_backups(keep_name=fresh.name)

    assert fresh.exists(), "keep_name 指定的包即使最旧也不得删除"
    assert removed == ["sticker_backup_old.zip"]


def test_prune_survives_stat_error(tmp_path, monkeypatch):
    """M3：某一份 stat 失败时，清理不能整体抛错或整体失效。"""
    main, bot = _bot(tmp_path, keep=1)
    now = time.time()
    for i in range(3):
        _zip(bot.backup_root, f"sticker_backup_{i}.zip", 100, now - (10 - i))

    real_stat = Path.stat
    bad = bot.backup_root / "sticker_backup_1.zip"

    def flaky(self, *args, **kwargs):
        if self.name == bad.name:
            raise OSError("stat 失败")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    removed = bot._prune_backups()

    assert removed, "坏条目不应让整份清理失效"


def test_prune_count_excludes_pre_restore(tmp_path):
    """M4：份数上限只约束自动备份，快照不占额度。"""
    main, bot = _bot(tmp_path, keep=2)
    now = time.time()
    _zip(bot.backup_root, "pre-restore_a.zip", 100, now - 100)
    for i in range(3):
        _zip(bot.backup_root, f"sticker_backup_{i}.zip", 100, now - (10 - i))

    bot._prune_backups()

    backups = list(bot.backup_root.glob("sticker_backup_*.zip"))
    snapshots = list(bot.backup_root.glob("pre-restore_*.zip"))
    assert len(backups) == 2, "自动备份保留 keep 份"
    assert len(snapshots) == 1, "快照不占份数额度"


def test_add_images_returns_reason_not_instance_flag(tmp_path):
    """M2：拒绝原因随返回值返回，不留在实例上（并发会串味）。"""
    main, bot = _bot(tmp_path)
    bot.config["max_storage_mb"] = 1
    (bot.data_dir / "清楚" / "big.jpg").write_bytes(b"x" * (2 * 1024 * 1024))

    added, skipped, failed, reason = asyncio.run(bot._add_images("明确", []))

    assert (added, skipped, failed) == (0, 0, 0)
    assert reason and "存储已满" in reason
    assert not hasattr(bot, "_last_storage_blocked")


def test_export_and_restore_snapshot_run_off_loop(tmp_path, monkeypatch):
    """M1：导出与恢复前快照必须走线程，不能在协程里同步压缩。"""
    main, bot = _bot(tmp_path)
    seen = []
    real = bot._backup_to_zip

    def spy(prefix="sticker_backup", progress=None, only_keyword=None):
        seen.append(threading.current_thread().name)
        return real(prefix=prefix, progress=progress, only_keyword=only_keyword)

    monkeypatch.setattr(bot, "_backup_to_zip", spy)
    monkeypatch.setattr(main, "request", type("R", (), {"query": {"keyword": "清楚"}}))

    asyncio.run(bot._web_export())

    assert seen, "导出应真的调用备份实现"
    assert seen[0] != threading.current_thread().name, "必须在工作线程里跑，而不是事件循环线程"