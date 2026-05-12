from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.enums import OrderStatus, OrderType, Side, TimeFrame
from domain.exceptions import OrderExecutionError
from infrastructure.ccxt_exchange_adapter import (
    SUPPORTED_EXCHANGES,
    CCXTExchangeAdapter,
    _to_balances,
    _to_candle,
    _to_order,
)


def _fake_exchange() -> MagicMock:
    ex = MagicMock()
    ex.amount_to_precision = MagicMock(side_effect=lambda symbol, amount: f"{amount:.8f}".rstrip("0").rstrip("."))
    ex.price_to_precision = MagicMock(side_effect=lambda symbol, price: f"{price:.2f}")
    ex.fetch_ohlcv = AsyncMock()
    ex.create_order = AsyncMock()
    ex.fetch_balance = AsyncMock()
    ex.close = AsyncMock()
    ex.set_sandbox_mode = MagicMock()
    return ex


def _adapter(ex: MagicMock) -> CCXTExchangeAdapter:
    return CCXTExchangeAdapter(
        exchange_name="binance",
        api_key="k",
        api_secret="s",
        testnet=True,
        exchange=ex,
    )


# ---------- конструктор ----------


def test_constructor_rejects_unsupported_exchange() -> None:
    with pytest.raises(ValueError, match="Unsupported exchange"):
        CCXTExchangeAdapter(
            exchange_name="kraken",
            api_key="k",
            api_secret="s",
            exchange=MagicMock(),
        )


def test_constructor_accepts_binance() -> None:
    assert "binance" in SUPPORTED_EXCHANGES
    adapter = _adapter(_fake_exchange())
    assert adapter.native is not None


# ---------- fetch_ohlcv ----------


async def test_fetch_ohlcv_normalizes_rows() -> None:
    ex = _fake_exchange()
    ex.fetch_ohlcv.return_value = [
        [1735689600000, 100.5, 101.0, 99.5, 100.8, 12.345],
        [1735689660000, 100.8, 102.0, 100.0, 101.5, 8.0],
    ]
    adapter = _adapter(ex)

    candles = await adapter.fetch_ohlcv("BTC/USDT", TimeFrame.M1)

    assert len(candles) == 2
    assert candles[0].symbol == "BTC/USDT"
    assert candles[0].timeframe is TimeFrame.M1
    assert candles[0].open == Decimal("100.5")
    assert candles[0].volume == Decimal("12.345")
    assert candles[0].timestamp == datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------- create_order ----------


async def test_create_order_uses_precision_and_normalizes_response() -> None:
    ex = _fake_exchange()
    ex.create_order.return_value = {
        "id": "o-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
        "amount": 0.001,
        "filled": 0.001,
        "status": "closed",
        "price": 50000.0,
        "fee": {"cost": 0.05, "currency": "USDT"},
    }
    adapter = _adapter(ex)

    order = await adapter.create_order(
        "BTC/USDT", Side.BUY, OrderType.LIMIT, Decimal("0.001"), Decimal("50000")
    )

    ex.amount_to_precision.assert_called_once()
    ex.price_to_precision.assert_called_once()
    assert order.order_id == "o-1"
    assert order.status is OrderStatus.FILLED
    assert order.size == Decimal("0.001")
    assert order.filled_size == Decimal("0.001")
    assert order.price == Decimal("50000.0")
    assert order.fee == Decimal("0.05")


async def test_create_order_market_without_price_returns_no_price() -> None:
    ex = _fake_exchange()
    ex.create_order.return_value = {
        "id": "o-2",
        "symbol": "BTC/USDT",
        "side": "sell",
        "type": "market",
        "amount": 0.5,
        "filled": 0.5,
        "status": "closed",
        "price": None,
    }
    adapter = _adapter(ex)

    order = await adapter.create_order(
        "BTC/USDT", Side.SELL, OrderType.MARKET, Decimal("0.5")
    )

    assert order.type is OrderType.MARKET
    assert order.price is None


async def test_create_order_limit_without_price_raises() -> None:
    adapter = _adapter(_fake_exchange())
    with pytest.raises(OrderExecutionError):
        await adapter.create_order(
            "BTC/USDT", Side.BUY, OrderType.LIMIT, Decimal("1.0"), price=None
        )


async def test_create_order_wraps_ccxt_error() -> None:
    import ccxt.pro as ccxtpro

    ex = _fake_exchange()
    ex.create_order.side_effect = ccxtpro.InsufficientFunds("not enough USDT")
    adapter = _adapter(ex)

    with pytest.raises(OrderExecutionError, match="create_order failed"):
        await adapter.create_order(
            "BTC/USDT", Side.BUY, OrderType.MARKET, Decimal("100")
        )


# ---------- get_balance ----------


async def test_get_balance_normalizes_and_skips_zero() -> None:
    ex = _fake_exchange()
    ex.fetch_balance.return_value = {
        "USDT": {"free": 100.5, "used": 0, "total": 100.5},
        "BTC": {"free": 0.001, "used": 0.0005, "total": 0.0015},
        "ETH": {"free": 0, "used": 0, "total": 0},  # должен быть пропущен
        "free": {"USDT": 100.5},
        "used": {},
        "total": {"USDT": 100.5},
        "info": {"raw": "exchange-specific"},
        "timestamp": 1234,
        "datetime": "2026-05-12",
    }
    adapter = _adapter(ex)

    balances = await adapter.get_balance()

    assert set(balances.keys()) == {"USDT", "BTC"}
    assert balances["USDT"].free == Decimal("100.5")
    assert balances["BTC"].free == Decimal("0.001")
    assert balances["BTC"].used == Decimal("0.0005")
    assert balances["BTC"].total == Decimal("0.0015")  # free + used


# ---------- raw normalization helpers ----------


def test_to_candle_decimal_precision() -> None:
    candle = _to_candle(
        "BTC/USDT", TimeFrame.M5, [1735689600000, 100.123456789, 101.0, 99.5, 100.8, 0.001]
    )
    # Преобразование через str → точное Decimal
    assert candle.volume == Decimal("0.001")


def test_to_balances_drops_zero() -> None:
    balances = _to_balances({"USDT": {"free": 0, "used": 0, "total": 0}, "info": {}})
    assert balances == {}


def test_to_order_with_partial_fill() -> None:
    raw: dict[str, Any] = {
        "id": "p-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
        "amount": 1.0,
        "filled": 0.4,
        "status": "open",
        "price": 50000,
    }
    order = _to_order(raw, fallback_size=Decimal("1.0"), fallback_price=Decimal("50000"))
    assert order.status is OrderStatus.OPEN
    assert order.filled_size == Decimal("0.4")
    assert order.size == Decimal("1.0")


# ---------- close ----------


async def test_close_calls_underlying() -> None:
    ex = _fake_exchange()
    adapter = _adapter(ex)
    await adapter.close()
    ex.close.assert_awaited_once()
