"""Smoke / acceptance test для BacktestRunner.

Используем `_FakeStrategy`, которая на свече N генерирует BUY,
на свече N+5 — SELL. Прогоняем по 30 синтетическим свечам с известной
динамикой цены и проверяем, что метрики посчитались.
"""

from __future__ import annotations

import csv
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from application.backtest_runner import BacktestRunner
from application.order_executor import OrderExecutor
from application.risk_manager import RiskManager
from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal
from infrastructure.csv_market_data import CSVMarketDataProvider
from infrastructure.in_memory_event_bus import InMemoryEventBus
from infrastructure.simulated_exchange import SimulatedExchangeAdapter


class _FakeStrategy(Strategy):
    name = "FakeBuySellAt"

    def __init__(self, symbol: str, timeframe: TimeFrame, buy_at: int, sell_at: int) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = 0
        self._buy_at = buy_at
        self._sell_at = sell_at
        self._i = -1

    def on_candle(self, candle: Candle) -> Signal | None:
        self._i += 1
        if self._i == self._buy_at:
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=Decimal("0.01"),
                price=candle.close,
            )
        if self._i == self._sell_at:
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=Decimal("0.01"),
                price=candle.close,
            )
        return None


def _write_data(path: Path, prices: list[int], spread: int = 2) -> None:
    """Свечи с high/low спредом — иначе LIMIT-ордера никогда не исполнятся."""
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        base_ts = 1_700_000_000_000  # 2023-11-15 ms
        for i, p in enumerate(prices):
            ts = base_ts + i * 60_000
            high = p + spread
            low = p - spread
            w.writerow([ts, str(p), str(high), str(low), str(p), "1"])


@pytest.mark.asyncio
async def test_runner_executes_buy_then_sell_and_emits_metrics(tmp_path: Path) -> None:
    # Цены растут с 100 до 200 за 30 свечей. BUY на свече 5, SELL на свече 20.
    prices = list(range(100, 130))
    csv_path = tmp_path / "data.csv"
    _write_data(csv_path, prices)

    provider = CSVMarketDataProvider(csv_path, "BTC/USDT", TimeFrame.M1)
    exchange = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("1000")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0"),
        slippage=Decimal("0"),
    )
    risk = RiskManager(
        adapter=exchange,
        max_position_pct=Decimal("1.0"),
        min_lot=Decimal("0.0001"),
        quote_currency="USDT",
    )
    event_bus = InMemoryEventBus()
    bot_id = uuid.uuid4()
    executor = OrderExecutor(adapter=exchange, event_bus=event_bus, bot_id=bot_id)
    strategy = _FakeStrategy("BTC/USDT", TimeFrame.M1, buy_at=5, sell_at=20)
    runner = BacktestRunner(
        strategy=strategy,
        market_data=provider,
        exchange=exchange,
        risk=risk,
        executor=executor,
        event_bus=event_bus,
        bot_id=bot_id,
        equity_snapshot_every=5,
    )
    result = await runner.run()
    assert result.trades_count == 2
    assert result.trades[0].side is Side.BUY
    assert result.trades[1].side is Side.SELL
    # На SELL должен быть положительный PnL (купили на 105, продали на 120)
    assert result.trades[1].pnl is not None and result.trades[1].pnl > 0
    # Equity curve должен быть непустой
    assert len(result.equity_curve) > 0
    assert result.win_rate == Decimal("100")


@pytest.mark.asyncio
async def test_to_json_roundtrip(tmp_path: Path) -> None:
    csv_path = tmp_path / "flat.csv"
    _write_data(csv_path, [100] * 10)
    provider = CSVMarketDataProvider(csv_path, "BTC/USDT", TimeFrame.M1)
    exchange = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("1000")},
        symbol="BTC/USDT",
    )
    risk = RiskManager(
        adapter=exchange,
        max_position_pct=Decimal("1.0"),
        min_lot=Decimal("0.0001"),
        quote_currency="USDT",
    )
    event_bus = InMemoryEventBus()
    bot_id = uuid.uuid4()
    executor = OrderExecutor(adapter=exchange, event_bus=event_bus, bot_id=bot_id)
    # Стратегия которая ничего не делает
    strategy = _FakeStrategy("BTC/USDT", TimeFrame.M1, buy_at=999, sell_at=1000)
    runner = BacktestRunner(
        strategy=strategy,
        market_data=provider,
        exchange=exchange,
        risk=risk,
        executor=executor,
        event_bus=event_bus,
        bot_id=bot_id,
        equity_snapshot_every=1,
    )
    result = await runner.run()
    blob = result.to_json()
    assert "total_return_pct" in blob
    assert isinstance(blob["trades"], list)
    assert blob["trades_count"] == 0
