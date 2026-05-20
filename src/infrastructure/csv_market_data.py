"""CSVMarketDataProvider — читает parquet/CSV с историческими свечами.

Формат parquet:
- timestamp: int64 (миллисекунды UNIX)
- open/high/low/close/volume: string — точное Decimal-представление (не float!)

Если по каким-то причинам колонки числовые — мы их аккуратно конвертим через
str(value), но точность float'а может потеряться. Поэтому fetch_historical.py
должен писать строки.
"""

from __future__ import annotations

import asyncio
import csv
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from domain.enums import TimeFrame
from domain.interfaces import MarketDataProvider
from domain.models import Candle

try:  # pragma: no cover — опциональная зависимость
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]


def _decimal(v: object) -> Decimal:
    return Decimal(str(v))


def _to_candle(symbol: str, timeframe: TimeFrame, row: tuple) -> Candle:
    # row: (timestamp_ms, open, high, low, close, volume) — порядок зафиксирован
    ts_ms, o, h, l, c, v = row
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc),
        open=_decimal(o),
        high=_decimal(h),
        low=_decimal(l),
        close=_decimal(c),
        volume=_decimal(v),
    )


def _load_rows(path: Path) -> list[tuple]:
    """Загружает все строки из файла в память. Возвращает список tuple'ов
    `(ts_ms, open, high, low, close, volume)` в порядке возрастания timestamp.
    """
    if path.suffix == ".parquet":
        if pd is None:
            raise RuntimeError(
                "pandas/pyarrow not installed — install with `pip install -e .[backtest]`"
            )
        df = pd.read_parquet(path)
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        df = df[cols].sort_values("timestamp")
        return [tuple(row) for row in df.itertuples(index=False, name=None)]
    if path.suffix == ".csv":
        with path.open(newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return []
            rows = [tuple(r) for r in reader]
        rows.sort(key=lambda r: int(r[0]))
        return rows
    raise ValueError(f"Unsupported file extension for market data: {path.suffix}")


class CSVMarketDataProvider(MarketDataProvider):
    def __init__(
        self,
        path: Path | str,
        symbol: str,
        timeframe: TimeFrame,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"market data file not found: {self._path}")
        self._symbol = symbol
        self._timeframe = timeframe
        rows = _load_rows(self._path)
        # Фильтрация по диапазону [start_ms, end_ms] — бэктест считает метрики
        # строго за выбранный период, а не по всему файлу.
        if start_ms is not None or end_ms is not None:
            lo = start_ms if start_ms is not None else 0
            hi = end_ms if end_ms is not None else 2**63 - 1
            rows = [r for r in rows if lo <= int(r[0]) <= hi]
        self._rows = rows

    @property
    def rows(self) -> list[tuple]:
        return self._rows

    def subscribe(
        self, symbol: str, timeframe: TimeFrame
    ) -> AsyncIterator[Candle]:
        if symbol != self._symbol:
            raise ValueError(
                f"provider initialized for {self._symbol}, got {symbol}"
            )
        if timeframe != self._timeframe:
            raise ValueError(
                f"provider initialized for {self._timeframe.value}, got {timeframe.value}"
            )
        return self._iter()

    async def _iter(self) -> AsyncIterator[Candle]:
        for row in self._rows:
            yield _to_candle(self._symbol, self._timeframe, row)
            # отдаём control event-loop'у, чтобы SimulatedExchange (отдельная
            # таска, если будет) и метрики успели проработать
            await asyncio.sleep(0)


__all__ = ["CSVMarketDataProvider"]
