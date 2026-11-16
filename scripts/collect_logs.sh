#!/usr/bin/env bash
# Copy the sandbox logs out of the named volumes so scripts/count_egress.py can read them.
#   logs/squid/access.log   <- squid:/var/log/squid/access.log
#   logs/agent/             <- agent:/app/logs (JSONL session logs)
set -eu
export MSYS_NO_PATHCONV=1
mkdir -p logs/squid logs/agent
docker compose cp squid:/var/log/squid/access.log logs/squid/access.log
docker compose cp agent:/app/logs/. logs/agent/
echo "collected into logs/squid and logs/agent"
