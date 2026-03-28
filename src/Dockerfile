FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev deps, frozen lockfile)
RUN uv sync --frozen --no-dev --no-install-project

# Auto-install OpenTelemetry instrumentations for detected libraries (e.g. FastAPI, uvicorn)
RUN uv run opentelemetry-bootstrap --action=install

# Copy application source
COPY . .

# Install the project itself
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Use opentelemetry-instrument to auto-instrument the FastAPI app
CMD ["uv", "run", "opentelemetry-instrument", "fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]
