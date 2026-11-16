"""Verifier contract (Interface spec v0, section 3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Verdict = Literal["finding", "not_finding", "uncertain"]
Basis = Literal["code", "llm", "code+llm"]


@dataclass(frozen=True)
class Goal:
    seed_id: str
    success_criterion: str


@dataclass(frozen=True)
class CodeChecks:
    canary_found: bool = False
    refusal_matched: bool = False


@dataclass(frozen=True)
class VerifierInput:
    attempt_id: str
    goal: Goal
    code_checks: CodeChecks
    attack_text: str               # untrusted; reaches the prompt only via wrap_untrusted()
    target_text: str               # untrusted; same


@dataclass(frozen=True)
class VerifierOutput:
    attempt_id: str
    verdict: Verdict
    basis: Basis
    evidence_quote: str = ""
    rationale: str = ""
    severity_hint: Optional[str] = None
