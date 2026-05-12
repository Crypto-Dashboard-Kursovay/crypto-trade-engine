import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from domain.enums import TimeFrame
from infrastructure.db_models import Bot, ExchangeCredential
from infrastructure.db_repositories import (
    BotConfig,
    BotRepository,
    CredentialDecryptError,
    CredentialRepository,
    DecryptedCredential,
)


def _session_factory(row: Any) -> MagicMock:
    """Возвращает context-manager-фабрику сессий, где session.get(...) → row."""
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=session)
    return factory


# ---------- BotRepository ----------


async def test_bot_repository_returns_dto_for_existing_bot() -> None:
    bot_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    bot = Bot(
        id=bot_id,
        credential_id=cred_id,
        strategy_class="strategies.SmaCross",
        symbol="BTC/USDT",
        timeframe="1m",
        params={"fast": 50, "slow": 200},
    )
    factory = _session_factory(bot)

    config = await BotRepository(factory).get(bot_id)

    assert isinstance(config, BotConfig)
    assert config.id == bot_id
    assert config.credential_id == cred_id
    assert config.strategy_class == "strategies.SmaCross"
    assert config.symbol == "BTC/USDT"
    assert config.timeframe is TimeFrame.M1
    assert config.params == {"fast": 50, "slow": 200}


async def test_bot_repository_returns_none_when_not_found() -> None:
    factory = _session_factory(None)
    result = await BotRepository(factory).get(uuid.uuid4())
    assert result is None


# ---------- CredentialRepository ----------


async def test_credential_repository_decrypts_round_trip() -> None:
    key = Fernet.generate_key()
    fernet = Fernet(key)
    cred_id = uuid.uuid4()
    cred = ExchangeCredential(
        id=cred_id,
        exchange="binance",
        api_key_enc=fernet.encrypt(b"my-real-api-key").decode(),
        api_secret_enc=fernet.encrypt(b"my-real-secret").decode(),
    )
    factory = _session_factory(cred)

    decrypted = await CredentialRepository(factory, fernet).get_decrypted(cred_id)

    assert isinstance(decrypted, DecryptedCredential)
    assert decrypted.id == cred_id
    assert decrypted.exchange == "binance"
    assert decrypted.api_key == "my-real-api-key"
    assert decrypted.api_secret == "my-real-secret"


async def test_credential_repository_raises_on_wrong_key() -> None:
    backend_key = Fernet.generate_key()
    engine_key = Fernet.generate_key()  # другой!

    cred_id = uuid.uuid4()
    cred = ExchangeCredential(
        id=cred_id,
        exchange="binance",
        api_key_enc=Fernet(backend_key).encrypt(b"key").decode(),
        api_secret_enc=Fernet(backend_key).encrypt(b"secret").decode(),
    )
    factory = _session_factory(cred)

    with pytest.raises(CredentialDecryptError, match="ENGINE_ENCRYPTION_KEY must match"):
        await CredentialRepository(factory, Fernet(engine_key)).get_decrypted(cred_id)


async def test_credential_repository_returns_none_when_not_found() -> None:
    factory = _session_factory(None)
    fernet = Fernet(Fernet.generate_key())
    result = await CredentialRepository(factory, fernet).get_decrypted(uuid.uuid4())
    assert result is None
