"""下载进度：服务器流式发送时记账，页面轮询显示（浏览器不给进度）。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from conftest import import_main

PAGE = Path(__file__).resolve().parent.parent / "pages" / "manage" / "index.html"


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.config = {"max_storage_mb": 4096}
    bot.index = {}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot._store = None
    bot._md5_cache = {}
    return main, bot


def body(resp):
    if isinstance(resp, dict):
        return resp
    raw = getattr(resp, "body", None)
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def test_chunks_carry_progress_and_finish(tmp_path):
    main, bot = _bot(tmp_path)
    target = bot.backup_root / "sticker_backup_x.zip"
    payload = b"z" * (3 * 1024 * 1024 + 7)
    target.write_bytes(payload)
    state = {"filename": target.name, "total": len(payload), "sent": 0, "percent": 0, "state": "downloading"}

    async def consume():
        chunks = []
        async for chunk in bot._iter_backup_chunks(target, state):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(consume())

    assert b"".join(chunks) == payload, "分块拼回必须与源文件一致"
    assert len(chunks) >= 3, "应按 1MB 分块"
    assert state["sent"] == len(payload)
    assert state["percent"] == 100
    assert state["state"] == "done"
    assert "finished_at" in state


def test_missing_file_marks_aborted(tmp_path):
    main, bot = _bot(tmp_path)
    target = bot.backup_root / "not-exist.zip"
    state = {"filename": "not-exist.zip", "total": 10, "sent": 0, "percent": 0, "state": "downloading"}

    async def consume():
        async for _ in bot._iter_backup_chunks(target, state):
            pass

    asyncio.run(consume())

    assert state["state"] == "aborted"
    assert state.get("error")


def test_progress_endpoint_reports_idle_then_state(tmp_path):
    main, bot = _bot(tmp_path)
    assert body(asyncio.run(bot._web_download_progress()))["data"]["state"] == "idle"
    bot._download_state = {"filename": "a.zip", "percent": 42, "state": "downloading"}
    assert body(asyncio.run(bot._web_download_progress()))["data"]["percent"] == 42


def test_download_route_registered():
    main = import_main()
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "backup/download/progress" in src
    assert "_iter_backup_chunks" in src
    assert "stream_response" in src


def test_page_shows_download_progress_panel():
    text = PAGE.read_text(encoding="utf-8")
    for element in ("dlPanel", "dlFill", "dlText"):
        assert 'id="%s"' % element in text, "缺元素：" + element
    assert "function startDownload" in text or "async function startDownload" in text
    assert "backup/download/progress" in text
    assert "服务器已发送" in text, "要说明进度口径，避免误解为浏览器进度"

def test_download_url_endpoint_builds_absolute_url(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    target = bot.backup_root / "sticker_backup_a.zip"
    target.write_bytes(b"z" * 16)

    class _Req:
        query = {"name": target.name}
        base_url = "https://example.com/dashboard/"
        path = "/sticker_plugin/backup/download/url"

    monkeypatch.setattr(main, "request", _Req)
    data = body(asyncio.run(bot._web_download_url()))["data"]

    assert data["url"] == "https://example.com/dashboard/sticker_plugin/backup/download?name=sticker_backup_a.zip"
    assert data["size"] == 16


def test_download_url_rejects_missing_and_traversal(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)

    class _Req:
        def __init__(self, name):
            self.query = {"name": name}
            self.base_url = "https://example.com/"
            self.path = "/p/backup/download/url"

    for bad in ("", "../../etc/passwd", "nope.zip"):
        monkeypatch.setattr(main, "request", _Req(bad))
        resp = body(asyncio.run(bot._web_download_url()))
        assert resp.get("status") != "success", bad


def test_page_uses_streaming_fetch_with_auth_and_fallback():
    text = PAGE.read_text(encoding="utf-8")
    assert "backup/download/url" in text
    assert "getReader" in text, "必须读流才能拿到真实接收字节"
    assert "Authorization" in text and "Bearer" in text
    assert "saveBlob" in text
    assert "A.download" in text, "拿不到流时要回退到桥接下载"
    assert "真实接收字节" in text
