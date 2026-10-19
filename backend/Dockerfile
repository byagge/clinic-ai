FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes do not re-resolve the wheel set.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install "." && pip uninstall -y arix

COPY alembic.ini ./
COPY alembic ./alembic
COPY arix ./arix

# Run as a non-root user.
RUN useradd --create-home --uid 10001 arix && chown -R arix:arix /app
USER arix

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "arix.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
