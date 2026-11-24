"""P2 批 12（续）：别名进备份、恢复到空目录后别名一致。"""

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
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"max_storage_mb": 200, "backup_keep": 2, "backup_total_mb": 1024}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.index = {}
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def test_backup_contains_aliases_and_restore_merges(tmp_path):
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "蓝色大肥鱼"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.jpg").write_bytes(b"x" * 16)
    bot.index = {"蓝色大肥鱼": ["a.jpg"]}
    bot.alias_groups = {"蓝色大肥鱼": ["大肥鱼"]}
    bot._refresh_alias_map()

    info = bot._backup_to_zip()
    archive = bot.backup_root / info["filename"]
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        aliases = json.loads(zf.read("aliases.json"))
    assert "aliases.json" in names
    assert aliases["groups"]["蓝色大肥鱼"]["aliases"] == ["大肥鱼"]

    # 恢复到空环境：别名应一起回来
    fresh = _bot(tmp_path / "fresh")

    stats = fresh[1]._restore_from_zip(archive)

    assert stats["added"] == 1
    assert fresh[1].alias_map == {"大肥鱼": "蓝色大肥鱼"}
    assert fresh[1]._resolve_keyword("大肥鱼") == ("蓝色大肥鱼", "alias")


def test_export_single_keyword_carries_its_group_only(tmp_path):
    main, bot = _bot(tmp_path)
    for kw in ("蓝色大肥鱼", "清楚"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "a.jpg").write_bytes(b"x" * 16)
        bot.index[kw] = ["a.jpg"]
    bot.alias_groups = {"蓝色大肥鱼": ["大肥鱼"], "清楚": ["清清楚楚"]}
    bot._refresh_alias_map()

    info = bot._backup_to_zip(prefix="export", only_keyword="清楚")

    with zipfile.ZipFile(bot.backup_root / info["filename"]) as zf:
        aliases = json.loads(zf.read("aliases.json"))
    assert set(aliases["groups"]) == {"清楚"}, "导出单个关键词不应带上别的组"