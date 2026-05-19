"""In-memory EventBus для тестов и backtest'а.

Аналог RedisEventBus, но без сети: publish → синхронно вызывает зарегистрированные
handler'ы для канала. Все события также накапливаются в self.history, чтобы тесты
могли проверить что было опубликовано.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from domain.interfaces import EventBus

Handler = Callable[[Mapping[str, Any]], Awaitable[None] | None]


class InMemoryEventBus(EventBus):
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self.history: list[tuple[str, Mapping[str, Any]]] = []

    def subscribe(self, channel: str, handler: Handler) -> None:
        self._handlers[channel].append(handler)

    async def publish(self, channel: str, payload: Mapping[str, Any]) -> None:
        self.history.append((channel, dict(payload)))
        for handler in self._handlers.get(channel, []):
            result = handler(payload)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
