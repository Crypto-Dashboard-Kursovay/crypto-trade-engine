"""Registry стратегий: строковое имя `strategy_class` (из БД) → класс стратегии.

Бэк хранит `bots.strategy_class` как string. Когда команда `engine.commands.start`
приходит с этим именем, движок резолвит его в класс через `StrategyRegistry.resolve`.
"""

from __future__ import annotations

from domain.interfaces import Strategy

from .adaptive_rsi import AdaptiveRsiStrategy
from .bollinger_bands import BollingerBands
from .bollinger_rsi import BollingerRsi
from .dca import DcaStrategy
from .macd_cross import MacdCross
from .ml_rsi import MlRsiStrategy
from .rsi_threshold import RsiThreshold
from .sma_cross import SmaCross
from .spot_grid import SpotGridStrategy


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
    reg.register("RsiThreshold", RsiThreshold)
    reg.register("AdaptiveRsi", AdaptiveRsiStrategy)
    reg.register("MacdCross", MacdCross)
    reg.register("BollingerBands", BollingerBands)
    reg.register("BollingerRsi", BollingerRsi)
    reg.register("DcaStrategy", DcaStrategy)
    reg.register("SpotGridStrategy", SpotGridStrategy)
    reg.register("MlRsi", MlRsiStrategy)
    return reg


__all__ = [
    "AdaptiveRsiStrategy",
    "BollingerBands",
    "BollingerRsi",
    "DcaStrategy",
    "MacdCross",
    "MlRsiStrategy",
    "RsiThreshold",
    "SmaCross",
    "SpotGridStrategy",
    "StrategyRegistry",
    "default_registry",
]
