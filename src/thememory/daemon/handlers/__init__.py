"""IPC method handlers (spec §3.4)."""

from __future__ import annotations

from typing import Any, Callable

from thememory import PROTOCOL_VERSION, __version__
from thememory.daemon.ipc.protocol import hello_handshake
from thememory.runtime.service import MemoryService

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def dispatch(
    method: str,
    params: dict[str, Any],
    *,
    service: MemoryService,
    state: str,
    root: Any = None,
    on_shutdown: Callable[[], None] | None = None,
) -> dict[str, Any]:
    svc = service
    if method == "hello":
        return hello_handshake(
            params,
            server_version=__version__,
            server_protocol=PROTOCOL_VERSION,
            state=state,
        )
    if method == "tool.remember":
        return svc.remember(params)
    if method == "tool.search":
        return svc.search(params)
    if method == "tool.context":
        return svc.context(params)
    if method == "tool.forget":
        return svc.forget(params)
    if method == "tool.correct":
        return svc.correct(params)
    if method == "capture.turn":
        return svc.capture_turn(params)
    if method == "capture.event":
        return svc.capture_event(params)
    if method == "inject.session_start":
        return svc.inject_session_start(params)
    if method == "inject.per_turn":
        return svc.inject_per_turn(params)
    if method == "inject.after_compact":
        return svc.inject_after_compact(params)
    if method == "admin.status":
        return {
            "version": __version__,
            "state": state,
            "embedder": svc.embedder.available(),
            "llm": svc.llm.available(),
            "vectors": svc.vectors.as_debug(),
        }
    if method == "admin.doctor":
        return svc.doctor()
    if method == "admin.probe":
        return svc.run_capability_probe()
    if method == "admin.benchmark":
        from thememory.installer.benchmark import run_benchmark

        return run_benchmark(root, write=bool(params.get("write", True)))
    if method == "admin.import_host":
        from pathlib import Path

        from thememory.installer.host_import import import_host_memories

        pr = params.get("project_root")
        return import_host_memories(
            host=str(params.get("host") or "claude-code"),
            project_root=Path(pr) if pr else None,
            root=root,
            dry_run=bool(params.get("dry_run")),
            service=svc,
        )
    if method == "admin.eval_retrieval":
        import tempfile
        from pathlib import Path

        from thememory.eval.runner import run_retrieval_eval
        from thememory.eval.scores import append_score

        fix = params.get("fixture")
        tmp = Path(tempfile.mkdtemp(prefix="thememory-eval-"))
        result = run_retrieval_eval(
            fixture=Path(fix) if fix else None,
            root=tmp,
            k=int(params.get("k") or 3),
        )
        append_score(
            root,
            {
                "suite": "retrieval",
                "fixture": result.get("fixture"),
                "recall_at_k": result.get("recall_at_k"),
                "mrr": result.get("mrr"),
                "empty_accuracy": result.get("empty_accuracy"),
                "k": result.get("k"),
                "n_queries": result.get("n_queries"),
            },
        )
        return result
    if method == "admin.extract_now":
        sid = params.get("session_id")
        if not sid and params.get("session_ref"):
            sess = svc.ensure_session(params)
            sid = sess["id"]
        if not sid:
            raise ValueError("session_id required")
        return svc.run_extract(str(sid))
    if method == "admin.shutdown":
        if params.get("drain"):
            try:
                svc.process_jobs(limit=20)
            except Exception:  # noqa: BLE001
                pass
        if on_shutdown:
            on_shutdown()
        return {"ok": True, "drain": bool(params.get("drain"))}
    if method == "admin.reload":
        from thememory.config import load

        svc.reload_config(load(root))
        return {"ok": True}
    if method == "admin.config":
        return _admin_config(svc, params, root=root)
    if method == "admin.backup":
        return svc.run_db_backup()
    if method == "admin.pause":
        until = params.get("until") or "forever"
        return svc.set_pause(until=str(until))
    if method == "admin.resume":
        return svc.set_pause(until=None)
    if method == "admin.purge":
        return svc.purge(params)
    if method == "admin.memory_list":
        return svc.list_memory(params)
    if method == "admin.memory_show":
        return svc.show_memory(params)
    if method == "admin.memory_log":
        return svc.memory_log(params)
    if method == "admin.turns_search":
        return svc.search_turns(params)
    if method == "admin.maintenance":
        return {
            "maintenance": svc.run_maintenance(),
            "evict": svc.maybe_evict_turns(),
        }
    raise KeyError(method)


def _admin_config(svc: MemoryService, params: dict[str, Any], *, root: Any) -> dict[str, Any]:
    """admin.config get|set|validate (spec §12.3)."""
    from thememory.config import load, mask_secrets, write_config
    from thememory.config.loader import ConfigError, explain_config, get_path, set_path, validate

    action = str(params.get("action") or "get").lower()
    if action == "validate":
        try:
            cfg = load(root)
            validate(cfg)
            return {"status": "ok", "valid": True, "sources": explain_config(root).get("sources")}
        except ConfigError as exc:
            return {"status": "failed", "reason": "config_invalid", "detail": str(exc)}
    if action == "get":
        cfg = load(root)
        key = params.get("key")
        if key:
            try:
                value = get_path(cfg, str(key))
            except ConfigError as exc:
                return {"status": "failed", "reason": "config_invalid", "detail": str(exc)}
            if str(key).endswith("api_key") and value:
                value = "***"
            return {"status": "ok", "key": key, "value": value}
        return {"status": "ok", "config": mask_secrets(cfg), "sources": explain_config(root).get("sources")}
    if action == "set":
        key = params.get("key")
        if not key:
            return {"status": "failed", "reason": "op_invalid", "detail": "key required"}
        cfg = load(root)
        try:
            set_path(cfg, str(key), params.get("value"))
            validate(cfg)
            write_config(cfg, root)
            svc.reload_config(cfg)
        except ConfigError as exc:
            return {"status": "failed", "reason": "config_invalid", "detail": str(exc)}
        return {"status": "ok", "key": key, "value": params.get("value")}
    return {"status": "failed", "reason": "op_invalid", "detail": f"unknown action {action}"}
