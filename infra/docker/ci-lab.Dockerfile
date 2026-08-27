# syntax=docker/dockerfile:1.7

# The CI Lab deliberately reuses the project's Python 3.10 dependency set so
# its HTTP contract is exercised with the same FastAPI/Pydantic versions as
# the QA backend. The deny-first Dockerfile-specific ignore file keeps every
# local database, secret and unrelated source tree out of this build context.
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

RUN groupadd --system --gid 10001 qa-ci-lab \
    && useradd --system --uid 10001 --gid qa-ci-lab \
        --home-dir /nonexistent qa-ci-lab \
    && install -d -o qa-ci-lab -g qa-ci-lab /app /data

WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=qa-ci-lab:qa-ci-lab backend/app/__init__.py ./app/__init__.py
COPY --chown=qa-ci-lab:qa-ci-lab backend/app/ci_lab ./app/ci_lab

USER 10001:10001
EXPOSE 8080

# Raw Uvicorn access logs include concrete run identifiers. The lab's own
# bounded application logging is the only permitted request log surface.
CMD ["python", "-m", "uvicorn", "app.ci_lab.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
