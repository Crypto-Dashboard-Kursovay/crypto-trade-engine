"""StateManager: heartbeat, balance polling и snapshot активных ботов в Redis.

Запускается одной асинхронной задачей `run()`. Внутри — три независимых loops:
- heartbeat → engine.status каждые `heartbeat_interval_sec`
- balance poll → engine.balance_update для каждого активного бота
- state snapshot → ключи engine:state:<bot_id> в Redis с TTL (бэк читает их в /api/engine/status)

Падение одной итерации loop'а логируется, но не валит loop. Кадое исключение
в balance poll/snapshot — на конкретного бота, остальные продолжают.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from redis.asyncio import Redis

from application.events import BALANCE_UPDATE, ENGINE_STATUS
from domain.interfaces import EventBus
from domain.models import Balance

from .logging import get_logger

logger = get_logger(__name__)


class _OrchestratorView(Protocol):
    @property
    def active_bot_ids(self) -> list[str]: ...
    def iter_running(self) -> list[Any]: ...


class StateManager:
    def __init__(
        self,
        redis: Redis,
        event_bus: EventBus,
        orchestrator: _OrchestratorView,
        heartbeat_interval_sec: int = 10,
        balance_poll_interval_sec: int = 15,
        snapshot_ttl_sec: int = 30,
    ) -> None:
        self._redis = redis
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._heartbeat_interval = heartbeat_interval_sec
        self._balance_interval = balance_poll_interval_sec
        self._snapshot_ttl = snapshot_ttl_sec
        self._started_monotonic = time.monotonic()
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        logger.info(
            "state_manager_started",
            heartbeat_sec=self._heartbeat_interval,
            balance_sec=self._balance_interval,
            snapshot_ttl_sec=self._snapshot_ttl,
        )
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._heartbeat_loop(), name="heartbeat")
                tg.create_task(self._balance_loop(), name="balance-poll")
                tg.create_task(self._snapshot_loop(), name="state-snapshot")
        except asyncio.CancelledError:
            logger.info("state_manager_cancelled")
            raise

    def stop(self) -> None:
        self._stopped.set()

    async def _heartbeat_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._event_bus.publish(
                    ENGINE_STATUS,
                    {
                        "uptime_sec": int(time.monotonic() - self._started_monotonic),
                        "active_bots": self._orchestrator.active_bot_ids,
                        "timestamp": _utc_iso(),
                    },
                )
            except Exception as exc:
                logger.warning("heartbeat_publish_failed", error=str(exc))
            await self._sleep_or_stop(self._heartbeat_interval)

    async def _balance_loop(self) -> None:
        while not self._stopped.is_set():
            for running in self._orchestrator.iter_running():
                try:
                    raw_balances = await running.adapter.get_balance()
                    await self._event_bus.publish(
                        BALANCE_UPDATE,
                        {
                            "bot_id": str(running.bot_id),
                            "credential_id": str(running.credential_id),
                            "balances": _serialize_balances(raw_balances),
                            "timestamp": _utc_iso(),
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "balance_poll_failed",
                        bot_id=str(running.bot_id),
                        error=str(exc),
                    )
            await self._sleep_or_stop(self._balance_interval)

    async def _snapshot_loop(self) -> None:
        # snapshot чаще heartbeat'а — раз в (heartbeat_interval / 2)
        period = max(2, self._heartbeat_interval // 2)
        while not self._stopped.is_set():
            for running in self._orchestrator.iter_running():
                try:
                    snapshot = json.dumps(
                        {
                            "bot_id": str(running.bot_id),
                            "credential_id": str(running.credential_id),
                            "symbol": running.symbol,
                            "strategy_name": running.strategy.name,
                            "timestamp": _utc_iso(),
                        }
                    )
                    await self._redis.set(
                        f"engine:state:{running.bot_id}",
                        snapshot,
                        ex=self._snapshot_ttl,
                    )
                except Exception as exc:
                    logger.warning(
                        "state_snapshot_failed",
                        bot_id=str(running.bot_id),
                        error=str(exc),
                    )
            await self._sleep_or_stop(period)

    async def _sleep_or_stop(self, seconds: int) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)


def _serialize_balances(raw: Mapping[str, Balance]) -> dict[str, dict[str, str]]:
    return {
        currency: {
            "free": str(bal.free),
            "used": str(bal.used),
            "total": str(bal.total),
        }
        for currency, bal in raw.items()
    }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
