# Nora v1.0 service image.
FROM python:3.12-slim

# uv for fast, locked installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies from the lockfile first (better layer caching).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source.
COPY app ./app

EXPOSE 8000

# Use uvicorn's factory mode: the app is built by create_app(), which enforces
# the boot gate (operator ack + required secrets) at startup.
CMD ["uv", "run", "--no-dev", "uvicorn", "--factory", "app.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000"]
