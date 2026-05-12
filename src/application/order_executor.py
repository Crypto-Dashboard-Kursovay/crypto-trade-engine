import logging
import uuid
from collections.abc import Mapping
from typing import Any

from domain.enums import OrderType
from domain.exceptions import OrderExecutionError
from domain.interfaces import EventBus, ExchangeAdapter
from domain.models import Order, Signal

from .events import NEW_TRADE

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Turns an approved Signal into an exchange order and emits NEW_TRADE on success.

    `bot_id` обязателен в payload — backend resolver идёт по нему (см. Phase 3.1
    в плане), без bot_id событие не получится корректно привязать к боту.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        event_bus: EventBus,
        bot_id: uuid.UUID,
    ) -> None:
        self._adapter = adapter
        self._event_bus = event_bus
        self._bot_id = bot_id

    async def execute(self, signal: Signal) -> Order:
        order_type = OrderType.LIMIT if signal.price is not None else OrderType.MARKET
        try:
            order = await self._adapter.create_order(
                symbol=signal.symbol,
                side=signal.side,
                type=order_type,
                size=signal.size,
                price=signal.price,
            )
        except Exception as exc:
            raise OrderExecutionError(
                f"failed to create order for {signal.strategy_name}/{signal.symbol}: {exc}"
            ) from exc

        await self._event_bus.publish(NEW_TRADE, _order_payload(order, signal, self._bot_id))
        logger.info(
            "Order placed: %s %s %s @ %s (id=%s, bot=%s)",
            order.side.value,
            order.size,
            order.symbol,
            order.price,
            order.order_id,
            self._bot_id,
        )
        return order


def _order_payload(order: Order, signal: Signal, bot_id: uuid.UUID) -> Mapping[str, Any]:
    return {
        "bot_id": str(bot_id),
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "type": order.type.value,
        "size": str(order.size),
        "price": str(order.price) if order.price is not None else None,
        "status": order.status.value,
        "strategy": signal.strategy_name,
    }
