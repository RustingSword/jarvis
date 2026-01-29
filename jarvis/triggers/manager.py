from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from jarvis.config import TriggersConfig
from jarvis.event_bus import EventBus
from jarvis.storage import ReminderRecord, Storage
from jarvis.triggers.monitor import MonitorTrigger
from jarvis.triggers.scheduler import ReminderPayload, SchedulerTrigger
from jarvis.triggers.webhook import WebhookServer

logger = logging.getLogger(__name__)


class TriggerManager:
    def __init__(self, event_bus: EventBus, storage: Storage, config: TriggersConfig) -> None:
        self._event_bus = event_bus
        self._storage = storage
        self._config = config
        self._scheduler = SchedulerTrigger(event_bus)
        self._monitor = MonitorTrigger(event_bus)
        self._webhook = WebhookServer(config.webhook, event_bus)

    async def start(self) -> None:
        await self._scheduler.start()
        self._scheduler.schedule_jobs(self._config.scheduler)
        await self._monitor.start(self._config.monitors)
        await self._webhook.start()
        await self._restore_reminders()
        logger.info("Trigger manager started")

    async def stop(self) -> None:
        await self._webhook.stop()
        await self._monitor.stop()
        await self._scheduler.stop()
        logger.info("Trigger manager stopped")

    async def schedule_reminder(self, reminder: ReminderRecord) -> None:
        run_at = reminder.trigger_time
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        self._scheduler.schedule_reminder(
            ReminderPayload(
                reminder_id=reminder.id,
                chat_id=reminder.chat_id,
                message=reminder.message,
                repeat_interval_seconds=reminder.repeat_interval_seconds,
            ),
            run_at,
        )

    async def handle_reminder_fired(self, reminder_id: int, repeat_interval_seconds: int | None) -> None:
        if repeat_interval_seconds:
            next_time = datetime.now(timezone.utc) + timedelta(seconds=repeat_interval_seconds)
            await self._reschedule_reminder(reminder_id, next_time)
        else:
            await self._storage.delete_reminder_by_id(reminder_id)

    async def _restore_reminders(self) -> None:
        reminders = await self._storage.list_pending_reminders()
        for reminder in reminders:
            await self.schedule_reminder(reminder)

    async def _reschedule_reminder(self, reminder_id: int, next_time: datetime) -> None:
        await self._storage.update_reminder_time(reminder_id, next_time)
        reminder = await self._storage.get_reminder_by_id(reminder_id)
        if reminder:
            await self.schedule_reminder(reminder)
