from decimal import Decimal

from domain.enums import Side
from domain.exceptions import (
    InsufficientBalanceError,
    InvalidSignalError,
    LotSizeError,
    RiskRejectedError,
)
from domain.interfaces import ExchangeAdapter
from domain.models import Signal


class RiskManager:
    """Validates a signal against current balance, lot size and position-size limits.

    Returns the (possibly normalized) signal on approval, raises a subclass of
    RiskRejectedError on refusal. Has no side effects on state — the caller decides
    what to do with the rejection.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        max_position_pct: Decimal,
        min_lot: Decimal,
        quote_currency: str = "USDT",
    ) -> None:
        if not (Decimal(0) < max_position_pct <= Decimal(1)):
            raise ValueError(
                f"max_position_pct must be in (0, 1], got {max_position_pct}"
            )
        if min_lot <= 0:
            raise ValueError(f"min_lot must be > 0, got {min_lot}")
        self._adapter = adapter
        self._max_position_pct = max_position_pct
        self._min_lot = min_lot
        self._quote_currency = quote_currency

    async def check(self, signal: Signal) -> Signal:
        if signal.size < self._min_lot:
            raise LotSizeError(
                f"size {signal.size} < min_lot {self._min_lot}"
            )
        if signal.price is None:
            raise InvalidSignalError(
                "RiskManager requires an explicit price on the signal "
                "(market orders must be priced by the strategy or executor)"
            )

        balances = await self._adapter.get_balance()
        base, _, quote = signal.symbol.partition("/")
        if not base or not quote:
            raise InvalidSignalError(
                f"symbol must be in BASE/QUOTE form, got {signal.symbol!r}"
            )

        notional = signal.size * signal.price

        if signal.side is Side.BUY:
            quote_bal = balances.get(quote)
            if quote_bal is None:
                raise InsufficientBalanceError(f"no {quote} balance available")
            if quote_bal.free < notional:
                raise InsufficientBalanceError(
                    f"free {quote_bal.free} {quote} < required notional {notional}"
                )
        else:
            base_bal = balances.get(base)
            if base_bal is None:
                raise InsufficientBalanceError(f"no {base} balance available")
            if base_bal.free < signal.size:
                raise InsufficientBalanceError(
                    f"free {base_bal.free} {base} < required size {signal.size}"
                )

        quote_bal = balances.get(quote)
        if quote_bal is not None:
            equity_cap = quote_bal.total * self._max_position_pct
            if notional > equity_cap:
                raise RiskRejectedError(
                    f"notional {notional} exceeds {self._max_position_pct} of equity ({equity_cap})"
                )

        return signal
