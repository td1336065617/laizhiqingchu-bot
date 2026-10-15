"""合规用例：AI 配置契约（默认值 / 页面 / 落盘）、版本一致、错误形态、无新增依赖。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import ai_common
from conftest import import_main

ROOT = Path(__file__).resolve().parent.parent
AI_KEYS = (
    "sticker_ai_tools_enabled",
    "sticker_ai_read_enabled",
    "sticker_ai_send_enabled",
    "sticker_ai_throttle_limit",
    "sticker_ai_throttle_window",
    "sticker_ai_send_dedup_window",
)


def run(coro):
    return asyncio.run(coro)


def body_of(response) -> dict:
    """兼容两种形态：桩返回 dict；真 astrbot 返回 starlette JSONResponse。"""
    if isinstance(response, dict):
        return response
    body = getattr(response, "body", None)
    if body is None:
        raise AssertionError("无法解析响应: %r" % (response,))
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return json.loads(body)

class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self, default=None):
        return self.payload


def make_bot(tmp_path):
    main = import_main()
    bot = main.StickerPlugin.__new__(main.StickerPlugin)
    bot.config = main._PluginConfig(tmp_path / "config.json")
    bot.data_dir = tmp_path
    bot.index = {}
    bot.blocked_keywords = set()
    bot._ai_throttle = None
    bot._ai_send_log = {}
    return main, bot


def test_config_contract_defaults_page_and_backend() -> None:
    main = import_main()
    page = (ROOT / "pages" / "manage" / "index.html").read_text(encoding="utf-8")
    for key in AI_KEYS:
        assert key in main.AI_CONFIG_DEFAULTS, "默认配置缺 " + key
        assert key in page, "后台页面缺控件 " + key
    assert set(main.AI_CONFIG_DEFAULTS) == set(AI_KEYS)
    assert main.AI_INT_BOUNDS["sticker_ai_throttle_limit"] == (1, 60)


def test_page_payload_is_really_saved(tmp_path, monkeypatch) -> None:
    """页面按 {admin_users, max_storage_mb, ai} 发，后端必须真的把 ai 落盘。"""
    main, bot = make_bot(tmp_path)
    payload = {
        "admin_users": ["1000"],
        "max_storage_mb": 128,
        "ai": {
            "sticker_ai_tools_enabled": False,
            "sticker_ai_read_enabled": True,
            "sticker_ai_send_enabled": False,
            "sticker_ai_throttle_limit": 5,
            "sticker_ai_throttle_window": 120,
            "sticker_ai_send_dedup_window": 0,
        },
    }
    monkeypatch.setattr(main, "request", FakeRequest(payload))
    response = run(bot._web_config_set())
    assert body_of(response).get("status") == "success", response
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    for key, value in payload["ai"].items():
        assert saved.get(key) == value, "配置没落盘: " + key
    assert saved["max_storage_mb"] == 128


def test_config_get_exposes_ai_block(tmp_path, monkeypatch) -> None:
    main, bot = make_bot(tmp_path)
    bot.config["sticker_ai_send_enabled"] = False
    response = run(bot._web_config_get())
    assert body_of(response)["data"]["ai"]["sticker_ai_send_enabled"] is False
    assert body_of(response)["data"]["ai"]["sticker_ai_throttle_limit"] == 3


def test_error_shape() -> None:
    payload = json.loads(ai_common.fail("invalid_argument"))
    assert payload["ok"] is False and payload["error"]["message"]
    assert ai_common.parse_int("abc", 1, 10) is None


def test_version_matches_metadata() -> None:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(.+)$", metadata, re.M)
    assert match, "metadata 缺 version"
    version = match.group(1).strip()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert version in changelog, "CHANGELOG 缺当前版本段：" + version


def test_no_new_third_party_dependency() -> None:
    for name in ("ai_common.py", "ai_impl.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("import ", "from ")):
                module = line.split()[1].split(".")[0]
                assert module in (
                    "annotations", "json", "pathlib", "random", "re", "threading", "time",
                    "collections", "typing", "__future__",
                ), "新增了第三方依赖：" + line


def test_config_payload_failure_rolls_back_memory(tmp_path, monkeypatch) -> None:
    """校验失败时内存配置必须回滚、磁盘不得写（不能留"admin 已改、ai 报错"的半状态）。"""
    import json as _json

    main, bot = make_bot(tmp_path)
    bot.config["admin_users"] = ["1000"]
    before = dict(bot.config)
    payload = {
        "admin_users": ["2000"],
        "max_storage_mb": 64,
        "ai": {"sticker_ai_throttle_limit": 999},   # 越界 → 必须整体失败
    }
    monkeypatch.setattr(main, "request", FakeRequest(payload))
    response = body_of(run(bot._web_config_set()))
    assert response.get("status") == "error", response
    assert dict(bot.config) == before, "失败后内存配置没有回滚"
    assert not (tmp_path / "config.json").exists(), "失败时不该写盘"
    assert _json.dumps(bot.config, sort_keys=True)
