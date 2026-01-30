from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
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
                return await self._run_once(prompt, session_id=session_id, progress_callback=progress_callback)
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

        if proc.returncode != 0:
            raise CodexProcessError(f"Codex exited with code {proc.returncode}: {stderr_text}")

        return CodexResult(thread_id=thread_id, response_text=response_text, events=events)

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
        cmd = [self._config.exec_path, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox"]
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
