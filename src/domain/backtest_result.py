"""Контейнер метрик одного backtest-прогона.

Decimal сериализуется как строка (точность), datetime — ISO-8601.
JSON-форма передаётся между процессами (engine → backend) через stdout
backtest_main CLI и сохраняется в backtest_jobs.result (JSONB).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .enums import Side


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    timestamp: datetime
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    pnl: Decimal | None = None      # для пары BUY→SELL заполняется на SELL

    def to_json(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "side": self.side.value,
            "price": str(self.price),
            "size": str(self.size),
            "fee": str(self.fee),
            "pnl": str(self.pnl) if self.pnl is not None else None,
        }


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": str(self.equity),
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_balance: Mapping[str, Decimal]
    final_balance: Mapping[str, Decimal]
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal | None
    trades_count: int
    win_rate: Decimal
    profit_factor: Decimal | None
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "initial_balance": {k: str(v) for k, v in self.initial_balance.items()},
            "final_balance": {k: str(v) for k, v in self.final_balance.items()},
            "total_return_pct": str(self.total_return_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "sharpe_ratio": str(self.sharpe_ratio) if self.sharpe_ratio is not None else None,
            "trades_count": self.trades_count,
            "win_rate": str(self.win_rate),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "trades": [t.to_json() for t in self.trades],
            "equity_curve": [p.to_json() for p in self.equity_curve],
        }


__all__ = ["BacktestResult", "BacktestTrade", "EquityPoint"]
