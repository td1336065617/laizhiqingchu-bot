"""取数重构用例：指令与 AI 工具共用 ai_impl（纯逻辑，不需要 astrbot）。"""

from __future__ import annotations

import ai_impl


def test_keyword_or_empty_strips_braces() -> None:
    assert ai_impl.keyword_or_empty("  {猫}  ") == "猫"
    assert ai_impl.keyword_or_empty(None) == ""
    assert ai_impl.keyword_or_empty("x" * 200) == "x" * 200   # 不截断，长度校验在工具入口


def test_block_reason() -> None:
    assert ai_impl.block_reason("猫", {"猫", "狗"}) == "关键词 猫 已被屏蔽"
    assert ai_impl.block_reason("猫", set()) == ""
    assert ai_impl.block_reason("", {"猫"}) == ""


def test_existing_files_filters_without_writing() -> None:
    keep = {"a.png"}
    assert ai_impl.existing_files(["a.png", "b.png"], lambda name: name in keep) == ["a.png"]
    assert ai_impl.existing_files(["a.png"], lambda name: (_ for _ in ()).throw(RuntimeError())) == []


def test_pick_sticker_is_injectable() -> None:
    assert ai_impl.pick_sticker(["a.png", "b.png"], lambda items: items[-1]) == "b.png"
    assert ai_impl.pick_sticker([]) == ""
    assert ai_impl.pick_sticker(["a.png"], lambda items: 1 / 0) == ""


def test_list_lines_matches_command_output() -> None:
    files = ["%02d.png" % index for index in range(1, 26)]
    first = ai_impl.list_lines(files, 1, 10)
    assert first["ok"] is True and first["total_pages"] == 3 and first["total"] == 25
    assert first["lines"][0] == "[1] 01.png"
    assert first["lines"][-1] == "第 1/3 页，共 25 张"
    last = ai_impl.list_lines(files, 3, 10)
    assert last["items"] == ["21.png", "22.png", "23.png", "24.png", "25.png"]
    assert ai_impl.list_lines(files, 9, 10)["ok"] is False
    assert ai_impl.list_lines([], 1, 10)["ok"] is False


def test_stats_lines_matches_command_output() -> None:
    result = ai_impl.stats_lines({"猫": [1, 2, 3], "狗": [4], "鱼": [5, 6]}, top=2)
    assert result["total_keywords"] == 3 and result["total_images"] == 6
    assert result["top"] == [("猫", 3), ("鱼", 2)]
    assert result["lines"][0] == "关键词数：3"
    assert result["lines"][-1] == "鱼: 2 张"
    assert ai_impl.stats_lines({})["ok"] is False


def test_keyword_hint_prefers_exact_then_partial() -> None:
    index = {"猫猫": [], "猫咪": [], "狗狗": []}
    assert ai_impl.keyword_hint(index, "猫咪") == ["猫咪"]
    assert ai_impl.keyword_hint(index, "猫") == ["猫猫", "猫咪"]
    assert ai_impl.keyword_hint(index, "") == ["猫猫", "猫咪", "狗狗"]
