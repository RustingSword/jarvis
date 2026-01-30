from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from jarvis.config import SchedulerJobConfig
from jarvis.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReminderPayload:
    reminder_id: int
    chat_id: str
    message: str
    repeat_interval_seconds: int | None


class SchedulerTrigger:
    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)

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
                trigger = CronTrigger.from_crontab(job.cron, timezone=timezone.utc)
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

    def schedule_reminder(
        self,
        reminder: ReminderPayload,
        run_at: datetime,
    ) -> None:
        job_id = f"reminder_{reminder.reminder_id}"
        self._scheduler.add_job(
            self._fire_reminder,
            trigger="date",
            run_date=run_at,
            id=job_id,
            replace_existing=True,
            kwargs={"reminder": reminder},
        )
        logger.info("Scheduled reminder %s at %s", reminder.reminder_id, run_at)

    async def _fire_job(self, name: str, chat_id: str | None, message: str | None) -> None:
        payload = {
            "type": "schedule",
            "name": name,
            "chat_id": chat_id,
            "message": message,
        }
        await self._event_bus.publish("trigger.fired", payload)

    async def _fire_reminder(self, reminder: ReminderPayload) -> None:
        payload = {
            "type": "reminder",
            "reminder_id": reminder.reminder_id,
            "chat_id": reminder.chat_id,
            "message": reminder.message,
            "repeat_interval_seconds": reminder.repeat_interval_seconds,
        }
        await self._event_bus.publish("trigger.fired", payload)
