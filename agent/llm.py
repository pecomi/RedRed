"""BAZE API Gateway client for Claude. Planner, generator and Verifier all call through here.

- Settings come only from config.get_settings().
- The full request body is logged BEFORE sending (headers are never logged); if logging fails, nothing is sent.
- Every call is checked against the session budget, and its cost is added afterwards.
- Inside the sandbox, httpx follows the HTTPS_PROXY environment variable, so traffic goes through Squid.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx

from agent import budget as budget_mod
from agent.budget import Budget, LogEvent, no_log
from config import get_settings

ANTHROPIC_VERSION = "2023-06-01"
ROLES = ("planner", "generator", "verifier")


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: Optional[str]
    raw: dict


class LLMClient:
    def __init__(
        self,
        budget: Budget,
        log_event: LogEvent = no_log,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 120.0,
    ) -> None:
        s = get_settings()
        self._url = s.ku_llm_base_url.rstrip("/") + "/v1/messages"
        self._model = s.ku_llm_model
        self._budget = budget
        self._log = log_event
        self._http = httpx.Client(
            headers={
                "x-api-key": s.ku_llm_api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def complete(
        self,
        role: str,
        system: str,
        messages: list[dict],
        *,
        attempt_id: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: Optional[float] = None,
    ) -> LLMResult:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        budget_mod.require_prices()
        self._budget.check()

        body = {"model": self._model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        self._log("model_request", role, {"attempt_id": attempt_id, "url": self._url, "body": body})

        t0 = time.monotonic()
        resp = self._http.post(self._url, json=body)
        latency_ms = round((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            self._log(
                "model_response",
                role,
                {"attempt_id": attempt_id, "status": resp.status_code, "error": resp.text, "latency_ms": latency_ms},
            )
            resp.raise_for_status()

        data = resp.json()
        usage = data.get("usage", {})
        in_tok, out_tok = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cost = budget_mod.cost_usd(in_tok, out_tok)
        self._budget.add_cost(cost)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        self._log(
            "model_response",
            role,
            {
                "attempt_id": attempt_id,
                "status": 200,
                "body": data,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": round(cost, 6),
                "latency_ms": latency_ms,
            },
        )
        return LLMResult(text, in_tok, out_tok, cost, data.get("stop_reason"), data)

    def close(self) -> None:
        self._http.close()
