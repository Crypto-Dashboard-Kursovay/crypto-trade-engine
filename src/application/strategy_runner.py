import logging
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from domain.exceptions import OrderExecutionError, RiskRejectedError
from domain.interfaces import EventBus, MarketDataProvider, Strategy
from domain.position_manager import PositionManager

from .events import STRATEGY_ERROR
from .order_executor import OrderExecutor
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class StrategyRunner:
    """Async loop wiring a single Strategy to market data and the execution path.

    Pulls candles from MarketDataProvider, hands each one to Strategy.on_candle,
    pushes resulting Signals through RiskManager and then OrderExecutor. Errors
    on a single candle are logged + published as STRATEGY_ERROR — the loop
    keeps running so one bad signal does not stop the whole strategy.

    After each candle, checks PositionManager for stop-loss/take-profit exits
    and executes them through the same RiskManager → OrderExecutor pipeline.

    `bot_id` is present in every STRATEGY_ERROR payload so the backend resolver
    can correctly associate the error with a bot.
    """

    def __init__(
        self,
        strategy: Strategy,
        market_data: MarketDataProvider,
        risk: RiskManager,
        executor: OrderExecutor,
        event_bus: EventBus,
        bot_id: uuid.UUID,
        position_manager: PositionManager | None = None,
    ) -> None:
        self._strategy = strategy
        self._market_data = market_data
        self._risk = risk
        self._executor = executor
        self._event_bus = event_bus
        self._bot_id = bot_id
        self._positions = position_manager

    async def run(self) -> None:
        await self._strategy.on_start()
        async for candle in self._market_data.subscribe(
            self._strategy.symbol, self._strategy.timeframe
        ):
            # 1. Execute any pending stop-loss / take-profit exits
            if self._positions is not None:
                await self._check_exits(candle)

            # 2. Get entry signal from strategy
            signal = self._strategy.on_candle(candle)
            if signal is None:
                continue

            # 3. Execute entry
            await self._execute_signal(signal)

    async def _check_exits(self, candle: Any) -> None:
        exit_signals = self._positions.check_exits(candle)  # type: ignore[union-attr]
        for signal in exit_signals:
            await self._execute_signal(signal)

    async def _execute_signal(self, signal: Any) -> None:
        try:
            approved = await self._risk.check(signal)
            order = await self._executor.execute(approved)
            # Track position if BUY signal executed (open position)
            if self._positions is not None and signal.side.value == "buy":
                # Determine entry price: prefer fill price from order, fallback to signal price
                entry_price = order.price if order.price is not None else signal.price
                if entry_price is not None:
                    stop_loss = _decimal_or_none(signal.metadata.get("stop_loss_pct"))
                    take_profit = _decimal_or_none(signal.metadata.get("take_profit_pct"))
                    self._positions.open(
                        side=signal.side,
                        entry_price=entry_price,
                        size=signal.size,
                        stop_loss_pct=stop_loss,
                        take_profit_pct=take_profit,
                    )
        except RiskRejectedError as exc:
            logger.info("Risk rejected signal from %s: %s", self._strategy.name, exc)
            await self._event_bus.publish(
                STRATEGY_ERROR,
                _err_payload(self._bot_id, self._strategy.name, "risk_rejected", str(exc)),
            )
        except OrderExecutionError as exc:
            logger.warning("Execution failed for %s: %s", self._strategy.name, exc)
            await self._event_bus.publish(
                STRATEGY_ERROR,
                _err_payload(self._bot_id, self._strategy.name, "execution_failed", str(exc)),
            )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _err_payload(
    bot_id: uuid.UUID, strategy: str, kind: str, message: str
) -> Mapping[str, Any]:
    return {
        "bot_id": str(bot_id),
        "strategy": strategy,
        "kind": kind,
        "message": message,
    }
