"""Adapter capability flags (spec §14.1). Core degrades when a host lacks a signal."""

from __future__ import annotations

HOST_CAPABILITIES: dict[str, dict[str, bool]] = {
    "claude-code": {
        "auto_capture": True,
        "session_inject": True,
        "per_turn_inject": True,
        "session_end": True,
        "native_tools": True,
    },
    "codex": {
        "auto_capture": True,
        "session_inject": False,
        "per_turn_inject": True,
        "session_end": False,
        "native_tools": True,
    },
    "cursor": {
        "auto_capture": True,
        "session_inject": True,
        "per_turn_inject": True,
        "session_end": True,
        "native_tools": True,
    },
    "hermes": {
        "auto_capture": True,
        "session_inject": True,
        "per_turn_inject": True,
        "session_end": False,
        "native_tools": True,
    },
    "deepseek-harness": {
        "auto_capture": False,
        "session_inject": False,
        "per_turn_inject": False,
        "session_end": False,
        "native_tools": True,
    },
    "workbuddy": {
        "auto_capture": False,
        "session_inject": False,
        "per_turn_inject": False,
        "session_end": False,
        "native_tools": True,
    },
    "doubao": {
        "auto_capture": False,
        "session_inject": False,
        "per_turn_inject": False,
        "session_end": False,
        "native_tools": True,
    },
    "cli": {
        "auto_capture": False,
        "session_inject": False,
        "per_turn_inject": False,
        "session_end": False,
        "native_tools": True,
    },
}


def host_capabilities() -> dict[str, dict[str, bool]]:
    return {name: dict(flags) for name, flags in HOST_CAPABILITIES.items()}


def host_can(host: str, capability: str) -> bool:
    flags = HOST_CAPABILITIES.get(host) or HOST_CAPABILITIES["cli"]
    return bool(flags.get(capability))
