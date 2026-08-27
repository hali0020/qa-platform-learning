# syntax=docker/dockerfile:1.7
FROM python:3.10-slim AS dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY backend/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip install -r requirements.txt

FROM python:3.10-slim AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 qa \
    && useradd --system --uid 10001 --gid qa --home-dir /nonexistent qa \
    && install -d -o qa -g qa /app /data

WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=qa:qa backend/app ./app
COPY --chown=qa:qa backend/alembic ./alembic
COPY --chown=qa:qa backend/alembic.ini ./alembic.ini

USER 10001:10001
EXPOSE 23100

# The application installs a structured access logger that records route
# templates only. Uvicorn's default access log includes the raw query string,
# which would expose OIDC authorization codes on the callback route.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "23100", "--workers", "1", "--no-access-log"]
