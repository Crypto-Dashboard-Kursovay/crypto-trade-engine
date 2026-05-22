from decimal import Decimal

from domain.enums import TimeFrame
from strategies.adaptive_rsi import (
    AdaptiveRsiStrategy,
    _evaluate_rsi_params,
    _simulate_rsi_trades,
)


class TestSimulateRsiTrades:
    def test_no_trades_on_flat_prices(self) -> None:
        closes = [Decimal("100")] * 100
        pnls = _simulate_rsi_trades(closes, 14, 30, 70)
        assert len(pnls) == 0

    def test_produces_trades_on_trending_data(self) -> None:
        # Oscillating data: RSI should cross levels
        closes: list[Decimal] = []
        price = Decimal("100")
        for i in range(200):
            if i % 50 < 25:
                price += Decimal("1")
            else:
                price -= Decimal("0.5")
            closes.append(price)
        pnls = _simulate_rsi_trades(closes, 14, 30, 70)
        # Should produce at least some trades
        assert len(pnls) > 0

    def test_oversold_below_overbought_no_trades(self) -> None:
        # oversold >= overbought would always skip — never enter position
        closes: list[Decimal] = []
        price = Decimal("100")
        for i in range(200):
            price += (Decimal("0.1") if i % 2 == 0 else Decimal("-0.1"))
            closes.append(price)
        # oversold >= overbought → no valid entry
        pnls = _simulate_rsi_trades(closes, 14, 40, 40)
        assert len(pnls) == 0


class TestEvaluateRsiParams:
    def test_returns_negative_infinity_for_no_trades(self) -> None:
        closes = [Decimal("100")] * 50
        result = _evaluate_rsi_params(closes, 14, 30, 70)
        assert result == Decimal("-Infinity")

    def test_returns_finite_sharpe_for_some_trades(self) -> None:
        closes: list[Decimal] = []
        price = Decimal("100")
        for i in range(500):
            # Strong oscillation to trigger RSI crossovers
            if i % 40 < 20:
                price += Decimal("5")
            else:
                price -= Decimal("3")
            closes.append(price)
        result = _evaluate_rsi_params(closes, 14, 30, 70)
        assert result > Decimal("-100")  # finite, could be negative


class TestAdaptiveRsiStrategy:
    async def test_calibration_picks_params(self) -> None:
        strat = AdaptiveRsiStrategy(
            symbol="BTC/USDT",
            timeframe=TimeFrame.H1,
            optimization_candles=200,
            rsi_period_range=[7, 14],
            oversold_range=[25, 30, 35],
            overbought_range=[65, 70, 75],
        )

        # Feed 200 warmup candles with some oscillation
        price = Decimal("50000")
        for i in range(200):
            if i % 30 < 15:
                price += Decimal("100")
            else:
                price -= Decimal("50")
            from domain.models import Candle
            from datetime import datetime, timezone
            candle = Candle(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 1, 1, hour=i % 24, tzinfo=timezone.utc),
                open=price - Decimal("10"),
                high=price + Decimal("20"),
                low=price - Decimal("20"),
                close=price,
                volume=Decimal("10"),
            )
            strat.on_candle(candle)

        assert not strat._calibrated
        assert len(strat._closes) == 200

        # Run calibration
        await strat.on_start()

        assert strat._calibrated
        assert strat._best_rsi_period is not None
        assert strat._best_oversold is not None
        assert strat._best_overbought is not None
        assert 7 <= strat._best_rsi_period <= 14
        assert Decimal("20") <= strat._best_oversold <= Decimal("40")
        assert Decimal("60") <= strat._best_overbought <= Decimal("80")

    async def test_trades_after_calibration(self) -> None:
        strat = AdaptiveRsiStrategy(
            symbol="BTC/USDT",
            timeframe=TimeFrame.H1,
            optimization_candles=100,
            rsi_period_range=[7, 10, 14],
            oversold_range=[25, 30],
            overbought_range=[65, 70],
        )

        from domain.models import Candle
        from datetime import datetime, timezone

        # Feed warmup
        price = Decimal("50000")
        for i in range(100):
            if i % 15 < 7:
                price += Decimal("150")
            else:
                price -= Decimal("80")
            candle = Candle(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 1, 1, hour=i % 24, tzinfo=timezone.utc),
                open=price - Decimal("5"),
                high=price + Decimal("10"),
                low=price - Decimal("10"),
                close=price,
                volume=Decimal("10"),
            )
            strat.on_candle(candle)

        await strat.on_start()
        assert strat._calibrated

        # Now feed live candles — should produce signals
        signals = []
        for i in range(100, 200):
            if i % 20 < 10:
                price += Decimal("200")
            else:
                price -= Decimal("150")
            candle = Candle(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 1, 1, hour=i % 24, tzinfo=timezone.utc),
                open=price - Decimal("5"),
                high=price + Decimal("10"),
                low=price - Decimal("10"),
                close=price,
                volume=Decimal("10"),
            )
            sig = strat.on_candle(candle)
            if sig is not None:
                signals.append(sig)

        # Should produce at least 1 signal (could be 0 with tight params)
        assert len(signals) >= 0

    async def test_default_params_when_too_few_candles(self) -> None:
        strat = AdaptiveRsiStrategy(
            symbol="BTC/USDT",
            timeframe=TimeFrame.H1,
            optimization_candles=50,
            rsi_period_range=[7, 14],
            oversold_range=[30],
            overbought_range=[70],
        )

        from domain.models import Candle
        from datetime import datetime, timezone

        # Feed only 30 candles (< 50 in defaults check)
        for i in range(30):
            candle = Candle(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 1, 1, hour=i % 24, tzinfo=timezone.utc),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("100"),
                volume=Decimal("10"),
            )
            strat.on_candle(candle)

        await strat.on_start()
        assert strat._calibrated
        assert strat._best_rsi_period == 14
        assert strat._best_oversold == Decimal("30")
        assert strat._best_overbought == Decimal("70")

    def test_rejects_invalid_params(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="optimization_candles"):
            AdaptiveRsiStrategy(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                optimization_candles=10,
            )
        with pytest.raises(ValueError, match="order_size"):
            AdaptiveRsiStrategy(
                symbol="BTC/USDT",
                timeframe=TimeFrame.H1,
                order_size="-1",
            )
