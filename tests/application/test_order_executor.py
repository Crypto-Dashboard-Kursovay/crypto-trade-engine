import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from application.events import NEW_TRADE
from application.order_executor import OrderExecutor
from domain.enums import OrderStatus, OrderType, Side
from domain.exceptions import OrderExecutionError
from domain.interfaces import EventBus, ExchangeAdapter
from domain.models import Order, Signal

_BOT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _order() -> Order:
    return Order(
        order_id="ord-1",
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.LIMIT,
        size=Decimal("0.01"),
        status=OrderStatus.NEW,
        price=Decimal("100"),
    )


@pytest.fixture
def adapter() -> AsyncMock:
    a = AsyncMock(spec=ExchangeAdapter)
    a.create_order.return_value = _order()
    return a


@pytest.fixture
def event_bus() -> AsyncMock:
    return AsyncMock(spec=EventBus)


@pytest.fixture
def executor(adapter: AsyncMock, event_bus: AsyncMock) -> OrderExecutor:
    return OrderExecutor(adapter=adapter, event_bus=event_bus, bot_id=_BOT_ID)


async def test_limit_order_publishes_new_trade(
    executor: OrderExecutor, adapter: AsyncMock, event_bus: AsyncMock
) -> None:
    signal = Signal("strat", "BTC/USDT", Side.BUY, Decimal("0.01"), Decimal("100"))
    order = await executor.execute(signal)

    assert order.order_id == "ord-1"
    adapter.create_order.assert_awaited_once_with(
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.LIMIT,
        size=Decimal("0.01"),
        price=Decimal("100"),
    )
    event_bus.publish.assert_awaited_once()
    channel, payload = event_bus.publish.call_args.args
    assert channel == NEW_TRADE
    assert payload["bot_id"] == str(_BOT_ID)
    assert payload["order_id"] == "ord-1"
    assert payload["strategy"] == "strat"
    assert payload["side"] == "buy"
    assert payload["size"] == "0.01"


async def test_market_order_when_signal_has_no_price(
    executor: OrderExecutor, adapter: AsyncMock
) -> None:
    signal = Signal("strat", "BTC/USDT", Side.BUY, Decimal("0.01"))
    await executor.execute(signal)
    kwargs = adapter.create_order.call_args.kwargs
    assert kwargs["type"] == OrderType.MARKET
    assert kwargs["price"] is None


async def test_adapter_failure_raises_execution_error_no_event(
    executor: OrderExecutor, adapter: AsyncMock, event_bus: AsyncMock
) -> None:
    adapter.create_order.side_effect = RuntimeError("network down")
    signal = Signal("strat", "BTC/USDT", Side.BUY, Decimal("0.01"), Decimal("100"))
    with pytest.raises(OrderExecutionError, match="failed to create order"):
        await executor.execute(signal)
    event_bus.publish.assert_not_awaited()
