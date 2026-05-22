FROM python:3.14-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /build/wheels ".[backtest,ml]"

FROM python:3.14-slim AS runtime

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid ${APP_GID} app 2>/dev/null || true && \
    useradd --create-home --uid ${APP_UID} --gid ${APP_GID} app

WORKDIR /app
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY --chown=app:app src ./src

USER app

EXPOSE 9100

# Healthcheck: процесс жив, если в Redis недавно был heartbeat.
# Просто сверяемся, что python процесс не упал — детальный мониторинг engine.status
# из бэка (Phase 3.4).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m healthcheck || exit 1

CMD ["trade-engine"]
