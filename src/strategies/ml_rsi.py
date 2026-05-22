"""ML RSI Strategy.

Двоичная классификация направления цены через LightGBM/GBDT.
Признаки: RSI(14), momentum RSI (Δ3), Z-score RSI (μ_24, σ_24).
Сигнал BUY: предсказанная вероятность роста > threshold_buy.
Выход: стоп-лосс/тейк-профит (через PositionManager) или вероятность < threshold_sell.

Модель переобучается ежемесячно (Walk-Forward).
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal
from ml.features import compute_features, compute_rsi
from ml.train import FEATURE_COLUMNS, load_model

logger = logging.getLogger(__name__)

_ModelType = Any  # LightGBM Booster or sklearn classifier


class MlRsiStrategy(Strategy):
    """ML-стратегия на основе RSI-признаков и градиентного бустинга."""

    name = "MlRsi"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        threshold_buy: Decimal | str = Decimal("0.55"),
        threshold_sell: Decimal | str = Decimal("0.45"),
        model_dir: str = "data/models/ml_rsi",
        rsi_period: int = 14,
        fee: Decimal | str = Decimal("0.001"),
        order_size: Decimal | str = Decimal("0.001"),
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._threshold_buy = Decimal(str(threshold_buy))
        self._threshold_sell = Decimal(str(threshold_sell))
        self._model_dir = Path(model_dir)
        self._rsi_period = int(rsi_period)
        self._fee = Decimal(str(fee))
        self._order_size = Decimal(str(order_size))
        self.startup_candle_count = self._rsi_period + 24 + 5  # buffer for Z-score

        # State
        self._model: _ModelType | None = None
        self._model_loaded: bool = False
        self._current_month: str | None = None

        # Rolling windows for incremental RSI
        self._closes: deque[Decimal] = deque(maxlen=self._rsi_period + 1)
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None

        # Rolling RSI values for momentum + Z-score
        self._rsi_values: deque[Decimal] = deque(maxlen=24)

        # Tracking position for sell signals
        self._in_position: bool = False

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        self._load_model()

    def on_candle(self, candle: Candle) -> Signal | None:
        # --- 1. Accumulate closes ---
        self._closes.append(candle.close)
        if len(self._closes) < self._rsi_period + 1:
            return None

        # --- 2. Compute RSI (incremental Wilder) ---
        rsi, self._avg_gain, self._avg_loss = compute_rsi(
            list(self._closes), self._rsi_period, self._avg_gain, self._avg_loss
        )
        if rsi is None:
            return None

        self._rsi_values.append(rsi)

        # --- 3. Compute features ---
        feats = compute_features(list(self._rsi_values), rsi)
        if len(feats) < len(FEATURE_COLUMNS):
            return None  # not enough data yet

        # --- 4. Predict ---
        prob_up = self._predict(feats)

        # --- 5. Generate signals ---
        prob_d = Decimal(str(round(prob_up, 6)))

        # BUY signal
        if prob_d >= self._threshold_buy and not self._in_position:
            self._in_position = True
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=self._order_size,
                price=candle.close,
                metadata={
                    "probability": str(prob_d),
                    "rsi": str(rsi),
                    "kind": "ml_buy",
                },
            )

        # SELL signal (probability-based exit)
        if prob_d <= self._threshold_sell and self._in_position:
            self._in_position = False
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=self._order_size,
                price=candle.close,
                metadata={
                    "probability": str(prob_d),
                    "rsi": str(rsi),
                    "kind": "ml_sell",
                },
            )

        return None

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Load LightGBM/GBDT model for the current month."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        self._current_month = month

        try:
            model, meta = load_model(
                self._model_dir, self.symbol, str(self.timeframe), month
            )
            self._model = model
            self._model_loaded = True
            logger.info(
                "ML model loaded: month=%s samples=%s",
                month,
                meta.get("train_samples", "?"),
            )
        except FileNotFoundError:
            logger.warning(
                "No ML model found for %s %s month=%s in %s — strategy will be idle",
                self.symbol, self.timeframe, month, self._model_dir,
            )
        except Exception as exc:
            logger.error(
                "Failed to load ML model for %s %s month=%s: %s",
                self.symbol, self.timeframe, month, exc,
            )

    def _predict(self, feats: dict[str, float]) -> float:
        """Run model.predict_proba and return probability of class 1."""
        if not self._model_loaded or self._model is None:
            return 0.0

        X = [[feats.get(col, 0.0) for col in FEATURE_COLUMNS]]
        try:
            proba = self._model.predict_proba(X)
            # proba shape: (1, 2) — [P(class_0), P(class_1)]
            return float(proba[0][1])
        except Exception as exc:
            logger.warning("Model prediction failed: %s", exc)
            return 0.0


__all__ = ["MlRsiStrategy"]
