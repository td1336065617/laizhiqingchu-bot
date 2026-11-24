"""菜单契约门禁：每个能触发的指令前缀都必须出现在菜单里。

背景：1.6.0 新增了别名与管理员指令，但 MENU_TEXT / menu.md 都没回填，
用户看菜单根本不知道有新功能（用户发现的）。这条门禁直接比对分发前缀与菜单文本。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
MENU_MD = ROOT / "menu.md"


def _dispatch_prefixes() -> set:
    text = MAIN.read_text(encoding="utf-8")
    body = text.split("async def on_message")[1]
    body = body.split("async def ", 1)[0]
    return set(re.findall(r'startswith\("([^"]+)"\)', body))


def _menu_text() -> str:
    text = MAIN.read_text(encoding="utf-8")
    block = text.split("MENU_TEXT = ", 1)[1]
    block = block.split("\n)\n", 1)[0]
    return block


def test_every_dispatched_command_appears_in_menu_text():
    prefixes = _dispatch_prefixes()
    assert len(prefixes) >= 15, prefixes
    menu = _menu_text()
    missing = sorted(p for p in prefixes if p not in menu)
    assert not missing, "以下指令没写进菜单：" + str(missing)


def test_menu_md_matches_menu_text():
    """仓库里的 menu.md 是同一份菜单的可读副本，不能各写各的。"""
    prefixes = _dispatch_prefixes()
    md = MENU_MD.read_text(encoding="utf-8")
    missing = sorted(p for p in prefixes if p not in md)
    assert not missing, "menu.md 缺以下指令：" + str(missing)


def test_alias_and_admin_commands_documented():
    menu = _menu_text()
    md = MENU_MD.read_text(encoding="utf-8")
    for word in ("绑定", "解绑", "别名", "授予管理员", "撤销管理员", "管理员列表"):
        assert word in menu, "MENU_TEXT 缺：" + word
        assert word in md, "menu.md 缺：" + word