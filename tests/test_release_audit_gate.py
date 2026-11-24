"""发布前置门禁：新版必须在 CHANGELOG 里写明独立审计结论，否则不许发布。

背景：1.8.0~1.10.1 连续五个版本"自测绿了就发"，其中四次问题都是实现阶段
就能被独立审计指出的。这条门禁把纪律从"靠记性"变成"靠测试"。

规则：
1. 版本 >= AUDIT_REQUIRED_FROM 的每个 CHANGELOG 段，必须有一行
   `> 审计：<子代理名> · <可发布|需先修>`；
2. 当前版本（metadata.yaml 里的 version）结论必须是 可发布；
3. 结论写 需先修 的版本允许存在（记录在案），但当前版本不许。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
METADATA = ROOT / "metadata.yaml"

#: 从哪个版本开始强制要求审计行（更早的版本是历史，不回填）
AUDIT_REQUIRED_FROM = (1, 8, 0)

SECTION_RE = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.M)
AUDIT_RE = re.compile(
    r"审计\s*[：:]\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*[·:：\s]\s*(可发布|需先修)"
)


def _current_version() -> tuple:
    match = re.search(r"^version:\s*(\d+)\.(\d+)\.(\d+)", METADATA.read_text(encoding="utf-8"), re.M)
    assert match, "metadata.yaml 缺 version"
    return tuple(int(x) for x in match.groups())


def _sections() -> list:
    text = CHANGELOG.read_text(encoding="utf-8")
    marks = list(SECTION_RE.finditer(text))
    out = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        version = tuple(int(x) for x in mark.groups())
        out.append((version, text[mark.start():end]))
    return out


def test_new_versions_carry_audit_verdict():
    missing = []
    for version, body in _sections():
        if version < AUDIT_REQUIRED_FROM:
            continue
        if not AUDIT_RE.search(body):
            missing.append(".".join(str(x) for x in version))
    assert not missing, (
        "以下版本没有写独立审计结论（格式：> 审计：<子代理名> · 可发布）："
        + "、".join(missing)
    )


def test_current_version_is_audited_as_releasable():
    current = _current_version()
    body = next((b for v, b in _sections() if v == current), None)
    assert body is not None, f"CHANGELOG 缺少当前版本段：{current}"
    match = AUDIT_RE.search(body)
    assert match, f"当前版本 {current} 缺审计结论行"
    verdict = match.group(2)
    assert verdict == "可发布", (
        f"当前版本 {current} 的审计结论是「{verdict}」，需先修完并重新审计后再发布"
    )