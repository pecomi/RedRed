#!/usr/bin/env bash
# Container check for the staging target. Run from the repo root; needs Docker. No outside traffic:
# the container runs with --network none, read-only, non-root, all capabilities dropped.
#   (1) docker build succeeds
#   (2) /health answers in a --read-only container
#   (3) POST /chat answers and /health counts it
set -u
export MSYS_NO_PATHCONV=1
IMAGE=redred-staging-target:check
NAME=redred-staging-target-check
fail=0
ok()  { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; fail=1; }
py()  { docker exec -i "$NAME" python - ; }

if docker build -q -t "$IMAGE" staging_target >/dev/null; then ok "(1) docker build"
else bad "(1) docker build"; exit 1; fi

docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges:true "$IMAGE" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1' EXIT

for _ in $(seq 1 20); do
  docker exec "$NAME" python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=1)" 2>/dev/null && break
  sleep 0.5
done

if out=$(py <<'PY' 2>&1
import json, urllib.request as u
h = json.load(u.urlopen("http://127.0.0.1:8000/health", timeout=3))
assert h == {"status": "ok", "chat_requests": 0}, h
print(h)
PY
); then ok "(2) /health in a read-only container: $out"
else bad "(2) /health in a read-only container: $out"; fi

if out=$(py <<'PY' 2>&1
import json, urllib.request as u
req = u.Request("http://127.0.0.1:8000/chat", data=json.dumps({"message": "Hello!"}).encode(),
                headers={"Content-Type": "application/json"})
r = json.load(u.urlopen(req, timeout=3))
assert isinstance(r.get("reply"), str) and r["reply"], r
n = json.load(u.urlopen("http://127.0.0.1:8000/health", timeout=3))["chat_requests"]
assert n == 1, n
print("reply received, chat_requests =", n)
PY
); then ok "(3) /chat answers and is counted: $out"
else bad "(3) /chat answers and is counted: $out"; fi

echo "--- container user: $(docker exec "$NAME" id -u):$(docker exec "$NAME" id -g)"
echo "--- container log:"
docker logs "$NAME" 2>&1 | sed 's/^/    /'
exit $fail
