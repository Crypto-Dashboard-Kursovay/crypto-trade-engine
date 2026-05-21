"""CLI обвязка вокруг BacktestRunner для запуска из backend subprocess'а.

Использование:
    python -m backtest_main --config /path/to/config.json

Конфиг JSON:
{
  "strategy": "SmaCross",
  "params": {"fast_period": 5, "slow_period": 20, "order_size": "0.001"},
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "parquet_path": "/data/historical/binance_btc_usdt_1h.parquet",
  "initial_balance": {"USDT": "10000"},
  "fee_rate": "0.001",            # необязательно
  "slippage": "0.0005",           # необязательно
  "max_position_pct": "0.5",      # необязательно (RiskManager)
  "min_lot": "0.00001",           # необязательно (RiskManager)
  "equity_snapshot_every": 100    # необязательно
}

На stdout пишет BacktestResult.to_json() при успехе (exit 0).
При ошибке — JSON {"error": "..."} (exit 1).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

# Логи backtest_main валим в stderr — stdout полностью для JSON-результата.
# structlog по умолчанию пишет в stdout через PrintLoggerFactory —
# переопределяем до того как любой модуль сделает get_logger().
import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

import structlog
structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

from application.backtest_runner import BacktestRunner
from application.order_executor import OrderExecutor
from application.risk_manager import RiskManager
from domain.enums import TimeFrame
from infrastructure.ccxt_historical import fetch_ohlcv_rows, save_parquet, timeframe_ms
from infrastructure.csv_market_data import CSVMarketDataProvider
from infrastructure.in_memory_event_bus import InMemoryEventBus
from infrastructure.simulated_exchange import SimulatedExchangeAdapter
from strategies import default_registry


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a backtest from JSON config")
    p.add_argument("--config", required=True, type=Path, help="path to config JSON")
    return p.parse_args(argv)


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _parquet_range(path: Path) -> tuple[int, int] | None:
    """Возвращает (min_ts_ms, max_ts_ms) кэша или None, если файла/данных нет."""
    actual = _find_data_file(path)
    if actual is None:
        return None
    try:
        rows = _load_data_file(actual)
        if not rows:
            return None
        timestamps = [int(r[0]) for r in rows]
        return min(timestamps), max(timestamps)
    except Exception:
        return None


def _find_data_file(path: Path) -> Path | None:
    """Ищет parquet или CSV-файл по базовому пути (без суффикса)."""
    base = path.with_suffix("")
    parquet = base.with_suffix(".parquet")
    csv_file = base.with_suffix(".csv")
    if parquet.exists():
        return parquet
    if csv_file.exists():
        return csv_file
    return None


def _load_data_file(path: Path) -> list[tuple]:
    """Загружает все строки из parquet или CSV в память."""
    try:
        import pandas as pd
    except ImportError:
        pd = None

    if path.suffix == ".parquet" and pd is not None:
        df = pd.read_parquet(path)
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        df = df[cols].sort_values("timestamp")
        return [tuple(row) for row in df.itertuples(index=False, name=None)]

    if path.suffix == ".csv":
        import csv
        with path.open(newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return []
            rows = [tuple(r) for r in reader]
        rows.sort(key=lambda r: int(r[0]))
        return rows

    return []


def _resolve_actual_path(parquet_path: Path) -> Path:
    """Возвращает реальный путь к файлу данных (parquet или CSV)."""
    found = _find_data_file(parquet_path)
    if found is not None:
        return found
    return parquet_path  # вернём канонический паркет-путь


async def _ensure_market_data(
    *,
    exchange: str,
    symbol: str,
    timeframe: TimeFrame,
    parquet_path: Path,
    start_ms: int | None,
    end_ms: int | None,
) -> CSVMarketDataProvider:
    """Гибрид: parquet/CSV-кэш, иначе докачиваем с реальной биржи и кэшируем.

    Кэш считается покрывающим диапазон, если его свечи начинаются не позже start_ms
    и заканчиваются не раньше чем за один таймфрейм до end_ms.
    """
    tf_str = str(timeframe)
    covered = False
    actual_path = _find_data_file(parquet_path)
    if actual_path is not None and start_ms is not None and end_ms is not None:
        rng = _parquet_range(parquet_path)
        if rng is not None:
            file_min, file_max = rng
            step = timeframe_ms(tf_str)
            covered = file_min <= start_ms and file_max >= end_ms - step
    elif actual_path is not None:
        covered = True

    if not covered:
        if start_ms is None or end_ms is None:
            raise ValueError(
                "date_from_ms/date_to_ms required to fetch historical data"
            )
        rows = await fetch_ohlcv_rows(exchange, symbol, tf_str, start_ms, end_ms)
        if not rows:
            raise ValueError(
                f"биржа {exchange} не вернула исторических данных для {symbol} "
                f"{tf_str} в выбранном диапазоне"
            )
        save_parquet(parquet_path, rows)
        actual_path = _find_data_file(parquet_path)
        if actual_path is None:
            actual_path = parquet_path

    provider = CSVMarketDataProvider(
        actual_path, symbol, timeframe, start_ms=start_ms, end_ms=end_ms
    )
    if not provider.rows:
        raise ValueError(
            f"нет свечей {symbol} {tf_str} в выбранном диапазоне дат"
        )
    return provider


async def _run(config: dict[str, Any]) -> dict[str, Any]:
    strategy_name = str(config["strategy"])
    exchange = str(config.get("exchange", "binance"))
    symbol = str(config["symbol"])
    timeframe = TimeFrame(str(config["timeframe"]))
    parquet_path = Path(str(config["parquet_path"]))
    start_ms = config.get("date_from_ms")
    end_ms = config.get("date_to_ms")
    start_ms = int(start_ms) if start_ms is not None else None
    end_ms = int(end_ms) if end_ms is not None else None

    params = dict(config.get("params", {}))
    initial_balance = {
        cur: Decimal(str(v)) for cur, v in config["initial_balance"].items()
    }
    fee_rate = Decimal(str(config.get("fee_rate", "0.001")))
    slippage = Decimal(str(config.get("slippage", "0.0005")))
    max_position_pct = Decimal(str(config.get("max_position_pct", "0.95")))
    min_lot = Decimal(str(config.get("min_lot", "0.00001")))
    snapshot_every = int(config.get("equity_snapshot_every", 100))

    market_data = await _ensure_market_data(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        parquet_path=parquet_path,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    # Warmup для стратегии берём из тех же исторических свечей —
    # SimExchange отдаст их через fetch_ohlcv. Однако наши новые стратегии
    # инициализируются по on_candle, поэтому warmup пустой OK для большинства.
    exchange = SimulatedExchangeAdapter(
        initial_balance=initial_balance,
        symbol=symbol,
        fee_rate=fee_rate,
        slippage=slippage,
        warmup_candles=[],
    )

    quote = symbol.split("/")[1]
    risk = RiskManager(
        adapter=exchange,
        max_position_pct=max_position_pct,
        min_lot=min_lot,
        quote_currency=quote,
    )

    registry = default_registry()
    cls = registry.resolve(strategy_name)
    strategy = cls(symbol=symbol, timeframe=timeframe, **params)

    event_bus = InMemoryEventBus()
    bot_id = uuid.uuid4()
    executor = OrderExecutor(adapter=exchange, event_bus=event_bus, bot_id=bot_id)

    runner = BacktestRunner(
        strategy=strategy,
        market_data=market_data,
        exchange=exchange,
        risk=risk,
        executor=executor,
        event_bus=event_bus,
        bot_id=bot_id,
        equity_snapshot_every=snapshot_every,
    )
    result = await runner.run()
    result_json = result.to_json()
    # Добавляем диагностику: сколько свечей, какой диапазон дат
    rows = market_data.rows
    result_json["_candle_count"] = len(rows)
    result_json["_candle_range"] = {
        "from_ms": int(rows[0][0]) if rows else None,
        "to_ms": int(rows[-1][0]) if rows else None,
        "requested_from_ms": start_ms,
        "requested_to_ms": end_ms,
    }
    return result_json


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = _load_config(args.config)
        result = asyncio.run(_run(config))
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:  # noqa: BLE001 — surface error to backend
        json.dump({"error": str(exc), "type": type(exc).__name__}, sys.stdout)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
