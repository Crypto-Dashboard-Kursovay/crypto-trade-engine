"""EngineOrchestrator: жизненный цикл стратегий на уровне Application.

Один процесс держит словарь bot_id → RunningStrategy. Каждая стратегия — отдельная
asyncio.Task в общем TaskGroup. Падение одной не валит остальных (StrategyRunner
сам глотает Domain-исключения).

Зависимости (DI):
- BotRepository / CredentialRepository — конфиги и расшифрованные creds из БД.
- StrategyRegistry — резолв `strategy_class` (string) → класс стратегии.
- EventBus — публикация событий движка.
- exchange_factory / market_data_factory — фабрики инфраструктурных адаптеров
  (orchestrator не импортирует CCXT напрямую).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from domain.interfaces import EventBus, ExchangeAdapter, MarketDataProvider, Strategy
from domain.position_manager import PositionManager

from .order_executor import OrderExecutor
from .risk_manager import RiskManager
from .strategy_runner import StrategyRunner

logger = logging.getLogger(__name__)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class _BotRepository(Protocol):
    async def get(self, bot_id: uuid.UUID) -> Any: ...


class _CredentialRepository(Protocol):
    async def get_decrypted(self, credential_id: uuid.UUID) -> Any: ...


class _StrategyRegistry(Protocol):
    def resolve(self, name: str) -> type[Strategy]: ...


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_position_pct: Decimal
    min_lot: Decimal
    quote_currency: str = "USDT"


@dataclass(frozen=True, slots=True)
class RunningStrategy:
    bot_id: uuid.UUID
    credential_id: uuid.UUID
    symbol: str
    task: asyncio.Task[None]
    adapter: ExchangeAdapter
    strategy: Strategy


class OrchestratorError(Exception):
    """Не удалось запустить/остановить стратегию."""


ExchangeFactory = Callable[[Any], ExchangeAdapter]
MarketDataFactory = Callable[[ExchangeAdapter], MarketDataProvider]


class EngineOrchestrator:
    def __init__(
        self,
        bot_repo: _BotRepository,
        credential_repo: _CredentialRepository,
        strategy_registry: _StrategyRegistry,
        event_bus: EventBus,
        exchange_factory: ExchangeFactory,
        market_data_factory: MarketDataFactory,
        risk_config: RiskConfig,
    ) -> None:
        self._bot_repo = bot_repo
        self._credential_repo = credential_repo
        self._registry = strategy_registry
        self._event_bus = event_bus
        self._exchange_factory = exchange_factory
        self._market_data_factory = market_data_factory
        self._risk_config = risk_config
        self._running: dict[uuid.UUID, RunningStrategy] = {}
        self._lock = asyncio.Lock()

    @property
    def active_bot_ids(self) -> list[str]:
        return [str(bot_id) for bot_id in self._running]

    def iter_running(self) -> list[RunningStrategy]:
        """Snapshot активных стратегий — безопасен для итерации (новый list)."""
        return list(self._running.values())

    async def start_strategy(self, bot_id: uuid.UUID) -> None:
        async with self._lock:
            if bot_id in self._running:
                logger.info("start ignored, bot already running: %s", bot_id)
                return

            bot = await self._bot_repo.get(bot_id)
            if bot is None:
                raise OrchestratorError(f"Bot {bot_id} not found in DB")

            cred = await self._credential_repo.get_decrypted(bot.credential_id)
            if cred is None:
                raise OrchestratorError(
                    f"Credential {bot.credential_id} not found for bot {bot_id}"
                )

            strategy_cls = self._registry.resolve(bot.strategy_class)
            adapter = self._exchange_factory(cred)
            market_data = self._market_data_factory(adapter)

            strategy = strategy_cls(  # type: ignore[call-arg]
                symbol=bot.symbol,
                timeframe=bot.timeframe,
                **bot.params,
            )

            await _warmup_strategy(strategy, adapter, bot.symbol, bot.timeframe)

            # Build position manager with optional stop-loss / take-profit defaults
            # extracted from strategy params.
            sl = _decimal_or_none(bot.params.get("stop_loss_pct"))
            tp = _decimal_or_none(bot.params.get("take_profit_pct"))
            position_manager = PositionManager(
                strategy_name=strategy.name,
                symbol=bot.symbol,
                default_stop_loss_pct=sl,
                default_take_profit_pct=tp,
            )

            # Reconcile with exchange — load existing positions so stop-loss /
            # take-profit works from the correct entry point after restart.
            try:
                exchange_positions = await adapter.get_positions()
                own_positions = [p for p in exchange_positions if p.symbol == bot.symbol]
                if own_positions:
                    position_manager.reconcile(own_positions)
            except Exception as exc:
                logger.warning(
                    "position_reconcile_skipped",
                    bot_id=str(bot_id),
                    error=str(exc),
                )

            risk = RiskManager(
                adapter=adapter,
                max_position_pct=self._risk_config.max_position_pct,
                min_lot=self._risk_config.min_lot,
                quote_currency=self._risk_config.quote_currency,
            )
            executor = OrderExecutor(adapter, self._event_bus, bot_id=bot_id)
            runner = StrategyRunner(
                strategy=strategy,
                market_data=market_data,
                risk=risk,
                executor=executor,
                event_bus=self._event_bus,
                bot_id=bot_id,
                position_manager=position_manager,
            )

            task = asyncio.create_task(runner.run(), name=f"bot-{bot_id}")
            self._running[bot_id] = RunningStrategy(
                bot_id=bot_id,
                credential_id=bot.credential_id,
                symbol=bot.symbol,
                task=task,
                adapter=adapter,
                strategy=strategy,
            )
            logger.info(
                "strategy started: bot_id=%s strategy=%s symbol=%s",
                bot_id,
                bot.strategy_class,
                bot.symbol,
            )

    async def stop_strategy(self, bot_id: uuid.UUID) -> None:
        async with self._lock:
            running = self._running.pop(bot_id, None)
            if running is None:
                logger.info("stop ignored, bot not running: %s", bot_id)
                return

            running.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running.task
            with contextlib.suppress(Exception):
                await running.adapter.close()
            logger.info("strategy stopped: bot_id=%s", bot_id)

    async def update_strategy(self, bot_id: uuid.UUID, payload: dict[str, Any] | None = None) -> None:
        """Restart strategy with latest config from DB. Payload (params) is already persisted."""
        if bot_id in self._running:
            await self.stop_strategy(bot_id)
        await self.start_strategy(bot_id)

    async def shutdown(self) -> None:
        """Останавливает все стратегии. Используется при graceful shutdown."""
        bot_ids = list(self._running)
        for bot_id in bot_ids:
            await self.stop_strategy(bot_id)


async def _warmup_strategy(
    strategy: Strategy,
    adapter: ExchangeAdapter,
    symbol: str,
    timeframe: Any,
) -> None:
    """Прогревает стратегию историческими свечами через REST до начала live-цикла."""
    if strategy.startup_candle_count <= 0:
        return
    history = await adapter.fetch_ohlcv(
        symbol=symbol, timeframe=timeframe, limit=strategy.startup_candle_count
    )
    for candle in history:
        strategy.on_candle(candle)
    logger.debug(
        "strategy warmed up: %d candles for %s", len(history), symbol
    )
