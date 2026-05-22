"""Lightweight async Prometheus endpoint for the live trading engine."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Protocol

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest


class MetricsOrchestrator(Protocol):
    def iter_running(self) -> Sequence[object]: ...


ENGINE_RUNNING_STRATEGIES = Gauge(
    "engine_running_strategies",
    "Number of live strategies currently managed by the trading engine.",
)
ENGINE_METRICS_HTTP_REQUESTS = Counter(
    "engine_metrics_http_requests_total",
    "HTTP requests served by the trading engine metrics endpoint.",
    ("path", "http_status"),
)


class EngineMetricsServer:
    def __init__(self, server: asyncio.Server, orchestrator: MetricsOrchestrator) -> None:
        self._server = server
        self._orchestrator = orchestrator

    @classmethod
    async def start(
        cls,
        host: str,
        port: int,
        orchestrator: MetricsOrchestrator,
    ) -> EngineMetricsServer:
        instance: EngineMetricsServer | None = None

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if instance is None:
                return
            await instance._handle_request(reader, writer)

        server = await asyncio.start_server(_handle, host, port)
        instance = cls(server, orchestrator)
        return instance

    @property
    def port(self) -> int:
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Metrics server has no listening sockets")
        return int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            method, path = _parse_request_line(request)
            if method != "GET":
                self._write_response(writer, 405, b"method not allowed\n", "text/plain; charset=utf-8")
                ENGINE_METRICS_HTTP_REQUESTS.labels(path=path, http_status="405").inc()
                return
            if path == "/metrics":
                ENGINE_RUNNING_STRATEGIES.set(len(self._orchestrator.iter_running()))
                body = generate_latest()
                self._write_response(writer, 200, body, CONTENT_TYPE_LATEST)
                ENGINE_METRICS_HTTP_REQUESTS.labels(path=path, http_status="200").inc()
                return
            if path == "/healthz":
                self._write_response(writer, 200, b"ok\n", "text/plain; charset=utf-8")
                ENGINE_METRICS_HTTP_REQUESTS.labels(path=path, http_status="200").inc()
                return
            self._write_response(writer, 404, b"not found\n", "text/plain; charset=utf-8")
            ENGINE_METRICS_HTTP_REQUESTS.labels(path=path, http_status="404").inc()
        except Exception:
            with contextlib.suppress(Exception):
                self._write_response(writer, 400, b"bad request\n", "text/plain; charset=utf-8")
                ENGINE_METRICS_HTTP_REQUESTS.labels(path="unknown", http_status="400").inc()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _write_response(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
        }.get(status, "OK")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        writer.write(headers + body)


def _parse_request_line(request: bytes) -> tuple[str, str]:
    line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = line.split()
    if len(parts) < 2:
        return "", "unknown"
    return parts[0].upper(), parts[1].split("?", 1)[0]
