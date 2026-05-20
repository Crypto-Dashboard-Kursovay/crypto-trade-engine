import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from application.events import COMMAND_START, COMMAND_STOP, COMMAND_UPDATE
from infrastructure.command_listener import CommandListener


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def orchestrator() -> AsyncMock:
    o = AsyncMock()
    o.start_strategy = AsyncMock()
    o.stop_strategy = AsyncMock()
    o.update_strategy = AsyncMock()
    return o


async def _run_listener(
    redis: fakeredis.aioredis.FakeRedis,
    orchestrator: AsyncMock,
    publish: list[tuple[str, dict[str, str]]],
    settle_for: float = 0.3,
) -> CommandListener:
    listener = CommandListener(redis, orchestrator, dedup_ttl_sec=60)
    task = asyncio.create_task(listener.run())

    # Дать listener'у время подписаться, иначе ранние publish теряются.
    await asyncio.sleep(0.05)
    for channel, payload in publish:
        await redis.publish(channel, json.dumps(payload))
    await asyncio.sleep(settle_for)

    listener.stop()
    await asyncio.wait_for(task, timeout=2.0)
    return listener


async def test_dispatches_start_command(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    await _run_listener(
        redis,
        orchestrator,
        [(COMMAND_START, {"command_id": str(cmd_id), "bot_id": str(bot_id)})],
    )
    orchestrator.start_strategy.assert_awaited_once_with(bot_id)


async def test_dispatches_stop_command(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    await _run_listener(
        redis,
        orchestrator,
        [(COMMAND_STOP, {"command_id": str(cmd_id), "bot_id": str(bot_id)})],
    )
    orchestrator.stop_strategy.assert_awaited_once_with(bot_id)


async def test_dispatches_update_command(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    payload = {"command_id": str(cmd_id), "bot_id": str(bot_id)}
    await _run_listener(
        redis,
        orchestrator,
        [(COMMAND_UPDATE, payload)],
    )
    orchestrator.update_strategy.assert_awaited_once_with(bot_id, payload)


async def test_duplicate_command_id_is_skipped(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    bot_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    same = {"command_id": str(cmd_id), "bot_id": str(bot_id)}
    await _run_listener(
        redis,
        orchestrator,
        [(COMMAND_START, same), (COMMAND_START, same)],
    )
    # Despite two publishes, обработали только раз.
    orchestrator.start_strategy.assert_awaited_once()
    # Ключ дедупа должен быть в Redis с TTL.
    assert await redis.get(f"engine:commands:processed:{cmd_id}") is not None


async def test_missing_required_fields_logged_not_dispatched(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    await _run_listener(
        redis,
        orchestrator,
        [
            (COMMAND_START, {"bot_id": str(uuid.uuid4())}),  # no command_id
            (COMMAND_START, {"command_id": str(uuid.uuid4())}),  # no bot_id
        ],
    )
    orchestrator.start_strategy.assert_not_awaited()


async def test_invalid_json_is_tolerated(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    listener = CommandListener(redis, orchestrator, dedup_ttl_sec=60)
    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.05)
    await redis.publish(COMMAND_START, "not-json-at-all")
    await redis.publish(
        COMMAND_START,
        json.dumps({"command_id": str(uuid.uuid4()), "bot_id": str(uuid.uuid4())}),
    )
    await asyncio.sleep(0.3)
    listener.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # Listener выжил после плохого JSON и обработал следующее сообщение.
    orchestrator.start_strategy.assert_awaited_once()


async def test_handler_failure_does_not_kill_listener(
    redis: fakeredis.aioredis.FakeRedis, orchestrator: AsyncMock
) -> None:
    orchestrator.start_strategy.side_effect = [
        RuntimeError("boom"),
        None,
    ]
    bot_id_1 = uuid.uuid4()
    bot_id_2 = uuid.uuid4()
    await _run_listener(
        redis,
        orchestrator,
        [
            (COMMAND_START, {"command_id": str(uuid.uuid4()), "bot_id": str(bot_id_1)}),
            (COMMAND_START, {"command_id": str(uuid.uuid4()), "bot_id": str(bot_id_2)}),
        ],
    )
    # Оба сообщения попали в handler, listener не упал.
    assert orchestrator.start_strategy.await_count == 2
