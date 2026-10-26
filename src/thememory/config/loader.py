"""Load / validate / write TheMemory configuration."""

from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from thememory.config.defaults import DEFAULT_CONFIG_TOML, DEFAULTS
from thememory.paths import config_path, ensure_layout


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _env_overrides() -> dict[str, Any]:
    """Apply THEMEMORY_<SECTION>_<KEY> style overrides (flat, one level deep mostly)."""
    result: dict[str, Any] = {}
    prefix = "THEMEMORY_"
    known_roots = set(DEFAULTS.keys())
    for name, raw in os.environ.items():
        if not name.startswith(prefix) or name == "THEMEMORY_HOME":
            continue
        rest = name[len(prefix) :].lower()
        parts = rest.split("_", 1)
        if len(parts) != 2:
            continue
        section, key = parts
        if section not in known_roots and section != "llm":
            # allow llm_extraction_base_url style via nested parse
            pass
        # Support THEMEMORY_EXTRACTION_TRIGGER_TURNS etc.
        if section in known_roots:
            bucket = result.setdefault(section, {})
            bucket[key] = _coerce(raw)
            continue
        # THEMEMORY_LLM_EXTRACTION_BASE_URL
        m = re.match(r"llm_(extraction|consolidation|budget)_(.+)", rest)
        if m:
            llm = result.setdefault("llm", {})
            sub = llm.setdefault(m.group(1), {})
            sub[m.group(2)] = _coerce(raw)
    return result


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in {"true", "yes", "1"}:
        return True
    if low in {"false", "no", "0"}:
        return False
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def validate(cfg: dict[str, Any]) -> None:
    """Reject unknown top-level and second-level keys relative to defaults."""
    for section, value in cfg.items():
        if section not in DEFAULTS:
            raise ConfigError(f"unknown config section: {section}")
        if not isinstance(value, dict):
            raise ConfigError(f"section {section} must be a table")
        _validate_section(section, value, DEFAULTS[section])


def _validate_section(path: str, value: dict[str, Any], template: dict[str, Any]) -> None:
    host_keys = {"write", "read", "isolated", "procedural_write"}
    for key, child in value.items():
        if path == "hosts":
            if not isinstance(child, dict):
                raise ConfigError(f"hosts.{key} must be a table")
            for hk, hv in child.items():
                if hk not in host_keys:
                    raise ConfigError(f"unknown config key: hosts.{key}.{hk}")
                if not isinstance(hv, bool):
                    raise ConfigError(f"hosts.{key}.{hk} must be a boolean")
            continue
        if key not in template:
            if path == "llm" and key in {"extraction", "consolidation", "budget"}:
                if not isinstance(child, dict):
                    raise ConfigError(f"{path}.{key} must be a table")
                base = (
                    DEFAULTS["llm"]["extraction"]
                    if key == "consolidation"
                    else template.get(key, DEFAULTS["llm"].get(key, {}))
                )
                _validate_section(f"{path}.{key}", child, base if isinstance(base, dict) else {})
                continue
            raise ConfigError(f"unknown config key: {path}.{key}")
        expected = template[key]
        if isinstance(expected, dict):
            if not isinstance(child, dict):
                raise ConfigError(f"{path}.{key} must be a table")
            if path == "llm" and key == "consolidation":
                # partial overlay allowed; keys must match extraction schema
                _validate_section(f"{path}.{key}", child, DEFAULTS["llm"]["extraction"])
                continue
            _validate_section(f"{path}.{key}", child, expected)
            continue
        if isinstance(expected, bool) and not isinstance(child, bool):
            raise ConfigError(f"{path}.{key} must be a boolean")
        elif isinstance(expected, int) and not isinstance(expected, bool):
            # TOML/JSON often coerce whole numbers; also accept float for hour-like fields.
            if isinstance(child, bool) or not isinstance(child, (int, float)):
                raise ConfigError(f"{path}.{key} must be a number")
        elif isinstance(expected, float):
            if isinstance(child, bool) or not isinstance(child, (int, float)):
                raise ConfigError(f"{path}.{key} must be a number")
        elif isinstance(expected, str) and not isinstance(child, str):
            raise ConfigError(f"{path}.{key} must be a string")
        elif isinstance(expected, list) and not isinstance(child, list):
            raise ConfigError(f"{path}.{key} must be an array")


