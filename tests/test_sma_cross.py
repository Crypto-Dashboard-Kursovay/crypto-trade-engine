from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.sma_cross import SmaCross


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
    strat = SmaCross("BTC/USDT", TimeFrame.M1, fast_period=3, slow_period=5)
    for i in range(4):  # < slow_period
        assert strat.on_candle(_candle(100, i)) is None


def test_bullish_cross_emits_buy() -> None:
    strat = SmaCross("BTC/USDT", TimeFrame.M1, fast_period=2, slow_period=4)
    # fast on price [100, 100, 100, 100, 110, 120] — fast=mean(last 2), slow=mean(last 4)
    # warmup: первые 4 свечи без сигналов; затем падение fast SMA — нет cross-up
    prices = [100, 100, 100, 100, 110, 120, 130, 140]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    assert len(buys) >= 1, "ожидаем как минимум один BUY-сигнал"


def test_bearish_cross_emits_sell() -> None:
    strat = SmaCross("BTC/USDT", TimeFrame.M1, fast_period=2, slow_period=4)
    # цены растут, потом падают — fast пересекает slow сверху вниз
    prices = [100, 110, 120, 130, 140, 130, 100, 80, 60]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    sells = [s for s in signals if s is not None and s.side is Side.SELL]
    assert len(sells) >= 1, "ожидаем как минимум один SELL-сигнал"


def test_signal_carries_strategy_name_and_size() -> None:
    strat = SmaCross(
        "BTC/USDT", TimeFrame.M1, fast_period=2, slow_period=4, order_size=Decimal("0.05")
    )
    prices = [100, 100, 100, 100, 105, 110, 115]
    sig = None
    for i, p in enumerate(prices):
        s = strat.on_candle(_candle(p, i))
        if s is not None:
            sig = s
            break
    assert sig is not None
    assert sig.strategy_name == "SmaCross"
    assert sig.symbol == "BTC/USDT"
    assert sig.size == Decimal("0.05")
    assert sig.price is not None


def test_startup_candle_count_equals_slow_period() -> None:
    strat = SmaCross("BTC/USDT", TimeFrame.M1, fast_period=10, slow_period=50)
    assert strat.startup_candle_count == 50


def test_invalid_periods_rejected() -> None:
    with pytest.raises(ValueError):
        SmaCross("BTC/USDT", TimeFrame.M1, fast_period=200, slow_period=50)
    with pytest.raises(ValueError):
        SmaCross("BTC/USDT", TimeFrame.M1, fast_period=0, slow_period=10)
    with pytest.raises(ValueError):
        SmaCross("BTC/USDT", TimeFrame.M1, fast_period=10, slow_period=20, order_size=Decimal("0"))
