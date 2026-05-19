from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.rsi_threshold import RsiThreshold


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
    strat = RsiThreshold("BTC/USDT", TimeFrame.M1, rsi_period=14)
    # rsi_period + 1 свечей нужно для первой инициализации
    for i in range(14):
        assert strat.on_candle(_candle(100, i)) is None


def test_oversold_emits_buy() -> None:
    strat = RsiThreshold("BTC/USDT", TimeFrame.M1, rsi_period=5, oversold=Decimal("40"))
    # резкое падение — RSI уходит вниз
    prices = [100, 99, 98, 95, 90, 85, 80, 70, 60, 50]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    assert len(buys) >= 1


def test_no_duplicate_buy_while_in_position() -> None:
    strat = RsiThreshold("BTC/USDT", TimeFrame.M1, rsi_period=3, oversold=Decimal("40"))
    prices = [100, 80, 60, 50, 45, 40, 35, 30]
    buys = []
    for i, p in enumerate(prices):
        s = strat.on_candle(_candle(p, i))
        if s is not None and s.side is Side.BUY:
            buys.append(s)
    assert len(buys) == 1, "BUY должен быть один пока не закрылась позиция"


def test_overbought_emits_sell_after_buy() -> None:
    strat = RsiThreshold(
        "BTC/USDT",
        TimeFrame.M1,
        rsi_period=3,
        oversold=Decimal("40"),
        overbought=Decimal("60"),
    )
    # вниз — BUY; вверх — SELL
    prices = [100, 80, 60, 40, 30, 60, 80, 100, 120]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    sides = [s.side for s in signals if s is not None]
    assert Side.BUY in sides
    assert Side.SELL in sides
    assert sides.index(Side.BUY) < sides.index(Side.SELL)


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError):
        RsiThreshold("BTC/USDT", TimeFrame.M1, rsi_period=1)
    with pytest.raises(ValueError):
        RsiThreshold("BTC/USDT", TimeFrame.M1, oversold=Decimal("80"), overbought=Decimal("20"))
    with pytest.raises(ValueError):
        RsiThreshold("BTC/USDT", TimeFrame.M1, order_size=Decimal("0"))
