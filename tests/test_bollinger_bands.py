from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.bollinger_bands import BollingerBands


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
    strat = BollingerBands("BTC/USDT", TimeFrame.M1, period=10)
    for i in range(9):
        assert strat.on_candle(_candle(100, i)) is None


def test_lower_band_touch_emits_buy() -> None:
    strat = BollingerBands(
        "BTC/USDT", TimeFrame.M1, period=5, num_std=Decimal("1.5")
    )
    # ровный шум вокруг 100, потом резкое падение → close < lower band
    prices = [100, 100, 100, 100, 100, 100, 100, 100, 60, 50]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    buys = [s for s in signals if s is not None and s.side is Side.BUY]
    assert len(buys) >= 1


def test_upper_band_touch_emits_sell_after_buy() -> None:
    strat = BollingerBands(
        "BTC/USDT", TimeFrame.M1, period=5, num_std=Decimal("1.5")
    )
    prices = [100, 100, 100, 100, 100, 60, 50, 140, 150, 160]
    signals = [strat.on_candle(_candle(p, i)) for i, p in enumerate(prices)]
    sides = [s.side for s in signals if s is not None]
    assert Side.BUY in sides
    assert Side.SELL in sides


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError):
        BollingerBands("BTC/USDT", TimeFrame.M1, period=1)
    with pytest.raises(ValueError):
        BollingerBands("BTC/USDT", TimeFrame.M1, num_std=Decimal("0"))
    with pytest.raises(ValueError):
        BollingerBands("BTC/USDT", TimeFrame.M1, order_size=Decimal("0"))
