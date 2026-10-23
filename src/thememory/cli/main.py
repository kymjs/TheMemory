"""CLI entry for `thememory`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from thememory.client.ipc_client import IpcClient, IpcError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thememory")
    parser.add_argument("--home", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="initialize data dir, model, optional hosts")
    p_setup.add_argument("--dev", action="store_true")
    p_setup.add_argument("--skip-model", action="store_true")
    p_setup.add_argument("--install-hosts", action="store_true")

    p_daemon = sub.add_parser("daemon", help="control thememoryd")
    p_daemon.add_argument("action", choices=["start", "stop", "status"])

    p_rem = sub.add_parser("remember", help="remember a fact")
    p_rem.add_argument("content")
    p_rem.add_argument("--origin", default="user_requested")
    p_rem.add_argument("--type", default="preference")
    p_rem.add_argument("--slot-key", default=None)

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)

    sub.add_parser("context")
    sub.add_parser("doctor")

    p_cfg = sub.add_parser("config")
    cfg_sub = p_cfg.add_subparsers(dest="cfg_cmd", required=True)
    g = cfg_sub.add_parser("get")
    g.add_argument("key")
    s = cfg_sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")
    cfg_sub.add_parser("validate")

    p_inst = sub.add_parser("install")
    p_inst.add_argument(
        "host",
        choices=["claude-code", "deepseek-harness", "dsh", "hermes", "cursor"],
    )
    p_inst.add_argument("--profile", default=None)

    p_un = sub.add_parser("uninstall")
    p_un.add_argument(
        "host",
        choices=["claude-code", "deepseek-harness", "dsh", "hermes", "cursor", "all"],
    )
    p_un.add_argument("--profile", default=None)

    p_ex = sub.add_parser("extract")
    p_ex.add_argument("--now", action="store_true")
    p_ex.add_argument("--session-id", default=None)
    p_ex.add_argument("--session-ref", default=None)
    p_ex.add_argument("--host", default="claude-code")

    p_ui = sub.add_parser("ui", help="open local web config UI")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    root = args.home
    if root:
        import os

        os.environ["THEMEMORY_HOME"] = str(root.resolve())

    try:
        if args.cmd == "setup":
            return cmd_setup(args)
        if args.cmd == "daemon":
            return cmd_daemon(args)
        if args.cmd == "remember":
            return _print(rpc("tool.remember", {
                "host": "cli",
                "content": args.content,
                "origin": args.origin,
                "type": args.type,
                "slot_key": args.slot_key,
            }))
        if args.cmd == "search":
            return _print(rpc("tool.search", {"host": "cli", "query": args.query, "limit": args.limit}))
        if args.cmd == "context":
            return _print(rpc("tool.context", {"host": "cli"}))
        if args.cmd == "doctor":
            return _print(rpc("admin.doctor", {}))
        if args.cmd == "config":
            return cmd_config(args)
        if args.cmd == "install":
            return cmd_install(args)
        if args.cmd == "uninstall":
            return cmd_uninstall(args)
        if args.cmd == "extract":
            return _print(rpc("admin.extract_now", {
                "session_id": args.session_id,
                "session_ref": args.session_ref,
                "host": args.host,
            }))
        if args.cmd == "ui":
            from thememory.ui import run_ui

            return run_ui(port=args.port, open_browser=not args.no_browser)
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    with IpcClient() as client:
        return client.request(method, params)


def _print(data: dict[str, Any]) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data.get("status", "ok") != "failed" else 1


def cmd_setup(args: argparse.Namespace) -> int:
    from thememory.config import load, write_default
    from thememory.installer.hook_template import install_hook_script
    from thememory.installer.model import ensure_model
    from thememory.paths import ensure_layout, home_dir

    root = home_dir()
    ensure_layout(root)
    write_default(root)
    install_hook_script(root)
    if not args.skip_model:
        print("Installing embedding model (may download ~90MB)...")
        ensure_model(root)
    cfg = load(root)
    print(f"data dir: {root}")
    print(f"llm configured: {bool(cfg['llm']['extraction'].get('base_url') and cfg['llm']['extraction'].get('model'))}")
    # start daemon
    cmd_daemon(argparse.Namespace(action="start", home=None))
    if args.install_hosts:
        from thememory.installer import host_claude_code, host_cursor, host_dsh, host_hermes

        print("Installing Claude Code hooks + MCP...")
        print(host_claude_code.install())
        print("Installing DSH cordis patch (global + headless)...")
        print(host_dsh.install())
        print(host_dsh.install(profile="headless"))
        print("Installing Hermes MCP + memory provider...")
        print(host_hermes.install())
        print("Installing Cursor MCP + hooks...")
        print(host_cursor.install())
    print("setup done. Configure LLM via: thememory config set llm.extraction.base_url ...")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    import time

    from thememory.daemon.spawn import spawn_daemon

    if args.action == "start":
        try:
            with IpcClient(timeout=1.0) as client:
                st = client.request("admin.status", {})
                print(json.dumps(st, ensure_ascii=False))
                return 0
        except Exception:
            pass
        spawn_daemon()
        for _ in range(50):
            try:
                with IpcClient(timeout=1.0, autostart=False) as client:
                    print(json.dumps(client.request("admin.status", {}), ensure_ascii=False))
                    return 0
            except Exception:
                time.sleep(0.1)
        print("daemon start timeout", file=sys.stderr)
        return 1
    if args.action == "stop":
        try:
            with IpcClient(timeout=2.0, autostart=False) as client:
                print(json.dumps(client.request("admin.shutdown", {}), ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"stop failed: {exc}", file=sys.stderr)
            return 1
    if args.action == "status":
        try:
            with IpcClient(timeout=2.0, autostart=False) as client:
                print(json.dumps(client.request("admin.status", {}), ensure_ascii=False))
            return 0
        except Exception as exc:
            print(json.dumps({"running": False, "error": str(exc)}))
            return 1
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    from thememory.config import load, mask_secrets, write_config
    from thememory.config.loader import ConfigError, get_path, set_path, validate

    cfg = load()
    if args.cfg_cmd == "get":
        try:
            val = get_path(cfg, args.key)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        if "api_key" in args.key:
            val = "***"
        print(json.dumps(val, ensure_ascii=False, indent=2) if isinstance(val, (dict, list)) else val)
        return 0
    if args.cfg_cmd == "set":
        raw = args.value
        if raw.lower() in {"true", "false"}:
            value: Any = raw.lower() == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        set_path(cfg, args.key, value)
        try:
            validate(cfg)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        write_config(cfg)
        print("ok")
        try:
            rpc("admin.reload", {})
        except Exception:
            pass
        return 0
    if args.cfg_cmd == "validate":
        print(json.dumps(mask_secrets(cfg), ensure_ascii=False, indent=2))
        return 0
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    host = args.host
    if host in {"deepseek-harness", "dsh"}:
        from thememory.installer import host_dsh

        path = host_dsh.install(profile=args.profile)
        print(f"wrote {path}")
        if args.profile is None:
            # also patch headless for convenience
            print(f"wrote {host_dsh.install(profile='headless')}")
        return 0
    if host == "claude-code":
        from thememory.installer import host_claude_code

        print(json.dumps(host_claude_code.install(), ensure_ascii=False, indent=2))
        return 0
    if host == "hermes":
        from thememory.installer import host_hermes

        print(json.dumps(host_hermes.install(), ensure_ascii=False, indent=2))
        return 0
    if host == "cursor":
        from thememory.installer import host_cursor

        print(json.dumps(host_cursor.install(), ensure_ascii=False, indent=2))
        return 0
    return 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    hosts = (
        ["claude-code", "deepseek-harness", "hermes", "cursor"]
        if args.host == "all"
        else [args.host]
    )
    for host in hosts:
        if host in {"deepseek-harness", "dsh"}:
            from thememory.installer import host_dsh

            host_dsh.uninstall(profile=args.profile)
            if args.profile is None:
                host_dsh.uninstall(profile="headless")
            print(f"uninstalled {host}")
        elif host == "claude-code":
            from thememory.installer import host_claude_code

            host_claude_code.uninstall()
            print("uninstalled claude-code")
        elif host == "hermes":
            from thememory.installer import host_hermes

            host_hermes.uninstall()
            print("uninstalled hermes")
        elif host == "cursor":
            from thememory.installer import host_cursor

            host_cursor.uninstall()
            print("uninstalled cursor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
