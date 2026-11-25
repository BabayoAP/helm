# Helm engine image (OPS-5, SEC-6). Non-root, no build tools at runtime.
# syntax=docker/dockerfile:1

FROM node:24.11.1-bookworm-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npx next build

FROM python:3.12.8-slim-bookworm AS app
COPY --from=ghcr.io/astral-sh/uv:0.5.9 /uv /usr/local/bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends age \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 helm
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONHASHSEED=0 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY --from=ui /ui/out ./ui/out
RUN mkdir -p /app/data && chown -R helm:helm /app/data
USER helm
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["helm"]
CMD ["paper", "--config", "/app/config/helm.toml", "--risk", "/app/config/risk.toml", "--feed", "alpaca", "--secrets", "file"]
