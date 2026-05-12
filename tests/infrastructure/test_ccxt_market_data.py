import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from domain.enums import TimeFrame
from infrastructure.ccxt_market_data import CCXTMarketDataProvider


def _row(ts_ms: int, close: float = 100.0) -> list[Any]:
    return [ts_ms, 100.0, 101.0, 99.0, close, 1.0]


class _FakeExchange:
    """Имитирует ccxt.pro exchange.watch_ohlcv: список ответов или исключений."""

    def __init__(self, scenario: list[Any]) -> None:
        self._scenario = scenario
        self._idx = 0
        self.calls: list[tuple[str, str]] = []

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[Any]]:
        self.calls.append((symbol, timeframe))
        if self._idx >= len(self._scenario):
            # после сценария — бесконечно ждём, чтобы тест мог отменить таск
            await asyncio.sleep(3600)
        item = self._scenario[self._idx]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]


async def _take(stream: AsyncIterator[Any], n: int, timeout: float = 2.0) -> list[Any]:
    out: list[Any] = []
    async with asyncio.timeout(timeout):
        async for item in stream:
            out.append(item)
            if len(out) >= n:
                return out
    return out


async def test_subscribe_yields_normalized_candles() -> None:
    ex = _FakeExchange(scenario=[[_row(1735689600000, close=100.5)]])
    provider = CCXTMarketDataProvider(ex)

    candles = await _take(provider.subscribe("BTC/USDT", TimeFrame.M1), 1)

    assert len(candles) == 1
    assert candles[0].symbol == "BTC/USDT"
    assert candles[0].timeframe is TimeFrame.M1
    assert candles[0].close == Decimal("100.5")
    assert ex.calls == [("BTC/USDT", "1m")]


async def test_dedupes_same_timestamp() -> None:
    """Если ccxt.pro прислал ту же свечу дважды (промежуточный апдейт) — отдаём раз."""
    ex = _FakeExchange(
        scenario=[
            [_row(1735689600000, close=100.0)],
            [_row(1735689600000, close=100.5)],  # тот же ts → пропускаем
            [_row(1735689660000, close=101.0)],
        ]
    )
    provider = CCXTMarketDataProvider(ex)

    candles = await _take(provider.subscribe("BTC/USDT", TimeFrame.M1), 2)

    assert len(candles) == 2
    assert candles[0].timestamp != candles[1].timestamp


async def test_reconnects_on_error_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """При исключении watch_ohlcv логируем и ждём, потом повторяем."""
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("infrastructure.ccxt_market_data.asyncio.sleep", fake_sleep)

    ex = _FakeExchange(
        scenario=[
            ConnectionError("ws drop"),
            ConnectionError("still down"),
            [_row(1735689600000)],
        ]
    )
    provider = CCXTMarketDataProvider(ex)

    candles = await _take(provider.subscribe("BTC/USDT", TimeFrame.M1), 1)

    assert len(candles) == 1
    # первый retry — backoff 1.0, второй — 2.0 (экспонента)
    assert sleep_calls == [1.0, 2.0]


async def test_cancellation_propagates() -> None:
    ex = MagicMock()

    async def slow_watch(*a: Any, **kw: Any) -> list[Any]:
        await asyncio.sleep(10)
        return []

    ex.watch_ohlcv = slow_watch
    provider = CCXTMarketDataProvider(ex)

    async def consume() -> None:
        async for _ in provider.subscribe("BTC/USDT", TimeFrame.M1):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
