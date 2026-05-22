"""Walk-Forward training pipeline for ML RSI strategy.

Label construction:
  y_t = 1  if  P_{t+h} > P_t * (1 + 2*fee),  h = horizon, fee = 0.001

Training:
  Monthly walk-forward: train on trailing N months, predict next 1 month.
  Each window produces one model saved to data/models/ml_rsi/.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .features import compute_features, compute_rsi

logger = logging.getLogger(__name__)

# --- Defaults matching thesis Section 6 ---
FEE: Decimal = Decimal("0.001")          # 0.1% spot fee
HORIZON: int = 1                          # predict next candle
TRAIN_MONTHS: int = 6                     # trailing window for training
RSI_PERIOD: int = 14                      # RSI(14)
MIN_TRAIN_SAMPLES: int = 500              # minimum samples to train
THRESHOLD_BUY: float = 0.55               # default BUY probability threshold

# Feature column order (must match model training)
FEATURE_COLUMNS: list[str] = ["rsi", "rsi_momentum_3", "rsi_zscore_24"]

_ModelType = Any  # LightGBM Booster or sklearn GradientBoostingClassifier


def _build_labels(
    closes: list[Decimal],
    horizon: int = HORIZON,
    fee: Decimal = FEE,
) -> list[int]:
    """y[t] = 1 if close[t+horizon] > close[t] * (1 + 2*fee)."""
    threshold_mult = Decimal("1") + 2 * fee
    labels: list[int] = []
    for i in range(len(closes) - horizon):
        if closes[i + horizon] > closes[i] * threshold_mult:
            labels.append(1)
        else:
            labels.append(0)
    return labels


def _build_features(
    closes: list[Decimal],
    rsi_period: int = RSI_PERIOD,
) -> list[dict[str, float]]:
    """Slide over `closes` and compute RSI + derived features."""
    features: list[dict[str, float]] = []
    rsi_values: deque[Decimal] = deque(maxlen=24)
    avg_gain: Decimal | None = None
    avg_loss: Decimal | None = None

    window: deque[Decimal] = deque(maxlen=rsi_period + 1)

    for i, close in enumerate(closes):
        window.append(close)
        if len(window) < rsi_period + 1:
            continue

        rsi, avg_gain, avg_loss = compute_rsi(
            list(window), rsi_period, avg_gain, avg_loss
        )
        if rsi is None:
            continue

        rsi_values.append(rsi)
        feat = compute_features(list(rsi_values), rsi)
        # Only emit features when we have the full set
        if len(feat) == len(FEATURE_COLUMNS):
            features.append(feat)

    return features


def _parse_month(dt: datetime) -> str:
    """Format datetime to YYYY-MM string label."""
    return dt.strftime("%Y-%m")


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def train_walk_forward(
    data_dir: str | Path,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    exchange: str = "binance",
    rsi_period: int = RSI_PERIOD,
    train_months: int = TRAIN_MONTHS,
    horizon: int = HORIZON,
    fee: Decimal = FEE,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Walk-forward train: monthly sliding window over historical data.

    Args:
        data_dir: Path to parquet/csv OHLCV files (output of fetch_historical.py).
        symbol: Trading pair, e.g. 'BTC/USDT'.
        timeframe: Candle timeframe, e.g. '1h'.
        rsi_period: RSI calculation period.
        train_months: Number of trailing months to use for each training window.
        horizon: Prediction horizon in candles.
        fee: Exchange fee as Decimal.
        output_dir: Directory for model checkpoints. Defaults to
                     <project>/data/models/ml_rsi/.

    Returns:
        dict with per-month metrics (sharpe, profit_factor, n_samples, ...).
    """
    try:
        import lightgbm as lgb
        from sklearn.metrics import accuracy_score
    except ImportError:
        raise ImportError(
            "ML dependencies required. Install with: pip install lightgbm scikit-learn"
        )

    # --- 1. Load OHLCV ---
    closes, timestamps = _load_closes(data_dir, symbol, timeframe, exchange)
    if len(closes) < MIN_TRAIN_SAMPLES:
        raise ValueError(
            f"Need at least {MIN_TRAIN_SAMPLES} candles, got {len(closes)}"
        )

    # --- 2. Build labels and features ---
    labels = _build_labels(closes, horizon, fee)
    features = _build_features(closes, rsi_period)

    # Align features and labels: feature[i] corresponds to label[i]
    # Features start after RSI warmup (rsi_period + 1 candles in)
    offset = len(closes) - len(features) - horizon
    aligned_labels = labels[offset : offset + len(features)]
    aligned_feats = features[: len(aligned_labels)]

    if len(aligned_feats) < MIN_TRAIN_SAMPLES:
        raise ValueError(
            f"Need at least {MIN_TRAIN_SAMPLES} aligned samples, got {len(aligned_feats)}"
        )

    # --- 3. Determine month boundaries ---
    # Use timestamps corresponding to aligned features
    aligned_timestamps = timestamps[offset + horizon : offset + horizon + len(aligned_feats)]
    month_indices, month_labels = _monthly_split(aligned_timestamps)

    # --- 4. Walk-forward training ---
    output_dir = Path(output_dir or "data/models/ml_rsi")
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe,
                                "months": {}}

    # Start from month index `train_months` (need that many for training)
    for test_idx in range(train_months, len(month_labels)):
        train_start_idx = month_indices[test_idx - train_months]
        test_start_idx = month_indices[test_idx]
        test_end_idx = month_indices[test_idx + 1]  # sentinel always present

        X_train = [_feat_row(aligned_feats[i]) for i in range(train_start_idx, test_start_idx)]
        y_train = aligned_labels[train_start_idx:test_start_idx]
        X_test = [_feat_row(aligned_feats[i]) for i in range(test_start_idx, test_end_idx)]
        y_test = aligned_labels[test_start_idx:test_end_idx]

        if len(y_train) < MIN_TRAIN_SAMPLES or len(y_test) < 50:
            continue

        # Train model
        model = lgb.LGBMClassifier(
            n_estimators=200,
            max_depth=4,
            num_leaves=15,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbose=-1,
        )
        model.fit(X_train, y_train)

        # Evaluate
        y_pred = model.predict(X_test)
        accuracy = accuracy_score(y_test, y_pred)
        proba = model.predict_proba(X_test)

        # Rough Sharpe estimate from test predictions
        test_month = month_labels[test_idx]
        metrics = _evaluate_trades(y_test, proba, THRESHOLD_BUY)

        # Save model
        model_path = output_dir / f"{symbol.replace('/', '_')}_{timeframe}_{test_month}.joblib"
        _save_model(model_path, model)
        logger.info("Model saved: %s  accuracy=%.3f  sharpe=%.2f  pf=%.2f",
                     model_path, accuracy, metrics.get("sharpe", 0), metrics.get("profit_factor", 0))

        results["months"][test_month] = {
            "train_samples": len(y_train),
            "test_samples": len(y_test),
            "accuracy": round(accuracy, 4),
            **{k: round(v, 4) if isinstance(v, float) else v for k, v in metrics.items()},
            "model_path": str(model_path),
        }

    # --- 5. Write summary ---
    summary_path = output_dir / f"{symbol.replace('/', '_')}_{timeframe}_summary.json"
    summary_path.write_text(json.dumps(results, indent=2, default=str))
    logger.info("Summary written: %s", summary_path)

    return results


