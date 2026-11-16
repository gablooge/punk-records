"""Catch credentials before they end up in your memory store."""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOW_MARKER = "punk:allow"

PATTERNS = [
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai-key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    ("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    ("stripe-key", r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}"),
    ("generic-secret", r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|access[_-]?token)\b"
                       r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{16,}"),
]
_COMPILED = [(name, re.compile(rx)) for name, rx in PATTERNS]


@dataclass
class Hit:
    kind: str
    line: int
    start: int
    end: int


def scan(text: str) -> list[Hit]:
    """Find likely secrets. Lines containing `punk:allow` are ignored."""
    hits: list[Hit] = []
    taken: list[tuple[int, int]] = []
    for name, rx in _COMPILED:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            line_start = text.rfind("\n", 0, s) + 1
            line_end = text.find("\n", s)
            line = text[line_start: line_end if line_end != -1 else len(text)]
            if ALLOW_MARKER in line:
                continue
            taken.append((s, e))
            hits.append(Hit(name, text.count("\n", 0, s) + 1, s, e))
    return sorted(hits, key=lambda h: h.start)


def redact(text: str, hits: list[Hit] | None = None) -> str:
    hits = scan(text) if hits is None else hits
    for h in sorted(hits, key=lambda h: h.start, reverse=True):
        text = text[: h.start] + f"[REDACTED:{h.kind}]" + text[h.end:]
    return text
