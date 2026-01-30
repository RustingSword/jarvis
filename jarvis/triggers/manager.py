from __future__ import annotations

import logging

from jarvis.config import TriggersConfig
from jarvis.event_bus import EventBus
from jarvis.triggers.monitor import MonitorTrigger
from jarvis.triggers.scheduler import SchedulerTrigger
from jarvis.triggers.webhook import WebhookServer

logger = logging.getLogger(__name__)


class TriggerManager:
    def __init__(self, event_bus: EventBus, config: TriggersConfig) -> None:
        self._event_bus = event_bus
        self._config = config
        self._scheduler = SchedulerTrigger(event_bus)
        self._monitor = MonitorTrigger(event_bus)
        self._webhook = WebhookServer(config.webhook, event_bus)

    async def start(self) -> None:
        await self._scheduler.start()
        self._scheduler.schedule_jobs(self._config.scheduler)
        await self._monitor.start(self._config.monitors)
        await self._webhook.start()
        logger.info("Trigger manager started")

    async def stop(self) -> None:
        await self._webhook.stop()
        await self._monitor.stop()
        await self._scheduler.stop()
        logger.info("Trigger manager stopped")
