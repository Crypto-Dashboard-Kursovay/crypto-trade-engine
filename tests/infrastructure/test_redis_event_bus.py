import json
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import pytest

from infrastructure.redis_event_bus import RedisEventBus


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def bus(redis: fakeredis.aioredis.FakeRedis) -> RedisEventBus:
    return RedisEventBus(redis)


async def test_publish_serializes_dict_payload(
    bus: RedisEventBus, redis: fakeredis.aioredis.FakeRedis
) -> None:
    pubsub = redis.pubsub()
    await pubsub.subscribe("engine.test")
    await _drain_subscribe_ack(pubsub)

    await bus.publish("engine.test", {"foo": "bar", "n": 42})

    msg = await _next_message(pubsub)
    assert msg["channel"] == b"engine.test"
    assert json.loads(msg["data"]) == {"foo": "bar", "n": 42}


async def test_publish_decimal_as_string(
    bus: RedisEventBus, redis: fakeredis.aioredis.FakeRedis
) -> None:
    pubsub = redis.pubsub()
    await pubsub.subscribe("engine.test")
    await _drain_subscribe_ack(pubsub)

    await bus.publish("engine.test", {"price": Decimal("0.001"), "size": Decimal("12345.6789")})

    msg = await _next_message(pubsub)
    parsed = json.loads(msg["data"])
    assert parsed == {"price": "0.001", "size": "12345.6789"}


async def test_publish_datetime_as_string(
    bus: RedisEventBus, redis: fakeredis.aioredis.FakeRedis
) -> None:
    pubsub = redis.pubsub()
    await pubsub.subscribe("engine.test")
    await _drain_subscribe_ack(pubsub)

    ts = datetime(2026, 5, 12, 10, 30, 0, tzinfo=timezone.utc)
    await bus.publish("engine.test", {"timestamp": ts})

    msg = await _next_message(pubsub)
    parsed = json.loads(msg["data"])
    assert parsed["timestamp"] == "2026-05-12 10:30:00+00:00"


async def test_publish_returns_when_no_subscribers(bus: RedisEventBus) -> None:
    # Не должно падать если никто не подписан — Redis publish вернёт 0 receivers.
    await bus.publish("engine.no_one", {"x": 1})


async def _drain_subscribe_ack(pubsub: object) -> None:
    # Первое сообщение от subscribe — это ack типа "subscribe", его пропускаем.
    while True:
        msg = await pubsub.get_message(ignore_subscribe_messages=False, timeout=1.0)  # type: ignore[attr-defined]
        if msg is None:
            return
        if msg["type"] == "subscribe":
            return


async def _next_message(pubsub: object, timeout: float = 1.0) -> dict[str, object]:
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)  # type: ignore[attr-defined]
    assert msg is not None, "no message received"
    return msg  # type: ignore[no-any-return]
