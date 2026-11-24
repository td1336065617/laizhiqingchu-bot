"""P0 批 6：按关键词导出（小卷）+ 前端进度/导出接线 + 保留策略不误删导出。"""

from __future__ import annotations

import asyncio
import json
import time
import zipfile
from pathlib import Path

from conftest import import_main

ROOT = Path(__file__).resolve().parent.parent


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.config = {"backup_keep": 2, "backup_total_mb": 1024}
    bot.blocked_keywords = {"御姐"}
    for keyword, names in (("清楚", ["a.jpg", "b.jpg"]), ("群友", ["c.jpg"])):
        folder = bot.data_dir / keyword
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x" * 32)
    bot.index = {"清楚": ["a.jpg", "b.jpg"], "群友": ["c.jpg"]}
    return main, bot


def test_export_only_keyword_is_standard_backup_package(tmp_path):
    main, bot = _bot(tmp_path)

    info = bot._backup_to_zip(prefix="export", only_keyword="清楚")

    target = bot.backup_root / info["filename"]
    assert target.name.startswith("export_")
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        index = json.loads(zf.read("index.json").decode("utf-8"))
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert set(index) == {"清楚"}, "导出包只能含该关键词"
    assert all(n.startswith("清楚/") for n in names if "/" in n)
    assert manifest["only_keyword"] == "清楚"
    assert info["images"] == 2


def test_export_rejects_unknown_keyword(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)

    monkeypatch.setattr(main, "request", type("R", (), {"query": {"keyword": "不存在"}}))
    response = asyncio.run(bot._web_export())
    body = response if isinstance(response, dict) else json.loads(response.body)
    assert body["status"] != "success"


def test_prune_never_touches_export_files(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    now = time.time()
    export = bot.backup_root / "export_清楚_1.zip"
    export.write_bytes(b"z" * 100)
    for i in range(4):
        path = bot.backup_root / f"sticker_backup_{i}.zip"
        path.write_bytes(b"z" * 100)
        (path).touch()

    bot._prune_backups()

    assert export.exists(), "用户主动导出的包不得被保留策略删除"
    assert len(list(bot.backup_root.glob("sticker_backup_*.zip"))) == 2


def test_export_endpoint_registered_and_page_wired():
    main = import_main()
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "manage" / "index.html").read_text(encoding="utf-8")
    assert "backup/export" in source
    assert "backup/progress" in source
    assert "btnExport" in page
    assert "backup/progress" in page
    assert "_web_export" in source