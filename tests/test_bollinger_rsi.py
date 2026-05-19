from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.bollinger_rsi import BollingerRsi


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


def test_no_signal_during_warmup() -> None:
    strat = BollingerRsi(
        "BTC/USDT", TimeFrame.M1, bb_period=10, rsi_period=5
    )
    for i in range(10):
        assert strat.on_candle(_candle(100, i)) is None


def test_combined_filter_buys_on_oversold_and_lower_band() -> None:
    strat = BollingerRsi(
        "BTC/USDT",
        TimeFrame.M1,
        bb_period=5,
        bb_std=Decimal("1.5"),
        rsi_period=3,
        oversold=Decimal("40"),
        overbought=Decimal("60"),
    )
    # шум потом резкое падение — оба условия совпадают
    prices = [100, 99, 101, 100, 99, 100, 70, 60, 50, 40]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    assert len(buys) >= 1


def test_flat_series_no_signals() -> None:
    # На плоской серии 100 BB-границы практически совпадают со средней,
    # close не пробивает ни нижнюю, ни верхнюю полосу — сигналов быть не должно.
    strat = BollingerRsi(
        "BTC/USDT",
        TimeFrame.M1,
        bb_period=5,
        bb_std=Decimal("2.0"),
        rsi_period=3,
    )
    signals = [strat.on_candle(_candle(100, i)) for i in range(20)]
    assert all(s is None for s in signals)


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError):
        BollingerRsi("BTC/USDT", TimeFrame.M1, bb_period=1)
    with pytest.raises(ValueError):
        BollingerRsi("BTC/USDT", TimeFrame.M1, oversold=Decimal("80"), overbought=Decimal("20"))
