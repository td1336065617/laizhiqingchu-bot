"""P0 批 2：MD5 分块 1MB + 目录级缓存（添加图片不再全量重算）。"""

from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path

from conftest import import_main


def _bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path
    return main, bot


def _write(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def test_md5_default_chunk_is_1mb():
    main = import_main()
    assert inspect.signature(main.StickerPlugin._md5).parameters["chunk_size"].default == 1 << 20


def test_md5_matches_hashlib(tmp_path):
    main, bot = _bot(tmp_path)
    payload = b"x" * (3 * 1024 * 1024)
    path = _write(tmp_path, "a.jpg", payload)

    assert main.StickerPlugin._md5(path) == hashlib.md5(payload).hexdigest()


def test_folder_md5s_reuses_cache(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    folder = tmp_path / "清楚"
    _write(folder, "1.jpg", b"one")
    _write(folder, "2.jpg", b"two")

    calls = []
    real = main.StickerPlugin._md5

    def counting(path, chunk_size=1 << 20):
        calls.append(path.name)
        return real(path, chunk_size)

    monkeypatch.setattr(main.StickerPlugin, "_md5", staticmethod(counting))

    first = bot._folder_md5s("清楚")
    assert len(calls) == 2 and len(first) == 2

    second = bot._folder_md5s("清楚")
    assert len(calls) == 2, "第二次不应再算任何 MD5"
    assert second == first


def test_folder_md5s_drops_stale_entries(tmp_path):
    main, bot = _bot(tmp_path)
    folder = tmp_path / "清楚"
    _write(folder, "1.jpg", b"one")
    keep = _write(folder, "2.jpg", b"two")

    bot._folder_md5s("清楚")
    (folder / "1.jpg").unlink()

    after = bot._folder_md5s("清楚")
    assert set(after) == {"2.jpg"}, "已删除的文件必须从缓存里消失"
    assert after["2.jpg"] == main.StickerPlugin._md5(keep)


def test_folder_md5s_recomputes_changed_content(tmp_path):
    main, bot = _bot(tmp_path)
    folder = tmp_path / "清楚"
    _write(folder, "1.jpg", b"old")
    bot._folder_md5s("清楚")

    (folder / "1.jpg").unlink()
    _write(folder, "1.jpg", b"new")

    after = bot._folder_md5s("清楚")
    assert after["1.jpg"] == main.StickerPlugin._md5(folder / "1.jpg")
    assert after["1.jpg"] != hashlib.md5(b"old").hexdigest()


def test_add_images_no_longer_scans_folder_inline():
    """源码守卫：_add_images 里不应再出现逐文件算 MD5 的循环。"""
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    body = source.split("async def _add_images")[1].split("async def ")[0]
    assert "existing_md5.add(self._md5(" not in body
    assert "_folder_md5s(" in body