from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .enums import OrderStatus, OrderType, Side, TimeFrame

_EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: TimeFrame
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"Candle high ({self.high}) < low ({self.low})")
        if self.high < self.open or self.high < self.close:
            raise ValueError("Candle high must be >= open and close")
        if self.low > self.open or self.low > self.close:
            raise ValueError("Candle low must be <= open and close")
        if self.volume < 0:
            raise ValueError(f"Candle volume must be non-negative, got {self.volume}")


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_name: str
    symbol: str
    side: Side
    size: Decimal
    price: Decimal | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_METADATA)

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError(f"Signal size must be > 0, got {self.size}")
        if self.price is not None and self.price <= 0:
            raise ValueError(f"Signal price must be > 0 if set, got {self.price}")


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    side: Side
    entry_price: Decimal
    size: Decimal
    current_pnl: Decimal

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError(f"Position entry_price must be > 0, got {self.entry_price}")
        if self.size <= 0:
            raise ValueError(f"Position size must be > 0, got {self.size}")


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    symbol: str
    side: Side
    type: OrderType
    size: Decimal
    status: OrderStatus
    filled_size: Decimal = Decimal("0")
    price: Decimal | None = None
    fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError(f"Order size must be > 0, got {self.size}")
        if self.filled_size < 0:
            raise ValueError(f"Order filled_size must be >= 0, got {self.filled_size}")
        if self.filled_size > self.size:
            raise ValueError(
                f"Order filled_size ({self.filled_size}) cannot exceed size ({self.size})"
            )
        if self.price is not None and self.price <= 0:
            raise ValueError(f"Order price must be > 0 if set, got {self.price}")
        if self.type is OrderType.LIMIT and self.price is None:
            raise ValueError("Limit order requires a price")
        if self.fee < 0:
            raise ValueError(f"Order fee must be non-negative, got {self.fee}")


@dataclass(frozen=True, slots=True)
class Balance:
    currency: str
    free: Decimal
    used: Decimal
    total: Decimal

    def __post_init__(self) -> None:
        if self.free < 0 or self.used < 0 or self.total < 0:
            raise ValueError("Balance components must be non-negative")
        if self.free + self.used != self.total:
            raise ValueError(
                f"Balance free ({self.free}) + used ({self.used}) != total ({self.total})"
            )
