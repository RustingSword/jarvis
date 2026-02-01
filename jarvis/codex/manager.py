from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, List, Optional

from jarvis.config import CodexConfig

logger = logging.getLogger(__name__)

# 进度回调函数类型：接收事件字典
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class CodexResult:
    thread_id: Optional[str]
    response_text: str
    events: List[dict[str, Any]]
    media: List[dict[str, Any]]


class CodexError(RuntimeError):
    pass


class CodexTimeoutError(CodexError):
    pass


class CodexProcessError(CodexError):
    pass


class CodexManager:
    def __init__(self, config: CodexConfig) -> None:
        self._config = config

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CodexResult:
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return await self._run_once(
                    prompt, session_id=session_id, progress_callback=progress_callback
                )
            except CodexTimeoutError as exc:
                last_error = exc
                if attempt >= self._config.max_retries:
                    raise
                await asyncio.sleep(self._backoff(attempt))
            except CodexProcessError as exc:
                last_error = exc
                if attempt >= self._config.max_retries:
                    raise
                await asyncio.sleep(self._backoff(attempt))
        raise CodexError("Codex execution failed") from last_error

    async def _run_once(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CodexResult:
        cmd = self._build_command(prompt, session_id)

        logger.info("Running Codex CLI: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=_expand_user(self._config.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 实时读取输出并调用回调
        events: List[dict[str, Any]] = []
        stderr_lines: List[str] = []

        stdout_task = asyncio.create_task(
            self._read_stdout_with_callback(proc.stdout, events, progress_callback)
        )
        stderr_task = asyncio.create_task(self._read_stderr(proc.stderr, stderr_lines))
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, proc.wait()),
                timeout=self._config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise CodexTimeoutError("Codex timed out") from exc

        stderr_text = "\n".join(stderr_lines).strip()
        if stderr_text:
            logger.warning("Codex stderr: %s", stderr_text)

        thread_id = _extract_thread_id(events)
        response_text = _extract_response_text(events)
        media = _extract_media(events, response_text, _expand_user(self._config.workspace_dir))
        if media:
            logger.info("Extracted %d media item(s) from response.", len(media))
            for item in media:
                logger.info(
                    "Media item: type=%s path=%s",
                    item.get("type"),
                    item.get("path") or item.get("file"),
                )
        else:
            logger.info("No media items extracted from response.")
        response_text = _strip_media_markers(response_text)

        if proc.returncode != 0:
            raise CodexProcessError(f"Codex exited with code {proc.returncode}: {stderr_text}")

        return CodexResult(
            thread_id=thread_id, response_text=response_text, events=events, media=media
        )

    async def _read_stdout_with_callback(
        self,
        stdout: asyncio.StreamReader | None,
        events: List[dict[str, Any]],
        progress_callback: ProgressCallback | None,
    ) -> None:
        """实时读取 stdout 并解析 JSONL，调用回调函数"""
        if not stdout:
            return

        buffer = b""
        while True:
            chunk = await stdout.read(8192)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line_str = line.decode(errors="ignore").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                    events.append(event)

                    # 如果有回调函数，调用它
                    if progress_callback:
                        try:
                            await progress_callback(event)
                        except Exception:
                            logger.exception("Error in progress callback")
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON line: %s", line_str)

        tail = buffer.decode(errors="ignore").strip()
        if tail:
            try:
                event = json.loads(tail)
                events.append(event)
                if progress_callback:
                    try:
                        await progress_callback(event)
                    except Exception:
                        logger.exception("Error in progress callback")
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON line: %s", tail)

    async def _read_stderr(
        self,
        stderr: asyncio.StreamReader | None,
        lines: List[str],
    ) -> None:
        """读取 stderr"""
        if not stderr:
            return

        while True:
            line = await stderr.readline()
            if not line:
                break
            lines.append(line.decode(errors="ignore"))

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_seconds * (2**attempt)

    def _build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [
            self._config.exec_path,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if session_id:
            cmd.extend(["resume", session_id])
        cmd.append(prompt)
        return cmd


def _expand_user(path: str) -> str:
    return str(_path(path))


def _path(path: str):
    from pathlib import Path

    return Path(path).expanduser()


def _extract_thread_id(events: Iterable[dict[str, Any]]) -> Optional[str]:
    for event in events:
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                return str(thread_id)
    return None


def _extract_response_text(events: Iterable[dict[str, Any]]) -> str:
    chunks: List[str] = []
    for event in events:
        text = _event_text(event)
        if text:
            chunks.append(text)
    return "".join(chunks).strip()


def _event_text(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type == "item.completed":
        item = event.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                return text
            if isinstance(text, list):
                return "".join(
                    part.get("text", "")
                    for part in text
                    if isinstance(part, dict) and part.get("type") == "output_text"
                )

    if event_type == "response.output_text.delta":
        return _coerce_text(event.get("delta"))

    if event_type == "response.output_text.done":
        return _coerce_text(event.get("text"))

    for key in ("delta", "content", "text", "message"):
        value = event.get(key)
        text = _coerce_text(value)
        if text:
            return text
    return ""


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("text") or value.get("content")
        if isinstance(inner, str):
            return inner
    if isinstance(value, list):
        parts = [item.get("text") for item in value if isinstance(item, dict) and item.get("text")]
        return "".join(parts)
    return ""


_MEDIA_EXT_PHOTO = {"png", "jpg", "jpeg", "gif", "webp"}
_MEDIA_SCHEME = "send_to_user://"
_MEDIA_MARKER_RE = re.compile(r"send_to_user://(?P<path>[^\s`'\"<>]+)", re.IGNORECASE)


def _extract_media(
    events: Iterable[dict[str, Any]],
    response_text: str,
    workspace_dir: str,
) -> List[dict[str, Any]]:
    media: List[dict[str, Any]] = []
    seen: set[str] = set()

    for path in _find_marked_media_paths(response_text, workspace_dir):
        if path in seen:
            continue
        seen.add(path)
        media.append(_media_item_from_path(path))

    for event in events:
        for value in _iter_string_values(event):
            for path in _find_marked_media_paths(value, workspace_dir):
                if path in seen:
                    continue
                seen.add(path)
                media.append(_media_item_from_path(path))

    return media


def _find_marked_media_paths(text: str, workspace_dir: str) -> List[str]:
    if not text:
        return []
    results: List[str] = []
    for match in _MEDIA_MARKER_RE.finditer(text):
        raw = match.group("path").strip().rstrip(").,;")
        resolved = _resolve_media_path(raw, workspace_dir)
        if resolved:
            results.append(resolved)
    return results


def _resolve_media_path(raw: str, workspace_dir: str) -> str | None:
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (Path(workspace_dir) / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        logger.warning("Media path does not exist: %s (raw=%s)", candidate, raw)
        return None
    if candidate.is_dir():
        logger.warning("Media path is a directory, skipping: %s (raw=%s)", candidate, raw)
        return None
    return str(candidate)


def _media_item_from_path(path: str) -> dict[str, Any]:
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in _MEDIA_EXT_PHOTO:
        kind = "photo"
    else:
        kind = "document"
    return {"type": kind, "path": path}


def _iter_string_values(value: Any) -> List[str]:
    results: List[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            results.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return results


def _strip_media_markers(text: str) -> str:
    if not text:
        return text
    cleaned = _MEDIA_MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
