from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from jarvis.config import WebhookConfig
from jarvis.event_bus import EventBus
from jarvis.events import TRIGGER_FIRED

logger = logging.getLogger(__name__)


@dataclass
class WebhookServer:
    config: WebhookConfig
    event_bus: EventBus
    _app: web.Application = field(init=False, default=None)
    _runner: web.AppRunner | None = field(init=False, default=None)
    _site: web.TCPSite | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._app = web.Application()
        self._app.add_routes([web.post("/webhook", self._handle_webhook)])

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()
        logger.info("Webhook server listening on %s:%s", self.config.host, self.config.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        logger.info("Webhook server stopped")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        if self.config.token:
            token = request.headers.get("X-Webhook-Token")
            if token != self.config.token:
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            payload: Any = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        await self.event_bus.publish(
            TRIGGER_FIRED,
            {
                "type": "webhook",
                "payload": payload,
            },
        )
        return web.json_response({"ok": True})
