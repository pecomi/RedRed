# Agent image. Built from the repo root (see docker-compose.yml). Runs read-only as UID 10001.
# .env is never copied in (.dockerignore); secrets arrive via env_file at run time.
# TODO: pin by digest (python:3.12-slim@sha256:...) once Docker is available to resolve it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/logs && chown 10001:10001 /app/logs

USER 10001:10001
# Placeholder until the agent runner exists: keep the container up for `docker compose exec`.
CMD ["sleep", "infinity"]
