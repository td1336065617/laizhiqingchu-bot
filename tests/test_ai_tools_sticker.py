"""sticker AI 工具行为测试：屏蔽校验 / 限流 / 幂等窗口 / 权限 / 只读不写盘。"""

from __future__ import annotations

import asyncio
import json

from conftest import import_main


def run(coro):
    return asyncio.run(coro)


def payload(raw) -> dict:
    return json.loads(raw)


class FakeEvent:
    def __init__(self, sender: str = "1000", umo: str = "aiocqhttp:GroupMessage:g1") -> None:
        self._sender = sender
        self.unified_msg_origin = umo
        self.sent: list = []

    def get_sender_id(self) -> str:
        return self._sender

    def get_platform_id(self) -> str:
        return "aiocqhttp"

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def plain_result(self, text: str):
        return type("R", (), {"text": str(text)})()

    def image_result(self, path: str):
        return type("R", (), {"file": str(path)})()


def make_bot(tmp_path, **overrides):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    config = dict(main.AI_CONFIG_DEFAULTS)
    config.update(overrides.pop("config", {}) or {})
    bot.config = config
    bot.data_dir = tmp_path
    bot.index = overrides.pop("index", {}) or {}
    bot.blocked_keywords = overrides.pop("blocked", set()) or set()
    bot._ai_throttle = None
    bot._ai_send_log = {}
    return main, bot


def add_files(tmp_path, keyword: str, names) -> None:
    folder = tmp_path / keyword
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"stub")


