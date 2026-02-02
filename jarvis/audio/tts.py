from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path

from loguru import logger

from jarvis.config import TTSConfig

try:
    import edge_tts
except Exception:  # pragma: no cover - optional dependency guard
    edge_tts = None


class TTSService:
    def __init__(self, config: TTSConfig, output_dir: str) -> None:
        self._config = config
        target_dir = config.output_dir or output_dir
        self._output_dir = Path(target_dir).expanduser()
        self._available = edge_tts is not None
        if self._config.enabled and not self._available:
            logger.warning("edge-tts not available; TTS disabled.")

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and self._available)

    async def synthesize(self, text: str) -> str | None:
        if not self.enabled:
            return None
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        self._ensure_output_dir()
        path = self._build_output_path(cleaned)

        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                communicate = edge_tts.Communicate(
                    cleaned,
                    voice=self._config.voice,
                    rate=self._config.rate,
                    pitch=self._config.pitch,
                )
                await asyncio.wait_for(
                    communicate.save(str(path)),
                    timeout=self._config.timeout_seconds,
                )
                ogg_path = await self._convert_to_ogg(path)
                final_path = ogg_path or str(path)
                logger.info("TTS generated: path={} chars={}", final_path, len(cleaned))
                return final_path
            except Exception as exc:
                last_error = exc
                if attempt < self._config.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                logger.warning("TTS generation failed: {}", exc)
        if last_error:
            logger.debug("TTS last error: {}", last_error)
        return None

    def _ensure_output_dir(self) -> None:
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Failed to create TTS output dir: {}", self._output_dir)

    def _build_output_path(self, text: str) -> Path:
        suffix = _guess_audio_suffix()
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        timestamp = int(time.time())
        filename = f"tts_{timestamp}_{digest}.{suffix}"
        return self._output_dir / filename

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_seconds * (2**attempt)

    async def _convert_to_ogg(self, source: Path) -> str | None:
        target = source.with_suffix(".ogg")
        if await self._convert_with_ffmpeg(source, target):
            return str(target)
        if await self._convert_with_sox(source, target):
            return str(target)
        return None

    async def _convert_with_ffmpeg(self, source: Path, target: Path) -> bool:
        if not shutil.which("ffmpeg"):
            return False
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(target),
        ]
        return await self._run_cmd(cmd, "ffmpeg")

    async def _convert_with_sox(self, source: Path, target: Path) -> bool:
        if not shutil.which("sox"):
            return False
        cmd = [
            "sox",
            str(source),
            "-C",
            "32",
            str(target),
        ]
        return await self._run_cmd(cmd, "sox")

    async def _run_cmd(self, cmd: list[str], label: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("TTS convert timed out: {}", label)
                return False
            if proc.returncode != 0:
                detail = (stderr or b"").decode(errors="ignore").strip()
                logger.warning("TTS convert failed ({}): {}", label, detail)
                return False
            logger.info("TTS converted via {}", label)
            return True
        except Exception as exc:
            logger.warning("TTS convert error ({}): {}", label, exc)
            return False


def _guess_audio_suffix() -> str:
    # edge-tts 7.2.x 默认输出为 mp3
    return "mp3"
