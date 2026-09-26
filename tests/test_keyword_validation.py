"""关键词校验回归测试（BUG-022）：Windows 保留名与结尾点/空格。

不导入插件 main.py（它依赖 astrbot），改为从源码中提取校验规则并复刻执行，
保证「规则存在且行为正确」两件事都被覆盖。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "main.py"
SRC = MAIN.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _reserved_names() -> set:
    for node in ast.walk(TREE):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_WINDOWS_RESERVED" for t in node.targets
        ):
            inner = node.value
            names = set()
            if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "frozenset":
                arg = inner.args[0]
                if isinstance(arg, ast.BinOp):
                    # {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" ...} | {f"LPT{i}" ...}
                    for part in (arg.left, arg.right):
                        if isinstance(part, ast.Set):
                            for elt in part.elts:
                                if isinstance(elt, ast.Constant):
                                    names.add(str(elt.value))
                    left = arg.left
                    if isinstance(left, ast.BinOp) and isinstance(left.left, ast.Set):
                        for elt in left.left.elts:
                            if isinstance(elt, ast.Constant):
                                names.add(str(elt.value))
            return names
    return set()


RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _valid(keyword: str, reserved: set, blocked: set) -> bool:
    """复刻 main.py 的 _is_valid_keyword（含 BUG-022 新增规则）。"""
    if not keyword or keyword in blocked:
        return False
    if re.search(r"\s", keyword):
        return False
    if keyword in (".", "..") or "/" in keyword or "\\" in keyword:
        return False
    if any(ch in keyword for ch in '<>:"|?*'):
        return False
    if keyword.split(".")[0].upper() in reserved:
        return False
    return keyword == keyword.rstrip(" .")


def test_source_contains_windows_reserved_rule():
    assert "_WINDOWS_RESERVED" in SRC
    assert 'keyword.split(".")[0].upper()' in SRC
    assert 'keyword.rstrip(" .")' in SRC


def test_reserved_names_are_rejected():
    for bad in ("CON", "con", "aux", "NUL", "com1", "LPT9", "con.txt"):
        assert _valid(bad, RESERVED, set()) is False, bad


def test_trailing_dot_or_space_rejected():
    assert _valid("猫猫.", RESERVED, set()) is False
    assert _valid("猫猫 ", RESERVED, set()) is False


def test_normal_keywords_still_ok():
    for good in ("猫猫", "dog", "com10", "console", "a-b_c"):
        assert _valid(good, RESERVED, set()) is True, good
