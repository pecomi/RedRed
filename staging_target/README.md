# Staging target

Our own test target for the red-team agent (CLAUDE.md §2, owner: Soona Lee). A deterministic,
rule-based chatbot: no model, no outgoing network traffic, standard library only.

## API

| Request | Response |
| --- | --- |
| `POST /chat` with `{"message": "<text>", "conversation_id": "<optional string>"}` | `200 {"reply": "<text>", "conversation_id": "<echoed or null>"}` |
| `POST /chat`, malformed (not JSON, no `message`, empty `message`) | `400 {"error": "..."}`; `411` without Content-Length; `413` above 64 KiB |
| `GET /health` | `200 {"status": "ok", "chat_requests": <n>}` |
| anything else | `404 {"error": "not found"}` |

This is the shape `agent/target_client.py` sends and reads (`reply`, `conversation_id`).

Behaviour, in this order:

1. a seeded rule matches (see below) → that rule's reply;
2. the base keyword filter flags the message as harmful → the fixed refusal `rules.REFUSAL`;
3. otherwise → the fixed benign reply `rules.BENIGN`.

The server keeps no conversation state and never echoes the message, so the same message always
gets the same reply. `conversation_id` is only echoed back.

`chat_requests` counts every `POST /chat` that arrived, including malformed ones, since the server
started. Compare it with the number of `target_request` records the agent logged (seam 3).

Each request is one line on the container's stdout: UTC time, method, path, body length, status and
outcome (`benign`, `refusal`, `rejected`, or `vulnerability:<id>` / `decoy:<id>`). Message content
is not logged. The outcome column is the code-based ground truth for comparing Verifier verdicts
(CLAUDE.md §8); it is never sent to the client.

## Seeded rules (not in the repo)

The seeded vulnerabilities, the decoy and the canary are read from `staging_target/secrets/vulns.json`.
That folder is in `.gitignore`. Do not put its contents in code, tests, comments, commit messages
or PR text, and keep them out of the Planner/generator (CLAUDE.md §1.7).

- `vulns.example.json` is committed and shows the format with one made-up rule.
- If `secrets/vulns.json` is missing, the server starts on the example file and prints a `WARNING`
  line at startup. Runs in that state are not valid for measurement.
- A malformed rules file stops the server at startup.
- `VULNS_PATH` overrides the path (for a file mounted into the container).

The format and the condition language are described at the top of `rules.py`. A `vulnerability`
rule's reply must contain `{canary}`; a `decoy` rule's reply must not contain the canary.

## Run

```bash
python -m staging_target.server                 # from the repo root; HOST / PORT / VULNS_PATH in the environment
pytest staging_target                           # offline tests, temporary rules file
bash staging_target/tests/container_check.sh    # build + read-only, no-network container check
```

## Container

`staging_target/Dockerfile`: base image pinned by digest, UID 10002, no writes to disk, port 8000,
with a `HEALTHCHECK` on `/health`. The build context is this folder, so a local
`secrets/vulns.json` is copied into the image.

Service definition for `docker-compose.yml` (Hyeyoung's file; not changed by this work). The
service name must be `staging-target`: Squid allows only that host on port 8000, and the agent's
`STAGING_TARGET_URL` is already `http://staging-target:8000`.

```yaml
  staging-target:
    build: ./staging_target
    networks: [internal]          # internal only: reachable through Squid, no route out
    expose: ["8000"]
    read_only: true
    user: "10002:10002"
    security_opt: ["no-new-privileges:true"]
    cap_drop: ["ALL"]
    restart: unless-stopped
```

Two things for whoever wires it in:

- The agent image is built with `COPY . .` from the repo root, so `staging_target/secrets/` would
  be copied into the agent container. Add `staging_target/secrets/` to the root `.dockerignore`.
- To read the request count after a session:
  `docker compose exec staging-target python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/health').read().decode())"`
  and the request log with `docker compose logs staging-target`.
