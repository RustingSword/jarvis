from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from jarvis.config import CodexConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CodexResult:
    thread_id: Optional[str]
    response_text: str
    events: List[dict[str, Any]]


class CodexError(RuntimeError):
    pass


class CodexTimeoutError(CodexError):
    pass


class CodexProcessError(CodexError):
    pass


class CodexManager:
    def __init__(self, config: CodexConfig) -> None:
        self._config = config

    async def run(self, prompt: str, session_id: str | None = None) -> CodexResult:
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return await self._run_once(prompt, session_id=session_id)
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

    async def _run_once(self, prompt: str, session_id: str | None = None) -> CodexResult:
        cmd = [self._config.exec_path, "exec", "--json"]
        if session_id:
            cmd.extend(["resume", session_id])
        cmd.append(prompt)

        logger.info("Running Codex CLI: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=_expand_user(self._config.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CodexTimeoutError("Codex timed out") from exc

        stderr_text = stderr.decode(errors="ignore").strip() if stderr else ""
        if stderr_text:
            logger.warning("Codex stderr: %s", stderr_text)

        events = _parse_jsonl(stdout.decode(errors="ignore"))
        thread_id = _extract_thread_id(events)
        response_text = _extract_response_text(events)

        if proc.returncode != 0:
            raise CodexProcessError(f"Codex exited with code {proc.returncode}: {stderr_text}")

        return CodexResult(thread_id=thread_id, response_text=response_text, events=events)

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_seconds * (2**attempt)


def _expand_user(path: str) -> str:
    return str(_path(path))


def _path(path: str):
    from pathlib import Path

    return Path(path).expanduser()


def _parse_jsonl(payload: str) -> List[dict[str, Any]]:
    events: List[dict[str, Any]] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON line: %s", line)
    return events


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
