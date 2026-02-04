from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable

from loguru import logger
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DEFAULT_FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
]


def render_digest_pdf(
    text: str,
    output_dir: Path,
    *,
    title: str | None = None,
    backend: str = "pandoc",
    template_path: Path | None = None,
    pdf_engine: str = "lualatex",
    timeout_seconds: int = 30,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"rss-digest-{timestamp}.pdf"
    path = output_dir / filename

    if backend == "pandoc":
        try:
            return _render_with_pandoc(
                text,
                path,
                title=title,
                template_path=template_path,
                pdf_engine=pdf_engine,
                timeout_seconds=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            retry_timeout = max(timeout_seconds, 120)
            logger.warning(
                "Pandoc PDF render timed out after {}s; retrying with {}s.",
                timeout_seconds,
                retry_timeout,
            )
            try:
                return _render_with_pandoc(
                    text,
                    path,
                    title=title,
                    template_path=template_path,
                    pdf_engine=pdf_engine,
                    timeout_seconds=retry_timeout,
                )
            except Exception as exc:
                logger.warning("Pandoc PDF retry failed, fallback to reportlab: {}", exc)
        except Exception as exc:
            logger.warning("Pandoc PDF render failed, fallback to reportlab: {}", exc)

    return _render_with_reportlab(text, path, title=title)


def _render_with_pandoc(
    text: str,
    path: Path,
    *,
    title: str | None,
    template_path: Path | None,
    pdf_engine: str,
    timeout_seconds: int,
) -> Path:
    md_path = path.with_suffix(".md")
    content = text
    if title:
        content = f"# {title}\n\n{content}"
    md_path.write_text(content, encoding="utf-8")

    if template_path:
        try:
            template_text = template_path.read_text(encoding="utf-8")
            updated = _inject_unicode_mappings(content, template_text)
            if updated != template_text:
                template_path.write_text(updated, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to inject unicode mappings into template: {}", exc)

    cmd = [
        "pandoc",
        str(md_path),
        "-o",
        str(path),
        f"--pdf-engine={pdf_engine}",
    ]
    if template_path:
        cmd.append(f"--template={template_path}")
    subprocess.run(cmd, check=True, timeout=timeout_seconds)
    return path


def _render_with_reportlab(text: str, path: Path, *, title: str | None) -> Path:
    font_name = _select_font()
    page_width, page_height = A4
    margin_x = 48
    margin_y = 54
    line_height = 16
    title_height = 24

    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title or "RSS 摘要")
    pdf.setAuthor("Jarvis RSS")

    y = page_height - margin_y
    if title:
        pdf.setFont(font_name, 16)
        for line in _wrap_text(title, page_width - 2 * margin_x, font_name, 16):
            pdf.drawString(margin_x, y, line)
            y -= title_height
        y -= 8
    else:
        pdf.setFont(font_name, 12)

    pdf.setFont(font_name, 12)
    clean_text = _strip_markdown(text)
    for paragraph in clean_text.splitlines():
        if not paragraph.strip():
            y -= line_height
            if y < margin_y:
                pdf.showPage()
                pdf.setFont(font_name, 12)
                y = page_height - margin_y
            continue
        for line in _wrap_text(paragraph, page_width - 2 * margin_x, font_name, 12):
            pdf.drawString(margin_x, y, line)
            y -= line_height
            if y < margin_y:
                pdf.showPage()
                pdf.setFont(font_name, 12)
                y = page_height - margin_y

    pdf.save()
    return path


def _select_font() -> str:
    font_name = "Helvetica"
    for path in DEFAULT_FONT_PATHS:
        if Path(path).exists():
            try:
                font_name = "JarvisCJK"
                pdfmetrics.registerFont(TTFont(font_name, path))
                return font_name
            except Exception as exc:
                logger.warning("Failed to register font {}: {}", path, exc)
                continue
    try:
        cid_font = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(cid_font))
        return cid_font
    except Exception as exc:
        logger.warning("Failed to register CID font: {}", exc)
    return font_name


def _wrap_text(text: str, max_width: float, font_name: str, font_size: int) -> Iterable[str]:
    if not text:
        return []
    words = list(text)
    lines: list[str] = []
    current: list[str] = []
    for ch in words:
        candidate = "".join(current) + ch
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current.append(ch)
        else:
            if current:
                lines.append("".join(current))
            current = [ch]
    if current:
        lines.append("".join(current))
    return lines


def _strip_markdown(text: str) -> str:
    if not text:
        return ""
    stripped = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    stripped = re.sub(r"^#+\s*", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", stripped)
    stripped = re.sub(r"^\s*-\s+", "• ", stripped, flags=re.MULTILINE)
    return stripped


def _inject_unicode_mappings(text: str, template: str) -> str:
    if not text:
        return template

    emoji_ranges = [
        (0x1F1E6, 0x1F1FF),
        (0x1F300, 0x1F5FF),
        (0x1F600, 0x1F64F),
        (0x1F680, 0x1F6FF),
        (0x1F700, 0x1F77F),
        (0x1F780, 0x1F7FF),
        (0x1F800, 0x1F8FF),
        (0x1F900, 0x1F9FF),
        (0x1FA00, 0x1FA6F),
        (0x1FA70, 0x1FAFF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
        (0x2300, 0x23FF),
    ]
    symbol_ranges = [
        (0x2B00, 0x2BFF),
    ]

    def _in_ranges(cp: int, ranges: list[tuple[int, int]]) -> bool:
        return any(lo <= cp <= hi for lo, hi in ranges)

    emojis: set[str] = set()
    symbols: set[str] = set()
    for ch in text:
        if ch == "\ufe0f":  # variation selector-16 handled by font
            continue
        cp = ord(ch)
        if _in_ranges(cp, symbol_ranges):
            symbols.add(ch)
        elif _in_ranges(cp, emoji_ranges):
            emojis.add(ch)

    if not emojis and not symbols:
        return template

    existing = set(re.findall(r"\\newunicodechar\{(.+?)\}", template))
    emoji_lines = [
        f"\\newunicodechar{{{ch}}}{{{{\\emojifont {ch}}}}}"
        for ch in sorted(emojis, key=ord)
        if ch not in existing
    ]
    symbol_lines = [
        f"\\newunicodechar{{{ch}}}{{{{\\symbolfont {ch}}}}}"
        for ch in sorted(symbols, key=ord)
        if ch not in existing
    ]

    if not emoji_lines and not symbol_lines:
        return template

    emojifont_marker = r"\newfontfamily\emojifont{Noto Color Emoji}[Renderer=HarfBuzz]"
    symbolfont_marker = (
        r"\newfontfamily\symbolfont{NotoSansSymbols2-Regular.ttf}["
        r"Path=/usr/share/fonts/truetype/noto/]"
    )

    injected = template
    if symbol_lines and "\\newfontfamily\\symbolfont" not in injected:
        if emojifont_marker in injected:
            injected = injected.replace(
                emojifont_marker, emojifont_marker + "\n" + symbolfont_marker, 1
            )
        else:
            injected = injected + "\n" + symbolfont_marker + "\n"

    if emoji_lines:
        if emojifont_marker in injected:
            injected = injected.replace(
                emojifont_marker, emojifont_marker + "\n" + "\n".join(emoji_lines), 1
            )
        else:
            injected = injected + "\n" + "\n".join(emoji_lines) + "\n"

    if symbol_lines:
        if symbolfont_marker in injected:
            injected = injected.replace(
                symbolfont_marker, symbolfont_marker + "\n" + "\n".join(symbol_lines), 1
            )
        else:
            injected = injected + "\n" + "\n".join(symbol_lines) + "\n"

    return injected
