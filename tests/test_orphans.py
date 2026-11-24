"""孤儿治理（A 档：可量化；B 档：隔离区可回滚）回归。"""

from __future__ import annotations

import asyncio
import json
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
    bot.backup_root = tmp_path / "backups"
    bot.config = {"admin_users": ["a"], "max_storage_mb": 4096}
    bot.blocked_keywords = set()
    bot.alias_groups = {}
    bot.alias_map = {}
    bot._store = None
    bot._md5_cache = {}
    bot.index = {}
    return main, bot


def _write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_scan_quantifies_orphans(tmp_path):
    main, bot = _bot(tmp_path)
    MB = 1 << 20
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * MB)
    bot.index = {"猫": ["a.jpg"]}
    # ① 已索引词内的多余文件（与索引内容重复）
    _write(bot.data_dir / "猫" / "dup.jpg", b"A" * MB)
    # ② 内容唯一的孤儿
    _write(bot.data_dir / "猫" / "unique.jpg", b"B" * (MB // 2))
    # ③ 磁盘独有目录
    _write(bot.data_dir / "孤儿词" / "x.jpg", b"C" * (MB // 4))
    # ④ 去重崩溃残留
    _write(bot.data_dir / "猫" / "a.jpg.lnk", b"A")
    # ⑤ 符号链接（应被跳过）
    (bot.data_dir / "猫" / "link.jpg").symlink_to(bot.data_dir / "猫" / "a.jpg")

    report = bot._scan_orphans()

    assert "猫/dup.jpg" in report["disk_orphan_files"]
    assert "孤儿词" in report["disk_only_keywords"]
    assert report["orphan_mb"] > 0
    assert report["orphan_unique_mb"] > 0, "内容唯一的孤儿要单独计量"
    assert report["orphan_unique_mb"] < report["orphan_mb"], "重复内容不该计入唯一体积"
    assert report["lnk_residue_total"] == 1
    assert report["symlinks_skipped"] >= 1
    assert any("孤儿词/" in item for item in report["orphan_adoptable"])


def test_dedupe_includes_orphans(tmp_path):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 4096)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "orphan.jpg", b"A" * 4096)   # 内容与 a.jpg 相同

    plan = bot._dedupe_by_hardlink(dry_run=True)
    assert plan["groups"] == 1, plan
    assert plan["files"] == 1, plan

    done = bot._dedupe_by_hardlink(dry_run=False)
    assert done["done"] == 1
    a = (bot.data_dir / "猫" / "a.jpg").stat()
    b = (bot.data_dir / "猫" / "orphan.jpg").stat()
    assert a.st_ino == b.st_ino, "孤儿应被并到同一 inode（只加链接，不删文件）"
    assert (bot.data_dir / "猫" / "orphan.jpg").exists(), "不得删除孤儿文件"
    assert bot.index == {"猫": ["a.jpg"]}, "索引不得改动"


def test_quarantine_move_and_restore(tmp_path):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "orphan.jpg", b"B" * 20)
    _write(bot.data_dir / "孤儿词" / "x.jpg", b"C" * (64 * 1024))

    dry = bot._quarantine_orphans(dry_run=True)
    assert dry["files"] == 2 and dry["dry_run"] is True
    assert (bot.data_dir / "猫" / "orphan.jpg").exists(), "干跑不得移动"

    done = bot._quarantine_orphans(dry_run=False, batch="b1")
    assert done["done"] == 2, done
    assert not (bot.data_dir / "猫" / "orphan.jpg").exists()
    assert (bot.data_dir / "猫" / "a.jpg").exists(), "索引内的图不得被搬走"
    assert (bot.data_dir / ".orphans" / "b1" / "猫" / "orphan.jpg").exists()

    listed = bot._list_quarantine()
    assert listed["batches"][0]["batch"] == "b1"
    assert listed["batches"][0]["files"] == 2

    back = bot._restore_quarantine("b1")
    assert back["restored"] == 2
    assert (bot.data_dir / "猫" / "orphan.jpg").read_bytes() == b"B" * 20
    assert (bot.data_dir / "孤儿词" / "x.jpg").exists()


def test_restore_skips_existing_and_prune_removes(tmp_path):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "orphan.jpg", b"B" * 20)
    bot._quarantine_orphans(dry_run=False, batch="b2")
    _write(bot.data_dir / "猫" / "orphan.jpg", b"NEW")   # 还原前又有同名文件

    back = bot._restore_quarantine("b2")
    assert back["skipped"] == 1 and back["restored"] == 0
    assert (bot.data_dir / "猫" / "orphan.jpg").read_bytes() == b"NEW", "不得覆盖现有文件"

    freed = bot._prune_quarantine("b2")
    assert freed["files"] == 1
    assert not (bot.data_dir / ".orphans" / "b2").exists()


