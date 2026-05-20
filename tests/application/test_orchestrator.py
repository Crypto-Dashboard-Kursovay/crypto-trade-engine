import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.orchestrator import (
    EngineOrchestrator,
    OrchestratorError,
    RiskConfig,
)
from domain.enums import TimeFrame
from domain.interfaces import EventBus, MarketDataProvider


def _bot(strategy_class: str = "SmaCross", params: dict[str, Any] | None = None) -> MagicMock:
    bot = MagicMock()
    bot.id = uuid.uuid4()
    bot.credential_id = uuid.uuid4()
    bot.strategy_class = strategy_class
    bot.symbol = "BTC/USDT"
    bot.timeframe = TimeFrame.M1
    bot.params = params or {"fast_period": 2, "slow_period": 4}
    return bot


def _cred() -> MagicMock:
    cred = MagicMock()
    cred.id = uuid.uuid4()
    cred.exchange = "binance"
    cred.api_key = "k"
    cred.api_secret = "s"
    return cred


async def _empty_stream() -> AsyncIterator[Any]:
    if False:
        yield  # pragma: no cover


def _market_data() -> MagicMock:
    md = MagicMock(spec=MarketDataProvider)
    md.subscribe = MagicMock(return_value=_empty_stream())
    return md


def _adapter() -> AsyncMock:
    a = AsyncMock()
    a.fetch_ohlcv = AsyncMock(return_value=[])
    a.create_order = AsyncMock()
    a.get_balance = AsyncMock(return_value={})
    a.get_positions = AsyncMock(return_value=[])
    a.close = AsyncMock()
    return a


@pytest.fixture
def event_bus() -> AsyncMock:
    return AsyncMock(spec=EventBus)


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(max_position_pct=Decimal("0.1"), min_lot=Decimal("0.0001"))


def _make_orchestrator(
    bot: Any,
    cred: Any,
    event_bus: AsyncMock,
    risk_config: RiskConfig,
    adapter: AsyncMock | None = None,
) -> tuple[EngineOrchestrator, AsyncMock, MagicMock]:
    adapter = adapter or _adapter()
    md = _market_data()

    bot_repo = MagicMock()
    bot_repo.get = AsyncMock(return_value=bot)
    cred_repo = MagicMock()
    cred_repo.get_decrypted = AsyncMock(return_value=cred)

    from strategies import default_registry

    orchestrator = EngineOrchestrator(
        bot_repo=bot_repo,
        credential_repo=cred_repo,
        strategy_registry=default_registry(),
        event_bus=event_bus,
        exchange_factory=lambda c: adapter,
        market_data_factory=lambda a: md,
        risk_config=risk_config,
    )
    return orchestrator, adapter, md


async def test_start_strategy_creates_running_task(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, adapter, _ = _make_orchestrator(bot, _cred(), event_bus, risk_config)

    await orch.start_strategy(bot.id)

    assert str(bot.id) in orch.active_bot_ids
    adapter.fetch_ohlcv.assert_awaited()  # warmup

    await orch.shutdown()


async def test_start_is_idempotent(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, adapter, _ = _make_orchestrator(bot, _cred(), event_bus, risk_config)

    await orch.start_strategy(bot.id)
    await orch.start_strategy(bot.id)  # второй вызов — no-op

    assert len(orch.active_bot_ids) == 1
    # fetch_ohlcv для warmup — только один раз
    assert adapter.fetch_ohlcv.await_count == 1

    await orch.shutdown()


async def test_stop_strategy_cancels_task_and_closes_adapter(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, adapter, _ = _make_orchestrator(bot, _cred(), event_bus, risk_config)
    await orch.start_strategy(bot.id)

    await orch.stop_strategy(bot.id)

    assert orch.active_bot_ids == []
    adapter.close.assert_awaited_once()


async def test_stop_unknown_bot_is_noop(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    orch, _, _ = _make_orchestrator(_bot(), _cred(), event_bus, risk_config)
    await orch.stop_strategy(uuid.uuid4())  # не запускали — no-op


async def test_start_raises_when_bot_not_found(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    orch, _, _ = _make_orchestrator(None, _cred(), event_bus, risk_config)
    with pytest.raises(OrchestratorError, match="Bot .* not found"):
        await orch.start_strategy(uuid.uuid4())


async def test_start_raises_when_credentials_missing(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, _, _ = _make_orchestrator(bot, None, event_bus, risk_config)
    with pytest.raises(OrchestratorError, match="Credential .* not found"):
        await orch.start_strategy(bot.id)


async def test_update_restarts_strategy(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, adapter, _ = _make_orchestrator(bot, _cred(), event_bus, risk_config)

    await orch.start_strategy(bot.id)
    await orch.update_strategy(bot.id)

    assert str(bot.id) in orch.active_bot_ids
    assert adapter.fetch_ohlcv.await_count == 2  # старт + рестарт

    await orch.shutdown()


async def test_shutdown_stops_all_strategies(
    event_bus: AsyncMock, risk_config: RiskConfig
) -> None:
    bot = _bot()
    orch, adapter, _ = _make_orchestrator(bot, _cred(), event_bus, risk_config)

    await orch.start_strategy(bot.id)
    await orch.shutdown()

    assert orch.active_bot_ids == []
    adapter.close.assert_awaited()
