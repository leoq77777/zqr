# ZQR runtime image: the desk (`zqr desk supervise`), and the egress proxy and strategy sandbox it starts as sibling
# containers, so all three always run the same version.
# Build: `zqr release` (tags zqr:<version>), or `zqr desk setup` in a checkout (tags zqr:dev).
# Agent-authored strategy code runs in this image: it must never contain benchmark answers or research data
# (.dockerignore is an allowlist; benchmark/evaluator_private and var/ are never sent to the daemon).
FROM python:3.12-slim
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /opt/zqr
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.7,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src src
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.7,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable && rm -rf src
COPY benchmark benchmark
COPY scripts/zqr-host.sh scripts/zqr-host.sh
RUN useradd --no-create-home --uid 10001 sandbox
ENV PATH=/opt/zqr/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ZQR_ROOT=/opt/zqr \
    ZQR_HOME=/zqr
ARG ZQR_VERSION=dev
ENV ZQR_VERSION=$ZQR_VERSION
LABEL org.opencontainers.image.title=zqr org.opencontainers.image.version=$ZQR_VERSION
WORKDIR /zqr
CMD ["zqr", "desk", "supervise"]
