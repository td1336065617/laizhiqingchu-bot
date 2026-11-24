"""页面契约门禁：脚本引用的每个元素 id 都必须在 HTML 里存在。

背景：曾在只加 JS 处理、忘记加对应 HTML 控件的情况下发布——脚本在绑定
`getElementById(...).onclick` 时抛异常，整段 JS 中断，于是管理员列表、备份列表、
配置项全部渲染不出来（表现为「WebUI 看不见数据」，而后端接口完全正常）。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "pages" / "manage" / "index.html"


def test_every_referenced_element_id_exists():
    text = PAGE.read_text(encoding="utf-8")
    used = set(re.findall(r"getElementById\('([^']+)'\)", text))
    defined = set(re.findall(r'id="([^"]+)"', text))
    assert used <= defined, "脚本引用了不存在的元素：" + str(sorted(used - defined))


def test_script_is_syntactically_valid_js():
    """node --check：语法错也会让整段脚本不执行。"""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        return
    text = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert blocks, "页面没有内联脚本"
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    handle.write("\n".join(blocks))
    handle.close()
    result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
    Path(handle.name).unlink(missing_ok=True)
    assert result.returncode == 0, "页面内联 JS 语法错误：" + result.stderr[:400]


def test_new_maintenance_controls_are_wired():
    text = PAGE.read_text(encoding="utf-8")
    for element in ("maxImageInput", "btnScan", "btnDedupe"):
        assert 'id="%s"' % element in text, "缺少控件：" + element
    assert "maintenance/scan" in text
    assert "maintenance/dedupe" in text