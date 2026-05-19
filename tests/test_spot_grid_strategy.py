from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import Side, TimeFrame
from domain.models import Candle
from strategies.spot_grid import SpotGridStrategy


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


def test_first_candle_initializes_cell() -> None:
    strat = SpotGridStrategy(
        "BTC/USDT",
        TimeFrame.M1,
        price_low=Decimal("100"),
        price_high=Decimal("200"),
        num_levels=5,
    )
    assert strat.on_candle(_candle(150, 0)) is None


def test_price_drop_emits_buy_per_cell() -> None:
    strat = SpotGridStrategy(
        "BTC/USDT",
        TimeFrame.M1,
        price_low=Decimal("100"),
        price_high=Decimal("200"),
        num_levels=5,           # уровни: 100, 125, 150, 175, 200
        base_per_level=Decimal("0.01"),
    )
    strat.on_candle(_candle(180, 0))  # init cell=3
    # цена падает до 105 — переход на cell=0, diff=-3 → 3 BUY
    strat.on_candle(_candle(105, 1))  # первый BUY
    s2 = strat.on_candle(_candle(105, 2))  # второй BUY (из очереди)
    s3 = strat.on_candle(_candle(105, 3))  # третий BUY
    s4 = strat.on_candle(_candle(105, 4))  # больше нет в очереди
    assert s2 is not None and s2.side is Side.BUY
    assert s3 is not None and s3.side is Side.BUY
    assert s4 is None


def test_price_rise_emits_sell_per_cell() -> None:
    strat = SpotGridStrategy(
        "BTC/USDT",
        TimeFrame.M1,
        price_low=Decimal("100"),
        price_high=Decimal("200"),
        num_levels=5,
    )
    strat.on_candle(_candle(110, 0))  # init cell=0
    # цена 190 — переход cell=3, diff=+3 → 3 SELL
    s1 = strat.on_candle(_candle(190, 1))
    s2 = strat.on_candle(_candle(190, 2))
    s3 = strat.on_candle(_candle(190, 3))
    assert s1 is not None and s1.side is Side.SELL
    assert s2 is not None and s2.side is Side.SELL
    assert s3 is not None and s3.side is Side.SELL


def test_size_per_signal_equals_base_per_level() -> None:
    strat = SpotGridStrategy(
        "BTC/USDT",
        TimeFrame.M1,
        price_low=Decimal("100"),
        price_high=Decimal("200"),
        num_levels=3,
        base_per_level=Decimal("0.5"),
    )
    strat.on_candle(_candle(110, 0))
    s = strat.on_candle(_candle(190, 1))
    assert s is not None
    assert s.size == Decimal("0.5")


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError):
        SpotGridStrategy(
            "BTC/USDT", TimeFrame.M1,
            price_low=Decimal("200"), price_high=Decimal("100"),
        )
    with pytest.raises(ValueError):
        SpotGridStrategy(
            "BTC/USDT", TimeFrame.M1,
            price_low=Decimal("100"), price_high=Decimal("200"), num_levels=1,
        )
    with pytest.raises(ValueError):
        SpotGridStrategy(
            "BTC/USDT", TimeFrame.M1,
            price_low=Decimal("0"), price_high=Decimal("100"),
        )
