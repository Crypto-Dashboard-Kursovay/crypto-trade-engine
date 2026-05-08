"""Channel names for EventBus.publish. Match Redis Pub/Sub channels in production."""

NEW_TRADE = "engine.new_trade"
BALANCE_UPDATE = "engine.balance_update"
ENGINE_STATUS = "engine.status"
STRATEGY_ERROR = "engine.strategy_error"
