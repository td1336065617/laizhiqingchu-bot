"""P3 审计闭环：配置契约、维护结果显示、扫描补档、全拒不建目录、去重确认。"""

from __future__ import annotations

import asyncio
import json
import re
import types
from pathlib import Path

from conftest import import_main

PAGE = Path(__file__).resolve().parent.parent / "pages" / "manage" / "index.html"


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.alias_path = bot.data_dir / "keyword_groups.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.backup_root = tmp_path / "sticker_backups"
    bot.config = {"admin_users": ["admin-1"], "max_storage_mb": 4096}
    bot.index = {}
    bot.alias_groups = {}
    bot.alias_map = {}
    bot.blocked_keywords = set()
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def body(resp):
    if isinstance(resp, dict):
        return resp
    raw = getattr(resp, "body", None)
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def test_config_get_exposes_max_image_mb(tmp_path):
    """P3 审计严重 2：页面控件必须有对应字段，否则永远显示回落值。"""
    main, bot = _bot(tmp_path)
    data = body(asyncio.run(bot._web_config_get()))["data"]
    assert data["max_image_mb"] == 10
    assert {"admin_users", "max_storage_mb", "backup_keep", "backup_total_mb", "max_image_mb"} <= set(data)


def test_config_set_accepts_max_image_mb(tmp_path):
    main, bot = _bot(tmp_path)
    bot.config = type("Cfg", (dict,), {"save_config": staticmethod(lambda: None)})(bot.config)
    updated = bot._apply_config_payload({"max_image_mb": 0})
    assert updated["max_image_mb"] == 0
    assert bot._max_image_mb() == 0, "0 表示不限制，必须真的生效"
    for bad in (-1, 51, "abc"):
        try:
            bot._apply_config_payload({"max_image_mb": bad})
        except ValueError:
            continue
        raise AssertionError(f"非法值应被拒：{bad}")


def test_page_never_reads_nested_data():
    """P3 审计严重 1：桥接已解包 data，页面不得再读一层。"""
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"\br\.data\b", text), "页面仍在读 r.data"
    assert "const st = p || {}" in text


def test_page_config_fields_are_served_by_backend(tmp_path):
    """页面引用的配置键必须都在 GET 里出现（防死键）。"""
    main, bot = _bot(tmp_path)
    data = body(asyncio.run(bot._web_config_get()))["data"]
    text = PAGE.read_text(encoding="utf-8")
    for key in ("max_image_mb", "backup_keep", "backup_total_mb", "max_storage_mb"):
        assert key in text, f"页面缺控件：{key}"
        assert key in data, f"后端 GET 缺字段：{key}"


def test_scan_reports_disk_orphan_files(tmp_path):
    """P3 审计一般 4：已索引关键词里多余的文件要能被发现。"""
    main, bot = _bot(tmp_path)
    folder = bot.data_dir / "部分"
    folder.mkdir(parents=True)
    (folder / "indexed.jpg").write_bytes(b"x" * 8)
    (folder / "stray.jpg").write_bytes(b"y" * 8)
    bot.index = {"部分": ["indexed.jpg"]}

    report = bot._scan_orphans()

    assert report["disk_orphan_files"] == ["部分/stray.jpg"]
    assert report["disk_orphan_files_total"] == 1


def test_rejected_batch_creates_no_dir_and_keeps_index(tmp_path):
    """P3 审计一般 5：整批被拒时不得建空目录、不得重写索引。"""
    main, bot = _bot(tmp_path)
    bot.config["max_image_mb"] = 1
    bot._save_index()
    before = bot.index_path.read_bytes()

    class _Img:
        def __init__(self, path):
            self._path = path

        async def convert_to_file_path(self):
            return str(self._path)

    big = tmp_path / "big.jpg"
    big.write_bytes(b"z" * (2 * 1024 * 1024))

    added, skipped, failed, reason = asyncio.run(bot._add_images("全新词", [_Img(big)]))

    assert (added, skipped, failed) == (0, 0, 0)
    assert reason and "上限" in reason
    assert not (bot.data_dir / "全新词").exists(), "整批被拒不得留下空目录"
    assert bot.index_path.read_bytes() == before, "索引不得被重写"


def test_dedupe_apply_requires_confirm_token(tmp_path, monkeypatch):
    """P3 审计一般 3 配套：改盘操作必须显式确认。"""
    main, bot = _bot(tmp_path)
    for kw in ("一", "二"):
        folder = bot.data_dir / kw
        folder.mkdir(parents=True)
        (folder / "same.jpg").write_bytes(b"dup" * 100)
        bot.index[kw] = ["same.jpg"]

    class _Req:
        @staticmethod
        async def json(default=None):
            return {"apply": True}

    monkeypatch.setattr(main, "request", _Req)
    resp = body(asyncio.run(bot._web_maintenance_dedupe()))

    assert resp.get("status") != "success"
    a = (bot.data_dir / "一" / "same.jpg").stat().st_ino
    b = (bot.data_dir / "二" / "same.jpg").stat().st_ino
    assert a != b, "未确认时不得改动 inode"