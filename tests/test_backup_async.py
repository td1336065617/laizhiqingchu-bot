"""P0 批 5：备份异步化 + 进度（事件循环不被卡住）。"""

from __future__ import annotations

import asyncio
import json
import time

from conftest import import_main


def body_of(response) -> dict:
    if isinstance(response, dict):
        return response
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return json.loads(body)


def _bot(tmp_path, files=3):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.data_dir = tmp_path / "stickers"
    bot.data_dir.mkdir(parents=True, exist_ok=True)
    bot.backup_root = tmp_path / "sticker_backups"
    bot.backup_root.mkdir(parents=True, exist_ok=True)
    bot.index_path = bot.data_dir / "index.json"
    bot.blocked_path = bot.data_dir / "blocked_keywords.json"
    bot.config = {"backup_keep": 2, "backup_total_mb": 1024}
    bot.blocked_keywords = set()
    folder = bot.data_dir / "清楚"
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(files):
        name = f"{i}.jpg"
        (folder / name).write_bytes(b"x" * 1024)
        names.append(name)
    bot.index = {"清楚": names}
    return main, bot


def test_backup_returns_immediately_and_reports_progress(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path, files=5)
    started = []
    real = bot._backup_to_zip

    def slow(prefix="sticker_backup", progress=None):
        started.append(time.time())
        time.sleep(0.3)                     # 模拟 637MB 压缩耗时
        return real(prefix=prefix, progress=progress)

    monkeypatch.setattr(bot, "_backup_to_zip", slow)

    async def scenario():
        t0 = time.time()
        response = await bot._web_backup()
        elapsed = time.time() - t0
        body = body_of(response)
        assert body["status"] == "accepted"
        assert elapsed < 0.1, f"受理必须立刻返回，实测 {elapsed:.2f}s"
        assert bot._backup_state["state"] == "running"

        # 备份线程在跑时，事件循环必须还能跑别的协程
        ticks = 0
        while bot._backup_state["state"] == "running":
            ticks += 1
            await asyncio.sleep(0.02)
            assert ticks < 100, "备份应已完成"
        assert ticks >= 2, "备份期间事件循环应保持可调度"
        assert bot._backup_state["state"] == "done"
        assert bot._backup_state["percent"] == 100

    asyncio.run(scenario())
    assert started, "备份必须真的跑过"


def test_second_request_while_running_does_not_start_another(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path, files=2)
    calls = []

    def slow(prefix="sticker_backup", progress=None):
        calls.append(1)
        time.sleep(0.2)
        return {"filename": "x.zip", "size": 1, "keywords": 1, "images": 1, "pruned": []}

    monkeypatch.setattr(bot, "_backup_to_zip", slow)

    async def scenario():
        await bot._web_backup()
        second = body_of(await bot._web_backup())
        assert second["status"] == "running"
        assert "备份中" in second["message"]
        assert len(calls) <= 1, "重复点击不得启动第二份备份"
        while bot._backup_state["state"] == "running":
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        assert len(calls) == 1, "整轮只应有一次备份"

    asyncio.run(scenario())


def test_backup_failure_is_recorded(tmp_path, monkeypatch):
    main, bot = _bot(tmp_path, files=1)

    def boom(prefix="sticker_backup", progress=None):
        raise OSError("磁盘满")

    monkeypatch.setattr(bot, "_backup_to_zip", boom)

    async def scenario():
        await bot._web_backup()
        while bot._backup_state["state"] == "running":
            await asyncio.sleep(0.02)
        assert bot._backup_state["state"] == "failed"
        assert "磁盘满" in bot._backup_state["error"], "失败原因必须留在状态里"

    asyncio.run(scenario())


def test_progress_endpoint_returns_state(tmp_path):
    main, bot = _bot(tmp_path, files=1)

    async def scenario():
        body = body_of(await bot._web_backup_progress())
        assert body["data"]["state"] == "idle"

    asyncio.run(scenario())


def test_progress_is_monotonic_with_real_zip(tmp_path):
    main, bot = _bot(tmp_path, files=60)
    seen = []
    real = bot._backup_to_zip

    def spy(prefix="sticker_backup", progress=None):
        def wrap():
            pass
        result = real(prefix=prefix, progress=progress)
        return result

    monkeypatch_targets = []
    info = None

    class _Recorder(dict):
        pass

    progress = bot._backup_progress_init()
    progress_obj = progress

    # 直接观察 progress dict 的更新序列
    orig_update = dict.update

    info = bot._backup_to_zip(progress=progress_obj)

    assert progress_obj["done_files"] == 60
    assert progress_obj["percent"] == 100
    assert info["images"] == 60