"""Entrypoint движка.

Wire-up: настройки → Redis/БД → EventBus → Orchestrator → CommandListener + StateManager.
SIGTERM/SIGINT → graceful shutdown (отмена тасок, остановка стратегий, закрытие коннектов).
"""

from __future__ import annotations

import asyncio
import signal
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from redis.asyncio import Redis

from application.orchestrator import EngineOrchestrator, RiskConfig
from domain.interfaces import ExchangeAdapter, MarketDataProvider
from infrastructure.ccxt_exchange_adapter import CCXTExchangeAdapter
from infrastructure.ccxt_market_data import CCXTMarketDataProvider
from infrastructure.command_listener import CommandListener
from infrastructure.db import create_engine, create_session_factory
from infrastructure.db_repositories import (
    BotRepository,
    CredentialRepository,
    DecryptedCredential,
)
from infrastructure.logging import configure_logging, get_logger
from infrastructure.metrics import EngineMetricsServer
from infrastructure.redis_event_bus import RedisEventBus
from infrastructure.settings import load_settings
from infrastructure.state_manager import StateManager
from strategies import default_registry


def _exchange_factory(cred: DecryptedCredential) -> ExchangeAdapter:
    return CCXTExchangeAdapter(
        exchange_name=cred.exchange,
        api_key=cred.api_key,
        api_secret=cred.api_secret,
        passphrase=cred.passphrase,
        testnet=True,
    )


def _market_data_factory(adapter: ExchangeAdapter) -> MarketDataProvider:
    if not isinstance(adapter, CCXTExchangeAdapter):
        raise TypeError(f"Expected CCXTExchangeAdapter for live market data, got {type(adapter).__name__}")
    return CCXTMarketDataProvider(adapter.native)


async def _run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = get_logger(__name__)

    db_engine = create_engine(settings.database_url)
    session_factory = create_session_factory(db_engine)

    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=False)

    event_bus = RedisEventBus(redis)
    fernet = Fernet(settings.encryption_key.encode())
    bot_repo = BotRepository(session_factory)
    credential_repo = CredentialRepository(session_factory, fernet)

    # Phase 1: дефолтные риск-параметры. В Phase 3+ можно вынести в env или per-bot.
    risk_config = RiskConfig(
        max_position_pct=Decimal("0.1"),
        min_lot=Decimal("0.0001"),
        quote_currency="USDT",
    )

    orchestrator = EngineOrchestrator(
        bot_repo=bot_repo,
        credential_repo=credential_repo,
        strategy_registry=default_registry(),
        event_bus=event_bus,
        exchange_factory=_exchange_factory,
        market_data_factory=_market_data_factory,
        risk_config=risk_config,
    )

    listener = CommandListener(
        redis, orchestrator, dedup_ttl_sec=settings.command_dedup_ttl_sec
    )
    state_manager = StateManager(
        redis=redis,
        event_bus=event_bus,
        orchestrator=orchestrator,
        heartbeat_interval_sec=settings.heartbeat_interval_sec,
        balance_poll_interval_sec=settings.balance_poll_interval_sec,
        snapshot_ttl_sec=settings.state_snapshot_ttl_sec,
    )
    metrics_server: EngineMetricsServer | None = None
    if settings.metrics_enabled:
        metrics_server = await EngineMetricsServer.start(
            settings.metrics_host,
            settings.metrics_port,
            orchestrator,
        )
        logger.info(
            "metrics_server_started",
            host=settings.metrics_host,
            port=settings.metrics_port,
        )

    stop_event = asyncio.Event()

    def _handle_signal(sig_name: str) -> None:
        if stop_event.is_set():
            return
        logger.info("signal_received", signal=sig_name)
        listener.stop()
        state_manager.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _handle_signal, "SIGTERM")
    loop.add_signal_handler(signal.SIGINT, _handle_signal, "SIGINT")

    logger.info(
        "engine_starting",
        redis=_redact_url(settings.redis_url),
        db=_redact_url(settings.database_url),
    )

    listener_task = asyncio.create_task(listener.run(), name="command-listener")
    state_task = asyncio.create_task(state_manager.run(), name="state-manager")

    try:
        await stop_event.wait()
    finally:
        logger.info("engine_shutting_down")
        await asyncio.gather(listener_task, state_task, return_exceptions=True)
        if metrics_server is not None:
            await metrics_server.stop()
        await orchestrator.shutdown()
        await redis.aclose()
        await db_engine.dispose()
        logger.info("engine_stopped")


def main() -> None:
    """Sync entrypoint для [project.scripts]/`python -m`."""
    asyncio.run(_run())


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.netloc:
        return value
    if "@" not in parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    netloc = f"{username}:***@{hostinfo}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


if __name__ == "__main__":
    main()
