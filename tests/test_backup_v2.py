"""P1 批 10：备份 v2（单段元数据白名单）与 v1 向后兼容。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from conftest import import_main


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"max_storage_mb": 200}
    bot.index = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def _make_zip(path: Path, version: int, extra=None):
    index = {"清楚": ["a.jpg"]}
    manifest = {"version": version, "keywords": 1, "images": 1}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("index.json", json.dumps(index))
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("清楚/a.jpg", b"x" * 8)
        for name, payload in (extra or {}).items():
            zf.writestr(name, payload)
    return path


def test_v2_package_with_alias_metadata_is_accepted(tmp_path):
    main, bot = _bot(tmp_path)
    archive = _make_zip(tmp_path / "v2.zip", 2, {"aliases.json": "{}", "admin.json": "[]"})

    stats = bot._restore_from_zip(archive)

    assert stats["added"] == 1
    assert "清楚" in bot.index


def test_v1_package_still_restores(tmp_path):
    main, bot = _bot(tmp_path)
    archive = _make_zip(tmp_path / "v1.zip", 1)

    stats = bot._restore_from_zip(archive)

    assert stats["added"] == 1


def test_unknown_single_file_still_rejected(tmp_path):
    """白名单只放行已知元数据，其他单段文件仍要拒绝（防夹带）。"""
    import pytest

    main, bot = _bot(tmp_path)
    archive = _make_zip(tmp_path / "bad.zip", 2, {"evil.txt": "x"})

    with pytest.raises(ValueError):
        bot._restore_from_zip(archive)


def test_manifest_written_as_v2(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.jpg").write_bytes(b"x" * 8)
    bot.index = {"清楚": ["a.jpg"]}

    info = bot._backup_to_zip()

    with zipfile.ZipFile(bot.backup_root / info["filename"]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["version"] == 2