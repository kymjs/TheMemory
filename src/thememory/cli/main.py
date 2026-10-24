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

    p_setup = sub.add_parser(
        "setup",
        help="initialize data dir, model, optional hosts",
        description=(
            "Non-interactive setup. Autostart, benchmark, LLM probe, and host "
            "install run only when the matching flag is passed, so scripts never "
            "block on a question."
        ),
    )
    p_setup.add_argument("--dev", action="store_true")
    p_setup.add_argument(
        "--repair",
        action="store_true",
        help="rewrite the stable entry and re-register autostart if it was already on",
    )
    p_setup.add_argument("--skip-model", action="store_true")
    p_setup.add_argument("--install-hosts", action="store_true")
    p_setup.add_argument("--autostart", action="store_true", help="register OS login autostart")
    p_setup.add_argument("--benchmark", action="store_true", help="run latency/residency probe")
    p_setup.add_argument("--probe", action="store_true", help="run LLM capability probe")
    p_setup.add_argument(
        "--import-claude",
        action="store_true",
        help="one-shot import Claude Code memory files as candidates",
    )

    p_daemon = sub.add_parser("daemon", help="control thememoryd")
    p_daemon.add_argument("action", choices=["start", "stop", "restart", "status"])

    p_rem = sub.add_parser("remember", help="remember a fact")
    p_rem.add_argument("content")
    p_rem.add_argument("--origin", default="user_requested")
    p_rem.add_argument("--type", default="preference")
    p_rem.add_argument("--slot-key", default=None)

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)

    sub.add_parser("context")
    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--probe", action="store_true", help="also run LLM capability probe")
    sub.add_parser("backup", help="snapshot memory.db and turns.db now")
    p_bench = sub.add_parser("benchmark", help="measure embed latency and write [latency]")
    p_bench.add_argument("--no-write", action="store_true")
    p_imp = sub.add_parser("import-host", help="import host native memory (path B)")
    p_imp.add_argument("--host", default="claude-code")
    p_imp.add_argument("--project-root", type=Path, default=None)
    p_imp.add_argument("--dry-run", action="store_true")
    p_eval = sub.add_parser("eval", help="run offline evaluation fixtures (M4)")
    p_eval.add_argument(
        "suite", choices=["retrieval", "counterfactual"], nargs="?", default="retrieval"
    )
    p_eval.add_argument("--fixture", type=Path, default=None)
    p_eval.add_argument("--k", type=int, default=3)
    p_eval.add_argument("--calibrate", action="store_true", help="sweep cosine tau")
    p_eval.add_argument("--write", action="store_true", help="persist best tau into config")

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
        choices=[
            "claude-code",
            "deepseek-harness",
            "dsh",
            "hermes",
            "cursor",
            "codex",
            "workbuddy",
            "doubao",
        ],
    )
    p_inst.add_argument("--profile", default=None)
    p_inst.add_argument(
        "--allow-tools",
        action="store_true",
        help="opt in to host allow-rules for context/search/remember/correct; forget stays prompted",
    )

    p_un = sub.add_parser("uninstall")
    p_un.add_argument(
        "host",
        choices=[
            "claude-code",
            "deepseek-harness",
            "dsh",
            "hermes",
            "cursor",
            "codex",
            "workbuddy",
            "doubao",
            "all",
        ],
    )
    p_un.add_argument("--profile", default=None)
    p_un.add_argument(
        "--delete-data",
        action="store_true",
        help="with uninstall all, delete the data directory after two confirmations",
    )
    p_un.add_argument("--yes", action="store_true", help="first confirmation for --delete-data")
    p_un.add_argument(
        "--confirm",
        default=None,
        help="second confirmation; must be the word DELETE",
    )

    p_ex = sub.add_parser("extract")
    p_ex.add_argument("--now", action="store_true")
    p_ex.add_argument("--session-id", default=None)
    p_ex.add_argument("--session-ref", default=None)
    p_ex.add_argument("--host", default="claude-code")

    p_ui = sub.add_parser("ui", help="open local web config UI")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true")

    p_mem = sub.add_parser("memory", help="inspect memory items")
    mem_sub = p_mem.add_subparsers(dest="mem_cmd", required=True)
    ml = mem_sub.add_parser("list")
    ml.add_argument("--type", default=None)
    ml.add_argument("--scope", default=None)
    ml.add_argument("--status", default=None)
    ml.add_argument("--since", default=None)
    ml.add_argument("--limit", type=int, default=50)
    ms = mem_sub.add_parser("show")
    ms.add_argument("id")
    msearch = mem_sub.add_parser("search")
    msearch.add_argument("query")
    msearch.add_argument("--limit", type=int, default=8)
    mf = mem_sub.add_parser("forget")
    mf.add_argument("id")
    mlog = mem_sub.add_parser("log")
    mlog.add_argument("--since", default=None)
    mlog.add_argument("--limit", type=int, default=50)

    p_turns = sub.add_parser("turns")
    turns_sub = p_turns.add_subparsers(dest="turns_cmd", required=True)
    ts = turns_sub.add_parser("search")
    ts.add_argument("query")
    ts.add_argument("--limit", type=int, default=20)

    p_purge = sub.add_parser("purge", help="physical purge (destructive)")
    p_purge.add_argument("--scope", default="turns", choices=["turns"])
    p_purge.add_argument("--yes", action="store_true", help="confirm PURGE")

    p_pause = sub.add_parser("pause", help="pause capture globally")
    p_pause.add_argument("--for", dest="duration", default=None, help="e.g. 2h / 30m")
    sub.add_parser("resume", help="resume capture")

    p_as = sub.add_parser("autostart", help="register OS login autostart")
    p_as.add_argument("action", choices=["install", "uninstall", "write-entry"])

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
            out = rpc("admin.doctor", {})
            if getattr(args, "probe", False):
                out["probe"] = rpc("admin.probe", {})
            return _print(out)
        if args.cmd == "backup":
            return _print(rpc("admin.backup", {}))
        if args.cmd == "benchmark":
            return _print(rpc("admin.benchmark", {"write": not args.no_write}))
        if args.cmd == "import-host":
            return _print(
                rpc(
                    "admin.import_host",
                    {
                        "host": args.host,
                        "project_root": str(args.project_root) if args.project_root else None,
                        "dry_run": args.dry_run,
                    },
                )
            )
        if args.cmd == "eval":
            if args.suite == "counterfactual":
                from thememory.eval.counterfactual import run_counterfactual
                from thememory.paths import home_dir

                return _print(run_counterfactual(home_dir()))
            if args.calibrate:
                from thememory.eval.calibrate import calibrate_tau
                from thememory.paths import home_dir

                result = calibrate_tau(
                    root=home_dir() if args.write else None,
                    fixture=args.fixture,
                    k=args.k,
                    write=args.write,
                )
                return _print(result)
            return _print(
                rpc(
                    "admin.eval_retrieval",
                    {
                        "fixture": str(args.fixture) if args.fixture else None,
                        "k": args.k,
                    },
                )
            )
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
        if args.cmd == "memory":
            return cmd_memory(args)
        if args.cmd == "turns":
            return _print(
                rpc(
                    "admin.turns_search",
                    {"query": args.query, "limit": args.limit},
                )
            )
        if args.cmd == "purge":
            if not confirm_once(phrase="PURGE", yes=args.yes):
                print("refusing: pass --yes or type PURGE to confirm", file=sys.stderr)
                return 1
            return _print(rpc("admin.purge", {"scope": args.scope, "confirm": "PURGE"}))
        if args.cmd == "pause":
            until = _pause_until(args.duration)
            return _print(rpc("admin.pause", {"until": until}))
        if args.cmd == "resume":
            return _print(rpc("admin.resume", {}))
        if args.cmd == "autostart":
            return cmd_autostart(args)
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


