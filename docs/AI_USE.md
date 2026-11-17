# AI-use log

| Date | Person | Tool | Model | What was done |
| --- | --- | --- | --- | --- |
| 2026-11-16 | Hyeyoung You | Claude Code | claude-opus-5-5 | Target client, Squid/compose sandbox, seam 1/3 tests, smoke script, count_egress.py; added log_failed to TargetError; wired Target client, smoke/seam 1 and count_egress (hash-chain check) to the merged Logger; session runner skeleton (agent/run_session.py) with injected Planner/generator/Verifier |
| 2026-11-16 | Soona Lee | Claude Code | claude-fable-5-2 | Staging target (staging_target/): rule-based /chat and /health server, rules-file loader with example file, Dockerfile, offline tests and container check script, README; .gitignore entry for staging_target/secrets/ |
| 2026-11-16 | Soona Lee | Claude Code | claude-fable-5-2 | Verifier (agent/verifier.py, prompts/verifier.md): code checks, model-judge path with evidence-quote check, unit tests and seam 2 test; STAGING_TARGET_CANARY setting; wired code checks into run_session main() |
| 2026-11-16 | Hyeyoung You | Claude Code | claude-opus-5-5 | Wired staging target into compose (secrets kept out of agent image), pinned data/ line endings, LLM injection point in run_session, first live loop on example rules |
