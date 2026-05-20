"""SimulatedExchangeAdapter — биржа в памяти для backtest'а.

Реализует тот же интерфейс `ExchangeAdapter`, что и `CCXTExchangeAdapter` —
поэтому `RiskManager`, `OrderExecutor` и `StrategyRunner` работают идентично
в live и backtest.

Особенности симуляции:
- MARKET: исполняется немедленно по `last_close * (1 ± slippage)`.
- LIMIT: уходит в self._open_orders, ждёт on_candle(); если candle.low <= price <= high — fill.
- Комиссия списывается с quote (для BUY) или quote-эквивалента (для SELL).
- Баланс хранится в self._balance: {currency: Balance}. Если баланс уходит
  в минус — OrderExecutionError (RiskManager должен ловить заранее).
- fetch_ohlcv возвращает self._warmup_candles[:limit] — для warmup стратегии.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal

from domain.enums import OrderStatus, OrderType, Side, TimeFrame
from domain.exceptions import OrderExecutionError
from domain.interfaces import ExchangeAdapter
from domain.models import Balance, Candle, Order, Position


def _balance(currency: str, free: Decimal) -> Balance:
    return Balance(currency=currency, free=free, used=Decimal("0"), total=free)


class SimulatedExchangeAdapter(ExchangeAdapter):
    """In-memory exchange. Не делает сетевых запросов, не зависит от ccxt."""

    def __init__(
        self,
        initial_balance: Mapping[str, Decimal],
        symbol: str,
        fee_rate: Decimal = Decimal("0.001"),
        slippage: Decimal = Decimal("0.0005"),
        warmup_candles: list[Candle] | None = None,
    ) -> None:
        if "/" not in symbol:
            raise ValueError(f"symbol must be BASE/QUOTE, got {symbol!r}")
        if fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")
        if slippage < 0:
            raise ValueError("slippage must be >= 0")
        self._symbol = symbol
        self._base, _, self._quote = symbol.partition("/")
        self._fee_rate = fee_rate
        self._slippage = slippage
        self._balance: dict[str, Balance] = {
            cur: _balance(cur, Decimal(str(amount)))
            for cur, amount in initial_balance.items()
        }
        # На случай если базовой валюты нет — заводим нулевой баланс,
        # чтобы SELL не падал на KeyError.
        self._balance.setdefault(self._base, _balance(self._base, Decimal("0")))
        self._balance.setdefault(self._quote, _balance(self._quote, Decimal("0")))
        self._last_close: Decimal | None = None
        self._open_orders: list[Order] = []
        self._filled_orders: list[Order] = []
        self._warmup_candles = warmup_candles or []

    # --- ExchangeAdapter interface ---

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: TimeFrame,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        # symbol/timeframe игнорируем — warmup готовится backtest_runner'ом
        # из того же parquet, что и market_data.
        candles = list(self._warmup_candles)
        if limit is not None:
            candles = candles[:limit]
        return candles

    async def create_order(
        self,
        symbol: str,
        side: Side,
        type: OrderType,
        size: Decimal,
        price: Decimal | None = None,
    ) -> Order:
        if symbol != self._symbol:
            raise OrderExecutionError(
                f"sim exchange initialized for {self._symbol}, got {symbol}"
            )
        if size <= 0:
            raise OrderExecutionError("order size must be > 0")

        if type is OrderType.MARKET:
            return self._fill_market(side, size)

        # LIMIT
        if price is None or price <= 0:
            raise OrderExecutionError("LIMIT order requires positive price")
        order = Order(
            order_id=f"sim-{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=side,
            type=OrderType.LIMIT,
            size=size,
            status=OrderStatus.OPEN,
            filled_size=Decimal("0"),
            price=price,
            fee=Decimal("0"),
        )
        self._open_orders.append(order)
        return order

    async def get_balance(self) -> Mapping[str, Balance]:
        return dict(self._balance)

    async def get_positions(self) -> list[Position]:
        return []

    async def close(self) -> None:
        return None

    # --- extra hooks для backtest_runner ---

    @property
    def filled_orders(self) -> list[Order]:
        return list(self._filled_orders)

    @property
    def last_close(self) -> Decimal | None:
        return self._last_close

    def on_candle(self, candle: Candle) -> list[Order]:
        """Вызывается backtest_runner'ом до strategy.on_candle.

        1) обновляет last_close (нужен для MARKET и метрик)
        2) пробует исполнить любые open LIMITs против range [low, high]
        Возвращает список новых филлов (для логирования / метрик).
        """
        self._last_close = candle.close
        filled: list[Order] = []
        remaining: list[Order] = []
        for o in self._open_orders:
            if o.price is None:
                remaining.append(o)
                continue
            if candle.low <= o.price <= candle.high:
                fill = self._fill_at_price(o, o.price)
                filled.append(fill)
                self._filled_orders.append(fill)
            else:
                remaining.append(o)
        self._open_orders = remaining
        return filled

    # --- internal helpers ---

    def _fill_market(self, side: Side, size: Decimal) -> Order:
        if self._last_close is None:
            raise OrderExecutionError(
                "MARKET order before any candle observed — last_close is None"
            )
        slip = self._slippage
        if side is Side.BUY:
            fill_price = self._last_close * (Decimal("1") + slip)
        else:
            fill_price = self._last_close * (Decimal("1") - slip)
        order = Order(
            order_id=f"sim-{uuid.uuid4().hex[:12]}",
            symbol=self._symbol,
            side=side,
            type=OrderType.MARKET,
            size=size,
            status=OrderStatus.FILLED,
            filled_size=size,
            price=fill_price,
            fee=self._fee(side, size, fill_price),
        )
        self._apply_fill(order)
        self._filled_orders.append(order)
        return order

    def _fill_at_price(self, original: Order, price: Decimal) -> Order:
        order = Order(
            order_id=original.order_id,
            symbol=original.symbol,
            side=original.side,
            type=original.type,
            size=original.size,
            status=OrderStatus.FILLED,
            filled_size=original.size,
            price=price,
            fee=self._fee(original.side, original.size, price),
        )
        self._apply_fill(order)
        return order

    def _fee(self, side: Side, size: Decimal, price: Decimal) -> Decimal:
        # Комиссия маркет-мейкера в quote (как у Binance spot).
        del side  # симметрично для buy/sell
        return size * price * self._fee_rate

    def _apply_fill(self, order: Order) -> None:
        assert order.price is not None
        size = order.filled_size
        price = order.price
        notional = size * price
        fee = order.fee
        base_bal = self._balance[self._base]
        quote_bal = self._balance[self._quote]
        if order.side is Side.BUY:
            new_quote_free = quote_bal.free - notional - fee
            if new_quote_free < 0:
                raise OrderExecutionError(
                    f"insufficient simulated {self._quote} balance: "
                    f"free {quote_bal.free} - notional {notional} - fee {fee} < 0"
                )
            self._balance[self._quote] = _balance(self._quote, new_quote_free)
            self._balance[self._base] = _balance(self._base, base_bal.free + size)
        else:  # SELL
            if base_bal.free < size:
                raise OrderExecutionError(
                    f"insufficient simulated {self._base} balance: "
                    f"free {base_bal.free} < size {size}"
                )
            self._balance[self._base] = _balance(self._base, base_bal.free - size)
            self._balance[self._quote] = _balance(
                self._quote, quote_bal.free + notional - fee
            )


__all__ = ["SimulatedExchangeAdapter"]
