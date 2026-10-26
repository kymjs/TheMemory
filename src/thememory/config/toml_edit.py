"""Update TOML scalar values while preserving comments and unknown layout."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        inner = ", ".join(_format_value(v) for v in value)
        return f"[{inner}]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _section_header(path: tuple[str, ...]) -> str:
    return "[" + ".".join(path) + "]"


def apply_updates(text: str, updates: dict[str, Any]) -> str:
    """
    Apply dotted-key updates to TOML text.

    Only flat scalars / arrays under tables are patched. Nested tables are
    addressed via `[a.b]` headers. Missing keys are appended under their table.
    """
    lines = text.splitlines(keepends=True)
    # Map dotted key -> (line_index, indent, trailing_comment)
    flat: dict[str, Any] = {}

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, path)
            else:
                flat[path] = value

    walk(updates)

    current: list[str] = []
    key_lines: dict[str, int] = {}
    section_last_line: dict[tuple[str, ...], int] = {(): -1}

    header_re = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    assign_re = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)\s*=\s*(.*?)(\s*(#.*)?)?$")

    for i, line in enumerate(lines):
        hm = header_re.match(line.rstrip("\n"))
        if hm:
            parts = tuple(p.strip() for p in hm.group(1).split(".") if p.strip())
            current = list(parts)
            section_last_line[tuple(current)] = i
            continue
        am = assign_re.match(line.rstrip("\n"))
        if am and current is not None:
            key = am.group(2)
            dotted = ".".join([*current, key]) if current else key
            key_lines[dotted] = i
            section_last_line[tuple(current)] = i

    # Apply in-place replacements
    for dotted, value in flat.items():
        if dotted in key_lines:
            idx = key_lines[dotted]
            am = assign_re.match(lines[idx].rstrip("\n"))
            if not am:
                continue
            indent, key, _old, comment = am.group(1), am.group(2), am.group(3), am.group(4) or ""
            newline = "\n" if lines[idx].endswith("\n") else ""
            lines[idx] = f"{indent}{key} = {_format_value(value)}{comment}{newline}"
            continue
        # Append under section
        parts = dotted.split(".")
        if len(parts) == 1:
            section: tuple[str, ...] = ()
            key = parts[0]
        else:
            section = tuple(parts[:-1])
            key = parts[-1]
        insert_at = section_last_line.get(section)
        new_line = f"{key} = {_format_value(value)}\n"
        if insert_at is None:
            # create section header at end
            if section:
                lines.append("\n")
                lines.append(_section_header(section) + "\n")
            lines.append(new_line)
            section_last_line[section] = len(lines) - 1
            key_lines[dotted] = len(lines) - 1
        else:
            lines.insert(insert_at + 1, new_line)
            # shift indexes after insert
            for k, li in list(key_lines.items()):
                if li > insert_at:
                    key_lines[k] = li + 1
            for sec, li in list(section_last_line.items()):
                if li > insert_at:
                    section_last_line[sec] = li + 1
            section_last_line[section] = insert_at + 1
            key_lines[dotted] = insert_at + 1

    return "".join(lines)


def write_preserving(path: Path, cfg: dict[str, Any], *, fallback_dump) -> None:
    """Patch existing file when present; otherwise write a full dump."""
    if path.exists():
        original = path.read_text(encoding="utf-8")
        updated = apply_updates(original, cfg)
        path.write_text(updated, encoding="utf-8")
        return
    path.write_text(fallback_dump(cfg), encoding="utf-8")
