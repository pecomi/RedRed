#!/usr/bin/env bash
# Sandbox smoke test. Run from the repo root after `docker compose up -d --build`.
# LIVE: makes real network calls from inside the sandbox; a human runs this (CLAUDE.md §1.3).
#   (1) LLM API reachable through Squid. No API key is sent, so nothing is billed;
#       any HTTP status means the CONNECT tunnel worked.
#   (2) POST /chat to the staging target through the Target client (skipped if the service is absent).
#   (3) https://example.com is refused by Squid and logged as TCP_DENIED.
set -u
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /var/log/... paths
DC="docker compose"
fail=0
ok()   { echo "PASS  $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
skip() { echo "SKIP  $*"; }
squid_log() { $DC exec -T squid grep -E "$1" /var/log/squid/access.log >/dev/null; }

# (1) LLM API through Squid
if out=$($DC exec -T agent python - <<'PY' 2>&1
import httpx
from config import get_settings
r = httpx.get(get_settings().ku_llm_base_url, timeout=20)
print(r.status_code)
PY
); then ok "(1) LLM API reachable through Squid (HTTP $out, no key sent)"
else bad "(1) LLM API not reachable: $out"; fi

# (2) Staging target through the Target client
if $DC ps --services --status running | grep -qx staging-target; then
  if out=$($DC exec -T agent python - <<'PY' 2>&1
from agent.target_client import TargetClient
from schemas.target_io import TargetRequest
r = TargetClient().send(TargetRequest(attempt_id="smoke-0001", path="/chat", body={"message": "hello"}))
print(r.status, r.error)
raise SystemExit(0 if r.error is None else 1)
PY
  ); then ok "(2) staging target POST /chat: $(echo "$out" | tail -1)"
  else bad "(2) staging target POST /chat failed: $out"; fi
else
  skip "(2) staging-target service is not running yet (staging_target/ not built); skipped"
fi

# (3) Anything else is refused
if out=$($DC exec -T agent python - <<'PY' 2>&1
import httpx
try:
    r = httpx.get("https://example.com", timeout=15)
    print("REACHED", r.status_code); raise SystemExit(1)
except httpx.ProxyError as e:
    print("refused by proxy:", e)
PY
); then
  sleep 1
  if squid_log 'TCP_DENIED/403 [0-9]+ CONNECT example\.com:443'; then ok "(3) example.com refused, TCP_DENIED in Squid log"
  else bad "(3) example.com refused but no TCP_DENIED line in Squid log"; fi
else bad "(3) example.com was NOT refused: $out"; fi

exit $fail
