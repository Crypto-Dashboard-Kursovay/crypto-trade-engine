from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import Any

from .enums import OrderType, Side, TimeFrame
from .models import Balance, Candle, Order, Signal


class ExchangeAdapter(ABC):
    """Async port to an exchange. Concrete adapters wrap CCXT (Binance for now)."""

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: TimeFrame,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: Side,
        type: OrderType,
        size: Decimal,
        price: Decimal | None = None,
    ) -> Order: ...

    @abstractmethod
    async def get_balance(self) -> Mapping[str, Balance]: ...

    async def close(self) -> None:
        """Close any held resources (network sessions etc.). Default: no-op."""
        return None


class MarketDataProvider(ABC):
    """Async source of OHLCV candles. WS in live, CSV/Parquet in backtest."""

    @abstractmethod
    def subscribe(
        self, symbol: str, timeframe: TimeFrame
    ) -> AsyncIterator[Candle]: ...


class Strategy(ABC):
    """Trading strategy. One instance per (symbol, timeframe).

    Implementations own their rolling history (e.g. collections.deque(maxlen=N))
    and compute indicators in `on_candle` synchronously — runner stays async.
    """

    name: str
    symbol: str
    timeframe: TimeFrame
    startup_candle_count: int = 0

    async def on_start(self) -> None:
        """One-time setup hook. Default: no-op."""
        return None

    @abstractmethod
    def on_candle(self, candle: Candle) -> Signal | None: ...


class EventBus(ABC):
    """Pub/Sub port. In live wraps Redis Pub/Sub, in tests an in-memory bus."""

    @abstractmethod
    async def publish(self, channel: str, payload: Mapping[str, Any]) -> None: ...
