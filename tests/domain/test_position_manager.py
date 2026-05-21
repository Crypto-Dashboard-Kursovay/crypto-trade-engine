from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.models import Candle, Position
from domain.position_manager import PositionManager, TrackedPosition


def _candle(close: str, high: str | None = None, low: str | None = None) -> Candle:
    from datetime import datetime, timezone
    return Candle(
        symbol="BTC/USDT",
        timeframe=TimeFrame.M15,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=Decimal(close),
        high=Decimal(high or close),
        low=Decimal(low or close),
        close=Decimal(close),
        volume=Decimal("10"),
    )


class TestTrackedPosition:
    def test_stop_loss_price_long(self) -> None:
        pos = TrackedPosition(
            symbol="BTC/USDT", side=Side.BUY,
            entry_price=Decimal("50000"), size=Decimal("0.1"),
            stop_loss_pct=Decimal("0.05"),
        )
        assert pos.stop_loss_price() == Decimal("47500")

    def test_stop_loss_price_short(self) -> None:
        pos = TrackedPosition(
            symbol="BTC/USDT", side=Side.SELL,
            entry_price=Decimal("50000"), size=Decimal("0.1"),
            stop_loss_pct=Decimal("0.05"),
        )
        assert pos.stop_loss_price() == Decimal("52500")

    def test_take_profit_price_long(self) -> None:
        pos = TrackedPosition(
            symbol="BTC/USDT", side=Side.BUY,
            entry_price=Decimal("50000"), size=Decimal("0.1"),
            take_profit_pct=Decimal("0.10"),
        )
        assert pos.take_profit_price() == Decimal("55000")

    def test_take_profit_price_short(self) -> None:
        pos = TrackedPosition(
            symbol="BTC/USDT", side=Side.SELL,
            entry_price=Decimal("50000"), size=Decimal("0.1"),
            take_profit_pct=Decimal("0.10"),
        )
        assert pos.take_profit_price() == Decimal("45000")

    def test_no_stop_loss_returns_none(self) -> None:
        pos = TrackedPosition(
            symbol="BTC/USDT", side=Side.BUY,
            entry_price=Decimal("50000"), size=Decimal("0.1"),
        )
        assert pos.stop_loss_price() is None
        assert pos.take_profit_price() is None


class TestPositionManager:
    def test_open_and_track(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"))
        assert pm.has_position
        assert len(pm.open_positions) == 1
        assert pm.open_positions[0].entry_price == Decimal("50000")

    def test_default_stop_take_from_manager(self) -> None:
        pm = PositionManager(
            "test", "BTC/USDT",
            default_stop_loss_pct=Decimal("0.05"),
            default_take_profit_pct=Decimal("0.10"),
        )
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"))
        pos = pm.open_positions[0]
        assert pos.stop_loss_pct == Decimal("0.05")
        assert pos.take_profit_pct == Decimal("0.10")

    def test_stop_loss_triggered_long(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"),
                stop_loss_pct=Decimal("0.05"))
        # Low drops below 47500 → stop triggered
        exits = pm.check_exits(_candle("48000", high="48500", low="47000"))
        assert len(exits) == 1
        assert exits[0].side == Side.SELL
        assert not pm.has_position

    def test_stop_loss_not_triggered_long(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"),
                stop_loss_pct=Decimal("0.05"))
        # Low stays above 47500
        exits = pm.check_exits(_candle("48000", high="48500", low="48000"))
        assert len(exits) == 0
        assert pm.has_position

    def test_take_profit_triggered_long(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"),
                take_profit_pct=Decimal("0.10"))
        # High reaches above 55000
        exits = pm.check_exits(_candle("55000", high="56000", low="54000"))
        assert len(exits) == 1
        assert exits[0].side == Side.SELL

    def test_stop_loss_triggered_short(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.SELL, Decimal("50000"), Decimal("0.1"),
                stop_loss_pct=Decimal("0.05"))
        # High above 52500
        exits = pm.check_exits(_candle("53000", high="53000", low="51000"))
        assert len(exits) == 1
        assert exits[0].side == Side.BUY

    def test_take_profit_triggered_short(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.SELL, Decimal("50000"), Decimal("0.1"),
                take_profit_pct=Decimal("0.10"))
        # Low below 45000
        exits = pm.check_exits(_candle("45000", high="46000", low="44000"))
        assert len(exits) == 1
        assert exits[0].side == Side.BUY

    def test_no_exit_without_stop_or_take(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"))
        exits = pm.check_exits(_candle("40000", high="41000", low="39000"))
        assert len(exits) == 0

    def test_multiple_positions(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"),
                stop_loss_pct=Decimal("0.50"))  # very wide, won't trigger
        pm.open(Side.BUY, Decimal("51000"), Decimal("0.05"),
                stop_loss_pct=Decimal("0.05"))  # triggers at 48450
        exits = pm.check_exits(_candle("48000", high="49000", low="47000"))
        # Only the second position triggers
        assert len(exits) == 1
        assert len(pm.open_positions) == 1

    def test_reconcile_loads_positions(self) -> None:
        pm = PositionManager(
            "test", "BTC/USDT",
            default_stop_loss_pct=Decimal("0.05"),
            default_take_profit_pct=Decimal("0.10"),
        )
        exchange_positions = [
            Position(symbol="BTC/USDT", side=Side.BUY,
                     entry_price=Decimal("60000"), size=Decimal("0.2"),
                     current_pnl=Decimal("100")),
        ]
        pm.reconcile(exchange_positions)
        assert pm.has_position
        pos = pm.open_positions[0]
        assert pos.entry_price == Decimal("60000")
        assert pos.size == Decimal("0.2")
        assert pos.stop_loss_pct == Decimal("0.05")
        assert pos.take_profit_pct == Decimal("0.10")

    def test_reconcile_clears_previous(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"))
        pm.reconcile([])
        assert not pm.has_position

    def test_close_manually(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"))
        pm.close(0)
        assert not pm.has_position

    def test_exit_signal_uses_correct_price(self) -> None:
        pm = PositionManager("test", "BTC/USDT")
        pm.open(Side.BUY, Decimal("50000"), Decimal("0.1"),
                stop_loss_pct=Decimal("0.02"))
        exits = pm.check_exits(_candle("49000", high="49500", low="48800"))
        assert len(exits) == 1
        assert exits[0].price == Decimal("49000")
        assert exits[0].size == Decimal("0.1")
        assert exits[0].symbol == "BTC/USDT"
