import asyncio
import json
import uuid
from collections.abc import Mapping
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from application.events import BALANCE_UPDATE, ENGINE_STATUS
from domain.interfaces import EventBus
from domain.models import Balance
from infrastructure.state_manager import StateManager


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def event_bus() -> AsyncMock:
    return AsyncMock(spec=EventBus)


def _running(
    bot_id: uuid.UUID,
    balances: Mapping[str, Balance],
    symbol: str = "BTC/USDT",
) -> MagicMock:
    running = MagicMock()
    running.bot_id = bot_id
    running.credential_id = uuid.uuid4()
    running.symbol = symbol
    running.strategy = MagicMock()
    running.strategy.name = "SmaCross"
    running.adapter = AsyncMock()
    running.adapter.get_balance = AsyncMock(return_value=balances)
    running.adapter.get_positions = AsyncMock(return_value=[])
    return running


async def test_heartbeat_publishes_engine_status(
    redis: fakeredis.aioredis.FakeRedis, event_bus: AsyncMock
) -> None:
    orchestrator = MagicMock()
    orchestrator.active_bot_ids = ["aaa", "bbb"]
    orchestrator.iter_running = MagicMock(return_value=[])

    sm = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=1,
        balance_poll_interval_sec=60,
        snapshot_ttl_sec=10,
    )

    task = asyncio.create_task(sm.run())
    await asyncio.sleep(0.05)  # дать первому heartbeat'у пройти
    sm.stop()
    await asyncio.wait_for(task, timeout=2.0)

    heartbeat_calls = [
        call.args for call in event_bus.publish.call_args_list if call.args[0] == ENGINE_STATUS
    ]
    assert len(heartbeat_calls) >= 1
    _, payload = heartbeat_calls[0]
    assert payload["active_bots"] == ["aaa", "bbb"]
    assert "uptime_sec" in payload
    assert "timestamp" in payload


async def test_balance_poll_publishes_per_bot(
    redis: fakeredis.aioredis.FakeRedis, event_bus: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    bal: Mapping[str, Balance] = {
        "USDT": Balance("USDT", Decimal("100.5"), Decimal("0"), Decimal("100.5")),
        "BTC": Balance("BTC", Decimal("0.001"), Decimal("0"), Decimal("0.001")),
    }
    running = _running(bot_id, bal)
    orchestrator = MagicMock()
    orchestrator.active_bot_ids = [str(bot_id)]
    orchestrator.iter_running = MagicMock(return_value=[running])

    sm = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=60,
        balance_poll_interval_sec=1,
        snapshot_ttl_sec=10,
    )

    task = asyncio.create_task(sm.run())
    await asyncio.sleep(0.05)
    sm.stop()
    await asyncio.wait_for(task, timeout=2.0)

    balance_calls = [
        call.args for call in event_bus.publish.call_args_list if call.args[0] == BALANCE_UPDATE
    ]
    assert len(balance_calls) >= 1
    _, payload = balance_calls[0]
    assert payload["bot_id"] == str(bot_id)
    assert payload["credential_id"] == str(running.credential_id)
    assert payload["balances"]["USDT"]["free"] == "100.5"
    assert payload["balances"]["BTC"]["total"] == "0.001"


async def test_snapshot_writes_to_redis(
    redis: fakeredis.aioredis.FakeRedis, event_bus: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    running = _running(bot_id, {})
    orchestrator = MagicMock()
    orchestrator.active_bot_ids = [str(bot_id)]
    orchestrator.iter_running = MagicMock(return_value=[running])

    sm = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=60,
        balance_poll_interval_sec=60,
        snapshot_ttl_sec=30,
    )

    task = asyncio.create_task(sm.run())
    await asyncio.sleep(0.05)
    sm.stop()
    await asyncio.wait_for(task, timeout=2.0)

    raw = await redis.get(f"engine:state:{bot_id}")
    assert raw is not None
    snapshot = json.loads(raw)
    assert snapshot["bot_id"] == str(bot_id)
    assert snapshot["symbol"] == "BTC/USDT"
    assert snapshot["strategy_name"] == "SmaCross"


async def test_balance_poll_failure_does_not_kill_loop(
    redis: fakeredis.aioredis.FakeRedis, event_bus: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    running = _running(bot_id, {})
    running.adapter.get_balance.side_effect = [
        RuntimeError("ws down"),
        {"USDT": Balance("USDT", Decimal("50"), Decimal("0"), Decimal("50"))},
    ]
    orchestrator = MagicMock()
    orchestrator.active_bot_ids = [str(bot_id)]
    orchestrator.iter_running = MagicMock(return_value=[running])

    sm = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=60,
        balance_poll_interval_sec=1,
        snapshot_ttl_sec=10,
    )

    task = asyncio.create_task(sm.run())
    await asyncio.sleep(1.2)  # ждём 2 итерации
    sm.stop()
    await asyncio.wait_for(task, timeout=3.0)

    balance_calls = [
        c for c in event_bus.publish.call_args_list if c.args[0] == BALANCE_UPDATE
    ]
    # Второй вызов после успешного fetch должен дать publish
    assert len(balance_calls) >= 1


async def test_no_active_bots_means_no_balance_publish(
    redis: fakeredis.aioredis.FakeRedis, event_bus: AsyncMock
) -> None:
    orchestrator = MagicMock()
    orchestrator.active_bot_ids = []
    orchestrator.iter_running = MagicMock(return_value=[])

    sm = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=60,
        balance_poll_interval_sec=1,
        snapshot_ttl_sec=10,
    )

    task = asyncio.create_task(sm.run())
    await asyncio.sleep(0.05)
    sm.stop()
    await asyncio.wait_for(task, timeout=2.0)

    balance_calls = [
        c for c in event_bus.publish.call_args_list if c.args[0] == BALANCE_UPDATE
    ]
    assert balance_calls == []
