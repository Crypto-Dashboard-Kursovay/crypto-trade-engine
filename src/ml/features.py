"""Feature engineering for ML strategy: RSI, momentum, Z-score."""

from __future__ import annotations

from collections import deque
from decimal import Decimal


def compute_rsi(
    closes: list[Decimal],
    period: int = 14,
    avg_gain: Decimal | None = None,
    avg_loss: Decimal | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Wilder's RSI. Returns (rsi, avg_gain, avg_loss) for continuation.

    If avg_gain/avg_loss are provided, performs incremental update
    using only the last two closes. Otherwise initialises from the
    full `closes` list using SMA of `period` initial values.
    """
    if len(closes) < period + 1:
        return None, None, None

    if avg_gain is not None and avg_loss is not None and len(closes) >= 2:
        # Incremental Wilder update
        diff = closes[-1] - closes[-2]
        current_gain = diff if diff > 0 else Decimal("0")
        current_loss = -diff if diff < 0 else Decimal("0")
        n = Decimal(period)
        avg_gain = (avg_gain * (n - 1) + current_gain) / n
        avg_loss = (avg_loss * (n - 1) + current_loss) / n
    else:
        # SMA initialisation over first `period+1` closes
        gains = Decimal("0")
        losses = Decimal("0")
        window = closes[-(period + 1):]
        for i in range(1, len(window)):
            diff = window[i] - window[i - 1]
            if diff >= 0:
                gains += diff
            else:
                losses += -diff
        avg_gain = gains / Decimal(period)
        avg_loss = losses / Decimal(period)

    if avg_loss == 0:
        rsi = Decimal("100")
    else:
        rs = avg_gain / avg_loss
        rsi = Decimal("100") - Decimal("100") / (Decimal("1") + rs)

    return rsi, avg_gain, avg_loss


def compute_features(
    rsi_values: list[Decimal],
    rsi_current: Decimal,
) -> dict[str, float]:
    """Build feature vector from RSI history.

    Returns dict with:
      - rsi: current RSI value (0-100)
      - rsi_momentum_3: RSI_{t} - RSI_{t-3}
      - rsi_zscore_24: (RSI_t - μ_24) / σ_24  (μ, σ over last 24 values)
    """
    features: dict[str, float] = {
        "rsi": float(rsi_current),
    }

    # Momentum: ΔRSI = RSI_t − RSI_{t−3}
    if len(rsi_values) >= 4:
        features["rsi_momentum_3"] = float(rsi_current - rsi_values[-4])

    # Z-score over 24-period window
    if len(rsi_values) >= 24:
        window = list(rsi_values[-24:])
        n = len(window)
        mean = sum(window) / n
        var = sum((v - mean) ** 2 for v in window) / n
        try:
            std = float(var.sqrt())
        except Exception:
            std = 0.0
        if std > 0:
            features["rsi_zscore_24"] = float((rsi_current - mean) / Decimal(str(std)))
        else:
            features["rsi_zscore_24"] = 0.0

    return features


__all__ = ["compute_features", "compute_rsi"]
