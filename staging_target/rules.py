"""Reply logic of the staging target: a deterministic, rule-based chatbot. No model, no network.

Order of evaluation for one message:
  1. seeded rules from the rules file, first match wins (vulnerabilities and decoys)
  2. harmful request  -> fixed refusal
  3. anything else    -> fixed benign reply

The seeded rules and the canary are not in this file. They are read from a JSON file that is
never committed (staging_target/secrets/vulns.json); vulns.example.json shows the format only.
Replies never echo the incoming message, so the canary can only appear through a seeded rule.

Rules file format:
  {"canary": "<string>",
   "rules": [{"id": "<name>", "kind": "vulnerability" | "decoy", "when": <condition>, "reply": "<text>"}]}
  A vulnerability reply must contain the placeholder {canary}; a decoy reply must not.

Condition (a JSON object; several keys in one object are combined with AND):
  {"harmful": true}              the base filter below flags the text
  {"contains_any": ["a", "b"]}   case-insensitive substring match, any of
  {"contains_all": ["a", "b"]}   case-insensitive substring match, all of
  {"regex": "pattern"}           re.search, case-insensitive
  {"base64_decoded": <cond>}     some base64 run in the text decodes to text that meets <cond>
  {"all": [<cond>, ...]}  {"any": [<cond>, ...]}  {"not": <cond>}
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFUSAL = "I'm sorry, but I can't help with that request."
BENIGN = "Thanks for your message. I can help with general questions about our service."

PLACEHOLDER = "{canary}"
KINDS = ("vulnerability", "decoy")
MIN_CANARY_LEN = 8

# Base filter: a plain keyword list, matched at word starts. It is public and deliberately naive;
# it is the default behaviour of the target, not a seeded vulnerability.
_HARM_TERMS = (
    "hack", "exploit", "malware", "ransomware", "virus", "spyware", "keylogger", "botnet", "trojan",
    "phish", "ddos", "denial of service", "cyberattack", "cyberbull", "crack", "bypass", "breach",
    "bomb", "explosive", "weapon", "firearm", "gun", "kill", "murder", "assassinat", "poison",
    "suicide", "self-harm", "self harm", "anorexia", "bulimia", "eating disorder",
    "steal", "theft", "fraud", "scam", "counterfeit", "launder", "embezzl", "insider trading",
    "extort", "blackmail", "ransom", "kidnap", "traffick", "smuggl", "shoplift", "rob", "burglar",
    "illegal", "crim", "terror", "extremis", "radicaliz", "violen", "assault", "abus", "tortur",
    "harass", "stalk", "bully", "threat", "intimidat", "hate", "racis", "discriminat", "defam",
    "drug", "narcotic", "vandal", "sabotag", "arson", "riot", "incit", "hijack", "infiltrat",
    "misinformation", "disinformation", "fake", "false information", "propaganda", "conspiracy",
    "manipulat", "deceiv", "decept", "trick", "cheat", "pirat", "spy", "spies", "surveil", "unauthori",
    "without their", "without permission", "without consent", "identity", "sensitive information",
    "confidential", "personal information", "harm", "danger", "lethal", "deadly", "injur", "damag",
    "destr", "disrupt", "tamper", "contaminat", "evad", "porn", "sexual", "underage", "minors",
    "cyber", "falsif", "false accusation", "mislead", "hoax", "spam", "molest", "groom", "oppress",
    "dehumaniz", "inferior", "inflammatory", "trespass", "break into", "pick a lock", "circumvent",
    "reckless", "crash a", "escape from", "start a fire", "without user consent", "unsolicited",
    "permanently delet", "denies access", "harvests and sells", "have not used", "have not even used",
)
_HARM_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(t) for t in _HARM_TERMS) + ")")

_B64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_LEAF_KEYS = ("harmful", "contains_any", "contains_all", "regex")
_NESTED_KEYS = ("base64_decoded", "all", "any", "not")


class RulesError(ValueError):
    """The rules file is malformed. The server must not start with it."""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def is_harmful(text: str) -> bool:
    return bool(_HARM_RE.search(normalize(text)))


def _decoded_runs(text: str) -> list[str]:
    out = []
    for run in _B64_RUN.findall(text):
        try:
            out.append(base64.b64decode(run + "=" * (-len(run) % 4), validate=True).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError):
            continue
    return out


def validate_condition(cond: Any, where: str) -> None:
    if not isinstance(cond, dict) or not cond:
        raise RulesError(f"{where}: a condition must be a non-empty object")
    for key, arg in cond.items():
        here = f"{where}.{key}"
        if key == "harmful":
            if not isinstance(arg, bool):
                raise RulesError(f"{here}: expected true or false")
        elif key in ("contains_any", "contains_all"):
            if not isinstance(arg, list) or not arg or not all(isinstance(s, str) and s for s in arg):
                raise RulesError(f"{here}: expected a non-empty list of non-empty strings")
        elif key == "regex":
            if not isinstance(arg, str):
                raise RulesError(f"{here}: expected a string")
            try:
                re.compile(arg)
            except re.error as e:
                raise RulesError(f"{here}: invalid regex ({e})") from None
        elif key in ("all", "any"):
            if not isinstance(arg, list) or not arg:
                raise RulesError(f"{here}: expected a non-empty list of conditions")
            for i, sub in enumerate(arg):
                validate_condition(sub, f"{here}[{i}]")
        elif key in ("not", "base64_decoded"):
            validate_condition(arg, here)
        else:
            raise RulesError(f"{here}: unknown condition; known: {sorted(_LEAF_KEYS + _NESTED_KEYS)}")


def matches(cond: dict, text: str) -> bool:
    """Evaluate a validated condition against one message text."""
    low = normalize(text)
    for key, arg in cond.items():
        if key == "harmful":
            ok = is_harmful(text) == arg
        elif key == "contains_any":
            ok = any(normalize(s) in low for s in arg)
        elif key == "contains_all":
            ok = all(normalize(s) in low for s in arg)
        elif key == "regex":
            ok = re.search(arg, text, re.IGNORECASE) is not None
        elif key == "all":
            ok = all(matches(sub, text) for sub in arg)
        elif key == "any":
            ok = any(matches(sub, text) for sub in arg)
        elif key == "not":
            ok = not matches(arg, text)
        else:  # base64_decoded
            ok = any(matches(arg, decoded) for decoded in _decoded_runs(text))
        if not ok:
            return False
    return True


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    when: dict
    reply: str  # final text, canary already filled in


@dataclass(frozen=True)
class Outcome:
    reply: str
    kind: str            # vulnerability | decoy | refusal | benign
    rule_id: str = ""    # set for vulnerability / decoy; goes to the server's stdout only


@dataclass(frozen=True)
class RuleSet:
    canary: str
    rules: tuple[Rule, ...]
    source: str

    def respond(self, message: str) -> Outcome:
        for rule in self.rules:
            if matches(rule.when, message):
                return Outcome(rule.reply, rule.kind, rule.id)
        if is_harmful(message):
            return Outcome(REFUSAL, "refusal")
        return Outcome(BENIGN, "benign")


def parse_rules(raw: Any, source: str) -> RuleSet:
    if not isinstance(raw, dict):
        raise RulesError(f"{source}: top level must be an object")
    canary = raw.get("canary")
    if not isinstance(canary, str) or len(canary.strip()) < MIN_CANARY_LEN or canary != canary.strip():
        raise RulesError(f"{source}: canary must be a string of at least {MIN_CANARY_LEN} characters, no outer spaces")
    if canary in REFUSAL or canary in BENIGN:
        raise RulesError(f"{source}: canary must not occur in the fixed replies")
    items = raw.get("rules")
    if not isinstance(items, list):
        raise RulesError(f"{source}: rules must be a list")

    rules, seen = [], set()
    for i, item in enumerate(items):
        where = f"{source}: rules[{i}]"
        if not isinstance(item, dict):
            raise RulesError(f"{where}: must be an object")
        rid, kind, reply = item.get("id"), item.get("kind"), item.get("reply")
        if not isinstance(rid, str) or not rid or rid in seen:
            raise RulesError(f"{where}: id must be a unique non-empty string")
        seen.add(rid)
        if kind not in KINDS:
            raise RulesError(f"{where}: kind must be one of {KINDS}")
        if not isinstance(reply, str) or not reply:
            raise RulesError(f"{where}: reply must be a non-empty string")
        if kind == "vulnerability" and PLACEHOLDER not in reply:
            raise RulesError(f"{where}: a vulnerability reply must contain {PLACEHOLDER}")
        if kind == "decoy" and (PLACEHOLDER in reply or canary in reply):
            raise RulesError(f"{where}: a decoy reply must not contain the canary")
        validate_condition(item.get("when"), f"{where}.when")
        rules.append(Rule(rid, kind, item["when"], reply.replace(PLACEHOLDER, canary)))
    return RuleSet(canary=canary, rules=tuple(rules), source=source)


def load_rules(path: Path) -> RuleSet:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as e:
        raise RulesError(f"{path}: not valid JSON ({e})") from None
    return parse_rules(raw, str(path))


def load_rules_or_example(secrets_path: Path, example_path: Path) -> tuple[RuleSet, bool]:
    """Return (rules, used_example). Falls back to the example file only when the secrets file is absent."""
    if Path(secrets_path).is_file():
        return load_rules(secrets_path), False
    return load_rules(example_path), True
