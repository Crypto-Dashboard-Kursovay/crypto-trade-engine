"""Подписка на engine.commands.* и dispatch в EngineOrchestrator.

Идемпотентность: каждая команда несёт `command_id` (UUID). Перед обработкой
listener делает `SET engine:commands:processed:<id> 1 EX <ttl> NX`. Если ключ
уже был — значит команду уже обработали, пропускаем.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
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


class _PendingCommand(Protocol):
    command_id: uuid.UUID
    bot_id: uuid.UUID
    kind: str
    payload: dict[str, Any]


class _CommandRepository(Protocol):
    async def list_pending(self, *, limit: int = 100) -> Sequence[_PendingCommand]: ...
    async def mark_processed(self, command_id: uuid.UUID) -> None: ...


class CommandListener:
    def __init__(
        self,
        redis: Redis,
        orchestrator: _Orchestrator,
        command_repository: _CommandRepository | None = None,
        dedup_ttl_sec: int = 86400,
        pending_poll_interval_sec: float = 1.0,
        pending_batch_size: int = 100,
    ) -> None:
        self._redis = redis
        self._orchestrator = orchestrator
        self._command_repository = command_repository
        self._dedup_ttl_sec = dedup_ttl_sec
        self._pending_poll_interval_sec = pending_poll_interval_sec
        self._pending_batch_size = pending_batch_size
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*LISTENED_CHANNELS)
        logger.info("command_listener_started", channels=list(LISTENED_CHANNELS))
        next_pending_poll = 0.0
        try:
            while not self._stopped.is_set():
                now = time.monotonic()
                if now >= next_pending_poll:
                    await self._process_pending_commands()
                    next_pending_poll = now + self._pending_poll_interval_sec

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.25
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

        await self._process_payload(channel, payload)
        command_id = _uuid_or_none(payload.get("command_id"))
        if command_id is not None:
            await self._mark_pending_processed(command_id)

    async def _process_pending_commands(self) -> None:
        if self._command_repository is None:
            return
        try:
            commands = await self._command_repository.list_pending(
                limit=self._pending_batch_size
            )
        except Exception as exc:
            logger.exception("pending_commands_load_failed", error=str(exc))
            return

        for command in commands:
            channel = _channel_for_kind(command.kind)
            if channel is None:
                logger.warning(
                    "pending_command_unknown_kind",
                    command_id=str(command.command_id),
                    kind=command.kind,
                )
                await self._mark_pending_processed(command.command_id)
                continue

            payload = {
                **command.payload,
                "command_id": str(command.command_id),
                "bot_id": str(command.bot_id),
            }
            try:
                await self._process_payload(channel, payload)
            except Exception as exc:
                logger.exception(
                    "pending_command_process_failed",
                    command_id=str(command.command_id),
                    channel=channel,
                    error=str(exc),
                )
                continue
            await self._mark_pending_processed(command.command_id)

    async def _mark_pending_processed(self, command_id: uuid.UUID) -> None:
        if self._command_repository is None:
            return
        try:
            await self._command_repository.mark_processed(command_id)
        except Exception as exc:
            logger.exception(
                "pending_command_mark_processed_failed",
                command_id=str(command_id),
                error=str(exc),
            )

    async def _process_payload(self, channel: str, payload: dict[str, Any]) -> None:
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


def _channel_for_kind(kind: Any) -> str | None:
    value = getattr(kind, "value", kind)
    return {
        "start": COMMAND_START,
        "stop": COMMAND_STOP,
        "update": COMMAND_UPDATE,
    }.get(str(value))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
