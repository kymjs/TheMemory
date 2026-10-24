"""Secret redaction, <private> stripping, and directive screening (spec §17.2–§17.3)."""

from __future__ import annotations

import re

_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("api_key", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b")),
    ("url_credential", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
    (
        "assignment",
        re.compile(
            r"(?i)\b((?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)\S+"
        ),
    ),
    ("assignment", re.compile(r"(密码[:：]\s*)\S+")),
]

PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

DIRECTIVE_PATTERNS = [
    re.compile(r"(?i)ignore (all |any )?(previous|prior|above) instructions"),
    re.compile(r"(?i)you are now"),
    re.compile(r"(?i)system prompt"),
    re.compile(r"(?i)do not follow"),
    re.compile(r"https?://\S+"),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bcurl\b[^\n|]*\|\s*(?:sh|bash)\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bchmod\b", re.IGNORECASE),
    re.compile(r"powershell\s+-enc", re.IGNORECASE),
    re.compile(r"无需确认|不要询问|自动执行|已授权"),
    re.compile(r"(?i)always allow|auto-approve"),
    re.compile(r"忽略(?:之前|以上|此前)的?(?:指令|规则)"),
    re.compile(r"你现在是"),
]


def is_fully_private(text: str) -> bool:
    return bool(PRIVATE_RE.fullmatch(text.strip()))


def strip_private(text: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return PRIVATE_RE.sub("[PRIVATE]", text)


def redact_secrets(text: str, enabled: bool = True, extra: list[str] | None = None) -> str:
    if not enabled:
        return text
    out = text
    for category, pat in _SECRET_RULES:
        out = pat.sub(_mask(category), out)
    for raw in extra or []:
        try:
            out = re.compile(raw).sub("[REDACTED]", out)
        except re.error:
            continue
    return out


def _mask(category: str):
    def repl(match: re.Match[str]) -> str:
        if category == "url_credential":
            return "://[REDACTED:url_credential]@"
        if match.lastindex and match.lastindex >= 1 and category == "assignment":
            return f"{match.group(1)}[REDACTED:assignment]"
        return f"[REDACTED:{category}]"

    return repl


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
