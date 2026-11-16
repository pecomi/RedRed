# Staging target image. Build context is this folder (docker build staging_target/).
# Standard library only, so there is nothing to pip install; the base image is pinned by digest.
# Runs as UID 10002 and never writes to disk, so it works with a read-only root filesystem.
# If secrets/vulns.json exists at build time it is copied in; otherwise the server starts on
# vulns.example.json and says so in its startup log.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

RUN groupadd --gid 10002 target \
 && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin target

WORKDIR /app
COPY . /app/staging_target

USER 10002:10002
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["python", "-m", "staging_target.server"]
