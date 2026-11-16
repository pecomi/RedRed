"""Count egress from the sandbox logs. Source of the mid-check number "0 reached outside the allowed hosts".

Reads every Squid access.log and every agent JSONL session log, then prints one line:
  reached_non_target  Squid entries for a non-allowed host that were NOT denied (must be 0).
                      Counted conservatively: any non-TCP_DENIED entry counts, even a failed connect.
  egress_denied       Target client refusals (bad path / host) from the agent logs.
  squid_denied        TCP_DENIED entries in the Squid logs.
  sent==logged        target_request records == target_response records (every logged request
                      got its outcome logged; a send never happens without a target_request first).
  squid_target        non-denied Squid entries to the staging target; should equal the logged count.

Usage: python scripts/count_egress.py [--squid GLOB ...] [--agent GLOB ...] [--json]
Collect the logs first with scripts/collect_logs.sh. Exit code 1 if reached_non_target > 0 or sent != logged.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from urllib.parse import urlsplit

# Must match infra/squid/squid.conf
LLM_HOSTS = {"api.baze.co"}
TARGET_HOSTS = {"staging-target"}
ALLOWED_HOSTS = LLM_HOSTS | TARGET_HOSTS


def files(patterns: list[str]) -> list[str]:
    return sorted({p for pat in patterns for p in glob.glob(pat, recursive=True)})


def squid_host(method: str, url: str) -> str:
    if method == "CONNECT":
        return url.rsplit(":", 1)[0].lower()
    return (urlsplit(url).hostname or "").lower()


def count_squid(paths: list[str]) -> dict:
    c = {"squid_entries": 0, "squid_denied": 0, "reached_non_target": 0, "squid_target": 0, "reached_hosts": []}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 7:
                    continue
                code, method, url = parts[3], parts[5], parts[6]
                c["squid_entries"] += 1
                if code.startswith("TCP_DENIED"):
                    c["squid_denied"] += 1
                    continue
                host = squid_host(method, url)
                if host in TARGET_HOSTS:
                    c["squid_target"] += 1
                elif host not in ALLOWED_HOSTS:
                    c["reached_non_target"] += 1
                    c["reached_hosts"].append(host)
    return c


def count_agent(paths: list[str]) -> dict:
    c = {"egress_denied": 0, "target_request": 0, "target_response": 0}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line).get("type")
                if t in c:
                    c[t] += 1
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--squid", nargs="+", default=["logs/squid/access.log*"])
    ap.add_argument("--agent", nargs="+", default=["logs/agent/**/*.jsonl"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    squid_files, agent_files = files(args.squid), files(args.agent)
    r = {**count_squid(squid_files), **count_agent(agent_files)}
    r["sent_eq_logged"] = r["target_request"] == r["target_response"]
    r["files"] = {"squid": len(squid_files), "agent": len(agent_files)}

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        target_check = "n/a" if not agent_files else str(r["squid_target"] == r["target_request"])
        print(
            f"reached_non_target={r['reached_non_target']} egress_denied={r['egress_denied']} "
            f"squid_denied={r['squid_denied']} sent==logged={r['sent_eq_logged']} "
            f"(requests={r['target_request']}) squid_target==logged={target_check} "
            f"[files: squid={len(squid_files)} agent={len(agent_files)}]"
        )
    if not squid_files:
        print("warning: no Squid access.log found; run scripts/collect_logs.sh first", file=sys.stderr)
    return 1 if r["reached_non_target"] or not r["sent_eq_logged"] else 0


if __name__ == "__main__":
    sys.exit(main())