def test_send_requires_keyword(tmp_path) -> None:
    _main, bot = make_bot(tmp_path)
    data = payload(run(bot.sticker_send(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument"


def test_send_checks_blocked_keyword_first(tmp_path) -> None:
    """ST-01：屏蔽集合必须先查，屏蔽后不发图。"""
    _main, bot = make_bot(tmp_path, blocked={"猫"}, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    event = FakeEvent()
    data = payload(run(bot.sticker_send(event, keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "disabled"
    assert event.sent == [], "屏蔽关键词绝不能发图"


def test_send_missing_keyword_hints_candidates(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫猫": ["a.png"], "狗狗": ["b.png"]})
    data = payload(run(bot.sticker_send(FakeEvent(), keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "not_found"
    assert "猫猫" in str(data["error"].get("hint") or "")


def test_send_success_and_idempotent_window(tmp_path) -> None:
    """GR-12：窗口内重复调用不重复发图，返回 deduped=True。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png", "b.png"]})
    add_files(tmp_path, "猫", ["a.png", "b.png"])
    event = FakeEvent()
    first = payload(run(bot.sticker_send(event, keyword="猫")))
    second = payload(run(bot.sticker_send(event, keyword="猫")))
    assert first["ok"] is True and first["data"]["deduped"] is False
    assert len(event.sent) == 1, "第二次不应重复发图"
    assert second["ok"] is True and second["data"]["deduped"] is True


def test_send_dedup_window_can_be_zero(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_send_dedup_window": 0}, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    event = FakeEvent()
    run(bot.sticker_send(event, keyword="猫"))
    run(bot.sticker_send(event, keyword="猫"))
    assert len(event.sent) == 2


def test_send_switch_disables_only_sending(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_send_enabled": False}, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    event = FakeEvent()
    data = payload(run(bot.sticker_send(event, keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "disabled"
    assert event.sent == []
    bot._is_admin = lambda event: True
    stats = payload(run(bot.sticker_stats(FakeEvent())))
    assert stats["ok"] is True, "只读工具不该被发送开关关掉"


def test_send_platform_failure_is_reported(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])

    class BadEvent(FakeEvent):
        async def send(self, chain):
            raise RuntimeError("platform down")

    data = payload(run(bot.sticker_send(BadEvent(), keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "capability_unavailable"


def test_send_throttled_by_caller(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_throttle_limit": 2}, index={"猫": ["a.png"], "狗": ["b.png"], "鱼": ["c.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    add_files(tmp_path, "狗", ["b.png"])
    add_files(tmp_path, "鱼", ["c.png"])
    event = FakeEvent()
    assert payload(run(bot.sticker_send(event, keyword="猫")))["ok"] is True
    assert payload(run(bot.sticker_send(event, keyword="狗")))["ok"] is True
    third = payload(run(bot.sticker_send(event, keyword="鱼")))
    assert third["ok"] is False and third["error"]["code"] == "throttled"


def test_throttle_is_per_caller(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_throttle_limit": 1}, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    first = payload(run(bot.sticker_send(FakeEvent(sender="1000"), keyword="猫")))
    other = payload(run(bot.sticker_send(FakeEvent(sender="2000", umo="aiocqhttp:GroupMessage:g1"), keyword="猫")))
    assert first["ok"] is True
    assert other["ok"] is True, "节流维度是每个调用者，别人不该被连坐"


def test_stats_requires_admin(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    bot._is_admin = lambda event: False
    data = payload(run(bot.sticker_stats(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "permission_denied"


def test_stats_and_list_share_command_output(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png", "b.png"], "狗": ["c.png"]})
    add_files(tmp_path, "猫", ["a.png", "b.png"])
    add_files(tmp_path, "狗", ["c.png"])
    bot._is_admin = lambda event: True
    stats = payload(run(bot.sticker_stats(FakeEvent())))
    assert stats["ok"] is True
    assert stats["data"]["total_keywords"] == 2 and stats["data"]["total_images"] == 3
    assert "关键词数：2" in stats["data"]["text"]
    listing = payload(run(bot.sticker_list(FakeEvent(), keyword="猫", page=1)))
    assert listing["ok"] is True
    assert listing["data"]["items"] == ["a.png", "b.png"]
    assert "第 1/1 页，共 2 张" in listing["data"]["text"]


def test_list_page_out_of_range(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    bot._is_admin = lambda event: True
    data = payload(run(bot.sticker_list(FakeEvent(), keyword="猫", page=9)))
    assert data["ok"] is False and data["error"]["code"] == "not_found"


def test_read_tools_do_not_write_index(tmp_path) -> None:
    """只读工具不得像 _prune_missing 那样改写索引。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png", "missing.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    bot._is_admin = lambda event: True
    before = json.dumps(bot.index, sort_keys=True)
    listing = payload(run(bot.sticker_list(FakeEvent(), keyword="猫")))
    assert listing["ok"] is True and listing["data"]["total"] == 1
    assert json.dumps(bot.index, sort_keys=True) == before, "只读工具改了索引"


def test_tools_off_switch(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_tools_enabled": False}, index={"猫": ["a.png"]})
    data = payload(run(bot.sticker_stats(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "disabled"


def test_ai_settings_defaults_and_rejects_bad_numbers(tmp_path) -> None:
    main, bot = make_bot(tmp_path)
    settings = bot._ai_settings()
    assert settings == main.AI_CONFIG_DEFAULTS
    try:
        bot._apply_ai_payload({"sticker_ai_throttle_limit": 999})
    except ValueError as exc:
        assert "1~60" in str(exc)
    else:
        raise AssertionError("越界配置必须报错，不能静默改数")
    updated = bot._apply_ai_payload({"sticker_ai_send_enabled": False, "sticker_ai_throttle_limit": 5})
    assert updated == {"sticker_ai_send_enabled": False, "sticker_ai_throttle_limit": 5}


def test_concurrent_sends_do_not_double_send(tmp_path) -> None:
    """并发（同会话同关键词）只允许一路真正发出去。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png", "b.png"]})
    add_files(tmp_path, "猫", ["a.png", "b.png"])

    class SlowEvent(FakeEvent):
        async def send(self, chain):
            await asyncio.sleep(0.05)
            await super().send(chain)

    event = SlowEvent()

    async def scenario():
        return await asyncio.gather(
            bot.sticker_send(event, keyword="猫"),
            bot.sticker_send(event, keyword="猫"),
        )

    first, second = run(scenario())
    assert len(event.sent) == 1, "并发时重复发图了"
    flags = sorted([payload(first)["data"]["deduped"], payload(second)["data"]["deduped"]])
    assert flags == [False, True]
    for item in (payload(first), payload(second)):
        assert "text" in item["data"], "幂等命中也要带 text 字段"


def test_index_path_traversal_is_rejected(tmp_path) -> None:
    """索引被改坏（../ 越出关键词目录）时不得把目录外的文件发出去。

    注意：目录外文件必须**真实存在**，否则 is_file() 会顺手挡掉，用例就变成了假绿。
    """
    outside = tmp_path / "secret.txt"          # 猫/../secret.txt 正好解析到这里
    outside.write_bytes(b"secret")
    _main, bot = make_bot(tmp_path, index={"猫": ["../secret.txt"]})
    add_files(tmp_path, "猫", ["ok.png"])
    event = FakeEvent()
    data = payload(run(bot.sticker_send(event, keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "not_found", data
    assert event.sent == [], "目录外文件被发出去了"
    assert outside.exists()
def test_list_admin_gate(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    bot._is_admin = lambda event: False
    data = payload(run(bot.sticker_list(FakeEvent(), keyword="猫")))
    assert data["ok"] is False and data["error"]["code"] == "permission_denied"


def test_read_switch_disables_stats_and_list(tmp_path) -> None:
    _main, bot = make_bot(tmp_path, config={"sticker_ai_read_enabled": False}, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    bot._is_admin = lambda event: True
    for result in (bot.sticker_stats(FakeEvent()), bot.sticker_list(FakeEvent(), keyword="猫")):
        assert payload(run(result))["error"]["code"] == "disabled"


def test_dedup_window_default_is_locked() -> None:
    main = import_main()
    assert main.AI_CONFIG_DEFAULTS["sticker_ai_send_dedup_window"] == 10
    low, high = main.AI_INT_BOUNDS["sticker_ai_send_dedup_window"]
    assert low <= 10 <= high


def test_overlong_keyword_is_rejected(tmp_path) -> None:
    """设计 §7.2：关键词 >32 字必须明确拒绝（不是静默截断）。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    data = payload(run(bot.sticker_send(FakeEvent(), keyword="x" * 40)))
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_argument", data
    assert "32" in str(data["error"]["message"])


def test_overlong_keyword_cannot_hit_shorter_keyword(tmp_path) -> None:
    """40 字入参不得命中已存在的 32 字关键词（截断会导致发错图）。"""
    long_key = "x" * 32
    _main, bot = make_bot(tmp_path, index={long_key: ["a.png"]})
    add_files(tmp_path, long_key, ["a.png"])
    event = FakeEvent()
    data = payload(run(bot.sticker_send(event, keyword="x" * 40)))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument"
    assert event.sent == []
def test_human_list_out_of_range_text_unchanged(tmp_path) -> None:
    """重构不得改变人类指令的提示文案（审计 P2-1）。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])
    bot._is_admin = lambda event: True
    event = FakeEvent()

    async def scenario():
        return [item async for item in bot._handle_list(event, "列表猫9")]

    results = run(scenario())
    texts = [str(getattr(item, "text", "")) for item in results]
    assert "页码超出范围" in texts, texts

def test_send_failure_allows_retry(tmp_path) -> None:
    """发送失败要回滚占位：下一次调用必须真的重试（不是 deduped）。"""
    _main, bot = make_bot(tmp_path, index={"猫": ["a.png"]})
    add_files(tmp_path, "猫", ["a.png"])

    class FlakyEvent(FakeEvent):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def send(self, chain):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("platform down")
            await super().send(chain)

    event = FlakyEvent()
    first = payload(run(bot.sticker_send(event, keyword="猫")))
    assert first["ok"] is False and first["error"]["code"] == "capability_unavailable"
    second = payload(run(bot.sticker_send(event, keyword="猫")))
    assert second["ok"] is True and second["data"]["deduped"] is False, second
    assert len(event.sent) == 1
