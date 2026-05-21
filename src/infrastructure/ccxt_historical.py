"""Загрузка исторических OHLCV с реальной биржи через CCXT REST + кэш в parquet.

Используется бэктестом как fallback, когда локального parquet-кэша нет (или он не
покрывает запрошенный диапазон). Тянем данные с **реальной** биржи (без
set_sandbox_mode), пагинируем `fetch_ohlcv` по `since`, отдаём строки
`(ts_ms, open, high, low, close, volume)` со строковыми Decimal-представлениями.
"""

from __future__ import annotations

from pathlib import Path

from .logging import get_logger

logger = get_logger(__name__)


_TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def timeframe_ms(timeframe: str) -> int:
    if timeframe not in _TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return _TIMEFRAME_MS[timeframe]


async def fetch_ohlcv_rows(
    exchange_name: str,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
) -> list[tuple]:
    """Скачивает свечи [since_ms, until_ms] с реальной биржи.

    Возвращает список tuple'ов `(ts_ms, open, high, low, close, volume)` (str для
    OHLCV — точный Decimal). Поднимает RuntimeError если ccxt не знает биржу.
    """
    import ccxt.async_support as ccxt_async  # тяжёлый импорт — внутри функции

    klass = getattr(ccxt_async, exchange_name, None)
    if klass is None:
        raise RuntimeError(f"ccxt has no async client for {exchange_name}")

    step = timeframe_ms(timeframe)
    client = klass({"enableRateLimit": True})
    rows: list[tuple] = []
    try:
        since = since_ms
        while since < until_ms:
            batch = await client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            for ts, o, h, l, c, v in batch:
                if ts > until_ms:
                    break
                rows.append((int(ts), str(o), str(h), str(l), str(c), str(v)))
            last_ts = batch[-1][0]
            if last_ts < since:
                break
            since = last_ts + step
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass
    logger.info(
        "historical_fetched",
        exchange=exchange_name,
        symbol=symbol,
        timeframe=timeframe,
        rows=len(rows),
    )
    return rows


def save_parquet(path: Path | str, rows: list[tuple]) -> None:
    """Пишет/мёрджит свечи в parquet-кэш (dedup по timestamp, сортировка по возр.).

    Если pandas/pyarrow не установлены — падает в CSV-формат.
    """
    path = Path(path)
    try:
        import pandas as pd
    except ImportError:
        _save_csv(path.with_suffix(".csv"), rows)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    df_new = pd.DataFrame(rows, columns=cols)
    if path.exists():
        try:
            df_old = pd.read_parquet(path)[cols]
            df_new = pd.concat([df_old, df_new], ignore_index=True)
        except Exception:
            pass
    df_new = df_new.astype(
        {
            "timestamp": "int64",
            "open": "string",
            "high": "string",
            "low": "string",
            "close": "string",
            "volume": "string",
        }
    )
    df_new.sort_values("timestamp", inplace=True)
    df_new.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    df_new.to_parquet(path, index=False)


def _save_csv(path: Path, rows: list[tuple]) -> None:
    """Сохраняет свечи в CSV-формат (fallback, когда pandas/pyarrow нет)."""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if path.exists() else "w"
    with path.open(mode, newline="") as fh:
        writer = csv.writer(fh)
        if mode == "w":
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        existing: set[int] = set()
        if path.exists() and mode == "a":
            from io import StringIO
            with path.open("r", newline="") as rf:
                reader = csv.reader(rf)
                next(reader, None)
                for r in reader:
                    if r:
                        existing.add(int(r[0]))
        for row in rows:
            if int(row[0]) not in existing:
                writer.writerow(row)
                existing.add(int(row[0]))


__all__ = ["fetch_ohlcv_rows", "save_parquet", "timeframe_ms"]
