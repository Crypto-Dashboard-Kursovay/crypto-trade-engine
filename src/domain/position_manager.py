"""PositionManager tracks open positions and checks stop-loss / take-profit exits.

One instance per strategy (bot). On each candle, check_exits() returns exit
signals if price has crossed stop-loss or take-profit thresholds.

Position reconciliation: on start, existing exchange positions are loaded via
reconcile() so that stop-loss/take-profit works from the correct entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from domain.enums import Side
from domain.models import Candle, Position, Signal

logger = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    symbol: str
    side: Side
    entry_price: Decimal
    size: Decimal
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None

    def stop_loss_price(self) -> Decimal | None:
        if self.stop_loss_pct is None:
            return None
        if self.side is Side.BUY:
            return self.entry_price * (Decimal("1") - self.stop_loss_pct)
        return self.entry_price * (Decimal("1") + self.stop_loss_pct)

    def take_profit_price(self) -> Decimal | None:
        if self.take_profit_pct is None:
            return None
        if self.side is Side.BUY:
            return self.entry_price * (Decimal("1") + self.take_profit_pct)
        return self.entry_price * (Decimal("1") - self.take_profit_pct)


@dataclass
class PositionManager:
    strategy_name: str
    symbol: str
    default_stop_loss_pct: Decimal | None = None
    default_take_profit_pct: Decimal | None = None

    _positions: list[TrackedPosition] = field(default_factory=list)

    # ---- position lifecycle ----

    def open(
        self,
        side: Side,
        entry_price: Decimal,
        size: Decimal,
        stop_loss_pct: Decimal | None = None,
        take_profit_pct: Decimal | None = None,
    ) -> None:
        sl = stop_loss_pct if stop_loss_pct is not None else self.default_stop_loss_pct
        tp = take_profit_pct if take_profit_pct is not None else self.default_take_profit_pct
        pos = TrackedPosition(
            symbol=self.symbol,
            side=side,
            entry_price=entry_price,
            size=size,
            stop_loss_pct=sl,
            take_profit_pct=tp,
        )
        self._positions.append(pos)
        logger.info(
            "position_opened", symbol=self.symbol, side=side.value,
            entry=str(entry_price), size=str(size),
            stop_loss_pct=str(sl) if sl else None,
            take_profit_pct=str(tp) if tp else None,
        )

    def close(self, index: int) -> TrackedPosition:
        pos = self._positions.pop(index)
        logger.info("position_closed", symbol=pos.symbol, side=pos.side.value,
                     entry=str(pos.entry_price))
        return pos

    def reconcile(self, exchange_positions: list[Position]) -> None:
        """Load existing positions from the exchange on startup."""
        self._positions.clear()
        for ep in exchange_positions:
            pos = TrackedPosition(
                symbol=ep.symbol,
                side=ep.side,
                entry_price=ep.entry_price,
                size=ep.size,
                stop_loss_pct=self.default_stop_loss_pct,
                take_profit_pct=self.default_take_profit_pct,
            )
            self._positions.append(pos)
        if self._positions:
            logger.info(
                "positions_reconciled", symbol=self.symbol,
                count=len(self._positions),
            )

    @property
    def open_positions(self) -> list[TrackedPosition]:
        return list(self._positions)

    @property
    def has_position(self) -> bool:
        return len(self._positions) > 0

    # ---- exit checks ----

    def check_exits(self, candle: Candle) -> list[Signal]:
        """Returns exit signals for any position whose stop/take level was hit.

        Uses candle.low for BUY stop-loss (price dropped below stop) and
        candle.high for SELL stop-loss (price rose above stop). For take-profit,
        uses candle.high for BUY and candle.low for SELL.
        """
        exits: list[Signal] = []
        indexes_to_close: list[int] = []

        for i, pos in enumerate(self._positions):
            exit_signal = self._check_single(pos, candle)
            if exit_signal is not None:
                exits.append(exit_signal)
                indexes_to_close.append(i)

        # Remove from end to preserve indices
        for i in reversed(indexes_to_close):
            self.close(i)

        return exits

    def _check_single(self, pos: TrackedPosition, candle: Candle) -> Signal | None:
        sl_price = pos.stop_loss_price()
        tp_price = pos.take_profit_price()

        if pos.side is Side.BUY:
            # Long: stop when price drops below stop-loss
            if sl_price is not None and candle.low <= sl_price:
                logger.info("stop_loss_triggered", symbol=self.symbol,
                            side="buy", stop_price=str(sl_price),
                            candle_low=str(candle.low))
                return _exit_signal(self.strategy_name, self.symbol, Side.SELL, pos, sl_price)

            # Long: take profit when price rises above take-profit
            if tp_price is not None and candle.high >= tp_price:
                logger.info("take_profit_triggered", symbol=self.symbol,
                            side="buy", tp_price=str(tp_price),
                            candle_high=str(candle.high))
                return _exit_signal(self.strategy_name, self.symbol, Side.SELL, pos, tp_price)
        else:
            # Short: stop when price rises above stop-loss
            if sl_price is not None and candle.high >= sl_price:
                logger.info("stop_loss_triggered", symbol=self.symbol,
                            side="sell", stop_price=str(sl_price),
                            candle_high=str(candle.high))
                return _exit_signal(self.strategy_name, self.symbol, Side.BUY, pos, sl_price)

            # Short: take profit when price drops below take-profit
            if tp_price is not None and candle.low <= tp_price:
                logger.info("take_profit_triggered", symbol=self.symbol,
                            side="sell", tp_price=str(tp_price),
                            candle_low=str(candle.low))
                return _exit_signal(self.strategy_name, self.symbol, Side.BUY, pos, tp_price)

        return None


def _exit_signal(
    strategy_name: str, symbol: str, exit_side: Side,
    pos: TrackedPosition, exit_price: Decimal,
) -> Signal:
    return Signal(
        strategy_name=strategy_name,
        symbol=symbol,
        side=exit_side,
        size=pos.size,
        price=exit_price,
    )
