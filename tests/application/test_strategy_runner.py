import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.events import STRATEGY_ERROR
from application.strategy_runner import StrategyRunner
from domain.enums import Side, TimeFrame
from domain.exceptions import OrderExecutionError, RiskRejectedError
from domain.models import Candle, Signal

_BOT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _candle(close: Decimal) -> Candle:
    return Candle(
        symbol="BTC/USDT",
        timeframe=TimeFrame.M5,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
    )


async def _stream(*candles: Candle) -> AsyncIterator[Candle]:
    for c in candles:
        yield c


def _strategy() -> MagicMock:
    s = MagicMock()
    s.name = "test_strategy"
    s.symbol = "BTC/USDT"
    s.timeframe = TimeFrame.M5
    s.startup_candle_count = 0
    s.on_start = AsyncMock(return_value=None)
    return s


@pytest.fixture
def signal() -> Signal:
    return Signal("test_strategy", "BTC/USDT", Side.BUY, Decimal("0.01"), Decimal("100"))


@pytest.fixture
def event_bus() -> AsyncMock:
    return AsyncMock()


async def test_signal_flows_through_risk_to_executor(
    event_bus: AsyncMock, signal: Signal
) -> None:
    candles = [_candle(Decimal("100")), _candle(Decimal("101")), _candle(Decimal("102"))]
    market_data = MagicMock()
    market_data.subscribe = MagicMock(return_value=_stream(*candles))

    strategy = _strategy()
    strategy.on_candle = MagicMock(side_effect=[None, None, signal])

    risk = AsyncMock()
    risk.check.return_value = signal
    executor = AsyncMock()

    runner = StrategyRunner(strategy, market_data, risk, executor, event_bus, bot_id=_BOT_ID)
    await runner.run()

    strategy.on_start.assert_awaited_once()
    market_data.subscribe.assert_called_once_with("BTC/USDT", TimeFrame.M5)
    assert strategy.on_candle.call_count == 3
    risk.check.assert_awaited_once_with(signal)
    executor.execute.assert_awaited_once_with(signal)
    event_bus.publish.assert_not_awaited()


async def test_no_signal_no_call(event_bus: AsyncMock) -> None:
    market_data = MagicMock()
    market_data.subscribe = MagicMock(return_value=_stream(_candle(Decimal("100"))))

    strategy = _strategy()
    strategy.on_candle = MagicMock(return_value=None)

    risk = AsyncMock()
    executor = AsyncMock()

    runner = StrategyRunner(strategy, market_data, risk, executor, event_bus, bot_id=_BOT_ID)
    await runner.run()

    risk.check.assert_not_awaited()
    executor.execute.assert_not_awaited()


async def test_risk_rejection_continues_loop_and_publishes_error(
    event_bus: AsyncMock, signal: Signal
) -> None:
    candles = [_candle(Decimal("100")), _candle(Decimal("101"))]
    market_data = MagicMock()
    market_data.subscribe = MagicMock(return_value=_stream(*candles))

    strategy = _strategy()
    strategy.on_candle = MagicMock(side_effect=[signal, None])

    risk = AsyncMock()
    risk.check.side_effect = RiskRejectedError("not enough balance")
    executor = AsyncMock()

    runner = StrategyRunner(strategy, market_data, risk, executor, event_bus, bot_id=_BOT_ID)
    await runner.run()

    executor.execute.assert_not_awaited()
    event_bus.publish.assert_awaited_once()
    channel, payload = event_bus.publish.call_args.args
    assert channel == STRATEGY_ERROR
    assert payload["kind"] == "risk_rejected"
    assert payload["strategy"] == "test_strategy"
    assert payload["bot_id"] == str(_BOT_ID)
    # second candle was still consumed
    assert strategy.on_candle.call_count == 2


async def test_execution_error_publishes_strategy_error(
    event_bus: AsyncMock, signal: Signal
) -> None:
    market_data = MagicMock()
    market_data.subscribe = MagicMock(return_value=_stream(_candle(Decimal("100"))))

    strategy = _strategy()
    strategy.on_candle = MagicMock(return_value=signal)

    risk = AsyncMock()
    risk.check.return_value = signal
    executor = AsyncMock()
    executor.execute.side_effect = OrderExecutionError("api down")

    runner = StrategyRunner(strategy, market_data, risk, executor, event_bus, bot_id=_BOT_ID)
    await runner.run()

    event_bus.publish.assert_awaited_once()
    channel, payload = event_bus.publish.call_args.args
    assert channel == STRATEGY_ERROR
    assert payload["kind"] == "execution_failed"
    assert payload["bot_id"] == str(_BOT_ID)


async def test_unexpected_exception_propagates(event_bus: AsyncMock, signal: Signal) -> None:
    """RuntimeError isn't a known domain error — it should bubble up, not be swallowed."""
    market_data = MagicMock()
    market_data.subscribe = MagicMock(return_value=_stream(_candle(Decimal("100"))))

    strategy = _strategy()
    strategy.on_candle = MagicMock(return_value=signal)

    risk = AsyncMock()
    risk.check.side_effect = RuntimeError("bug in risk manager")
    executor = AsyncMock()

    runner = StrategyRunner(strategy, market_data, risk, executor, event_bus, bot_id=_BOT_ID)
    with pytest.raises(RuntimeError, match="bug in risk manager"):
        await runner.run()
