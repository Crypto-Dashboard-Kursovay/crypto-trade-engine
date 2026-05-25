from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from domain.enums import TimeFrame

from .db_models import Bot, BotCommand, CommandKind, ExchangeCredential
from .logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BotConfig:
    """DTO для движка — без зависимости от ORM."""

    id: uuid.UUID
    credential_id: uuid.UUID
    strategy_class: str
    symbol: str
    timeframe: TimeFrame
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DecryptedCredential:
    id: uuid.UUID
    exchange: str
    api_key: str
    api_secret: str
    passphrase: str | None = None  # OKX и Coinbase Pro


@dataclass(frozen=True, slots=True)
class PendingBotCommand:
    command_id: uuid.UUID
    bot_id: uuid.UUID
    kind: str
    payload: dict[str, Any]


class CredentialDecryptError(Exception):
    """Не удалось расшифровать credential — обычно несовпадение Fernet-ключей с бэком."""


class BotRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, bot_id: uuid.UUID) -> BotConfig | None:
        async with self._session_factory() as session:
            row = await session.get(Bot, bot_id)
            if row is None:
                return None
            return BotConfig(
                id=row.id,
                credential_id=row.credential_id,
                strategy_class=row.strategy_class,
                symbol=row.symbol,
                timeframe=TimeFrame(row.timeframe),
                params=dict(row.params),
            )


class BotCommandRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_pending(
        self, *, limit: int = 100, max_age_sec: int = 86400
    ) -> list[PendingBotCommand]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)
        async with self._session_factory() as session:
            result = await session.execute(
                select(BotCommand)
                .join(Bot, Bot.id == BotCommand.bot_id)
                .where(BotCommand.processed_at.is_(None))
                .where(BotCommand.created_at >= cutoff)
                .where(
                    or_(
                        and_(
                            BotCommand.kind == CommandKind.START,
                            Bot.status.in_(["starting", "running"]),
                        ),
                        and_(
                            BotCommand.kind == CommandKind.STOP,
                            Bot.status.in_(["stopping", "stopped"]),
                        ),
                        and_(
                            BotCommand.kind == CommandKind.UPDATE,
                            Bot.status.in_(["starting", "running"]),
                        ),
                    )
                )
                .order_by(BotCommand.created_at.asc(), BotCommand.id.asc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            PendingBotCommand(
                command_id=row.command_id,
                bot_id=row.bot_id,
                kind=row.kind.value,
                payload=dict(row.payload),
            )
            for row in rows
        ]

    async def mark_processed(self, command_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(BotCommand)
                .where(
                    BotCommand.command_id == command_id,
                    BotCommand.processed_at.is_(None),
                )
                .values(processed_at=datetime.now(timezone.utc))
            )
            await session.commit()


class CredentialRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fernet: Fernet,
    ) -> None:
        self._session_factory = session_factory
        self._fernet = fernet

    async def get_decrypted(self, cred_id: uuid.UUID) -> DecryptedCredential | None:
        async with self._session_factory() as session:
            row = await session.get(ExchangeCredential, cred_id)
            if row is None:
                return None
            return self._decrypt_row(row)

    async def list_decrypted(self) -> list[DecryptedCredential]:
        async with self._session_factory() as session:
            result = await session.execute(select(ExchangeCredential))
            rows = result.scalars().all()

        credentials: list[DecryptedCredential] = []
        for row in rows:
            credentials.append(self._decrypt_row(row))
        return credentials

    def _decrypt_row(self, row: ExchangeCredential) -> DecryptedCredential:
        try:
            api_key = self._fernet.decrypt(row.api_key_enc.encode()).decode()
            api_secret = self._fernet.decrypt(row.api_secret_enc.encode()).decode()
            passphrase: str | None = None
            if row.passphrase_enc:
                passphrase = self._fernet.decrypt(row.passphrase_enc.encode()).decode()
        except InvalidToken as exc:
            logger.error(
                "credential_decrypt_failed",
                credential_id=str(row.id),
                hint="ENGINE_ENCRYPTION_KEY likely doesn't match BACKEND_ENCRYPTION_KEY",
            )
            raise CredentialDecryptError(
                f"Cannot decrypt credential {row.id}. "
                "ENGINE_ENCRYPTION_KEY must match BACKEND_ENCRYPTION_KEY."
            ) from exc
        return DecryptedCredential(
            id=row.id,
            exchange=row.exchange,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
        )
