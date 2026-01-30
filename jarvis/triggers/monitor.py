from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
import psutil

from jarvis.config import MonitorConfig
from jarvis.event_bus import EventBus
from jarvis.events import TRIGGER_FIRED

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MonitorState:
    config: MonitorConfig
    task: asyncio.Task | None = None


class MonitorTrigger:
    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._states: list[MonitorState] = []
        self._running = False

    async def start(self, monitors: list[MonitorConfig]) -> None:
        self._running = True
        self._states = [MonitorState(config=m) for m in monitors if m.enabled]
        for state in self._states:
            state.task = asyncio.create_task(self._monitor_loop(state.config))
        logger.info("Monitor trigger started with %d monitors", len(self._states))

    async def stop(self) -> None:
        self._running = False
        tasks = [state.task for state in self._states if state.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._states = []
        logger.info("Monitor trigger stopped")

    async def _monitor_loop(self, config: MonitorConfig) -> None:
        while self._running:
            try:
                value = _read_metric(config.type)
                if value is not None and value >= config.threshold:
                    payload = {
                        "type": "monitor",
                        "name": config.name,
                        "metric": config.type,
                        "value": value,
                        "threshold": config.threshold,
                        "chat_id": config.chat_id,
                    }
                    await self._event_bus.publish(TRIGGER_FIRED, payload)
            except Exception:
                logger.exception("Monitor '%s' failed", config.name)
            await asyncio.sleep(config.interval_seconds)


def _read_metric(metric_type: str) -> float | None:
    metric = metric_type.lower()
    if metric == "cpu":
        return psutil.cpu_percent(interval=None)
    if metric == "memory":
        return psutil.virtual_memory().percent
    if metric == "disk":
        return psutil.disk_usage("/").percent
    if metric == "load":
        load1, _, _ = os.getloadavg()
        return load1
    logger.warning("Unknown monitor metric: %s", metric_type)
    return None
