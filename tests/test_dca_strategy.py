from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.dca import DcaStrategy


def _candle(close: float, n: int) -> Candle:
    c = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        timeframe=TimeFrame.H1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=n),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("1"),
    )


def test_no_signal_before_interval() -> None:
    strat = DcaStrategy(
        "BTC/USDT", TimeFrame.H1, buy_amount_quote=Decimal("10"), interval_candles=24
    )
    for i in range(23):
        assert strat.on_candle(_candle(60000, i)) is None


def test_signal_on_interval_candle() -> None:
    strat = DcaStrategy(
        "BTC/USDT", TimeFrame.H1, buy_amount_quote=Decimal("10"), interval_candles=3
    )
    s1 = strat.on_candle(_candle(60000, 0))
    s2 = strat.on_candle(_candle(60000, 1))
    s3 = strat.on_candle(_candle(60000, 2))  # третья = interval — должен быть BUY
    assert s1 is None
    assert s2 is None
    assert s3 is not None
    assert s3.side is Side.BUY
    # size = 10 USDT / 60000 = 0.0001666...
    assert s3.size == Decimal("10") / Decimal("60000")


def test_repeated_intervals() -> None:
    strat = DcaStrategy(
        "BTC/USDT", TimeFrame.H1, buy_amount_quote=Decimal("100"), interval_candles=2
    )
    signals = [strat.on_candle(_candle(50000, i)) for i in range(10)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    # На 2-й, 4-й, ..., 10-й свечах — итого 5 BUY
    assert len(buys) == 5


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError):
        DcaStrategy("BTC/USDT", TimeFrame.H1, buy_amount_quote=Decimal("0"))
    with pytest.raises(ValueError):
        DcaStrategy("BTC/USDT", TimeFrame.H1, interval_candles=0)
