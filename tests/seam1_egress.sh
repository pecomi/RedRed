#!/usr/bin/env bash
# Seam 1: all three escape routes are blocked and recorded. Run after `docker compose up -d --build`.
# LIVE (inside the sandbox; no request reaches any outside host). Called by tests/test_seam1_egress.py -m live.
#   (A)  Target client given path "//evil.example/x"  -> egress_denied logged, nothing sent
#   (B)  explicit request to https://evil.example via Squid -> TCP_DENIED in Squid access.log
#   (B') direct connection to 1.1.1.1 bypassing Squid  -> no route, connection fails
set -u
export MSYS_NO_PATHCONV=1
DC="docker compose"
fail=0
ok()  { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; fail=1; }

# (A)
if out=$($DC exec -T agent python - <<'PY' 2>&1
from agent.target_client import TargetClient
from schemas.target_io import TargetRequest
r = TargetClient().send(TargetRequest(attempt_id="seam1-a", path="//evil.example/x"))
raise SystemExit(0 if r.error == "blocked_path" else 1)
PY
) && echo "$out" | grep -q '"type": "egress_denied"'; then ok "(A) blocked_path + egress_denied logged"
else bad "(A) path was not denied: $out"; fi

# (B)
if out=$($DC exec -T agent python - <<'PY' 2>&1
import httpx
try:
    httpx.get("https://evil.example/", timeout=15); raise SystemExit(1)
except httpx.ProxyError:
    pass
PY
); then
  sleep 1
  if $DC exec -T squid grep -E 'TCP_DENIED/403 [0-9]+ CONNECT evil\.example:443' /var/log/squid/access.log >/dev/null
  then ok "(B) evil.example refused, TCP_DENIED in Squid log"
  else bad "(B) refused but no TCP_DENIED line in Squid log"; fi
else bad "(B) evil.example was NOT refused by Squid: $out"; fi

# (B')
if out=$($DC exec -T agent python - <<'PY' 2>&1
import httpx
try:
    httpx.get("https://1.1.1.1/", timeout=10, trust_env=False)  # ignore HTTP(S)_PROXY on purpose
    raise SystemExit(1)
except httpx.ConnectError as e:
    print("no route:", e)
except httpx.TimeoutException as e:
    print("timed out:", e)
PY
); then ok "(B') direct connection failed: $(echo "$out" | tail -1)"
else bad "(B') direct connection bypassing Squid SUCCEEDED: $out"; fi

exit $fail
