"""Registry стратегий: строковое имя `strategy_class` (из БД) → класс стратегии.

Бэк хранит `bots.strategy_class` как string. Когда команда `engine.commands.start`
приходит с этим именем, движок резолвит его в класс через `StrategyRegistry.resolve`.
"""

from __future__ import annotations

from domain.interfaces import Strategy

from .sma_cross import SmaCross


class StrategyRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Strategy]] = {}

    def register(self, name: str, cls: type[Strategy]) -> None:
        self._classes[name] = cls

    def resolve(self, name: str) -> type[Strategy]:
        if name not in self._classes:
            raise KeyError(
                f"Unknown strategy '{name}'. Registered: {sorted(self._classes)}"
            )
        return self._classes[name]


def default_registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register("SmaCross", SmaCross)
    return reg


__all__ = ["SmaCross", "StrategyRegistry", "default_registry"]
