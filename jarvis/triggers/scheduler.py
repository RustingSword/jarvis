from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from jarvis.config import SchedulerJobConfig
from jarvis.event_bus import EventBus
from jarvis.events import TRIGGER_FIRED

logger = logging.getLogger(__name__)


class SchedulerTrigger:
    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        self._scheduler = AsyncIOScheduler(timezone=local_tz)
        self._timezone = local_tz

    async def start(self) -> None:
        self._scheduler.start()
        logger.info("Scheduler started")

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    def schedule_jobs(self, jobs: list[SchedulerJobConfig]) -> None:
        for job in jobs:
            if not job.name or not job.cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(job.cron, timezone=self._timezone)
            except Exception:
                logger.exception("Invalid cron expression for job '%s': %s", job.name, job.cron)
                continue
            self._scheduler.add_job(
                self._fire_job,
                trigger=trigger,
                id=f"schedule_{job.name}",
                replace_existing=True,
                kwargs={
                    "name": job.name,
                    "chat_id": job.chat_id,
                    "message": job.message,
                },
            )
            logger.info("Scheduled job '%s' with cron '%s'", job.name, job.cron)

    async def _fire_job(self, name: str, chat_id: str | None, message: str | None) -> None:
        payload = {
            "type": "schedule",
            "name": name,
            "chat_id": chat_id,
            "message": message,
        }
        await self._event_bus.publish(TRIGGER_FIRED, payload)