def test_orphan_endpoint_requires_confirm(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "orphan.jpg", b"B" * 20)

    class _Req:
        @staticmethod
        def json(default=None):
            return {"action": "apply"}

    monkeypatch.setattr(main, "request", _Req)
    resp = asyncio.run(bot._web_maintenance_orphans())
    raw = resp if isinstance(resp, dict) else json.loads(getattr(resp, "body", b"{}"))
    assert raw.get("status") == "error"
    assert (bot.data_dir / "猫" / "orphan.jpg").exists(), "未确认不得移动"


def test_quarantine_dir_not_scanned_as_keyword(tmp_path):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / ".orphans" / "b1" / "猫" / "old.jpg", b"Z" * 5)

    report = bot._scan_orphans()
    assert ".orphans" not in report["disk_only_keywords"]

def test_unique_orphan_inside_indexed_keyword_is_counted(tmp_path):
    """审计 S2：索引词内的孤儿不能被"目录级 MD5"判成已存在。"""
    main, bot = _bot(tmp_path)
    MB = 1 << 20
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * MB)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "unique.jpg", b"B" * (MB // 2))

    report = bot._scan_orphans()

    assert report["orphan_unique_mb"] >= 0.4, report
    assert any("unique.jpg" in item for item in report["orphan_adoptable"]), report


def test_quarantine_not_truncated_by_display_limit(tmp_path):
    """审计 G3：展示列表截 200，执行必须按磁盘全量。"""
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    for i in range(205):
        _write(bot.data_dir / "猫" / f"o{i}.jpg", bytes([i % 256]) * 4)

    report = bot._scan_orphans()
    assert report["disk_orphan_files_total"] == 205
    assert len(report["disk_orphan_files"]) == 200, "展示应截断"

    done = bot._quarantine_orphans(dry_run=False, batch="big")
    assert done["done"] == 205, "执行不得被展示截断限制"


def test_clear_keeps_quarantine(tmp_path, monkeypatch):
    """审计 G6：清空图库不得销毁隔离区里的保留副本。"""
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "orphan.jpg", b"B" * 10)
    bot._quarantine_orphans(dry_run=False, batch="keep")

    class _Req:
        @staticmethod
        def json(default=None):
            return {"confirm": "clear"}

    monkeypatch.setattr(main, "request", _Req)
    asyncio.run(bot._web_clear())

    assert (bot.data_dir / ".orphans" / "keep" / "猫" / "orphan.jpg").exists(), "隔离区必须保留"
    assert not (bot.data_dir / "猫" / "a.jpg").exists(), "图库本身应被清空"


def test_maintenance_lock_serializes_without_deadlock(tmp_path):
    """审计必修：锁只能在线程里持有；两个并发维护请求必须秒级返回且不重叠。"""
    import time

    main, bot = _bot(tmp_path)
    spans = []

    def slow(dry_run=True, batch=""):
        start = time.time()
        time.sleep(0.15)
        spans.append((start, time.time()))
        return {"dry_run": dry_run}

    async def run():
        return await asyncio.wait_for(
            asyncio.gather(
                bot._run_exclusive(slow, True, ""),
                bot._run_exclusive(slow, False, ""),
            ),
            timeout=5,
        )

    results = asyncio.run(run())

    assert len(results) == 2, "两个请求都应返回（不能死锁）"
    assert len(spans) == 2
    assert spans[1][0] >= spans[0][1] - 0.005, "两段执行时间窗口不得重叠"


def test_lnk_action_requires_confirm_and_spares_quarantine(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path)
    _write(bot.data_dir / "猫" / "a.jpg", b"A" * 10)
    bot.index = {"猫": ["a.jpg"]}
    _write(bot.data_dir / "猫" / "a.jpg.lnk", b"tmp")
    _write(bot.data_dir / ".orphans" / "b1" / "猫" / "keep.jpg.lnk", b"keep")

    def call(payload):
        class _Req:
            @staticmethod
            def json(default=None):
                return payload

        monkeypatch.setattr(main, "request", _Req)
        resp = asyncio.run(bot._web_maintenance_orphans())
        return resp if isinstance(resp, dict) else json.loads(getattr(resp, "body", b"{}"))

    denied = call({"action": "lnk"})
    assert denied.get("status") == "error"
    assert (bot.data_dir / "猫" / "a.jpg.lnk").exists(), "未确认不得删除"

    done = call({"action": "lnk", "confirm": "lnk"})
    assert done["data"]["removed"] == 1
    assert not (bot.data_dir / "猫" / "a.jpg.lnk").exists()
    assert (bot.data_dir / ".orphans" / "b1" / "猫" / "keep.jpg.lnk").exists(), "不得动隔离区"
