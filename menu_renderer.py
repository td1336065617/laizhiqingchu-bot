"""表情包管理菜单的 HTML/PNG 渲染与文字回退。"""
from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

RENDER_WIDTH = 1200
MIN_RENDER_HEIGHT = 760
MAX_RENDER_HEIGHT = 8000
RENDER_FORMAT_VERSION = 5
MAX_TEXT_CHUNK = 1500
EMOJI_FONT_SIZE = 109
BUNDLED_EMOJI_FONT = (
    Path(__file__).resolve().parent
    / "assets"
    / "fonts"
    / "NotoColorEmoji.ttf"
)

# HTML 渲染器和 Pillow 回退统一使用简体中文字体。Pillow 读取 TTC
# 时必须显式指定 SC face（NotoSansCJK 的 index=2），否则默认会加载
# 日文字库面，菜单中文会出现方框或字形错乱。
MENU_FONT_FAMILY = (
    '"Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", '
    '"WenQuanYi Zen Hei", "Microsoft YaHei", sans-serif'
)
EMOJI_FONT_FAMILY = (
    '"Sticker Bundled Emoji", "Noto Color Emoji", '
    "sans-serif"
)

EMOJI_FALLBACKS = {
    "👥": "◆",
    "🔑": "◆",
    "🌙": "◆",
    "⚙️": "⚙",
    "🌸": "✦",
    "🎴": "▣",
    "📢": "◆",
    "🔗": "↗",
    "📋": "▣",
    "📚": "▣",
}

EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2300, 0x23FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
)


def _text_units(value: str) -> List[str]:
    """按近似字素切分，避免把 emoji 的变体选择符拆开。"""
    units: List[str] = []
    for char in str(value):
        if units and (
            unicodedata.combining(char)
            or "\ufe00" <= char <= "\ufe0f"
            or char == "\u20e3"
            or char == "\u200d"
            or units[-1].endswith("\u200d")
        ):
            units[-1] += char
        else:
            units.append(char)
    return units


def _is_emoji_unit(value: str) -> bool:
    """判断一个 grapheme 单元是否需要交给 Emoji 字体渲染。"""
    text = str(value or "")
    if not text:
        return False
    if "\ufe0f" in text or "\u20e3" in text or "\u200d" in text:
        return True
    return any(
        start <= ord(char) <= end
        for char in text
        for start, end in EMOJI_RANGES
    )


def _html_text(value: object) -> str:
    """转义文字，并把 Emoji 单独交给内置 Emoji 字体。"""
    pieces = []
    for unit in _text_units(str(value or "")):
        escaped = html.escape(unit, quote=True)
        if _is_emoji_unit(unit):
            pieces.append(f'<span class="emoji">{escaped}</span>')
        else:
            pieces.append(escaped)
    return "".join(pieces)


def _tracked_width(draw, value: str, font, tracking: float) -> float:
    units = _text_units(value)
    width = sum(
        draw.textbbox((0, 0), unit, font=font)[2]
        - draw.textbbox((0, 0), unit, font=font)[0]
        for unit in units
    )
    return width + max(0, len(units) - 1) * tracking


def _draw_tracked(draw, xy, value: str, font, fill, tracking: float) -> None:
    cursor = float(xy[0])
    y = xy[1]
    for unit in _text_units(value):
        draw.text((round(cursor), y), unit, font=font, fill=fill)
        box = draw.textbbox((0, 0), unit, font=font)
        cursor += box[2] - box[0] + tracking

_ITEM_RE = re.compile(r"^\s*[•●▪◦*-]\s*")
_DIVIDER_RE = re.compile(r"^\s*[\-_=─—–━]{3,}\s*$")


