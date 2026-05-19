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
import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from application.backtest_runner import BacktestRunner
from application.order_executor import OrderExecutor
from application.risk_manager import RiskManager
from domain.enums import TimeFrame
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


async def _run(config: dict[str, Any]) -> dict[str, Any]:
    strategy_name = str(config["strategy"])
    symbol = str(config["symbol"])
    timeframe = TimeFrame(str(config["timeframe"]))
    parquet_path = Path(str(config["parquet_path"]))

    params = dict(config.get("params", {}))
    initial_balance = {
        cur: Decimal(str(v)) for cur, v in config["initial_balance"].items()
    }
    fee_rate = Decimal(str(config.get("fee_rate", "0.001")))
    slippage = Decimal(str(config.get("slippage", "0.0005")))
    max_position_pct = Decimal(str(config.get("max_position_pct", "0.95")))
    min_lot = Decimal(str(config.get("min_lot", "0.00001")))
    snapshot_every = int(config.get("equity_snapshot_every", 100))

    market_data = CSVMarketDataProvider(parquet_path, symbol, timeframe)
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
    return result.to_json()


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
