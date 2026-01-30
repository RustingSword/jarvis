from __future__ import annotations


def as_blockquote(text: str) -> str:
    lines = text.splitlines() or [text]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def format_code_block(label: str, content: str) -> str:
    return f"{label}\n```\n{content}\n```"


def format_tool_path(label: str, value: str) -> str:
    return f"{label}\n{value}"


def normalize_verbosity(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    aliases = {
        "full": "full",
        "verbose": "full",
        "normal": "full",
        "detail": "full",
        "详细": "full",
        "完整": "full",
        "compact": "compact",
        "minimal": "compact",
        "lite": "compact",
        "quiet": "compact",
        "精简": "compact",
        "简洁": "compact",
        "简短": "compact",
        "安静": "compact",
    }
    return aliases.get(raw)