def load_model(model_dir: str | Path, symbol: str, timeframe: str, month: str) -> tuple[_ModelType, dict[str, Any]]:
    """Load a trained model and its metadata for a given month.

    Args:
        model_dir: Directory containing model checkpoints.
        symbol: Trading pair.
        timeframe: Candle timeframe.
        month: Month label in YYYY-MM format.

    Returns:
        Tuple of (model, metadata_dict).
    """
    import joblib

    model_dir = Path(model_dir)
    model_path = model_dir / f"{symbol.replace('/', '_')}_{timeframe}_{month}.joblib"

    if not model_path.exists():
        # Try loading the nearest available model (previous month)
        return _load_nearest_model(model_dir, symbol, timeframe, month)

    model = joblib.load(model_path)
    # Load summary for metadata
    summary_path = model_dir / f"{symbol.replace('/', '_')}_{timeframe}_summary.json"
    metadata: dict[str, Any] = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        metadata = summary.get("months", {}).get(month, {})

    return model, metadata


def _load_closes(
    data_dir: str | Path,
    symbol: str,
    timeframe: str,
    exchange: str | None = None,
) -> tuple[list[Decimal], list[int]]:
    """Load OHLCV close prices and timestamps from parquet or CSV."""
    import pandas as pd

    data_dir = Path(data_dir)
    # File naming: {exchange}_{symbol_lower}_{timeframe}.parquet/csv
    # e.g. binance_btc_usdt_1h.parquet
    safe_sym = symbol.replace("/", "_").lower()
    prefix = f"{exchange}_{safe_sym}" if exchange else safe_sym
    parquet_path = data_dir / f"{prefix}_{timeframe}.parquet"
    csv_path = data_dir / f"{prefix}_{timeframe}.csv"

    # Also try without exchange prefix
    alt_parquet = data_dir / f"{safe_sym}_{timeframe}.parquet"
    alt_csv = data_dir / f"{safe_sym}_{timeframe}.csv"

    if parquet_path.exists():
        df = pd.read_parquet(parquet_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path)
    elif alt_parquet.exists():
        df = pd.read_parquet(alt_parquet)
    elif alt_csv.exists():
        df = pd.read_csv(alt_csv)
    else:
        raise FileNotFoundError(
            f"No data found for {symbol} {timeframe} in {data_dir} "
            f"(tried {parquet_path.name}, {csv_path.name})"
        )

    # Column names: 'timestamp', 'open', 'high', 'low', 'close', 'volume'
    timestamps = df["timestamp"].tolist()
    closes = [Decimal(str(c)) for c in df["close"]]
    return closes, timestamps


