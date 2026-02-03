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
