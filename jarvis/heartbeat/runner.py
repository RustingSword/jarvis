from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


@dataclass(slots=True)
class HeartbeatConfig:
    state_path: Path
    heartbeat_paths: tuple[Path, ...]


class HeartbeatRunner:
    def __init__(self, config: HeartbeatConfig) -> None:
        self._config = config

    def run(self) -> str | None:
        """Return heartbeat content if it changed and is non-empty; otherwise None."""
        content = self._read_heartbeat_content()
        if content is None:
            self._write_state(last_hash=None, last_trigger_at=None)
            return None

        normalized = _normalize_content(content)
        if not normalized:
            self._write_state(last_hash=None, last_trigger_at=None)
            return None

        content_hash = _hash_content(normalized)
        state = self._read_state()
        last_hash = state.get("last_hash")
        if last_hash == content_hash:
            self._write_state(last_hash=last_hash, last_trigger_at=state.get("last_trigger_at"))
            return None

        self._write_state(last_hash=content_hash, last_trigger_at=_utc_now())
        return content.rstrip()

    def _read_heartbeat_content(self) -> str | None:
        for path in self._config.heartbeat_paths:
            try:
                if path.exists():
                    return path.read_text(encoding="utf-8")
            except Exception:
                logger.exception("Failed to read heartbeat file: {}", path)
                return None
        logger.debug("Heartbeat file not found in {}", self._config.heartbeat_paths)
        return None

    def _read_state(self) -> dict:
        path = self._config.state_path
        try:
            if not path.exists():
                return {}
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            logger.exception("Failed to read heartbeat state: {}", path)
        return {}

    def _write_state(self, *, last_hash: str | None, last_trigger_at: str | None) -> None:
        path = self._config.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_hash": last_hash,
                "last_checked_at": _utc_now(),
                "last_trigger_at": last_trigger_at,
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to write heartbeat state: {}", path)


def _normalize_content(content: str) -> str:
    lines = [line.strip() for line in content.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    return "\n".join(lines)


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