class StickerMenuRenderer:
    """把原版菜单文字排版渲染成图片；不依赖 Google Chrome。"""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self._lock = threading.Lock()

    @staticmethod
    def _line_kind(line: str, index: int) -> str:
        stripped = line.strip()
        if index == 0:
            return "title"
        if not stripped:
            return "blank"
        if _DIVIDER_RE.fullmatch(stripped):
            return "divider"
        if stripped.startswith(("👥", "🔑", "🌙")):
            return "section"
        if stripped.startswith(("⚙️", "🔗")):
            return "footer"
        if _ITEM_RE.match(line):
            return "item"
        return "normal"

    @classmethod
    def _html_for_text(cls, text: str) -> str:
        lines = str(text or "").splitlines() or [""]
        rendered: List[str] = []
        bundled_emoji_face = ""
        if BUNDLED_EMOJI_FONT.is_file():
            try:
                font_url = BUNDLED_EMOJI_FONT.as_uri()
            except ValueError:
                font_url = ""
            if font_url:
                bundled_emoji_face = f"""
    @font-face {{
      font-family: "Sticker Bundled Emoji";
      src: url("{font_url}") format("truetype");
      font-weight: normal;
      font-style: normal;
      font-display: block;
    }}
"""
        for index, line in enumerate(lines[1:], start=1):
            kind = cls._line_kind(line, index)
            if kind == "blank":
                rendered.append('<div class="blank"></div>')
            else:
                rendered.append(
                    f'<div class="line {kind}">{_html_text(line)}</div>'
                )
        title = _html_text(lines[0])
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>表情包管理菜单</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin:0; padding:0; background:#21162d; }}
    {bundled_emoji_face}
    body {{
      color:#4c315b;
      font-family:{MENU_FONT_FAMILY};
      font-variant-east-asian:simplified;
      text-rendering:optimizeLegibility;
      -webkit-font-smoothing:antialiased;
    }}
    .emoji {{ font-family:{EMOJI_FONT_FAMILY}; font-variant-emoji:emoji; font-weight:normal; letter-spacing:0; }}
    .page {{
      width:{RENDER_WIDTH}px;
      margin:0 auto;
      padding:46px 56px 56px;
      position:relative;
      overflow:hidden;
      background:
        radial-gradient(circle at 94% 8%,rgba(255,177,218,.28),transparent 24%),
        radial-gradient(circle at 6% 88%,rgba(145,223,247,.22),transparent 26%),
        linear-gradient(135deg,#24142f 0%,#382044 52%,#213847 100%);
    }}
    .page:before {{
      content:"";
      position:absolute;
      inset:0;
      opacity:.2;
      pointer-events:none;
      background-image:
        linear-gradient(120deg,transparent 0 46%,rgba(255,255,255,.18) 47%,transparent 48%),
        linear-gradient(60deg,transparent 0 72%,rgba(255,192,226,.12) 73%,transparent 74%);
      background-size:180px 180px,220px 220px;
      mask-image:linear-gradient(to bottom,black,transparent 90%);
    }}
    .page:after {{
      content:"✿";
      position:absolute;
      right:78px;
      top:84px;
      color:rgba(255,225,241,.62);
      font-size:54px;
      transform:rotate(14deg);
      pointer-events:none;
    }}
    .eyebrow {{
      position:relative;
      z-index:1;
      color:#ffd2e9;
      font-size:14px;
      font-weight:800;
      line-height:1.5;
      letter-spacing:3.6px;
      text-shadow:0 0 16px rgba(255,170,216,.45);
    }}
    .title {{
      position:relative;
      z-index:1;
      margin-top:9px;
      color:#fff7fb;
      font-size:38px;
      line-height:1.4;
      font-weight:900;
      letter-spacing:1.6px;
      text-shadow:0 3px 20px rgba(255,137,195,.45);
    }}
    .panel {{
      position:relative;
      z-index:1;
      margin-top:34px;
      padding:29px 32px 34px;
      background:linear-gradient(145deg,rgba(255,252,255,.98),rgba(255,230,244,.93));
      border:1px solid rgba(255,211,235,.92);
      border-radius:20px;
      box-shadow:0 16px 34px rgba(20,8,35,.28),inset 0 0 26px rgba(255,255,255,.65);
    }}
    .panel:before {{
      content:"";
      position:absolute;
      left:24px;
      right:24px;
      top:0;
      height:3px;
      border-radius:99px;
      background:linear-gradient(90deg,#e467a5,#b9eaf8,#e467a5);
    }}
    .line {{
      white-space:pre-wrap;
      overflow-wrap:anywhere;
      font-size:20px;
      line-height:1.72;
      letter-spacing:.35px;
      padding:5px 0;
    }}
    .item {{ color:#c44786; font-weight:800; letter-spacing:.45px; }}
    .section {{ color:#8d5f82; font-weight:900; line-height:1.5; margin-top:16px; letter-spacing:.45px; }}
    .divider {{ color:#bd83a7; font-size:18px; letter-spacing:1.5px; }}
    .footer {{ color:#6c93a8; font-size:16px; line-height:1.7; letter-spacing:.3px; }}
    .normal {{ color:#705276; }}
    .blank {{ height:14px; }}
  </style>
</head>
<body>
  <main class="page">
    <div class="eyebrow">ELYSIAN // PINK PEARL MENU</div>
    <div class="title">{title}</div>
    <section class="panel">{"".join(rendered) or '<div class="line normal"> </div>'}</section>
  </main>
</body>
</html>
"""

    @classmethod
    def _estimate_height(cls, text: str) -> int:
        rows = 3
        for index, line in enumerate(str(text or "").splitlines() or [""]):
            width = sum(
                2 if unicodedata.east_asian_width(char) in "WFA" else 1
                for char in line
            )
            rows += max(1, (width + 52) // 53)
            if cls._line_kind(line, index) in {"section", "footer"}:
                rows += 1
        return max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, 205 + rows * 45))

    @staticmethod
    def _find_renderers() -> List[Tuple[str, str]]:
        configured = (
            os.environ.get("STICKER_MENU_RENDERER")
            or os.environ.get("MENU_NAVIGATION_RENDERER")
        )
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "chromium",
                "chromium-browser",
                "google-chrome",
                "google-chrome-stable",
                "microsoft-edge",
                "msedge",
                "brave-browser",
                "firefox",
                "wkhtmltoimage",
            ]
        )
        renderers: List[Tuple[str, str]] = []
        seen = set()
        for candidate in candidates:
            if not candidate:
                continue
            if candidate.casefold() in {"pillow", "pil"}:
                if ("pillow", "pillow") not in renderers:
                    renderers.append(("pillow", "pillow"))
                continue
            path = shutil.which(candidate)
            if not path or path in seen:
                continue
            seen.add(path)
            name = Path(path).name.casefold()
            if "firefox" in name:
                kind = "firefox"
            elif "wkhtmltoimage" in name:
                kind = "wkhtmltoimage"
            else:
                kind = "chromium"
            renderers.append((kind, path))
        return renderers

    @staticmethod
    def _font_index_from_env(default: int = 0) -> int:
        try:
            return max(
                0,
                int(os.environ.get("STICKER_MENU_FONT_INDEX", default)),
            )
        except (TypeError, ValueError):
            return max(0, default)

    @staticmethod
    def _find_cjk_font_spec(*, bold: bool = False) -> Tuple[Optional[str], int]:
        """返回真正的简体中文字体路径及 TTC face index。"""
        configured = os.environ.get("STICKER_MENU_FONT")
        if configured and Path(configured).is_file():
            default_index = (
                2
                if Path(configured).suffix.casefold() == ".ttc"
                and "NotoSansCJK" in Path(configured).name
                else 0
            )
            return configured, StickerMenuRenderer._font_index_from_env(
                default_index
            )

        fc_match = shutil.which("fc-match")
        if fc_match:
            style = "Bold" if bold else "Regular"
            queries = (
                f"Noto Sans CJK SC:style={style}",
                "Noto Sans CJK SC",
                ":lang=zh-cn",
            )
            for query in queries:
                try:
                    result = subprocess.run(
                        [
                            fc_match,
                            "-f",
                            "%{file}|%{index}",
                            query,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    path_text, _, index_text = (
                        result.stdout.strip().partition("|")
                    )
                    if result.returncode != 0 or not Path(path_text).is_file():
                        continue
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
                except (OSError, subprocess.SubprocessError):
                    break

        if bold:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
            ]
        else:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
                ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
            ]
        for path, index in candidates:
            if Path(path).is_file():
                return path, index
        return None, 0

    @staticmethod
    def _find_cjk_font() -> Optional[str]:
        """兼容旧调用方，只返回字体路径。"""
        path, _ = StickerMenuRenderer._find_cjk_font_spec()
        return path

    @staticmethod
    def _emoji_font_index_from_env(default: int = 0) -> int:
        try:
            return max(
                0,
                int(
                    os.environ.get(
                        "STICKER_MENU_EMOJI_FONT_INDEX", default
                    )
                ),
            )
        except (TypeError, ValueError):
            return max(0, default)

    @staticmethod
    def _find_emoji_font_spec() -> Tuple[Optional[str], int]:
        """优先使用插件内置 Emoji 字体，再尝试系统字体。"""
        configured = os.environ.get("STICKER_MENU_EMOJI_FONT")
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_file():
                return (
                    str(configured_path),
                    StickerMenuRenderer._emoji_font_index_from_env(),
                )

        if BUNDLED_EMOJI_FONT.is_file():
            return str(BUNDLED_EMOJI_FONT), 0

        fc_match = shutil.which("fc-match")
        if fc_match:
            try:
                result = subprocess.run(
                    [
                        fc_match,
                        "-f",
                        "%{file}|%{index}",
                        "Noto Color Emoji",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                path_text, _, index_text = (
                    result.stdout.strip().partition("|")
                )
                if result.returncode == 0 and Path(path_text).is_file():
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
            except (OSError, subprocess.SubprocessError):
                pass

        for path in (
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        ):
            if Path(path).is_file():
                return path, 0
        return None, 0

    @staticmethod
    def _find_emoji_font_path() -> Optional[str]:
        """返回可用于 Pillow 的 Emoji 字体路径。"""
        path, _ = StickerMenuRenderer._find_emoji_font_spec()
        return path

    @staticmethod
    def _load_emoji_font(image_font_module):
        """加载固定像素面的彩色 Emoji 字体。"""
        path, index = StickerMenuRenderer._find_emoji_font_spec()
        if not path:
            return None
        for size in (
            EMOJI_FONT_SIZE,
            128,
            96,
            72,
            64,
            48,
            32,
        ):
            try:
                return image_font_module.truetype(path, size, index=index)
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _run_external_renderer(
        kind: str,
        executable: str,
        html_path: Path,
        image_path: Path,
        height: int,
    ) -> bool:
        if kind == "firefox":
            command = [
                executable,
                "--headless",
                "--no-remote",
                "--screenshot",
                str(image_path),
                "--window-size",
                f"{RENDER_WIDTH},{height}",
                html_path.as_uri(),
            ]
        elif kind == "wkhtmltoimage":
            command = [
                executable,
                "--quiet",
                "--enable-local-file-access",
                "--width",
                str(RENDER_WIDTH),
                "--height",
                str(height),
                html_path.as_uri(),
                str(image_path),
            ]
        else:
            command = [
                executable,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--force-device-scale-factor=1",
                "--allow-file-access-from-files",
                f"--window-size={RENDER_WIDTH},{height}",
                f"--screenshot={image_path}",
                html_path.as_uri(),
            ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            result.returncode == 0
            and image_path.is_file()
            and image_path.stat().st_size > 0
        )

    @classmethod
    def _render_with_pillow(cls, text: str, image_path: Path) -> bool:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw, ImageFont
        except ImportError:
            return False

        regular_spec = cls._find_cjk_font_spec()
        bold_spec = cls._find_cjk_font_spec(bold=True)
        if not regular_spec[0] or not bold_spec[0]:
            return False
        try:
            eyebrow_font = ImageFont.truetype(
                bold_spec[0], 16, index=bold_spec[1]
            )
            title_font = ImageFont.truetype(
                bold_spec[0], 38, index=bold_spec[1]
            )
            body_font = ImageFont.truetype(
                regular_spec[0], 20, index=regular_spec[1]
            )
            divider_font = ImageFont.truetype(
                regular_spec[0], 18, index=regular_spec[1]
            )
            footer_font = ImageFont.truetype(
                regular_spec[0], 16, index=regular_spec[1]
            )
        except (OSError, ValueError):
            return False

        emoji_font = cls._load_emoji_font(ImageFont)
        measure_image = PILImage.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)
        emoji_cache = {}

        def line_height(font) -> int:
            box = measure_draw.textbbox((0, 0), "菜单管理Ag", font=font)
            return max(24, box[3] - box[1] + 9)

        def emoji_target_height(font) -> int:
            font_size = getattr(font, "size", 0)
            if isinstance(font_size, int) and font_size > 0:
                return font_size
            box = measure_draw.textbbox((0, 0), "Ag", font=font)
            return max(1, box[3] - box[1])

        def render_emoji(unit: str, target_height: int):
            """在高分辨率透明画布绘制 Emoji，再缩放到目标字号。"""
            cache_key = (unit, target_height)
            if cache_key in emoji_cache:
                return emoji_cache[cache_key]
            if emoji_font is None:
                emoji_cache[cache_key] = None
                return None
            try:
                bbox = emoji_font.getbbox(unit)
                advance = max(1, int(round(emoji_font.getlength(unit))))
                bbox_width = max(1, bbox[2] - bbox[0], advance)
                bbox_height = max(1, bbox[3] - bbox[1])
                padding = 12
                tile = PILImage.new(
                    "RGBA",
                    (bbox_width + padding * 2, bbox_height + padding * 2),
                    (0, 0, 0, 0),
                )
                tile_draw = ImageDraw.Draw(tile)
                draw_position = (
                    padding - bbox[0],
                    padding - bbox[1],
                )
                try:
                    tile_draw.text(
                        draw_position,
                        unit,
                        font=emoji_font,
                        embedded_color=True,
                    )
                except TypeError:
                    tile_draw.text(
                        draw_position,
                        unit,
                        font=emoji_font,
                    )
                alpha_box = tile.getchannel("A").getbbox()
                if not alpha_box:
                    emoji_cache[cache_key] = None
                    return None
                cropped = tile.crop(alpha_box)
                target_height = max(1, int(target_height))
                target_width = max(
                    1,
                    int(round(cropped.width * target_height / cropped.height)),
                )
                resampling = getattr(PILImage, "Resampling", PILImage)
                resized = cropped.resize(
                    (target_width, target_height),
                    resampling.LANCZOS,
                )
                scaled_advance = max(
                    target_width,
                    int(
                        round(
                            advance
                            * target_height
                            / max(1, bbox_height)
                        )
                    ),
                )
                result = (resized, scaled_advance)
            except (OSError, ValueError, TypeError):
                result = None
            emoji_cache[cache_key] = result
            return result

        def fallback_unit(unit: str) -> str:
            if _is_emoji_unit(unit):
                return EMOJI_FALLBACKS.get(unit, "◆")
            return unit

        def mixed_width(
            value: str,
            font,
            tracking: float,
        ) -> float:
            """按实际中文/Emoji 绘制方式测量一行文字。"""
            widths = []
            target_height = emoji_target_height(font)
            for unit in _text_units(value):
                if _is_emoji_unit(unit):
                    rendered = render_emoji(unit, target_height)
                    if rendered is not None:
                        widths.append(rendered[1])
                        continue
                    unit = fallback_unit(unit)
                box = measure_draw.textbbox((0, 0), unit, font=font)
                widths.append(max(0, box[2] - box[0]))
            return sum(widths) + max(0, len(widths) - 1) * tracking

        def draw_mixed(
            canvas,
            canvas_draw,
            xy,
            value: str,
            font,
            fill,
            tracking: float,
        ) -> None:
            """在同一行中混合绘制普通文字和彩色 Emoji。"""
            cursor = float(xy[0])
            y = xy[1]
            target_height = emoji_target_height(font)
            regular_box = measure_draw.textbbox((0, 0), "Ag", font=font)
            emoji_y = y + regular_box[1]
            for unit in _text_units(value):
                if _is_emoji_unit(unit):
                    rendered = render_emoji(unit, target_height)
                    if rendered is not None:
                        emoji_image, advance = rendered
                        paste_x = round(
                            cursor + max(0, (advance - emoji_image.width) / 2)
                        )
                        canvas.paste(
                            emoji_image,
                            (paste_x, round(emoji_y)),
                            emoji_image,
                        )
                        cursor += advance + tracking
                        continue
                    unit = fallback_unit(unit)
                canvas_draw.text(
                    (round(cursor), y),
                    unit,
                    font=font,
                    fill=fill,
                )
                box = measure_draw.textbbox((0, 0), unit, font=font)
                cursor += box[2] - box[0] + tracking

        def wrap(
            value: str,
            font,
            max_width: int,
            tracking: float,
        ) -> List[str]:
            result: List[str] = []
            for paragraph in value.splitlines() or [""]:
                current = ""
                for unit in _text_units(paragraph):
                    candidate = current + unit
                    if current and mixed_width(
                        candidate, font, tracking
                    ) > max_width:
                        result.append(current)
                        current = unit
                    else:
                        current = candidate
                result.append(current or " ")
            return result

        lines = str(text or "").splitlines() or [""]
        inner_width = RENDER_WIDTH - 56 * 2 - 30 * 2
        body_rows = []
        for index, line in enumerate(lines[1:], start=1):
            kind = cls._line_kind(line, index)
            font = (
                divider_font
                if kind == "divider"
                else footer_font
                if kind == "footer"
                else body_font
            )
            color = {
                "item": "#c44786",
                "section": "#8d5f82",
                "divider": "#bd83a7",
                "footer": "#6c93a8",
            }.get(kind, "#705276")
            tracking = (
                0.5
                if kind in {"item", "section"}
                else 0.3
                if kind == "footer"
                else 0.35
            )
            for wrapped in wrap(line, font, inner_width, tracking):
                body_rows.append((kind, wrapped, font, color, tracking))

        eyebrow_height = line_height(eyebrow_font)
        title_height = line_height(title_font)
        body_height = sum(
            line_height(font) + 7 for _, _, font, _, _ in body_rows
        )
        image_height = max(
            MIN_RENDER_HEIGHT,
            min(
                MAX_RENDER_HEIGHT,
                46
                + eyebrow_height
                + 7
                + title_height
                + 32
                + body_height
                + 64,
            ),
        )
        image = PILImage.new("RGB", (RENDER_WIDTH, image_height), "#2a193b")
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (RENDER_WIDTH - 250, -76, RENDER_WIDTH + 24, 180),
            outline="#f3a9cb",
            width=2,
        )
        draw.ellipse(
            (RENDER_WIDTH - 226, -52, RENDER_WIDTH - 8, 158),
            outline="#b6e7f0",
            width=2,
        )
        draw_mixed(
            image,
            draw,
            (56, 46),
            "ELYSIAN // PINK PEARL MENU",
            eyebrow_font,
            "#ffd5e8",
            0.65,
        )
        title_y = 46 + eyebrow_height + 9
        draw_mixed(
            image,
            draw,
            (56, title_y),
            lines[0],
            title_font,
            "#fff7fb",
            1.0,
        )
        panel_top = title_y + title_height + 22
        panel_bottom = image_height - 34
        draw.rounded_rectangle(
            (56, panel_top, RENDER_WIDTH - 56, panel_bottom),
            radius=20,
            fill="#fff5fb",
            outline="#efc5dc",
            width=1,
        )
        draw.line(
            (86, panel_top + 2, RENDER_WIDTH - 86, panel_top + 2),
            fill="#e467a5",
            width=3,
        )
        y = panel_top + 30
        for kind, value, font, color, tracking in body_rows:
            if kind in {"section", "footer"}:
                y += 9
            draw_mixed(
                image,
                draw,
                (86, y),
                value,
                font,
                color,
                tracking,
            )
            y += line_height(font) + 7
        draw_mixed(
            image,
            draw,
            (56, image_height - 30),
            "原版菜单排版 · 仅展示指令与权限说明",
            footer_font,
            "#e3b9d2",
            0.3,
        )
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0

    def render(self, text: str) -> Optional[Path]:
        """写入 HTML 并转 PNG；失败返回 None。"""
        value = str(text or "").strip()
        if not value:
            return None
        digest = hashlib.sha256(
            f"{RENDER_FORMAT_VERSION}\0{value}".encode("utf-8")
        ).hexdigest()[:24]
        html_path = self.cache_dir / f"menu-{digest}.html"
        image_path = self.cache_dir / f"menu-{digest}.png"
        html_tmp = self.cache_dir / f".menu-{digest}.html.tmp"
        image_tmp = self.cache_dir / f".menu-{digest}.tmp.png"

        with self._lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                if image_path.is_file() and image_path.stat().st_size > 0:
                    return image_path
                html_tmp.write_text(
                    self._html_for_text(value),
                    encoding="utf-8",
                )
                os.replace(html_tmp, html_path)
            except (OSError, UnicodeError):
                return None

            height = self._estimate_height(value)
            try:
                renderers = self._find_renderers()
            except Exception:
                renderers = []
            for kind, executable in renderers:
                try:
                    image_tmp.unlink(missing_ok=True)
                    success = (
                        self._render_with_pillow(value, image_tmp)
                        if kind == "pillow"
                        else self._run_external_renderer(
                            kind,
                            executable,
                            html_path,
                            image_tmp,
                            height,
                        )
                    )
                except Exception:
                    success = False
                if success and image_tmp.is_file() and image_tmp.stat().st_size > 0:
                    try:
                        os.replace(image_tmp, image_path)
                    except OSError:
                        return None
                    return image_path

            try:
                image_tmp.unlink(missing_ok=True)
                success = self._render_with_pillow(value, image_tmp)
            except Exception:
                success = False
            if success and image_tmp.is_file() and image_tmp.stat().st_size > 0:
                try:
                    os.replace(image_tmp, image_path)
                except OSError:
                    return None
                return image_path
        return None

    @staticmethod
    def text_chunks(text: str, max_chunk: int = MAX_TEXT_CHUNK):
        """转图失败时按换行切分，尽量保持原版菜单排版。"""
        value = str(text or "")
        start = 0
        while start < len(value):
            end = min(start + max_chunk, len(value))
            if end < len(value):
                newline = value.rfind("\n", start, end)
                if newline > start + 100:
                    end = newline
            piece = value[start:end].strip()
            if piece:
                yield piece
            start = end
