from __future__ import annotations

import asyncio

from infrastructure.metrics import EngineMetricsServer
from engine_main import _redact_url


class DummyOrchestrator:
    def __init__(self, running_count: int) -> None:
        self.running_count = running_count

    def iter_running(self) -> list[object]:
        return [object() for _ in range(self.running_count)]


async def _request(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


async def test_metrics_endpoint_exposes_running_strategy_count() -> None:
    server = await EngineMetricsServer.start("127.0.0.1", 0, DummyOrchestrator(2))
    try:
        response = await _request(server.port, "/metrics")
    finally:
        await server.stop()

    assert b"HTTP/1.1 200 OK" in response
    assert b"engine_running_strategies 2.0" in response


async def test_healthz_endpoint_returns_ok() -> None:
    server = await EngineMetricsServer.start("127.0.0.1", 0, DummyOrchestrator(0))
    try:
        response = await _request(server.port, "/healthz")
    finally:
        await server.stop()

    assert response.endswith(b"\r\n\r\nok\n")


def test_redact_url_hides_password_and_query() -> None:
    assert (
        _redact_url("postgresql+asyncpg://user:secret@db:5432/app?ssl=true")
        == "postgresql+asyncpg://user:***@db:5432/app"
    )
    assert _redact_url("redis://redis:6379/0") == "redis://redis:6379/0"
