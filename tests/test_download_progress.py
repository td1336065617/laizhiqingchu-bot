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

def test_page_does_not_fetch_directly():
    """框架 iframe 是 sandbox 无 allow-same-origin：localStorage 抛 SecurityError、
    跨源带 Authorization 过不了预检，所以页面不得自己 fetch 下载地址（审计 S1/S2）。
    """
    text = PAGE.read_text(encoding="utf-8")
    assert "localStorage" not in text, "沙箱 iframe 里读 localStorage 会抛 SecurityError"
    assert "fetch(" not in text, "页面不应直连下载地址"
    assert "backup/download/url" not in text, "该端点已移除，页面不得再引用"
    assert "A.download(" in text, "必须走宿主桥接下载"


def test_progress_polled_by_filename():
    text = PAGE.read_text(encoding="utf-8")
    assert "backup/download/progress" in text
    assert "name: item.filename" in text, "并发下载时必须按文件名取进度"
