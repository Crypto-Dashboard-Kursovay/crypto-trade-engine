from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.macd_cross import MacdCross


def _candle(close: float, n: int) -> Candle:
    c = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        timeframe=TimeFrame.M1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("1"),
    )


def test_no_signal_on_flat_series() -> None:
    strat = MacdCross("BTC/USDT", TimeFrame.M1, fast_period=3, slow_period=6, signal_period=2)
    signals = [strat.on_candle(_candle(100, i)) for i in range(30)]
    # На плоской серии EMA fast == EMA slow ≈ 100, кроссов нет
    assert all(s is None for s in signals)


def test_bullish_cross_emits_buy() -> None:
    strat = MacdCross("BTC/USDT", TimeFrame.M1, fast_period=3, slow_period=6, signal_period=2)
    # Падение, потом резкий рост — MACD должен пересечь signal снизу вверх
    prices = [100, 95, 90, 85, 80, 75, 70, 80, 90, 100, 110, 120, 130, 140]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    assert len(buys) >= 1


def test_bearish_cross_emits_sell() -> None:
    strat = MacdCross("BTC/USDT", TimeFrame.M1, fast_period=3, slow_period=6, signal_period=2)
    prices = [100, 110, 120, 130, 140, 150, 140, 120, 100, 80, 60, 40]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    sells = [s for s in signals if s is not None and s.side is Side.SELL]
    assert len(sells) >= 1


def test_invalid_periods_rejected() -> None:
    with pytest.raises(ValueError):
        MacdCross("BTC/USDT", TimeFrame.M1, fast_period=26, slow_period=12)
    with pytest.raises(ValueError):
        MacdCross("BTC/USDT", TimeFrame.M1, fast_period=0, slow_period=10)
    with pytest.raises(ValueError):
        MacdCross("BTC/USDT", TimeFrame.M1, signal_period=0)
