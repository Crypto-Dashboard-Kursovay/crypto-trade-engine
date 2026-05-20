from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from application.risk_manager import RiskManager
from domain.enums import Side
from domain.exceptions import (
    InsufficientBalanceError,
    InvalidSignalError,
    LotSizeError,
    RiskRejectedError,
)
from domain.interfaces import ExchangeAdapter
from domain.models import Balance, Signal


def _balances(
    usdt: Decimal = Decimal("10000"),
    btc: Decimal = Decimal("1"),
) -> dict[str, Balance]:
    return {
        "USDT": Balance("USDT", free=usdt, used=Decimal("0"), total=usdt),
        "BTC": Balance("BTC", free=btc, used=Decimal("0"), total=btc),
    }


@pytest.fixture
def adapter() -> AsyncMock:
    a = AsyncMock(spec=ExchangeAdapter)
    a.get_balance.return_value = _balances()
    a.get_positions.return_value = []
    return a


@pytest.fixture
def risk(adapter: AsyncMock) -> RiskManager:
    return RiskManager(
        adapter=adapter,
        max_position_pct=Decimal("0.5"),
        min_lot=Decimal("0.001"),
    )


class TestConstructor:
    def test_invalid_max_position_pct(self) -> None:
        a = AsyncMock(spec=ExchangeAdapter)
        with pytest.raises(ValueError, match="max_position_pct"):
            RiskManager(a, max_position_pct=Decimal("1.5"), min_lot=Decimal("0.001"))

    def test_invalid_min_lot(self) -> None:
        a = AsyncMock(spec=ExchangeAdapter)
        with pytest.raises(ValueError, match="min_lot"):
            RiskManager(a, max_position_pct=Decimal("0.5"), min_lot=Decimal("0"))


class TestCheck:
    async def test_buy_happy_path(
        self, risk: RiskManager, adapter: AsyncMock
    ) -> None:
        signal = Signal("s", "BTC/USDT", Side.BUY, Decimal("0.01"), Decimal("100"))
        result = await risk.check(signal)
        assert result is signal
        adapter.get_balance.assert_awaited_once()

    async def test_sell_happy_path(self, risk: RiskManager) -> None:
        signal = Signal("s", "BTC/USDT", Side.SELL, Decimal("0.5"), Decimal("100"))
        result = await risk.check(signal)
        assert result is signal

    async def test_size_below_min_lot(self, risk: RiskManager) -> None:
        signal = Signal("s", "BTC/USDT", Side.BUY, Decimal("0.0001"), Decimal("100"))
        with pytest.raises(LotSizeError):
            await risk.check(signal)

    async def test_signal_without_price_invalid(self, risk: RiskManager) -> None:
        signal = Signal("s", "BTC/USDT", Side.BUY, Decimal("0.01"))
        with pytest.raises(InvalidSignalError):
            await risk.check(signal)

    async def test_buy_insufficient_quote(self, adapter: AsyncMock) -> None:
        adapter.get_balance.return_value = _balances(usdt=Decimal("5"))
        risk = RiskManager(adapter, Decimal("1"), Decimal("0.001"))
        signal = Signal("s", "BTC/USDT", Side.BUY, Decimal("0.01"), Decimal("1000"))
        with pytest.raises(InsufficientBalanceError, match="USDT"):
            await risk.check(signal)

    async def test_sell_insufficient_base(self, adapter: AsyncMock) -> None:
        adapter.get_balance.return_value = _balances(btc=Decimal("0.001"))
        risk = RiskManager(adapter, Decimal("1"), Decimal("0.0001"))
        signal = Signal("s", "BTC/USDT", Side.SELL, Decimal("0.01"), Decimal("100"))
        with pytest.raises(InsufficientBalanceError, match="BTC"):
            await risk.check(signal)

    async def test_notional_exceeds_max_position_pct(
        self, risk: RiskManager
    ) -> None:
        # equity = 10000 USDT, max_pct = 0.5 → cap = 5000; notional = 6000
        signal = Signal("s", "BTC/USDT", Side.BUY, Decimal("1"), Decimal("6000"))
        with pytest.raises(RiskRejectedError, match="exceeds"):
            await risk.check(signal)

    async def test_invalid_symbol_format(self, risk: RiskManager) -> None:
        signal = Signal("s", "BTCUSDT", Side.BUY, Decimal("0.01"), Decimal("100"))
        with pytest.raises(InvalidSignalError, match="BASE/QUOTE"):
            await risk.check(signal)

    async def test_unknown_quote_currency(self, adapter: AsyncMock) -> None:
        adapter.get_balance.return_value = _balances()
        risk = RiskManager(adapter, Decimal("1"), Decimal("0.001"))
        signal = Signal("s", "BTC/EUR", Side.BUY, Decimal("0.01"), Decimal("100"))
        with pytest.raises(InsufficientBalanceError, match="EUR"):
            await risk.check(signal)
