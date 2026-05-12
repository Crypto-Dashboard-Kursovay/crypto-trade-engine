import json
from collections.abc import Mapping
from typing import Any

from redis.asyncio import Redis

from domain.interfaces import EventBus

from .logging import get_logger

logger = get_logger(__name__)


class RedisEventBus(EventBus):
    """Реализация EventBus поверх Redis Pub/Sub.

    Decimal и datetime сериализуются как строки через `default=str`, чтобы
    бэк получал точные значения без потери точности.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, channel: str, payload: Mapping[str, Any]) -> None:
        message = json.dumps(payload, default=str, ensure_ascii=False)
        receivers = await self._redis.publish(channel, message)
        logger.debug(
            "event_published",
            channel=channel,
            receivers=receivers,
            size_bytes=len(message),
        )
