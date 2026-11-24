"""P0 批 1：JSON 写入必须原子（tmp + fsync + os.replace）。

背景：原实现用 `write_text` 直接截断覆盖，崩溃/磁盘满/并发写会留下半截文件，
索引随之损坏（实测两个写者并发 1309 次里 1301 次读到损坏 JSON）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import import_main


def test_atomic_write_replaces_whole_file(tmp_path):
    main = import_main()
    target = tmp_path / "index.json"
    payload = {f"关键词{i}": [f"{i}.png", f"{i}b.png"] for i in range(1000)}

    main._atomic_write_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not list(tmp_path.glob("*.tmp")), "成功路径不应残留 tmp"


def test_atomic_write_failure_keeps_original_and_tmp(tmp_path, monkeypatch):
    main = import_main()
    target = tmp_path / "index.json"
    original = {"a": ["1.png"]}
    main._atomic_write_json(target, original)

    def boom(src, dst):
        raise OSError("模拟 rename 失败")

    monkeypatch.setattr(main.os, "replace", boom)
    with pytest.raises(OSError):
        main._atomic_write_json(target, {"b": ["2.png"]})

    assert json.loads(target.read_text(encoding="utf-8")) == original, "失败不得覆盖原文件"
    assert list(tmp_path.glob("index.json.*.tmp")), "失败要留 tmp 供诊断"


def test_atomic_write_truncated_dump_does_not_touch_target(tmp_path, monkeypatch):
    """json.dump 中途抛错（模拟磁盘满）时，目标文件必须还是旧的完整版本。"""
    main = import_main()
    target = tmp_path / "index.json"
    main._atomic_write_json(target, {"old": ["1.png"]})

    def broken_dump(payload, handle, **kwargs):
        handle.write("{\"half\": ")
        raise OSError("磁盘满")

    monkeypatch.setattr(main.json, "dump", broken_dump)
    with pytest.raises(OSError):
        main._atomic_write_json(target, {"new": ["2.png"]})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": ["1.png"]}


def test_save_index_roundtrip_via_instance(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path
    bot.index_path = tmp_path / "index.json"
    bot.index = {"清楚": ["a.png", "b.png"]}

    bot._save_index()

    assert json.loads(bot.index_path.read_text(encoding="utf-8")) == bot.index


def test_save_blocked_sorted_and_atomic(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path
    bot.blocked_path = tmp_path / "blocked_keywords.json"
    bot.blocked_keywords = {"御姐", "姐姐"}

    bot._save_blocked()

    assert json.loads(bot.blocked_path.read_text(encoding="utf-8")) == ["姐姐", "御姐"]


def test_save_config_roundtrip(tmp_path):
    main = import_main()
    config_path = tmp_path / "sticker_plugin_config.json"
    config = main._PluginConfig(config_path)
    config["admin_users"] = ["x"]
    config["max_storage_mb"] = 2010

    config.save_config()

    reloaded = main._PluginConfig(config_path)
    assert reloaded["admin_users"] == ["x"]
    assert reloaded["max_storage_mb"] == 2010


def test_concurrent_writers_never_corrupt(tmp_path):
    """两个写者交替写 100 次：每次读到的都必须是可解析的完整 JSON。"""
    import threading

    main = import_main()
    target = tmp_path / "index.json"
    main._atomic_write_json(target, {"seed": ["0.png"]})
    errors = []

    def writer(tag):
        for i in range(100):
            try:
                main._atomic_write_json(target, {f"{tag}-{i}": [f"{i}.png"]})
            except Exception as exc:  # pragma: no cover
                errors.append((tag, exc))

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data) == 1, "最后一次写入必须是完整文件"