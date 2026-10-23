"""Secret redaction and <private> stripping."""

from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{20,})\b"),
    re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)([^\s]+)"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
    re.compile(r"(?i)\b(password\s*[:=]\s*)(\S+)"),
]
PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

DIRECTIVE_PATTERNS = [
    re.compile(r"(?i)ignore (all |any )?(previous|prior|above) instructions"),
    re.compile(r"(?i)you are now"),
    re.compile(r"(?i)system prompt"),
    re.compile(r"(?i)do not follow"),
    re.compile(r"https?://\S+"),
]


def strip_private(text: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return PRIVATE_RE.sub("", text)


def redact_secrets(text: str, enabled: bool = True, extra: list[str] | None = None) -> str:
    if not enabled:
        return text
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub(_mask, out)
    for raw in extra or []:
        try:
            out = re.compile(raw).sub("[REDACTED]", out)
        except re.error:
            continue
    return out


def _mask(m: re.Match[str]) -> str:
    if m.lastindex and m.lastindex >= 2:
        return f"{m.group(1)}[REDACTED]"
    return "[REDACTED]"


def screen_directive(text: str, extra: list[str] | None = None) -> bool:
    for pat in DIRECTIVE_PATTERNS:
        if pat.search(text):
            return True
    for raw in extra or []:
        try:
            if re.search(raw, text):
                return True
        except re.error:
            continue
    return False


def clean_turn(text: str, *, redact: bool, private: bool, extra: list[str] | None = None) -> str:
    text = strip_private(text, private)
    text = redact_secrets(text, redact, extra)
    return text.strip()
