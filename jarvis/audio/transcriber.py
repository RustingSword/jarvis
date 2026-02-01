from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

import aiohttp

from jarvis.config import OpenAIAudioConfig

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
SUPPORTED_AUDIO_TYPES = {"audio", "voice"}
MAX_FILE_BYTES = 25 * 1024 * 1024


class TranscriptionService:
    def __init__(self, config: OpenAIAudioConfig, api_key: str | None, base_url: str | None) -> None:
        self._config = config
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        self._transcriptions_url = f"{self._base_url}/v1/audio/transcriptions"

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and self._api_key)

    async def process(self, text: str, attachments: list[dict]) -> tuple[str, list[dict]]:
        audio_items = [item for item in attachments if (item.get("type") or "").lower() in SUPPORTED_AUDIO_TYPES]
        other_items = [
            item for item in attachments if (item.get("type") or "").lower() not in SUPPORTED_AUDIO_TYPES
        ]
        if not audio_items:
            return text, attachments

        if not self.enabled:
            logger.info("Audio transcription disabled or missing API key; dropping %d audio item(s).", len(audio_items))
            return text, other_items

        transcripts: list[str] = []
        processed = 0
        succeeded = 0
        failed = 0

        for item in audio_items:
            path = item.get("path") or item.get("file")
            if not path:
                failed += 1
                continue
            processed += 1
            transcript = await self.transcribe_file(path, item.get("mime_type"))
            if transcript:
                transcripts.append(transcript)
                succeeded += 1
            else:
                failed += 1

        if transcripts:
            merged = self._merge_transcripts(transcripts)
            if text.strip():
                text = f"{text.rstrip()}\n\n语音转写：\n{merged}"
            else:
                text = f"语音转写：\n{merged}"

        logger.info(
            "Audio transcription summary: processed=%d succeeded=%d failed=%d",
            processed,
            succeeded,
            failed,
        )
        return text, other_items

    async def transcribe_file(self, path: str, mime_type: str | None = None) -> str | None:
        if not self.enabled:
            return None

        file_path = Path(path)
        if not file_path.exists():
            logger.warning("Audio file not found for transcription: %s", file_path)
            return None

        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_BYTES:
            logger.warning(
                "Audio file too large for transcription: %s (%d bytes)",
                file_path,
                file_size,
            )
            return None

        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                with file_path.open("rb") as handle:
                    form = aiohttp.FormData()
                    form.add_field(
                        "file",
                        handle,
                        filename=file_path.name,
                        content_type=mime_type or "application/octet-stream",
                    )
                    form.add_field("model", self._config.model)
                    if self._config.response_format:
                        form.add_field("response_format", self._config.response_format)

                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            self._transcriptions_url,
                            data=form,
                            headers=headers,
                        ) as resp:
                            if resp.status >= 500 or resp.status == 429:
                                last_error = RuntimeError(f"HTTP {resp.status}")
                                if attempt < self._config.max_retries:
                                    await asyncio.sleep(self._backoff(attempt))
                                    continue
                                logger.warning("Transcription failed after retries: HTTP %s", resp.status)
                                return None

                            if resp.status >= 400:
                                logger.warning("Transcription request rejected: HTTP %s", resp.status)
                                return None

                            transcript = await self._parse_transcription_response(resp)
                            if transcript is None:
                                return None
                            logger.info(
                                "Audio transcription succeeded: model=%s bytes=%d chars=%d",
                                self._config.model,
                                file_size,
                                len(transcript),
                            )
                            return transcript
            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt < self._config.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                logger.warning("Transcription timed out after retries.")
                return None
            except Exception as exc:
                last_error = exc
                if attempt < self._config.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                logger.exception("Transcription failed with exception.")
                return None

        if last_error:
            logger.warning("Transcription failed: %s", last_error)
        return None

    async def _parse_transcription_response(self, resp: aiohttp.ClientResponse) -> str | None:
        response_format = (self._config.response_format or "json").lower()
        if response_format in {"json", "verbose_json"}:
            payload = await resp.json()
            text = payload.get("text") if isinstance(payload, dict) else None
            if not text:
                logger.warning("Transcription response missing text field.")
                return None
            return str(text).strip()
        text = await resp.text()
        return text.strip() if text else None

    def _merge_transcripts(self, transcripts: Iterable[str]) -> str:
        items = [t.strip() for t in transcripts if t.strip()]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return "\n".join(f"{idx}. {item}" for idx, item in enumerate(items, start=1))

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_seconds * (2**attempt)
