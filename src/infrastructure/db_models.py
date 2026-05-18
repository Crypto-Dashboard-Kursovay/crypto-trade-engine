"""Engine-side ORM модели для read-only доступа к bot/credential таблицам.

Бэк и движок шарят одну БД. Эти модели зеркалят `backend/src/models/bot.py` и
`backend/src/models/exchange_credential.py` (минимальный набор полей, нужных
движку). Если бэк изменит схему — Alembic миграция там же, движок просто
перечитает.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exchange_credentials.id"),
        nullable=False,
    )
    strategy_class: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ExchangeCredential(Base):
    __tablename__ = "exchange_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(String, nullable=False)
    api_secret_enc: Mapped[str] = mapped_column(String, nullable=False)
    # OKX / Coinbase Pro используют третий секрет (passphrase). Для прочих — NULL.
    passphrase_enc: Mapped[str | None] = mapped_column(String, nullable=True)
