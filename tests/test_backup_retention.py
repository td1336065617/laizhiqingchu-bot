"""P0 批 4：备份保留策略（份数 + 总量）、pre-restore 快照、恢复前自动备份。"""

from __future__ import annotations

import asyncio
import os
import time
import zipfile
from pathlib import Path

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
    bot.index = {"清楚": ["1.jpg"]}
    bot.blocked_keywords = set()
    (bot.data_dir / "清楚").mkdir(parents=True, exist_ok=True)
    (bot.data_dir / "清楚" / "1.jpg").write_bytes(b"x" * 16)
    return main, bot


def _fake_backup(folder: Path, name: str, size: int, mtime: float) -> Path:
    path = folder / name
    path.write_bytes(b"z" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_newest_by_count(tmp_path):
    main, bot = _bot(tmp_path, keep=2)
    now = time.time()
    for i, name in enumerate(["a", "b", "c"]):
        _fake_backup(bot.backup_root, f"sticker_backup_{name}.zip", 100, now - (10 - i))

    removed = bot._prune_backups()

    assert removed == ["sticker_backup_a.zip"], "最旧的一份应被删"
    assert sorted(p.name for p in bot.backup_root.glob("*.zip")) == [
        "sticker_backup_b.zip",
        "sticker_backup_c.zip",
    ]


def test_prune_never_deletes_just_created(tmp_path):
    main, bot = _bot(tmp_path, keep=1)
    now = time.time()
    _fake_backup(bot.backup_root, "sticker_backup_old.zip", 100, now - 10)
    fresh = _fake_backup(bot.backup_root, "sticker_backup_new.zip", 100, now)

    removed = bot._prune_backups(keep_name=fresh.name)

    assert removed == ["sticker_backup_old.zip"]
    assert fresh.exists()


def test_prune_caps_total_size(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path, keep=10, total_mb=1024)
    monkeypatch.setattr(bot, "_backup_total_mb", lambda: 1)   # 生产下限是 100MB，测试直接注入
    now = time.time()
    for i in range(4):
        _fake_backup(bot.backup_root, f"sticker_backup_{i}.zip", 600 * 1024, now - (10 - i))

    bot._prune_backups()

    total = sum(p.stat().st_size for p in bot.backup_root.glob("*.zip"))
    assert total <= 1024 * 1024, "总量必须降到上限以内"


def test_prune_keeps_latest_pre_restore_snapshot_only(tmp_path):
    main, bot = _bot(tmp_path, keep=10, total_mb=20000)
    now = time.time()
    _fake_backup(bot.backup_root, "pre-restore_old.zip", 100, now - 10)
    newest = _fake_backup(bot.backup_root, "pre-restore_new.zip", 100, now)

    bot._prune_backups()

    assert newest.exists(), "最新快照必须保留"
    assert not (bot.backup_root / "pre-restore_old.zip").exists(), "旧快照要被回收（否则磁盘会被吃满）"


def test_backup_produces_stored_zip_and_prunes(tmp_path):
    main, bot = _bot(tmp_path, keep=1)
    older = _fake_backup(bot.backup_root, "sticker_backup_old.zip", 100, time.time() - 10)

    info = bot._backup_to_zip()

    target = bot.backup_root / info["filename"]
    assert target.exists()
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist()[0] == "index.json"
        stored = {item.compress_type for item in zf.infolist()}
        assert stored == {zipfile.ZIP_STORED}, "图片已压缩，不应再用 DEFLATE"
    assert not older.exists(), "按 keep=1 应清掉旧包"
    assert info["pruned"] == ["sticker_backup_old.zip"]


def test_restore_creates_pre_restore_snapshot(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    calls = []

    def fake_restore(path):
        calls.append(path)
        return {"keywords": 0, "images": 0}

    monkeypatch.setattr(bot, "_restore_from_zip", fake_restore)

    class _Upload:
        async def save(self, path):
            Path(path).write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    class _Files(dict):
        pass

    files = _Files(file=_Upload())

    async def fake_files():
        return files

    monkeypatch.setattr(main, "request", type("R", (), {"files": staticmethod(fake_files), "json": staticmethod(lambda default=None: {})}))

    response = asyncio.run(bot._web_restore())

    assert calls, "恢复应被调用"
    assert response.status_code == 200
    snapshots = list(bot.backup_root.glob("pre-restore_*.zip"))
    assert len(snapshots) == 1, "恢复前必须留一份快照"


def test_restore_aborts_when_snapshot_fails(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    called = []
    monkeypatch.setattr(bot, "_restore_from_zip", lambda path: called.append(path) or {})

    def boom(prefix="sticker_backup"):
        raise OSError("磁盘满")

    monkeypatch.setattr(bot, "_backup_to_zip", boom)

    class _Upload:
        async def save(self, path):
            Path(path).write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    async def fake_files():
        return {"file": _Upload()}

    monkeypatch.setattr(main, "request", type("R", (), {"files": staticmethod(fake_files), "json": staticmethod(lambda default=None: {})}))

    response = asyncio.run(bot._web_restore())

    assert not called, "快照失败时绝不执行恢复"
    assert response.status_code >= 400