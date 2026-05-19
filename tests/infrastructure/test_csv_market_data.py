import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from domain.enums import TimeFrame
from infrastructure.csv_market_data import CSVMarketDataProvider


def _write_csv(path: Path, rows: list[tuple]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow(r)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CSVMarketDataProvider(tmp_path / "missing.csv", "BTC/USDT", TimeFrame.M1)


def test_csv_reading_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    # Записываем не по порядку, провайдер должен отсортировать
    _write_csv(
        path,
        [
            (2000, "101", "102", "100", "101", "10"),
            (1000, "100", "101", "99", "100.5", "5"),
            (3000, "101.5", "102", "101", "101.8", "7"),
        ],
    )
    provider = CSVMarketDataProvider(path, "BTC/USDT", TimeFrame.M1)
    assert len(provider.rows) == 3
    assert int(provider.rows[0][0]) == 1000


async def test_subscribe_yields_candles_in_order(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    _write_csv(
        path,
        [
            (1000, "100", "101", "99", "100.5", "5"),
            (2000, "100.5", "102", "100", "101.5", "6"),
        ],
    )
    provider = CSVMarketDataProvider(path, "BTC/USDT", TimeFrame.M1)
    out = []
    async for c in provider.subscribe("BTC/USDT", TimeFrame.M1):
        out.append(c)
    assert len(out) == 2
    assert out[0].close == Decimal("100.5")
    assert out[0].timestamp == datetime.fromtimestamp(1, tz=timezone.utc)
    assert out[1].close == Decimal("101.5")


async def test_subscribe_mismatch_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    _write_csv(path, [(1000, "100", "100", "100", "100", "1")])
    provider = CSVMarketDataProvider(path, "BTC/USDT", TimeFrame.M1)
    with pytest.raises(ValueError):
        async for _ in provider.subscribe("ETH/USDT", TimeFrame.M1):
            pass
    with pytest.raises(ValueError):
        async for _ in provider.subscribe("BTC/USDT", TimeFrame.H1):
            pass
