"""用量面板：页面必须展示「图库 / 上限」的关系，并提示恢复不受上限约束。"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "pages" / "manage" / "index.html"


def test_page_has_usage_panel_elements():
    text = PAGE.read_text(encoding="utf-8")
    for element in ("usageFill", "usageText", "usageHint"):
        assert 'id="%s"' % element in text, "缺元素：" + element
    assert "function renderUsage" in text


def test_page_explains_restore_is_not_limited():
    """已知限制必须在用户操作的地方看得见，而不是只在 CHANGELOG 里。"""
    text = PAGE.read_text(encoding="utf-8")
    assert "恢复" in text and "不受存储上限约束" in text


def test_usage_states_cover_normal_near_and_over():
    text = PAGE.read_text(encoding="utf-8")
    body = text.split("function renderUsage", 1)[1].split("async function", 1)[0]
    assert "used > limit" in body, "超限判定必须与后端 _enforce_storage 一致（严格大于）"
    assert "pct >= 80" in body, "缺接近上限分支"
    assert "新增图片会被拒绝" in body


def test_usage_reads_backend_storage_field():
    text = PAGE.read_text(encoding="utf-8")
    assert "r.storage" in text, "必须读 GET config 返回的 storage 字段"
    main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert '"storage": self._storage_usage()' in main