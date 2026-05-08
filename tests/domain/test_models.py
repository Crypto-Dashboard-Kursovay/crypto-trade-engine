from datetime import datetime, timezone
from decimal import Decimal

import pytest

from domain.enums import OrderStatus, OrderType, Side, TimeFrame
from domain.models import Balance, Candle, Order, Position, Signal

TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


class TestCandle:
    def test_valid_candle(self) -> None:
        c = Candle(
            "BTC/USDT", TimeFrame.M5, TS,
            Decimal("100"), Decimal("110"), Decimal("95"),
            Decimal("105"), Decimal("1.5"),
        )
        assert c.close == Decimal("105")

    def test_high_below_low_rejected(self) -> None:
        with pytest.raises(ValueError, match="high"):
            Candle(
                "X", TimeFrame.M1, TS,
                Decimal("1"), Decimal("0.5"), Decimal("1"),
                Decimal("1"), Decimal("0"),
            )

    def test_high_below_close_rejected(self) -> None:
        with pytest.raises(ValueError, match="high"):
            Candle(
                "X", TimeFrame.M1, TS,
                Decimal("1"), Decimal("1"), Decimal("1"),
                Decimal("2"), Decimal("0"),
            )

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            Candle(
                "X", TimeFrame.M1, TS,
                Decimal("1"), Decimal("1"), Decimal("1"),
                Decimal("1"), Decimal("-1"),
            )


class TestSignal:
    def test_valid(self) -> None:
        s = Signal("st", "BTC/USDT", Side.BUY, Decimal("1"), Decimal("100"))
        assert s.size == Decimal("1")
        assert s.metadata == {}

    def test_zero_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="size"):
            Signal("st", "X", Side.BUY, Decimal("0"))

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="price"):
            Signal("st", "X", Side.BUY, Decimal("1"), Decimal("-1"))


class TestOrder:
    def test_market_order_no_price(self) -> None:
        o = Order(
            "1", "X", Side.BUY, OrderType.MARKET,
            Decimal("1"), OrderStatus.NEW,
        )
        assert o.price is None

    def test_limit_requires_price(self) -> None:
        with pytest.raises(ValueError, match="Limit order requires"):
            Order(
                "1", "X", Side.BUY, OrderType.LIMIT,
                Decimal("1"), OrderStatus.NEW,
            )

    def test_filled_exceeds_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot exceed"):
            Order(
                "1", "X", Side.BUY, OrderType.MARKET,
                Decimal("1"), OrderStatus.FILLED,
                filled_size=Decimal("2"),
            )


class TestBalance:
    def test_valid(self) -> None:
        b = Balance("USDT", Decimal("100"), Decimal("50"), Decimal("150"))
        assert b.total == Decimal("150")

    def test_components_must_sum_to_total(self) -> None:
        with pytest.raises(ValueError, match="total"):
            Balance("USDT", Decimal("100"), Decimal("50"), Decimal("200"))

    def test_negative_component_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Balance("USDT", Decimal("-1"), Decimal("0"), Decimal("-1"))


class TestPosition:
    def test_zero_entry_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="entry_price"):
            Position("X", Side.BUY, Decimal("0"), Decimal("1"), Decimal("0"))
