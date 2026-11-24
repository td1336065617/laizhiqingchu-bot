"""AstrBot 测试桩契约：main.py 从 astrbot.api.web 导入的每个名字，桩里都必须有。

背景：1.8.1 引入 stream_response 时漏了给 conftest 的桩补上，
导致 run_without_astrbot 门禁从 53 passed 掉到 30 failed——正常 pytest 却全绿。
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


def _imported_web_names() -> set:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api.web":
            names.update(alias.name for alias in node.names)
    return names


def test_conftest_stub_covers_every_web_api_used():
    import conftest  # noqa: F401  触发桩安装

    web = importlib.import_module("astrbot.api.web")
    used = _imported_web_names()
    assert used, "应当能解析出 astrbot.api.web 的导入"
    missing = sorted(name for name in used if not hasattr(web, name))
    assert not missing, "桩缺少这些 web API：" + str(missing)