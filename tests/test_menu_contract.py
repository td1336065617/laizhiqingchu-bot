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

README_ROW = re.compile(r"^\|\s*`([^`]+)`", re.M)


def test_readme_lists_every_command():
    """README 指令表必须**以该指令开头**列出（不能用更长指令的包含关系蒙混）。

    曾落后数版：权重/绑定/管理员指令全缺；而单纯查子串会被
    「权重列表」「权重重置」命中，所以这里比对表格首个单元格。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    cells = [m.group(1).strip() for m in README_ROW.finditer(readme)]

    def listed(prefix: str) -> bool:
        for cell in cells:
            if cell == prefix:
                return True
            if cell.startswith(prefix) and cell[len(prefix):][:1] in ("{", "（", "("):
                return True
        return False

    # 分发器里还有一条 "/" 前缀归一化，不是指令；只核对真正的指令名
    prefixes = {p for p in _dispatch_prefixes() if len(p) > 1 and p.isprintable()}
    missing = sorted(p for p in prefixes if not listed(p))
    assert not missing, "README 指令表缺以下指令（需单独一行）：" + str(missing)