def _pause_until(duration: str | None) -> str:
    if not duration:
        return "forever"
    from datetime import UTC, datetime, timedelta

    raw = duration.strip().lower()
    now = datetime.now(UTC)
    if raw.endswith("h"):
        delta = timedelta(hours=float(raw[:-1]))
    elif raw.endswith("m"):
        delta = timedelta(minutes=float(raw[:-1]))
    elif raw.endswith("d"):
        delta = timedelta(days=float(raw[:-1]))
    else:
        delta = timedelta(hours=float(raw))
    return (now + delta).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def cmd_memory(args: argparse.Namespace) -> int:
    if args.mem_cmd == "list":
        return _print(
            rpc(
                "admin.memory_list",
                {
                    "type": args.type,
                    "scope": args.scope,
                    "status": args.status,
                    "since": args.since,
                    "limit": args.limit,
                },
            )
        )
    if args.mem_cmd == "show":
        return _print(rpc("admin.memory_show", {"id": args.id}))
    if args.mem_cmd == "search":
        return _print(
            rpc("tool.search", {"host": "cli", "query": args.query, "limit": args.limit})
        )
    if args.mem_cmd == "forget":
        return _print(rpc("tool.forget", {"host": "cli", "id": args.id}))
    if args.mem_cmd == "log":
        return _print(
            rpc("admin.memory_log", {"since": args.since, "limit": args.limit})
        )
    return 1


