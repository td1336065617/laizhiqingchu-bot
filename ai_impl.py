"""表情包插件的纯逻辑（不 import astrbot，可独立单测）。

这里的函数与「来只{关键词}」/「列表」/「统计」指令**同源**：
指令与 AI 工具都调用它们，避免两套取数逻辑漂移。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PAGE_SIZE = 10

#: 关键词最大长度（设计 §3.5/§7.2：≤32）
KEYWORD_MAX_LEN = 32


def keyword_or_empty(value: Any) -> str:
    """工具入参归一：去空白、限长、去掉首尾的 {}（LLM 常写成「来只{猫}」）。"""
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    # 这里**不截断**：截断会让 40 字入参悄悄变成另一个 32 字关键词（曾把别人的图发出去）
    # 长度校验由调用方显式拒绝（见 KEYWORD_MAX_LEN 与 main.py 的工具入口）
    return text


def block_reason(keyword: str, blocked: Iterable[str]) -> str:
    """关键词被屏蔽时返回原因，否则返回空串（ST-01 要求先查屏蔽集合）。"""
    if not keyword:
        return ""
    if keyword in {str(item) for item in (blocked or [])}:
        return "关键词 %s 已被屏蔽" % keyword
    return ""


def existing_files(files: Iterable[str], exists: Callable[[str], bool]) -> list:
    """只读过滤：索引里存在但文件已丢的条目**只过滤不写回**。

    人类指令走的是 `_prune_missing`（会顺手把索引改干净）；AI 只读工具不能写盘。
    """
    kept: list = []
    for name in files or []:
        text = str(name or "")
        # 索引是本地文件、可能被手工改坏：只接受裸文件名，挡掉 ../ 之类的越目录条目
        if not text or Path(text).name != text:
            continue
        try:
            if exists(text):
                kept.append(text)
        except Exception:
            continue
    return kept


def pick_sticker(files: Sequence[str], chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    """从候选里随机挑一张（chooser 可注入，便于测试）。"""
    items = [str(item) for item in (files or []) if str(item)]
    if not items:
        return ""
    if chooser is not None:
        try:
            return str(chooser(items))
        except Exception:
            return ""
    return str(random.choice(items))


def list_lines(files: Sequence[str], page: int = 1, page_size: int = PAGE_SIZE) -> dict:
    """分页文案（与「列表{关键词}{页码}」同源）。页码越界返回 ok=False。"""
    items = [str(item) for item in (files or [])]
    size = max(1, int(page_size or PAGE_SIZE))
    total = len(items)
    if total == 0:
        return {"ok": False, "reason": "该关键词下没有图片"}
    total_pages = (total + size - 1) // size
    current = int(page or 1)
    if current < 1 or current > total_pages:
        # 人类指令的原提示保持逐字不变；工具侧另外用 total_pages 给更细的信息
        return {"ok": False, "reason": "页码超出范围", "total_pages": total_pages}
    start = (current - 1) * size
    page_items = items[start : start + size]
    lines: list = []
    for offset, name in enumerate(page_items, start=start + 1):
        lines.append("[%d] %s" % (offset, name))
    lines.append("第 %d/%d 页，共 %d 张" % (current, total_pages, total))
    return {
        "ok": True,
        "page": current,
        "total_pages": total_pages,
        "total": total,
        "items": page_items,
        "lines": lines,
    }


def stats_lines(index: Mapping[str, Sequence[Any]], top: int = 3) -> dict:
    """统计文案（与「统计」指令同源）：关键词数 / 图片总数 / Top N。"""
    data = {str(key): list(value or []) for key, value in (index or {}).items()}
    if not data:
        return {"ok": False, "reason": "暂无任何表情包数据"}
    total_keywords = len(data)
    total_images = sum(len(value) for value in data.values())
    ranked = sorted(data.items(), key=lambda item: len(item[1]), reverse=True)[: max(1, int(top))]
    lines = [
        "关键词数：%d" % total_keywords,
        "图片总数：%d" % total_images,
        "图片数 Top %d：" % max(1, int(top)),
    ]
    for keyword, files in ranked:
        lines.append("%s: %d 张" % (keyword, len(files)))
    return {
        "ok": True,
        "total_keywords": total_keywords,
        "total_images": total_images,
        "top": [(keyword, len(files)) for keyword, files in ranked],
        "lines": lines,
    }


def keyword_hint(index: Mapping[str, Sequence[Any]], text: str, limit: int = 5) -> list:
    """按关键词给出可用候选（给 AI 提示"是不是想找这几个"）。"""
    wanted = str(text or "").strip().casefold()
    names = [str(key) for key in (index or {})]
    if wanted:
        exact = [name for name in names if name.casefold() == wanted]
        if exact:
            return exact[: max(1, int(limit))]
        partial = [name for name in names if wanted in name.casefold()]
        if partial:
            return partial[: max(1, int(limit))]
    return names[: max(1, int(limit))]
