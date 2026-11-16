"""Staging target server: POST /chat and GET /health. Standard library only.

  POST /chat   {"message": "<text>", "conversation_id": "<optional>"}
               -> 200 {"reply": "<text>", "conversation_id": "<echoed or null>"}
               -> 400 / 411 / 413 {"error": "<reason>"} for a malformed request
  GET  /health -> 200 {"status": "ok", "chat_requests": <POST /chat requests received so far>}

The server is stateless apart from the request counter: the same message always gets the same
reply. It never opens an outgoing connection. Every request is written to stdout as one line
(time, method, path, body length, status, outcome); message content is never logged.

Run: python -m staging_target.server     (HOST, PORT, VULNS_PATH can be set in the environment)
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from staging_target.rules import RuleSet, load_rules_or_example

HERE = Path(__file__).resolve().parent
SECRETS_PATH = HERE / "secrets" / "vulns.json"
EXAMPLE_PATH = HERE / "vulns.example.json"
MAX_BODY_BYTES = 64 * 1024


def _now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def _printable(s: str, limit: int = 200) -> str:
    return "".join(c if c.isprintable() and c != " " else "?" for c in s)[:limit]


def log_line(text: str) -> None:
    print(f"{_now()} {text}", flush=True)


class TargetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], rules: RuleSet) -> None:
        super().__init__(address, Handler)
        self.rules = rules
        self._lock = threading.Lock()
        self._chat_requests = 0

    def count_chat(self) -> None:
        with self._lock:
            self._chat_requests += 1

    @property
    def chat_requests(self) -> int:
        with self._lock:
            return self._chat_requests


class Handler(BaseHTTPRequestHandler):
    server: TargetServer
    protocol_version = "HTTP/1.1"
    server_version = "staging-target"
    sys_version = ""
    timeout = 15  # seconds a connection may sit idle

    def log_request(self, code="-", size="-") -> None:  # replaced by the line written in _send
        pass

    def log_message(self, format: str, *args) -> None:  # protocol errors raised by the base class
        log_line("server " + _printable(format % args).replace("?", " "))

    def _send(self, status: int, payload: dict, body_len: int = 0, outcome: str = "-") -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        log_line(f"{self.command} {_printable(self.path)} len={body_len} status={status} outcome={outcome}")

    def _route(self) -> str:
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:
        if self._route() == "/health":
            self._send(200, {"status": "ok", "chat_requests": self.server.chat_requests})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self._route() != "/chat":
            self.close_connection = True  # the body is not read
            self._send(404, {"error": "not found"})
            return
        self.server.count_chat()  # every POST /chat that arrives counts, valid or not

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._send(411, {"error": "Content-Length required"}, outcome="rejected")
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send(413, {"error": f"body larger than {MAX_BODY_BYTES} bytes"}, length, "rejected")
            return

        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"error": "body must be JSON"}, length, "rejected")
            return
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._send(400, {"error": 'body must be {"message": "<non-empty text>"}'}, length, "rejected")
            return
        conversation_id = data.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            self._send(400, {"error": "conversation_id must be a string"}, length, "rejected")
            return

        out = self.server.rules.respond(message)
        outcome = f"{out.kind}:{_printable(out.rule_id, 60)}" if out.rule_id else out.kind
        self._send(200, {"reply": out.reply, "conversation_id": conversation_id}, length, outcome)


def make_server(rules: RuleSet, host: str = "127.0.0.1", port: int = 0) -> TargetServer:
    return TargetServer((host, port), rules)


def main(argv: Optional[list[str]] = None) -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    secrets_path = Path(os.environ.get("VULNS_PATH") or SECRETS_PATH)

    rules, used_example = load_rules_or_example(secrets_path, EXAMPLE_PATH)
    if used_example:
        log_line(f"WARNING rules file {secrets_path} not found; using the EXAMPLE rules "
                 f"({EXAMPLE_PATH.name}). Results from this run are not valid for measurement.")
    counts = {k: sum(r.kind == k for r in rules.rules) for k in ("vulnerability", "decoy")}
    log_line(f"startup rules={'example' if used_example else 'secrets'} "
             f"vulnerabilities={counts['vulnerability']} decoys={counts['decoy']} listen={host}:{port}")

    def stop(signum, frame):  # PID 1 in a container ignores SIGTERM unless a handler is set
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    server = make_server(rules, host, port)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        log_line(f"shutdown chat_requests={server.chat_requests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