def cmd_autostart(args: argparse.Namespace) -> int:
    from thememory.installer import autostart as auto

    if args.action == "install":
        print(json.dumps(auto.install_autostart(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "uninstall":
        auto.uninstall_autostart()
        print("ok")
        return 0
    if args.action == "write-entry":
        print(json.dumps(auto.write_stable_entry(), ensure_ascii=False, indent=2))
        return 0
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
    from thememory.installer.autostart import write_stable_entry

    print(write_stable_entry(root))
    if args.repair:
        from thememory.installer.autostart import repair_install

        print(json.dumps(repair_install(root), ensure_ascii=False, indent=2))
        return 0
    if args.autostart:
        from thememory.installer.autostart import install_autostart

        print(install_autostart(root))
    if not args.skip_model:
        print("Installing embedding model (may download ~90MB)...")
        from thememory.outbound.embed_onnx import optimize_onnx

        model_dir = ensure_model(root)
        print(json.dumps(optimize_onnx(model_dir), ensure_ascii=False))
    from thememory.installer.bytecode import compile_installed_package

    print(json.dumps(compile_installed_package(), ensure_ascii=False))
    cfg = load(root)
    print(f"data dir: {root}")
    print(f"llm configured: {bool(cfg['llm']['extraction'].get('base_url') and cfg['llm']['extraction'].get('model'))}")
    # start daemon
    cmd_daemon(argparse.Namespace(action="start", home=None))
    if args.benchmark:
        from thememory.installer.benchmark import run_benchmark

        print("Running local benchmark...")
        print(json.dumps(run_benchmark(root, write=True), ensure_ascii=False, indent=2))
    if args.probe:
        try:
            print("Running LLM capability probe...")
            print(json.dumps(rpc("admin.probe", {}), ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(f"probe skipped: {exc}")
    if args.import_claude:
        from thememory.installer.host_import import import_host_memories

        print("Importing Claude Code memory files as candidates...")
        print(
            json.dumps(
                import_host_memories(host="claude-code", root=root),
                ensure_ascii=False,
                indent=2,
            )
        )
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

    if args.action == "restart":
        cmd_daemon(argparse.Namespace(action="stop"))
        args.action = "start"
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
        print(host_dsh.INSTALL_PROMPT)
        if args.profile is None:
            # also patch headless for convenience
            print(f"wrote {host_dsh.install(profile='headless')}")
        _note_allow_tools_skipped(args)
        return 0
    if host == "claude-code":
        from thememory.installer import host_claude_code

        print(
            json.dumps(
                host_claude_code.install(allow_tools=bool(args.allow_tools)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if host == "hermes":
        from thememory.installer import host_hermes

        print(json.dumps(host_hermes.install(), ensure_ascii=False, indent=2))
        _note_allow_tools_skipped(args)
        return 0
    if host == "cursor":
        from thememory.installer import host_cursor

        print(json.dumps(host_cursor.install(), ensure_ascii=False, indent=2))
        _note_allow_tools_skipped(args)
        return 0
    if host == "codex":
        from thememory.installer import host_codex

        print(json.dumps(host_codex.install(), ensure_ascii=False, indent=2))
        _note_allow_tools_skipped(args)
        return 0
    if host == "workbuddy":
        from thememory.installer import host_workbuddy

        print(json.dumps(host_workbuddy.install(), ensure_ascii=False, indent=2))
        _note_allow_tools_skipped(args)
        return 0
    if host == "doubao":
        from thememory.installer import host_doubao

        info = host_doubao.install()
        print(host_doubao.instructions_text())
        print(json.dumps(info, ensure_ascii=False, indent=2))
        _note_allow_tools_skipped(args)
        return 0
    return 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    hosts = (
        ["claude-code", "deepseek-harness", "hermes", "cursor", "codex", "workbuddy", "doubao"]
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
        elif host == "codex":
            from thememory.installer import host_codex

            host_codex.uninstall()
            print("uninstalled codex")
        elif host == "workbuddy":
            from thememory.installer import host_workbuddy

            host_workbuddy.uninstall()
            print("uninstalled workbuddy")
        elif host == "doubao":
            from thememory.installer import host_doubao

            host_doubao.uninstall()
            print("uninstalled doubao")
    if args.host == "all":
        from thememory.installer.autostart import uninstall_autostart

        uninstall_autostart()
        print("uninstalled autostart")
    if args.delete_data:
        if args.host != "all":
            print("refusing: --delete-data is only valid with uninstall all", file=sys.stderr)
            return 1
        if not confirm_twice(phrase="DELETE", yes=args.yes, confirm=args.confirm):
            print("refusing: data directory kept", file=sys.stderr)
            return 1
        from thememory.paths import home_dir

        delete_data_dir(home_dir())
        print("deleted data directory")
    return 0


def confirm_once(*, phrase: str, yes: bool) -> bool:
    """One confirmation: --yes, or typing the phrase on a terminal."""
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"Type {phrase} to confirm: ")
    except EOFError:
        return False
    return answer.strip() == phrase


def confirm_twice(*, phrase: str, yes: bool, confirm: str | None) -> bool:
    """Two confirmations. --yes alone is not enough."""
    if yes and (confirm or "").strip() == phrase:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        first = input(f"Type {phrase} to confirm: ")
        second = input(f"Type {phrase} again: ")
    except EOFError:
        return False
    return first.strip() == phrase and second.strip() == phrase


def delete_data_dir(root: Path) -> None:
    import shutil

    target = root.resolve()
    home = Path.home().resolve()
    if target == home or target == Path(target.anchor).resolve():
        raise RuntimeError("refusing to delete the user home")
    if target.exists():
        shutil.rmtree(target)


def _note_allow_tools_skipped(args: argparse.Namespace) -> None:
    if getattr(args, "allow_tools", False) and args.host != "claude-code":
        print(
            json.dumps(
                {"allow_tools": "skipped", "reason": "permission_format_unverified"},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