def _stamp_sources(node: dict[str, Any], prefix: str, sources: dict[str, str], label: str) -> None:
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            _stamp_sources(value, path, sources, label)
        else:
            sources[path] = label


def explain_config(root: Path | None = None) -> dict[str, Any]:
    """Dotted key → default/file/env, plus a parse or validate error (spec §15.1 / §18.1)."""
    path = config_path(root)
    file_cfg: dict[str, Any] = {}
    error: str | None = None
    if path.exists():
        try:
            with path.open("rb") as fh:
                loaded = tomllib.load(fh)
            if isinstance(loaded, dict):
                file_cfg = loaded
        except (OSError, tomllib.TOMLDecodeError) as exc:
            error = f"{path.name}: {exc}"
    sources: dict[str, str] = {}
    _stamp_sources(DEFAULTS, "", sources, "default")
    _stamp_sources(file_cfg, "", sources, "file")
    _stamp_sources(_env_overrides(), "", sources, "env")
    if error is None:
        try:
            merged = _deep_merge(DEFAULTS, file_cfg)
            merged = _deep_merge(merged, _env_overrides())
            validate(merged)
        except ConfigError as exc:
            error = str(exc)
    return {"error": error, "sources": sources}


def load(root: Path | None = None) -> dict[str, Any]:
    ensure_layout(root)
    path = config_path(root)
    if not path.exists():
        write_default(root)
    with path.open("rb") as fh:
        file_cfg = tomllib.load(fh)
    merged = _deep_merge(DEFAULTS, file_cfg)
    merged = _deep_merge(merged, _env_overrides())
    # merge host defaults into each host
    hosts = merged.get("hosts", {})
    default_host = copy.deepcopy(hosts.get("default", DEFAULTS["hosts"]["default"]))
    for name, overrides in list(hosts.items()):
        if name == "default":
            continue
        hosts[name] = _deep_merge(default_host, overrides if isinstance(overrides, dict) else {})
    validate(merged)
    return merged


def write_default(root: Path | None = None) -> Path:
    ensure_layout(root)
    path = config_path(root)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def get_path(cfg: dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"missing key: {dotted}")
        cur = cur[part]
    return cur


def set_path(cfg: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise ConfigError(f"not a table: {part}")
        cur = nxt
    cur[parts[-1]] = value
    return cfg


def resolve_api_key(llm_section: dict[str, Any]) -> str:
    if llm_section.get("api_key"):
        return str(llm_section["api_key"])
    env_name = str(llm_section.get("api_key_env") or "")
    if env_name:
        return os.environ.get(env_name, "")
    return ""


def mask_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    for section in ("extraction", "consolidation"):
        node = out.get("llm", {}).get(section, {})
        if isinstance(node, dict) and node.get("api_key"):
            node["api_key"] = "***"
    return out


def _to_toml(data: dict[str, Any], prefix: str = "") -> str:
    lines: list[str] = []
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            scalars.append((key, value))
    if prefix:
        lines.append(f"[{prefix}]")
    for key, value in scalars:
        lines.append(f"{key} = {_toml_value(value)}")
    if prefix and scalars:
        lines.append("")
    for key, value in tables:
        nested = f"{prefix}.{key}" if prefix else key
        # detect plain table vs nested
        if value and all(not isinstance(v, dict) for v in value.values()):
            lines.append(f"[{nested}]")
            for k2, v2 in value.items():
                lines.append(f"{k2} = {_toml_value(v2)}")
            lines.append("")
        else:
            # hosts.default style mixed
            plain = {k: v for k, v in value.items() if not isinstance(v, dict)}
            nested_tables = {k: v for k, v in value.items() if isinstance(v, dict)}
            if plain:
                lines.append(f"[{nested}]")
                for k2, v2 in plain.items():
                    lines.append(f"{k2} = {_toml_value(v2)}")
                lines.append("")
            for k2, v2 in nested_tables.items():
                lines.append(_to_toml(v2, f"{nested}.{k2}"))
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        inner = ", ".join(_toml_value(v) for v in value)
        return f"[{inner}]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_config(cfg: dict[str, Any], root: Path | None = None) -> Path:
    validate(cfg)
    path = config_path(root)
    from thememory.config.toml_edit import write_preserving

    write_preserving(path, cfg, fallback_dump=_to_toml)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
