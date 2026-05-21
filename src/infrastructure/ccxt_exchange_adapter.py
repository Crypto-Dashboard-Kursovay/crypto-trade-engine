from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import ccxt.pro as ccxtpro
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from domain.enums import OrderStatus, OrderType, Side, TimeFrame
from domain.exceptions import OrderExecutionError
from domain.interfaces import ExchangeAdapter
from domain.models import Balance, Candle, Order, Position

from .logging import get_logger

logger = get_logger(__name__)

SUPPORTED_EXCHANGES: frozenset[str] = frozenset({"binance", "bybit", "okx", "mexc"})

# OKX и Coinbase Pro требуют третий секрет — passphrase. Используется только при
# построении ccxt-конфига (ключ "password" в ccxt).
_PASSPHRASE_EXCHANGES: frozenset[str] = frozenset({"okx"})

_CCXT_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}

_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ccxtpro.NetworkError,
    ccxtpro.ExchangeNotAvailable,
    ccxtpro.RequestTimeout,
)


def _network_retry() -> Any:
    return retry(
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=10),
        stop=stop_after_attempt(5),
        reraise=True,
    )


class CCXTExchangeAdapter(ExchangeAdapter):
    """Адаптер биржи поверх ccxt.pro. Поддерживаются Binance, Bybit, OKX, MEXC.

    Конструктор валидирует exchange_name по белому списку, чтобы при добавлении
    новых бирж не было сюрпризов в рантайме. OKX дополнительно требует passphrase
    (передаётся в ccxt-конфиг под ключом "password").
    """

    def __init__(
        self,
        exchange_name: str,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        passphrase: str | None = None,
        exchange: Any | None = None,
    ) -> None:
        if exchange_name not in SUPPORTED_EXCHANGES:
            raise ValueError(
                f"Unsupported exchange '{exchange_name}'. "
                f"Supported: {sorted(SUPPORTED_EXCHANGES)}"
            )
        if exchange_name in _PASSPHRASE_EXCHANGES and not passphrase:
            raise ValueError(f"{exchange_name} requires a passphrase")
        self._exchange_name = exchange_name
        if exchange is not None:
            self._exchange = exchange
        else:
            exchange_class = getattr(ccxtpro, exchange_name)
            config: dict[str, Any] = {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
            if passphrase:
                config["password"] = passphrase
            self._exchange = exchange_class(config)
            if testnet:
                self._exchange.set_sandbox_mode(True)
                # set_sandbox_mode может переключить fetch_balance на futures URL.
                # Принудительно задаём spot testnet напрямую.
                if exchange_name == "binance":
                    api_url = "https://testnet.binance.vision"
                    if "api" in self._exchange.urls:
                        self._exchange.urls["api"] = {
                            "public": f"{api_url}/api/v3",
                            "private": f"{api_url}/api/v3",
                        }
                    # Сбрасываем futures URL — иначе fetch_balance/load_markets лезут
                    # на testnet.binancefuture.com и тайматят
                    for key in ("fapiPublic", "fapiPrivate", "dapiPublic", "dapiPrivate"):
                        if key in self._exchange.urls:
                            del self._exchange.urls[key]
                    self._exchange.options["defaultType"] = "spot"

    @property
    def native(self) -> Any:
        """Прямой доступ к ccxt instance — нужен MarketDataProvider'у для watch_ohlcv."""
        return self._exchange

    async def close(self) -> None:
        await self._exchange.close()

    @_network_retry()  # type: ignore[untyped-decorator]
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: TimeFrame,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        raw = await self._exchange.fetch_ohlcv(
            symbol=symbol,
            timeframe=str(timeframe),
            since=since,
            limit=limit,
        )
        return [_to_candle(symbol, timeframe, row) for row in raw]

    @_network_retry()  # type: ignore[untyped-decorator]
    async def create_order(
        self,
        symbol: str,
        side: Side,
        type: OrderType,
        size: Decimal,
        price: Decimal | None = None,
    ) -> Order:
        if type is OrderType.LIMIT and price is None:
            raise OrderExecutionError("LIMIT order requires price")

        amount_str = self._exchange.amount_to_precision(symbol, float(size))
        amount_precise = Decimal(amount_str)

        price_arg: float | None = None
        price_precise: Decimal | None = None
        if price is not None:
            price_str = self._exchange.price_to_precision(symbol, float(price))
            price_arg = float(price_str)
            price_precise = Decimal(price_str)

        try:
            raw = await self._exchange.create_order(
                symbol=symbol,
                type=str(type),
                side=str(side),
                amount=float(amount_str),
                price=price_arg,
            )
        except ccxtpro.BaseError as exc:
            raise OrderExecutionError(f"create_order failed: {exc}") from exc

        return _to_order(
            raw, fallback_size=amount_precise, fallback_price=price_precise
        )

    @_network_retry()  # type: ignore[untyped-decorator]
    async def get_balance(self) -> Mapping[str, Balance]:
        raw = await self._exchange.fetch_balance()
        return _to_balances(raw)

    @_network_retry()  # type: ignore[untyped-decorator]
    async def get_positions(self) -> list[Position]:
        if not self._exchange.has.get("fetchPositions"):
            return []
        raw = await self._exchange.fetch_positions()
        return [_to_position(r) for r in raw if r.get("contracts", 0) > 0 or r.get("size", 0) > 0]


def _to_candle(symbol: str, timeframe: TimeFrame, row: list[Any]) -> Candle:
    ts_ms, o, h, l, c, v = row
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _to_order(
    raw: Mapping[str, Any],
    fallback_size: Decimal,
    fallback_price: Decimal | None,
) -> Order:
    raw_status = (raw.get("status") or "open").lower()
    status = _CCXT_STATUS_MAP.get(raw_status, OrderStatus.OPEN)

    raw_amount = raw.get("amount")
    size = Decimal(str(raw_amount)) if raw_amount is not None else fallback_size

    raw_filled = raw.get("filled")
    filled = Decimal(str(raw_filled)) if raw_filled is not None else Decimal("0")
    if filled > size:
        # CCXT может прислать filled чуть больше size из-за округления — клампим.
        filled = size

    raw_price = raw.get("price")
    price = Decimal(str(raw_price)) if raw_price not in (None, 0) else fallback_price

    fee = Decimal("0")
    raw_fee = raw.get("fee")
    if isinstance(raw_fee, Mapping) and raw_fee.get("cost") is not None:
        fee_val = raw_fee["cost"]
        fee = Decimal(str(fee_val)) if fee_val >= 0 else Decimal("0")

    side_str = raw.get("side") or "buy"
    type_str = raw.get("type") or "market"

    # для MARKET-ордеров без цены передаём None
    final_price = price if type_str != "market" or raw_price not in (None, 0) else None

    return Order(
        order_id=str(raw.get("id") or ""),
        symbol=str(raw.get("symbol") or ""),
        side=Side(side_str),
        type=OrderType(type_str),
        size=size,
        status=status,
        filled_size=filled,
        price=final_price,
        fee=fee,
    )


_BALANCE_META_KEYS: frozenset[str] = frozenset(
    {"free", "used", "total", "info", "timestamp", "datetime"}
)


def _to_balances(raw: Mapping[str, Any]) -> dict[str, Balance]:
    """CCXT format: {currency: {free, used, total}, free: {...}, used: {...}, info: {...}, ...}.

    Считаем total = free + used. Это игнорирует locked/staked суммы (которые недоступны
    для торговли всё равно), но гарантирует invariant Balance: free + used == total.
    """
    out: dict[str, Balance] = {}
    for code, body in raw.items():
        if code in _BALANCE_META_KEYS:
            continue
        if not isinstance(body, Mapping):
            continue
        free = Decimal(str(body.get("free") or 0))
        used = Decimal(str(body.get("used") or 0))
        if free == 0 and used == 0:
            continue
        out[code] = Balance(currency=code, free=free, used=used, total=free + used)
    return out


def _to_position(raw: Mapping[str, Any]) -> Position:
    # Handle unified size representation (can be 'contracts' or 'size' depending on market)
    raw_size = raw.get("contracts")
    if raw_size is None or raw_size == 0:
        raw_size = raw.get("size", 0)
    
    # Try different fields for pnl depending on exchange format
    raw_pnl = raw.get("unrealizedPnl")
    if raw_pnl is None:
        raw_pnl = raw.get("pnl", 0)

    side_val = raw.get("side")
    if side_val == "short":
        side = Side.SELL
    elif side_val == "long":
        side = Side.BUY
    else:
        # Fallback based on size sign if side string is not long/short
        size_dec = Decimal(str(raw_size))
        side = Side.BUY if size_dec >= 0 else Side.SELL

    return Position(
        symbol=str(raw.get("symbol", "")),
        side=side,
        entry_price=Decimal(str(raw.get("entryPrice", 0))),
        size=abs(Decimal(str(raw_size))),
        current_pnl=Decimal(str(raw_pnl))
    )
