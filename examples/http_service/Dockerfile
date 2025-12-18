FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv (used by this repo)
RUN pip install --no-cache-dir uv

# Copy dependency metadata first for better layer caching
COPY pyproject.toml uv.lock README.md /app/

# Copy source + examples + audio assets used by this demo
COPY src/ /app/src/
COPY examples/ /app/examples/
COPY audio_*.pcm /app/

# Install dependencies (and the local package) using the lockfile
RUN uv sync --frozen

EXPOSE 8080

CMD ["uv", "run", "python", "examples/http_service/main.py", "--host", "0.0.0.0", "--port", "8080"]


