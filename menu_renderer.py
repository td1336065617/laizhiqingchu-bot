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
RENDER_FORMAT_VERSION = 3
MAX_TEXT_CHUNK = 1500

# HTML 渲染器和 Pillow 回退统一使用简体中文字体。Pillow 读取 TTC
# 时必须显式指定 SC face（NotoSansCJK 的 index=2），否则默认会加载
# 日文字库面，菜单中文会出现方框或字形错乱。
MENU_FONT_FAMILY = (
    '"Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", '
    '"WenQuanYi Zen Hei", "Microsoft YaHei", "Noto Color Emoji", '
    "sans-serif"
)

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
        for index, line in enumerate(lines[1:], start=1):
            kind = cls._line_kind(line, index)
            if kind == "blank":
                rendered.append('<div class="blank"></div>')
            else:
                rendered.append(
                    f'<div class="line {kind}">{html.escape(line)}</div>'
                )
        title = html.escape(lines[0])
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>表情包管理菜单</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin:0; padding:0; background:#21162d; }}
    body {{
      color:#4c315b;
      font-family:{MENU_FONT_FAMILY};
      font-variant-east-asian:simplified;
      text-rendering:optimizeLegibility;
      -webkit-font-smoothing:antialiased;
    }}
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
      letter-spacing:3px;
      text-shadow:0 0 16px rgba(255,170,216,.45);
    }}
    .title {{
      position:relative;
      z-index:1;
      margin-top:7px;
      color:#fff7fb;
      font-size:38px;
      font-weight:900;
      letter-spacing:1px;
      text-shadow:0 3px 20px rgba(255,137,195,.45);
    }}
    .panel {{
      position:relative;
      z-index:1;
      margin-top:28px;
      padding:24px 30px 28px;
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
      line-height:1.55;
      padding:3px 0;
    }}
    .item {{ color:#c44786; font-weight:800; }}
    .section {{ color:#8d5f82; font-weight:900; margin-top:8px; }}
    .divider {{ color:#bd83a7; font-size:18px; letter-spacing:1px; }}
    .footer {{ color:#6c93a8; font-size:16px; }}
    .normal {{ color:#705276; }}
    .blank {{ height:8px; }}
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
        return max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, 190 + rows * 36))

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

        measure_image = PILImage.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)

        def line_height(font) -> int:
            box = measure_draw.textbbox((0, 0), "菜单管理Ag", font=font)
            return max(24, box[3] - box[1] + 9)

        def wrap(value: str, font, max_width: int) -> List[str]:
            result: List[str] = []
            for paragraph in value.splitlines() or [""]:
                current = ""
                for char in paragraph:
                    candidate = current + char
                    if current and measure_draw.textbbox(
                        (0, 0), candidate, font=font
                    )[2] > max_width:
                        result.append(current)
                        current = char
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
            for wrapped in wrap(line, font, inner_width):
                body_rows.append((kind, wrapped, font, color))

        eyebrow_height = line_height(eyebrow_font)
        title_height = line_height(title_font)
        body_height = sum(line_height(font) + 4 for _, _, font, _ in body_rows)
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
        draw.text(
            (56, 46),
            "ELYSIAN // PINK PEARL MENU",
            font=eyebrow_font,
            fill="#ffd5e8",
        )
        title_y = 46 + eyebrow_height + 7
        draw.text((56, title_y), lines[0], font=title_font, fill="#fff7fb")
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
        y = panel_top + 26
        for kind, value, font, color in body_rows:
            if kind in {"section", "footer"}:
                y += 6
            draw.text((86, y), value, font=font, fill=color)
            y += line_height(font) + 4
        draw.text(
            (56, image_height - 28),
            "原版菜单排版 · 仅展示指令与权限说明",
            font=footer_font,
            fill="#e3b9d2",
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
