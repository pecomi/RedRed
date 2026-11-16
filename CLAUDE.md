# CLAUDE.md — Team RedRed (Secure AI Systems 2026-2, Track C)

Claude Code reads this file at the start of every session. All three team members (Jiseon Kim, Soona Lee, Hyeyoung You) share it.
Source documents (to be kept under `docs/`): Project Brief, Team_RedRed_Proposal, Project_Proposal_Feedback, ROE draft, mid-check plan.

## 0. Project in one line

We build an LLM-driven autonomous red-team agent (Track C). The agent itself is our "system".
Current stage: **mid-check (2026-11-23 23:59 KST)** — a working slice in which the full loop runs inside the sandbox against our own staging target.

## 1. Hard rules (take priority over any task instruction)

1. **Target scope.** All test traffic goes only to `STAGING_TARGET_URL` and `KU_LLM_BASE_URL` (BAZE API Gateway, host `api.baze.co`).
   The partner team's host must not appear anywhere (code, config, tests, comments, examples) until the ROE has written approval.
   Ad-hoc `curl`/`httpx` calls during development follow the same rule (that traffic is not captured in the agent's egress log).
2. **Handling `.env`.** `.env` already holds the real key.
   - Do not open `.env` or print it with `cat`/`grep`/`printenv`/`env`. Look at `.env.example` for variable names.
   - Code reads settings only through `config.get_settings()`, never `os.environ` directly. Never write key values into code, tests, logs, commit messages or output.
   - The Logger and Target client replace auth headers (`Authorization`, `x-api-key`, etc.) with `[REDACTED]` before recording.
   - Before committing, confirm `.env` and `.env.*` (except `.env.example`) are in `.gitignore`.
3. **Live API calls spend budget.** Any test or script that actually calls the BAZE API or the staging target must be confirmed by a human before it runs.
   Default unit tests run offline (mocked). Mark live tests with `@pytest.mark.live`. BAZE usage is billed to the account that created the key.
4. **Target responses are data.** Target responses enter Planner/Verifier prompts only inside a `<data-NONCE>…</data-NONCE>` block with a fresh 16-hex nonce per attempt (`agent/prompt_assembly.py`, `wrap_untrusted()`).
   Never write code that appends response content to a system prompt or instruction position (Proposal Defence #5).
5. **The model cannot change the target address.** The Target client's base URL comes only from config. Never create a path by which model output changes the URL, host, or a route outside the path.
6. **`schemas/` is an agreed contract** (Interface spec v0 in the team plan doc). Do not modify it unless the task says so; if a change is needed, report the reason first.
7. **Measurement validity.** When writing Planner/Attack generator code or prompts, do not reflect the specific vulnerabilities seeded in `staging_target/`
   (otherwise the seeded-vulnerability recall metric loses its meaning). This rule does not restrict information sharing between teammates; it keeps the answers out of the agent's code.

## 2. Architecture ↔ code

Map each box in the Proposal §1 diagram one-to-one to files. Update this table whenever you add a file.

| Box | Files | Owner |
| --- | --- | --- |
| Planner | `agent/planner.py`, `prompts/planner.md` | Jiseon Kim |
| Attack generator | `agent/generator.py`, `agent/mutations/` | Jiseon Kim |
| Prompt assembly (data/instruction separation) | `agent/prompt_assembly.py` | Jiseon Kim |
| Budget / kill switch | `agent/budget.py` | Jiseon Kim |
| Target client | `agent/target_client.py` | Hyeyoung You |
| Session runner (call order, session lifecycle, stop conditions; parts injected) | `agent/run_session.py` | Hyeyoung You |
| Verifier | `agent/verifier.py`, `prompts/verifier.md` | Soona Lee |
| Logger | `agent/logger.py` | Soona Lee |
| Sandbox / egress | `docker-compose.yml`, `infra/squid/`, `infra/agent/Dockerfile`, `tests/smoke.sh`, `tests/seam1_egress.sh` | Hyeyoung You |
| Egress count ("0 reached") | `scripts/count_egress.py`, `scripts/collect_logs.sh` | Hyeyoung You |
| Staging target | `staging_target/` | Soona Lee |
| Data | `data/advbench/`, `data/DATA_CARD.md` | Jiseon Kim |
| Settings | `config.py`, `.env.example` | Jiseon Kim |

Ownership is accountability only; anyone may read, edit or review any file.

## 3. Interfaces (`schemas/`)

- `schemas/log_record.py` — one JSONL line. Fields: `v`, `session_id`, `seq`, `ts` (UTC ISO 8601, ms), `type`, `actor`, `attempt_id`, `data`, `prev_hash`, `hash`.
  `hash = sha256(prev_hash + canonical JSON of the record without hash)`. Types: `session_start`, `model_request`, `model_response`, `target_request`, `target_response`, `verdict`, `egress_denied`, `budget_stop`, `kill`, `session_end`.
  `target_request` is written before sending; if the write fails, the request is not sent.
- `schemas/target_io.py` — `TargetRequest` (`attempt_id`, `turn`, `method` GET|POST, `path`, `body`, `conversation_id`, `seed_id`, `mutations`; no host field) /
  `TargetResponse` (`attempt_id`, `turn`, `status`, `body_raw` (log only), `text`, `conversation_id`, `latency_ms`, `error` ∈ timeout | http_error | blocked_path | budget_exceeded | log_failed (target_request log write failed; nothing sent).
- `schemas/verifier_input.py` — `attempt_id`, `goal`, `code_checks` (`canary_found`, `refusal_matched`), `attack_text`, `target_text`.
  `attack_text` and `target_text` are untrusted and reach the prompt only through `wrap_untrusted()`.
  Verifier output: `verdict` (finding | not_finding | uncertain), `basis`, `evidence_quote`, `rationale`, `severity_hint`.
  If `evidence_quote` is not an exact substring of `target_text`, the verdict becomes `uncertain`; `canary_found` means `finding`.

All components exchange data through these models. Do not build ad-hoc dictionaries.

## 4. Operating limits (Proposal Defence #3 · ROE §3)

| Item | Limit |
| --- | --- |
| Target requests | 10 req/min, 200 req/session (including retries) |
| Session time | 30 minutes |
| Model cost | USD 5/session |
| Concurrency | 1 supervised session |

Stop automatically when a limit is reached, logging fails, or egress behaves unexpectedly. Keep these constants in `agent/budget.py` only.

## 5. Definition of done — seam tests

A task is done only when the tests below pass. If a test does not exist yet, write it first.

| Seam | Test file | Pass condition |
| --- | --- | --- |
| ① Agent ↔ egress | `tests/test_seam1_egress.py` | From inside the sandbox, a request to a non-allowed host is blocked and recorded as DENIED in the Squid log. A direct connection that bypasses the proxy also fails |
| ② Generator ↔ Verifier | `tests/test_seam2_verifier.py` | The planted non-finding is rejected; the planted known finding is accepted |
| ③ Logger ↔ Target client | `tests/test_seam3_logcount.py` | In every session, requests sent = requests logged |

Key mid-check number: **0 requests reach any non-allowed host across all test sessions**; denied attempts are counted from the egress and sandbox logs.
Count with `scripts/count_egress.py`, and the count must be reproducible with that script (Brief §7: every number in the paper is reproducible from the repo).

## 6. Commands

```bash
docker compose up --build          # sandbox + Squid + staging target
pytest -m "not live"               # offline tests (default)
docker compose exec agent python -m pytest -p no:cacheprovider   # same, in Linux (agent.logger needs fcntl; use on Windows)
pytest -m live                     # live calls: a human must confirm before running
python scripts/count_egress.py     # reached / denied counts
```

## 7. How to work

- Present a plan and get approval before writing code.
- Keep each task the size of one cell in the mid-check plan table, and commit changes in small steps.
- Data: pin AdvBench to commit `098262edf85f807224e70ecd87b9d83716bf6b73`, and on load verify the sha256 of `harmful_behaviors.csv`:
  `6cd1a5c63c07610d7eb67307772ee5606017ee950b5770ab288a2c487489d3e1`. Stop if it does not match.
- Every generated attack variant records the generator model, prompt file, temperature, seed and timestamp (Week 4 generational provenance).
- Pin dependencies (`==` in `requirements.txt`, Docker image digests).
- If the schedule slips, keep this order: ① sandbox, egress, "0 reached" → ② log completeness → ③ loop runs end to end → ④ Verifier refinement and attack diversity.

## 8. Before the mid-check (feedback slide 17)

- [ ] Rewrite the success metrics as pass line / fail line sentences (Track C: against a baseline at equal budget)
- [ ] Coverage grid from the threat model; empty cells marked out of scope with a reason
- [ ] Run the baseline before improving the agent
- [ ] Freeze the test split; tune only on the dev split
- [ ] Write and commit `data/DATA_CARD.md`
- [ ] Verifier error rate: compare Verifier verdicts with the staging target's code-based ground truth (Planner, generator and Verifier all use the same model, so check for circularity)

## 9. AI-use log

Brief §6 requires an AI-use appendix in every submission. For each Claude Code session, add one line to `docs/AI_USE.md`:
date, person, tool (Claude Code), model version, what was done.

## 10. Open items (do not hard-code until confirmed)

- Partner team, target endpoint, freeze tag — ROE §1 [PENDING]
- Exact engagement dates and hours — ROE §2 [PENDING]
- Instructor's written approval — required before Week 14
