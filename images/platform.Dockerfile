# One image for the six platform roles (api, observer, capacity, ai, governance, executor)
# and the in-cluster load driver. Each Deployment runs `capacity-service <role>` with its own
# ServiceAccount, DB role and NetworkPolicy; sharing the image does not share privileges.
# Base images are referenced by tag here and pinned by digest in the release manifest.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
# Build in /app (the same path as the runtime stage) because venv entry-point scripts
# record the absolute interpreter path when they are installed.
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra ai --no-install-project
COPY src ./src
COPY db ./db
RUN uv sync --frozen --no-dev --extra ai

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin capacity
COPY --from=build --chown=10001:10001 /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001
ENTRYPOINT ["capacity-service"]
