"""隔离用例：工具定义在入口模块、零跨插件 import、公共件不依赖 astrbot。"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_zero_cross_plugin_imports() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_no_cross_plugin_imports.py"), str(ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pure_modules_have_no_astrbot_import() -> None:
    for name in ("ai_common.py", "ai_impl.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("astrbot") for alias in node.names), name
            elif isinstance(node, ast.ImportFrom):
                assert not str(node.module or "").startswith("astrbot"), name


def test_tools_live_in_entry_module() -> None:
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    for tool in ("sticker_send", "sticker_stats", "sticker_list"):
        assert "llm_tool(name=" in text, "入口模块里没有 llm_tool"
        assert tool in text, tool
    assert "MessageChain" in text, "发表情包需要消息链"


def test_stub_never_shadows_real_astrbot() -> None:
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "find_spec" in text, "桩必须有真 astrbot 存在性守卫"


def test_gate_accepts_renamed_checkout(tmp_path) -> None:
    """门禁必须容忍目录名与插件名不同（CI 会把仓库克隆成 acmerQQ-group-bot 之类）。"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    copy = tmp_path / "renamed-checkout"
    (copy / "tools").mkdir(parents=True)
    shutil.copyfile(repo / "tools" / "check_no_cross_plugin_imports.py", copy / "tools" / "check_no_cross_plugin_imports.py")
    shutil.copyfile(repo / "metadata.yaml", copy / "metadata.yaml")
    sample = copy / "sample.py"
    meta = (repo / "metadata.yaml").read_text(encoding="utf-8")
    plugin_name = next(
        line.split(":", 1)[1].strip() for line in meta.splitlines() if line.startswith("name:")
    )
    text = chr(34) * 3 + "自身数据目录：data/plugin_data/" + plugin_name + "/x.db" + chr(34) * 3 + chr(10)
    sample.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(copy / "tools" / "check_no_cross_plugin_imports.py"), "."],
        cwd=str(copy),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
