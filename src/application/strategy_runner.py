import logging
from collections.abc import Mapping
from typing import Any

from domain.exceptions import OrderExecutionError, RiskRejectedError
from domain.interfaces import EventBus, MarketDataProvider, Strategy

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
    """

    def __init__(
        self,
        strategy: Strategy,
        market_data: MarketDataProvider,
        risk: RiskManager,
        executor: OrderExecutor,
        event_bus: EventBus,
    ) -> None:
        self._strategy = strategy
        self._market_data = market_data
        self._risk = risk
        self._executor = executor
        self._event_bus = event_bus

    async def run(self) -> None:
        await self._strategy.on_start()
        async for candle in self._market_data.subscribe(
            self._strategy.symbol, self._strategy.timeframe
        ):
            signal = self._strategy.on_candle(candle)
            if signal is None:
                continue
            try:
                approved = await self._risk.check(signal)
                await self._executor.execute(approved)
            except RiskRejectedError as exc:
                logger.info("Risk rejected signal from %s: %s", self._strategy.name, exc)
                await self._event_bus.publish(
                    STRATEGY_ERROR,
                    _err_payload(self._strategy.name, "risk_rejected", str(exc)),
                )
            except OrderExecutionError as exc:
                logger.warning("Execution failed for %s: %s", self._strategy.name, exc)
                await self._event_bus.publish(
                    STRATEGY_ERROR,
                    _err_payload(self._strategy.name, "execution_failed", str(exc)),
                )


def _err_payload(strategy: str, kind: str, message: str) -> Mapping[str, Any]:
    return {"strategy": strategy, "kind": kind, "message": message}