def _monthly_split(
    timestamps: list[int],
) -> tuple[list[int], list[str]]:
    """Split data into monthly chunks. Returns (start_indices, month_labels).
    
    start_indices[i] is the first index of month_labels[i].
    An extra sentinel len(timestamps) is appended as the end boundary.
    """
    if not timestamps:
        return [0], []

    start_indices: list[int] = [0]
    month_labels: list[str] = []
    current_month: str | None = None

    for i, ts_ms in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        month = _parse_month(dt)
        if month != current_month:
            if current_month is not None:
                start_indices.append(i)
                month_labels.append(current_month)
            current_month = month

    if current_month is not None:
        month_labels.append(current_month)

    start_indices.append(len(timestamps))  # sentinel for last month
    return start_indices, month_labels


def _feat_row(feat: dict[str, float]) -> list[float]:
    return [feat.get(col, 0.0) for col in FEATURE_COLUMNS]


def _evaluate_trades(
    y_true: list[int],
    proba: Any,
    threshold: float,
) -> dict[str, float]:
    """Simulate trades based on model predictions and compute simple metrics."""
    prob_class1 = [p[1] for p in proba] if proba is not None else []
    trades: list[float] = []
    in_position = False
    pnl_sum = 0.0
    wins = 0
    losses = 0

    for i, (true_label, p) in enumerate(zip(y_true, prob_class1)):
        if not in_position and p > threshold and i < len(y_true) - 1:
            in_position = True
            trades.append(0.0)  # place buy
        elif in_position:
            # Exit on next candle (simplified: use true label as PnL proxy)
            gain = 0.01 if true_label == 1 else -0.01
            trades.append(gain)
            pnl_sum += gain
            if gain > 0:
                wins += 1
            else:
                losses += 1
            in_position = False

    total_trades = wins + losses
    if total_trades == 0:
        return {"total_trades": 0, "sharpe": 0.0, "profit_factor": 0.0}

    avg_pnl = pnl_sum / total_trades
    var = sum((t - avg_pnl) ** 2 for t in trades if t != 0) / max(1, len([t for t in trades if t != 0]))
    std = var ** 0.5
    sharpe = avg_pnl / std if std > 0 else 0.0

    profit_sum = sum(t for t in trades if t > 0)
    loss_sum = -sum(t for t in trades if t < 0)
    profit_factor = profit_sum / loss_sum if loss_sum > 0 else float("inf")

    return {
        "total_trades": total_trades,
        "sharpe": round(sharpe, 4),
        "profit_factor": round(profit_factor, 4),
        "win_rate": round(wins / total_trades, 4) if total_trades else 0.0,
    }


def _save_model(path: Path, model: _ModelType) -> None:
    import joblib
    joblib.dump(model, str(path))


def _load_nearest_model(
    model_dir: Path,
    symbol: str,
    timeframe: str,
    month: str,
) -> tuple[_ModelType | None, dict[str, Any]]:
    """Find the nearest model checkpoint before or at the given month."""
    prefix = f"{symbol.replace('/', '_')}_{timeframe}_"
    files = sorted(model_dir.glob(f"{prefix}*.joblib"))

    if not files:
        raise FileNotFoundError(f"No models found in {model_dir} for {symbol} {timeframe}")

    # Find the model with month label <= requested month
    best: Path | None = None
    for f in files:
        stem = f.stem.replace(prefix, "")
        if stem <= month:
            best = f

    if best is None:
        best = files[0]  # fallback to earliest

    import joblib
    model = joblib.load(str(best))
    return model, {"source_month": best.stem.replace(prefix, "")}


__all__ = ["load_model", "train_walk_forward", "FEATURE_COLUMNS", "FEE", "HORIZON"]
