"""Built-in default configuration values."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "daemon": {
        "autostart": True,
        "log_level": "info",
        "ipc_timeout_ms": 2000,
        "max_request_bytes": 8_388_608,
    },
    "latency": {
        "per_turn_ms": 150,
        "session_start_ms": 500,
        "search_ms": 300,
    },
    "llm": {
        "extraction": {
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "THEMEMORY_LLM_API_KEY",
            "temperature": 0.0,
            "max_tokens": 4096,
            "timeout_s": 120,
            "json_mode": "auto",
        },
        "consolidation": {},
        "budget": {
            "daily_tokens": 2_000_000,
            "monthly_tokens": 40_000_000,
            "on_exceed": "pause",
        },
    },
    "extraction": {
        "trigger_turns": 80,
        "trigger_idle_hours": 3,
        "window_tokens": 6000,
        "prompt_file": "",
        "skip_phrases": ["好的", "谢谢", "ok", "继续"],
    },
    "consolidation": {
        "promote_repeat": 2,
        "dedupe_similarity": 0.92,
        "tombstone_similarity": 0.95,
        "cooldown_hours": 6,
        "min_new_items": 20,
        "candidate_ttl_days": 60,
    },
    "embedding": {
        "model": "bge-small-zh-v1.5",
        "resident": "auto",
        "resident_high_multiple": 4,
        "resident_high_min_gb": 2,
        "resident_low_multiple": 2,
        "resident_min_total_gb": 4.0,
        "idle_unload_minutes": 10,
        "query_instruction": "为这个句子生成表示以用于检索相关文章：",
        "footprint_gb": 0.5,
    },
    "retrieval": {
        "search_limit": 8,
        "inject_max_items": 3,
        "inject_max_chars": 400,
        "inject_min_interval_turns": 2,
        # Cosine thresholds keep §15.2 defaults. BM25 τ calibrated to FTS5 unicode61
        # + bigram fts_text scale (sqlite bm25 magnitudes are ~1e-5..1e-2 on small corpora);
        # keyword/alias hits still bypass τ (spec §9.1).
        "tau_search_cosine": 0.50,
        "tau_inject_cosine": 0.60,
        "tau_search_bm25": 0.0,
        "tau_inject_bm25": 0.0,
        "rrf_k": 60,
        "half_life_days": {
            "profile_fact": 365,
            "preference": 180,
            "pattern": 90,
            "note": 60,
            "procedural": 365,
        },
    },
    "snapshot": {
        "budget_profile": 1500,
        "budget_schedule": 500,
        "budget_project": 1000,
        "budget_procedural": 800,
        "budget_summary": 600,
        "budget_unextracted": 500,
        "include_summary": True,
        "annotated": True,
        "window_days": 14,
        "unextracted_max_turns": 6,
        "unextracted_max_age_hours": 24,
    },
    "turns": {
        "max_size_gb": 5,
        "protect_evidence": True,
        "max_chars_per_turn": 200_000,
        "keep_code_blocks": False,
    },
    "privacy": {
        "redact_secrets": True,
        "extra_patterns": [],
        "private_tag": True,
        "screen_enabled": True,
        "screen_patterns": [],
        "crosscheck_enabled": True,
        "crosscheck_method": "semantic",
        "crosscheck_min_similarity": 0.55,
        "crosscheck_min_overlap": 0.25,
        "high_risk_tools": [
            "WebFetch",
            "WebSearch",
            "Browser",
            "browser",
            "fetch",
            "curl",
            "ReadUrl",
            "mcp_web",
            "agent-browser",
        ],
    },
    "isolation": {
        "profile": "shared",
        "procedural": "shared",
    },
    "backup": {
        "interval_hours": 24,
        "keep": 7,
    },
    "limits": {
        "session_max_writes": 30,
        "session_max_procedural": 5,
        "burst_window_seconds": 60,
        "burst_max_writes": 8,
    },
    "hosts": {
        "default": {
            "write": True,
            "read": True,
            "isolated": False,
            "procedural_write": False,
        },
        "claude-code": {"procedural_write": True},
        "codex": {"procedural_write": True},
        "deepseek-harness": {"procedural_write": True},
        "hermes": {"procedural_write": True},
        "cursor": {"procedural_write": True},
        "workbuddy": {"procedural_write": False},
        "doubao": {"procedural_write": False},
        "eval": {"write": True, "read": True, "isolated": False, "procedural_write": True},
    },
}

DEFAULT_CONFIG_TOML = """# TheMemory configuration
# Docs: doc/技术方案文档.md §15

[daemon]
autostart = true
log_level = "info"
ipc_timeout_ms = 2000
max_request_bytes = 8388608

[latency]
per_turn_ms = 150
session_start_ms = 500
search_ms = 300

[llm.extraction]
base_url = ""
model = ""
api_key_env = "THEMEMORY_LLM_API_KEY"
# api_key = ""  # prefer api_key_env; file must be user-readable only
temperature = 0
max_tokens = 4096
timeout_s = 120
json_mode = "auto"

[llm.budget]
daily_tokens = 2000000
monthly_tokens = 40000000
on_exceed = "pause"

[extraction]
trigger_turns = 80
trigger_idle_hours = 3
window_tokens = 6000
skip_phrases = ["好的", "谢谢", "ok", "继续"]

[consolidation]
promote_repeat = 2
dedupe_similarity = 0.92
tombstone_similarity = 0.95
cooldown_hours = 6
min_new_items = 20
candidate_ttl_days = 60

[embedding]
model = "bge-small-zh-v1.5"
resident = "auto"
resident_high_multiple = 4
resident_high_min_gb = 2
resident_low_multiple = 2
resident_min_total_gb = 4.0
idle_unload_minutes = 10
footprint_gb = 0.5
query_instruction = "为这个句子生成表示以用于检索相关文章："

[retrieval]
search_limit = 8
inject_max_items = 3
inject_max_chars = 400
inject_min_interval_turns = 2
tau_search_cosine = 0.50
tau_inject_cosine = 0.60
# Calibrated for FTS5 unicode61 + Chinese bigram fts_text (M4); keyword/alias still bypass
tau_search_bm25 = 0.0
tau_inject_bm25 = 0.0
rrf_k = 60

[snapshot]
budget_profile = 1500
budget_schedule = 500
budget_project = 1000
budget_procedural = 800
budget_summary = 600
budget_unextracted = 500
include_summary = true
annotated = true
window_days = 14

[turns]
max_size_gb = 5
protect_evidence = true
max_chars_per_turn = 200000
keep_code_blocks = false

[privacy]
redact_secrets = true
private_tag = true
screen_enabled = true
crosscheck_enabled = true
crosscheck_method = "semantic"
crosscheck_min_similarity = 0.55
crosscheck_min_overlap = 0.25

[isolation]
profile = "shared"
procedural = "shared"

[hosts.default]
write = true
read = true
isolated = false
procedural_write = false

[hosts.claude-code]
procedural_write = true

[hosts.codex]
procedural_write = true

[hosts.deepseek-harness]
procedural_write = true

[backup]
interval_hours = 24
keep = 7

[limits]
session_max_writes = 30
session_max_procedural = 5
burst_window_seconds = 60
burst_max_writes = 8
"""
