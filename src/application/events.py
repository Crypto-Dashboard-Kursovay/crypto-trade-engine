"""Канонические имена каналов Redis Pub/Sub.

Источник истины для движка. Зеркало `backend/src/domain/events.py` — обе стороны
обязаны совпадать побитово. Никаких магических строк в коде вне этого файла.
"""

# Каналы, которые ПУБЛИКУЕТ движок (бэк подписан)
NEW_TRADE = "engine.new_trade"
BALANCE_UPDATE = "engine.balance_update"
POSITIONS_UPDATE = "engine.positions_update"
ENGINE_STATUS = "engine.status"
STRATEGY_ERROR = "engine.strategy_error"

PUBLISHED_CHANNELS: tuple[str, ...] = (
    NEW_TRADE,
    BALANCE_UPDATE,
    POSITIONS_UPDATE,
    ENGINE_STATUS,
    STRATEGY_ERROR,
)

# Каналы команд, которые ПУБЛИКУЕТ бэк (движок подписан)
COMMAND_START = "engine.commands.start"
COMMAND_STOP = "engine.commands.stop"
COMMAND_UPDATE = "engine.commands.update"

LISTENED_CHANNELS: tuple[str, ...] = (
    COMMAND_START,
    COMMAND_STOP,
    COMMAND_UPDATE,
)
