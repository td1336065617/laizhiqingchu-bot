"""表情包管理菜单渲染回归测试。"""
from __future__ import annotations

from menu_renderer import StickerMenuRenderer


ORIGINAL_MENU_TEXT = "\n".join(
    (
        "🎴 表情包管理菜单",
        "━━━━━━━━━━━━",
        "👥 所有人可用",
        "• 添加{关键词} ─ 回复图片批量入库",
        "• 批量添加合并转发{关键词} ─ 先发命令，90秒内本人发合并转发自动批量入库",
        "• 来只{关键词} ─ 随机发送一张该关键词的图片",
        "• 表情包管理菜单 ─ 显示本菜单",
        "━━━━━━━━━━━━",
        "🔑 仅管理员",
        "• 屏蔽{关键词} ─ 屏蔽关键词（禁止添加/发送/查看）",
        "• 屏蔽列表 ─ 查看屏蔽关键词",
        "• 解除屏蔽{关键词} ─ 解除屏蔽",
        "• 列表{关键词}{页码} ─ 分页查看（每页10张）",
        "• 删图 ─ 回复图片，按 MD5 自动定位并删除",
        "• 删图{关键词} ─ 仅在该关键词下按 MD5 删除",
        "• 删图{关键词}{序号} ─ 按序号删除单张",
        "• 删除{关键词} ─ 二次确认后删除整个关键词",
        "• 统计 ─ 关键词数 / 图片总数 / Top3",
        "━━━━━━━━━━━━",
        "⚙️ 备份/恢复/管理员设置：WebUI 表情包管理页",
        "🔗 开源：https://github.com/td1336065617/laizhiqingchu-bot",
    )
)


def test_original_menu_text_layout_is_preserved():
    lines = ORIGINAL_MENU_TEXT.splitlines()
    assert lines[0] == "🎴 表情包管理菜单"
    assert lines[1] == "━━━━━━━━━━━━"
    assert lines[8] == "🔑 仅管理员"
    assert lines[-2].startswith("⚙️ 备份")

    chunks = list(StickerMenuRenderer.text_chunks(ORIGINAL_MENU_TEXT))
    assert chunks == [ORIGINAL_MENU_TEXT]


def test_menu_html_escapes_text_and_keeps_original_dividers():
    html = StickerMenuRenderer._html_for_text(ORIGINAL_MENU_TEXT)
    assert "表情包管理菜单" in html
    assert "━━━━━━━━━━━━" in html
    assert "ELYSIAN // PINK PEARL MENU" in html
    assert '<script>' not in html


def test_menu_renderer_uses_pillow_and_reuses_cache(tmp_path, monkeypatch):
    renderer = StickerMenuRenderer(tmp_path)
    monkeypatch.setenv("STICKER_MENU_RENDERER", "pillow")

    image_path = renderer.render(ORIGINAL_MENU_TEXT)

    assert image_path is not None
    assert image_path.is_file()
    assert image_path.stat().st_size > 0
    assert renderer.render(ORIGINAL_MENU_TEXT) == image_path


def test_menu_renderer_can_fall_back_when_external_renderer_fails(
    tmp_path, monkeypatch
):
    renderer = StickerMenuRenderer(tmp_path)
    monkeypatch.setattr(
        StickerMenuRenderer,
        "_find_renderers",
        staticmethod(lambda: [("chromium", "missing-browser")]),
    )
    monkeypatch.setattr(
        StickerMenuRenderer,
        "_run_external_renderer",
        staticmethod(lambda *args, **kwargs: False),
    )

    image_path = renderer.render(ORIGINAL_MENU_TEXT)

    assert image_path is not None
    assert image_path.is_file()
    assert image_path.stat().st_size > 0
