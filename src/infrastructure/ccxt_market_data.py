import asyncio
from collections.abc import AsyncIterator
from typing import Any

from domain.enums import TimeFrame
from domain.interfaces import MarketDataProvider
from domain.models import Candle

from .ccxt_exchange_adapter import _to_candle
from .logging import get_logger

logger = get_logger(__name__)


class CCXTMarketDataProvider(MarketDataProvider):
    """Подписка на live-свечи через ccxt.pro `watch_ohlcv`.

    При обрыве WS — реконнект с экспоненциальным backoff. Не падает.
    Останавливается через `cancel()` снаружи (asyncio.Task.cancel()).

    Дедуп: одна свеча с одним timestamp выдаётся один раз. Это важно, потому что
    ccxt.pro может слать промежуточные обновления текущей свечи (на каждый тик).
    Стратегия должна работать на закрытых свечах — поэтому отдаём только
    «новый timestamp», старые игнорим.
    """

    _MIN_BACKOFF_SEC = 1.0
    _MAX_BACKOFF_SEC = 30.0

    def __init__(self, exchange: Any) -> None:
        self._exchange = exchange

    def subscribe(
        self, symbol: str, timeframe: TimeFrame
    ) -> AsyncIterator[Candle]:
        return self._stream(symbol, timeframe)

    async def _stream(
        self, symbol: str, timeframe: TimeFrame
    ) -> AsyncIterator[Candle]:
        backoff = self._MIN_BACKOFF_SEC
        last_ts_ms: int | None = None

        while True:
            try:
                rows = await self._exchange.watch_ohlcv(symbol, str(timeframe))
                backoff = self._MIN_BACKOFF_SEC  # успех → сбрасываем backoff

                # rows: list[[ts, open, high, low, close, volume], ...]
                for row in rows:
                    ts_ms = int(row[0])
                    if last_ts_ms is not None and ts_ms <= last_ts_ms:
                        continue
                    last_ts_ms = ts_ms
                    yield _to_candle(symbol, timeframe, row)
            except asyncio.CancelledError:
                logger.info("market_data_cancelled", symbol=symbol)
                raise
            except Exception as exc:
                logger.warning(
                    "market_data_reconnect",
                    symbol=symbol,
                    timeframe=str(timeframe),
                    error=str(exc),
                    backoff_sec=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF_SEC)
