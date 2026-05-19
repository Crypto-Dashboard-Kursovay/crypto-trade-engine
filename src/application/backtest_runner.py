"""BacktestRunner — драйвер прогона стратегии на исторических свечах.

Композирует те же компоненты, что и live StrategyRunner:
- Strategy.on_candle → Signal
- RiskManager.check → ApprovedSignal
- OrderExecutor.execute → Order (через SimulatedExchangeAdapter)
- InMemoryEventBus собирает engine.new_trade события

Дополнительно:
- перед каждой свечой кормит SimulatedExchangeAdapter.on_candle для матчинга
  open LIMIT-ордеров и обновления last_close;
- считает equity curve и собирает финальные метрики (return, drawdown,
  sharpe, winrate, profit factor).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from domain.backtest_result import BacktestResult, BacktestTrade, EquityPoint
from domain.enums import Side, TimeFrame
from domain.exceptions import OrderExecutionError, RiskRejectedError
from domain.interfaces import Strategy
from domain.models import Candle

from .events import NEW_TRADE
from .order_executor import OrderExecutor
from .risk_manager import RiskManager
from infrastructure.csv_market_data import CSVMarketDataProvider
from infrastructure.in_memory_event_bus import InMemoryEventBus
from infrastructure.simulated_exchange import SimulatedExchangeAdapter

logger = logging.getLogger(__name__)


# Сколько торговых периодов в году на каждый таймфрейм (для Sharpe).
_PERIODS_PER_YEAR: dict[TimeFrame, int] = {
    TimeFrame.M1: 60 * 24 * 365,
    TimeFrame.M5: 12 * 24 * 365,
    TimeFrame.M15: 4 * 24 * 365,
    TimeFrame.M30: 2 * 24 * 365,
    TimeFrame.H1: 24 * 365,
    TimeFrame.H4: 6 * 365,
    TimeFrame.D1: 365,
}


def _decimal_sqrt(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    try:
        return value.sqrt()
    except Exception:  # pragma: no cover
        return Decimal(str(math.sqrt(float(value))))


class BacktestRunner:
    def __init__(
        self,
        strategy: Strategy,
        market_data: CSVMarketDataProvider,
        exchange: SimulatedExchangeAdapter,
        risk: RiskManager,
        executor: OrderExecutor,
        event_bus: InMemoryEventBus,
        bot_id: uuid.UUID | None = None,
        equity_snapshot_every: int = 100,
    ) -> None:
        self._strategy = strategy
        self._market_data = market_data
        self._exchange = exchange
        self._risk = risk
        self._executor = executor
        self._event_bus = event_bus
        self._bot_id = bot_id or uuid.uuid4()
        self._equity_snapshot_every = max(1, equity_snapshot_every)

        # Накопители для метрик
        self._trades: list[BacktestTrade] = []
        self._equity_curve: list[EquityPoint] = []
        self._initial_balance: dict[str, Decimal] = {}
        self._last_buy_cost: Decimal | None = None  # для PnL пар BUY→SELL

        # Subscribe to engine.new_trade для построения списка сделок
        event_bus.subscribe(NEW_TRADE, self._on_new_trade)

    async def run(self) -> BacktestResult:
        # Зафиксируем начальный баланс (читаем через get_balance чтобы быть честными)
        balances = await self._exchange.get_balance()
        self._initial_balance = {k: v.total for k, v in balances.items()}

        await self._strategy.on_start()

        candle_index = 0
        last_candle: Candle | None = None
        async for candle in self._market_data.subscribe(
            self._strategy.symbol, self._strategy.timeframe
        ):
            # 1. Прогоняем open LIMIT ордера против range этой свечи + обновляем last_close
            self._exchange.on_candle(candle)

            # 2. Стратегия видит свечу
            signal = self._strategy.on_candle(candle)
            if signal is not None:
                try:
                    approved = await self._risk.check(signal)
                    await self._executor.execute(approved)
                except RiskRejectedError as exc:
                    logger.info(
                        "backtest.risk_rejected", extra={"err": str(exc)}
                    )
                except OrderExecutionError as exc:
                    logger.warning(
                        "backtest.execution_failed", extra={"err": str(exc)}
                    )

            # 3. Снапшоты equity раз в N свечей (плюс на каждой сделке — внутри _on_new_trade)
            if candle_index % self._equity_snapshot_every == 0:
                self._snapshot_equity(candle)

            last_candle = candle
            candle_index += 1

        # Финальная точка equity
        if last_candle is not None:
            self._snapshot_equity(last_candle)

        return self._build_result()

    def _on_new_trade(self, payload: Mapping[str, Any]) -> None:
        # Преобразуем payload (от OrderExecutor) в BacktestTrade
        from datetime import datetime, timezone

        side = Side(payload["side"])
        price = Decimal(str(payload["price"])) if payload.get("price") else Decimal("0")
        size = Decimal(str(payload["size"]))
        # Комиссия известна на стороне SimulatedExchangeAdapter через filled_orders;
        # для упрощения берём последний fill.
        fills = self._exchange.filled_orders
        fee = fills[-1].fee if fills else Decimal("0")
        # Точное время свечи у нас в exchange.last_close — но не хранится; берём now-ish.
        # На бэктесте порядок строк = порядок событий, точное время не критично.
        timestamp = datetime.now(timezone.utc)

        pnl: Decimal | None = None
        if side is Side.BUY:
            self._last_buy_cost = price * size + fee
        elif side is Side.SELL and self._last_buy_cost is not None:
            revenue = price * size - fee
            pnl = revenue - self._last_buy_cost
            self._last_buy_cost = None

        self._trades.append(
            BacktestTrade(
                timestamp=timestamp,
                side=side,
                price=price,
                size=size,
                fee=fee,
                pnl=pnl,
            )
        )

    def _equity_value(self, last_close: Decimal | None) -> Decimal:
        """Equity = sum(balance_currency * last_price_in_quote).

        Для нашего spot: quote бэлэнс + base * last_close.
        """
        # exchange._symbol = base/quote — мы можем спросить
        symbol = self._exchange._symbol  # noqa: SLF001
        base, _, quote = symbol.partition("/")
        bal_map = {k: v for k, v in self._exchange._balance.items()}  # noqa: SLF001
        quote_total = bal_map.get(quote)
        base_total = bal_map.get(base)
        eq = Decimal("0")
        if quote_total is not None:
            eq += quote_total.total
        if base_total is not None and last_close is not None:
            eq += base_total.total * last_close
        return eq

    def _snapshot_equity(self, candle: Candle) -> None:
        eq = self._equity_value(candle.close)
        self._equity_curve.append(EquityPoint(timestamp=candle.timestamp, equity=eq))

    def _build_result(self) -> BacktestResult:
        balances = self._exchange._balance  # noqa: SLF001
        final_balance = {k: v.total for k, v in balances.items()}

        initial_equity = self._equity_curve[0].equity if self._equity_curve else Decimal("0")
        final_equity = self._equity_curve[-1].equity if self._equity_curve else initial_equity
        if initial_equity > 0:
            total_return_pct = (final_equity / initial_equity - Decimal("1")) * Decimal("100")
        else:
            total_return_pct = Decimal("0")

        # Max drawdown
        max_dd = Decimal("0")
        peak = initial_equity
        for pt in self._equity_curve:
            if pt.equity > peak:
                peak = pt.equity
            if peak > 0:
                dd = (peak - pt.equity) / peak * Decimal("100")
                if dd > max_dd:
                    max_dd = dd

        # Sharpe — среднедневная per-candle доходность / std * sqrt(periods/year)
        sharpe: Decimal | None = None
        if len(self._equity_curve) >= 2:
            returns: list[Decimal] = []
            for i in range(1, len(self._equity_curve)):
                prev = self._equity_curve[i - 1].equity
                cur = self._equity_curve[i].equity
                if prev > 0:
                    returns.append((cur - prev) / prev)
            if len(returns) >= 2:
                mean = sum(returns) / Decimal(len(returns))
                var = sum((r - mean) ** 2 for r in returns) / Decimal(len(returns))
                std = _decimal_sqrt(var)
                if std > 0:
                    periods = _PERIODS_PER_YEAR.get(self._strategy.timeframe, 252)
                    # Снапшоты делаем каждые N свечей, так что фактический период длиннее
                    effective_periods = max(
                        1,
                        periods // self._equity_snapshot_every,
                    )
                    sharpe = mean / std * _decimal_sqrt(Decimal(effective_periods))

        # Win rate + profit factor по PnL пар BUY→SELL
        pnls = [t.pnl for t in self._trades if t.pnl is not None]
        if pnls:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            win_rate = Decimal(len(wins)) / Decimal(len(pnls)) * Decimal("100")
            total_profit = sum(wins, Decimal("0"))
            total_loss = -sum(losses, Decimal("0"))
            profit_factor: Decimal | None = (
                total_profit / total_loss if total_loss > 0 else None
            )
        else:
            win_rate = Decimal("0")
            profit_factor = None

        return BacktestResult(
            initial_balance=self._initial_balance,
            final_balance=final_balance,
            total_return_pct=total_return_pct,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            trades_count=len(self._trades),
            win_rate=win_rate,
            profit_factor=profit_factor,
            trades=self._trades,
            equity_curve=self._equity_curve,
        )


__all__ = ["BacktestRunner"]
