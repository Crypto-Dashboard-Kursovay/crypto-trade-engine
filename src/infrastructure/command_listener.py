"""Подписка на engine.commands.* и dispatch в EngineOrchestrator.

Идемпотентность: каждая команда несёт `command_id` (UUID). Перед обработкой
movement делает `SET engine:commands:processed:<id> 1 EX <ttl> NX`. Если ключ
уже был — значит команду уже обработали, пропускаем.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from redis.asyncio import Redis

from application.events import (
    COMMAND_START,
    COMMAND_STOP,
    COMMAND_UPDATE,
    LISTENED_CHANNELS,
)

from .logging import get_logger

logger = get_logger(__name__)


_DEDUP_KEY_TEMPLATE = "engine:commands:processed:{command_id}"


class _Orchestrator(Protocol):
    async def start_strategy(self, bot_id: uuid.UUID) -> None: ...
    async def stop_strategy(self, bot_id: uuid.UUID) -> None: ...
    async def update_strategy(self, bot_id: uuid.UUID, payload: dict[str, Any] | None = None) -> None: ...


class CommandListener:
    def __init__(
        self,
        redis: Redis,
        orchestrator: _Orchestrator,
        dedup_ttl_sec: int = 86400,
    ) -> None:
        self._redis = redis
        self._orchestrator = orchestrator
        self._dedup_ttl_sec = dedup_ttl_sec
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*LISTENED_CHANNELS)
        logger.info("command_listener_started", channels=list(LISTENED_CHANNELS))
        try:
            while not self._stopped.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                channel = _decode(message.get("channel"))
                data = _decode(message.get("data"))
                if channel is None or data is None:
                    continue
                await self._handle_message(channel, data)
        except asyncio.CancelledError:
            logger.info("command_listener_cancelled")
            raise
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(*LISTENED_CHANNELS)
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    def stop(self) -> None:
        self._stopped.set()

    async def _handle_message(self, channel: str, raw: str) -> None:
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("command_invalid_json", channel=channel, data=raw[:200])
            return

        command_id_raw = payload.get("command_id")
        bot_id_raw = payload.get("bot_id")
        if not command_id_raw or not bot_id_raw:
            logger.warning(
                "command_missing_required_fields",
                channel=channel,
                payload_keys=list(payload.keys()),
            )
            return

        if not await self._claim(command_id_raw):
            logger.info("command_skipped_duplicate", command_id=command_id_raw)
            return

        try:
            bot_id = uuid.UUID(bot_id_raw)
        except (ValueError, TypeError):
            logger.warning("command_invalid_bot_id", bot_id=bot_id_raw)
            return

        handler, use_payload = self._dispatch_table().get(channel) or (None, False)
        if handler is None:
            logger.warning("command_unknown_channel", channel=channel)
            return

        try:
            if use_payload:
                await handler(bot_id, payload)
            else:
                await handler(bot_id)
        except Exception as exc:
            logger.exception(
                "command_handler_failed",
                channel=channel,
                bot_id=str(bot_id),
                error=str(exc),
            )

    async def _claim(self, command_id: str) -> bool:
        """Idempotency: пытается захватить command_id. True если впервые."""
        key = _DEDUP_KEY_TEMPLATE.format(command_id=command_id)
        result = await self._redis.set(key, "1", ex=self._dedup_ttl_sec, nx=True)
        return bool(result)

    def _dispatch_table(self) -> dict[str, tuple[Callable[..., Awaitable[None]], bool]]:
        return {
            COMMAND_START: (self._orchestrator.start_strategy, False),
            COMMAND_STOP: (self._orchestrator.stop_strategy, False),
            COMMAND_UPDATE: (self._orchestrator.update_strategy, True),
        }


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None
