from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.enums import OrderStatus, OrderType, Side, TimeFrame
from domain.exceptions import OrderExecutionError
from domain.models import Candle
from infrastructure.simulated_exchange import SimulatedExchangeAdapter


def _candle(close: float, high: float | None = None, low: float | None = None, n: int = 0) -> Candle:
    c = Decimal(str(close))
    h = Decimal(str(high if high is not None else close))
    l = Decimal(str(low if low is not None else close))
    return Candle(
        symbol="BTC/USDT",
        timeframe=TimeFrame.M1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n),
        open=c,
        high=h,
        low=l,
        close=c,
        volume=Decimal("1"),
    )


async def test_market_buy_debits_quote_and_credits_base() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("10000")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0.001"),
        slippage=Decimal("0"),
    )
    ex.on_candle(_candle(60000))  # обновляем last_close
    order = await ex.create_order("BTC/USDT", Side.BUY, OrderType.MARKET, Decimal("0.01"))
    assert order.status is OrderStatus.FILLED
    bal = await ex.get_balance()
    # quote: 10000 - 0.01*60000 - 0.001*0.01*60000 = 10000 - 600 - 0.6 = 9399.4
    assert bal["USDT"].free == Decimal("9399.4")
    assert bal["BTC"].free == Decimal("0.01")


async def test_market_sell_credits_quote_and_debits_base() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("0"), "BTC": Decimal("1")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0.001"),
        slippage=Decimal("0"),
    )
    ex.on_candle(_candle(60000))
    await ex.create_order("BTC/USDT", Side.SELL, OrderType.MARKET, Decimal("0.5"))
    bal = await ex.get_balance()
    # quote: 0 + 0.5*60000 - 0.001*0.5*60000 = 30000 - 30 = 29970
    assert bal["USDT"].free == Decimal("29970")
    assert bal["BTC"].free == Decimal("0.5")


async def test_slippage_applied_on_market_buy() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("10000")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0"),
        slippage=Decimal("0.01"),  # 1%
    )
    ex.on_candle(_candle(60000))
    order = await ex.create_order("BTC/USDT", Side.BUY, OrderType.MARKET, Decimal("0.01"))
    # fill price = 60000 * 1.01 = 60600
    assert order.price == Decimal("60600.00")


async def test_limit_buy_fills_when_price_in_range() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("10000")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0"),
        slippage=Decimal("0"),
    )
    ex.on_candle(_candle(60000))
    order = await ex.create_order(
        "BTC/USDT", Side.BUY, OrderType.LIMIT, Decimal("0.01"), price=Decimal("59000")
    )
    assert order.status is OrderStatus.OPEN
    # следующая свеча с low<=59000<=high → fill
    fills = ex.on_candle(_candle(close=60000, high=60500, low=58000, n=1))
    assert len(fills) == 1
    bal = await ex.get_balance()
    assert bal["BTC"].free == Decimal("0.01")
    assert bal["USDT"].free == Decimal("10000") - Decimal("590")  # 0.01*59000


async def test_limit_not_filled_when_price_out_of_range() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("10000")},
        symbol="BTC/USDT",
    )
    ex.on_candle(_candle(60000))
    await ex.create_order(
        "BTC/USDT", Side.BUY, OrderType.LIMIT, Decimal("0.01"), price=Decimal("50000")
    )
    fills = ex.on_candle(_candle(close=60000, high=61000, low=59500, n=1))
    assert fills == []


async def test_insufficient_balance_raises() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("100")},
        symbol="BTC/USDT",
        fee_rate=Decimal("0"),
        slippage=Decimal("0"),
    )
    ex.on_candle(_candle(60000))
    with pytest.raises(OrderExecutionError):
        await ex.create_order("BTC/USDT", Side.BUY, OrderType.MARKET, Decimal("0.01"))


async def test_market_before_first_candle_raises() -> None:
    ex = SimulatedExchangeAdapter(
        initial_balance={"USDT": Decimal("10000")},
        symbol="BTC/USDT",
    )
    with pytest.raises(OrderExecutionError):
        await ex.create_order("BTC/USDT", Side.BUY, OrderType.MARKET, Decimal("0.01"))
