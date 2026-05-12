"""Контракт каналов: имена в движке должны совпадать с бэковским источником истины.

Если backend submodule доступен — парсим `backend/src/domain/events.py` и сверяем
константы побитово. Если submodule не инициализирован (CI движка изолированно) —
скипаем, но фиксируем канонические значения в `test_canonical_channel_values`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from application.events import (
    BALANCE_UPDATE,
    COMMAND_START,
    COMMAND_STOP,
    COMMAND_UPDATE,
    ENGINE_STATUS,
    NEW_TRADE,
    STRATEGY_ERROR,
)


_CANONICAL: dict[str, str] = {
    "NEW_TRADE": "engine.new_trade",
    "BALANCE_UPDATE": "engine.balance_update",
    "ENGINE_STATUS": "engine.status",
    "STRATEGY_ERROR": "engine.strategy_error",
    "COMMAND_START": "engine.commands.start",
    "COMMAND_STOP": "engine.commands.stop",
    "COMMAND_UPDATE": "engine.commands.update",
}


def test_canonical_channel_values() -> None:
    """Фиксация эталонных значений — обновлять синхронно с бэком."""
    assert NEW_TRADE == _CANONICAL["NEW_TRADE"]
    assert BALANCE_UPDATE == _CANONICAL["BALANCE_UPDATE"]
    assert ENGINE_STATUS == _CANONICAL["ENGINE_STATUS"]
    assert STRATEGY_ERROR == _CANONICAL["STRATEGY_ERROR"]
    assert COMMAND_START == _CANONICAL["COMMAND_START"]
    assert COMMAND_STOP == _CANONICAL["COMMAND_STOP"]
    assert COMMAND_UPDATE == _CANONICAL["COMMAND_UPDATE"]


def _backend_events_path() -> pathlib.Path | None:
    """Ищет backend/src/domain/events.py в монорепо. Возвращает None если нет."""
    here = pathlib.Path(__file__).resolve()
    # engine: /…/crypto-dashboard/trade-engine-crypto/tests/integration/this.py
    # backend: /…/crypto-dashboard/backend/src/domain/events.py
    for parent in here.parents:
        candidate = parent / "backend" / "src" / "domain" / "events.py"
        if candidate.is_file():
            return candidate
    return None


def test_engine_channels_match_backend_source_of_truth() -> None:
    backend_path = _backend_events_path()
    if backend_path is None:
        pytest.skip("backend submodule not available; canonical values check is enough")

    text = backend_path.read_text(encoding="utf-8")
    for name, value in _CANONICAL.items():
        pattern = rf'^\s*{name}\s*=\s*"([^"]+)"'
        match = re.search(pattern, text, re.MULTILINE)
        assert match is not None, f"{name} not found in {backend_path}"
        assert match.group(1) == value, (
            f"channel mismatch for {name}: engine={value!r}, backend={match.group(1)!r}"
        )
