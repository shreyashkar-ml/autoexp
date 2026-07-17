import argparse
import json
import shlex
import sys
import tempfile
import webbrowser
from urllib.parse import quote

from .reports import add_document, report_instruction, set_report_instruction
from .review import create_review_session, wait_for_review
from .runtime import diff_runs, doctor, list_runs
from .workspace import (
    FILE_ROLES, create_experiment, declare_file, die, experiment_entry,
    list_experiments, manifest_files, materialize_workspace, now,
    repository_root, resolve_root,
)


def emit(value, *, as_json=False):
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, default=str))
    else:
        print(value)


def selected(args):
    return resolve_root(experiment=getattr(args, "experiment", None))


def experiment_create_cmd(args):
    config = {}
    entrypoint = args.entrypoint
    command = args.command
    working_dir = args.working_dir
    if args.kind == "autoresearch":
        missing = [name for name in ("program", "candidate", "evaluator") if not getattr(args, name)]
        if missing:
            raise ValueError(f"autoresearch requires: {', '.join('--' + name for name in missing)}")
        entrypoint = args.candidate
        working_dir = working_dir or "."
        command = command or f"python {shlex.quote(args.candidate)} --ctx ${{CTX}} && python {shlex.quote(args.evaluator)} --ctx ${{CTX}}"
        config["autoresearch"] = {
            "objective": {"metric": args.metric, "direction": args.direction, "baseline": args.baseline, "budget_sec": args.budget},
            "files": [
                {"path": args.program, "role": "human", "desc": "research directions"},
                {"path": args.candidate, "role": "agent", "desc": "agent-owned candidate"},
                {"path": args.evaluator, "role": "frozen", "desc": "frozen evaluator"},
            ],
            "metric": {"kind": args.metric_kind, "path": args.metric_path, "key": args.metric_key},
            "agent": {"cmd": shlex.split(args.agent_command) if args.agent_command else ["codex", "exec", "Use the installed autoexp CLI to optimize the active Autoresearch experiment."]},
        }
    entry = create_experiment(
        args.objective, root=args.repo, title=args.title, kind=args.kind,
        entrypoint=entrypoint, command=command, working_dir=working_dir,
        runner=args.runner, config=config,
    )
    if args.kind == "autoresearch":
        declare_file(entry["experiment_id"], args.program, "supporting-source", description="research program")
        declare_file(entry["experiment_id"], args.evaluator, "frozen-evaluator", description="frozen evaluator")
    emit(_public_entry(entry), as_json=args.json)


def _public_entry(entry):
    return {key: entry.get(key) for key in (
        "experiment_id", "repo_id", "repo_path", "title", "objective", "kind",
        "status", "runner", "data_path", "created_at", "updated_at",
    )}


def experiment_list_cmd(args):
    emit([_public_entry(item) for item in list_experiments(args.repo_id)], as_json=True)


def experiment_show_cmd(args):
    emit(_public_entry(experiment_entry(selected(args))), as_json=True)


def files_add_cmd(args):
    emit(declare_file(selected(args), args.path, args.role, description=args.description), as_json=True)


def files_list_cmd(args):
    emit(manifest_files(selected(args)), as_json=True)


def print_run(run, root):
    emit(run, as_json=True)


def run_cmd(args):
    from .execution import execute
    root = selected(args)
    run = execute(root=root, run_id=args.run_id, trigger_kind="agent" if args.agent else "cli", actor_name=args.actor, metadata={"title": args.title} if args.title else None)
    print_run(run, root)
    if run["status"] != "success":
        raise SystemExit(130 if run["status"] == "canceled" else (run.get("exit_code") or 1))


def snapshot_cmd(args):
    from .provenance import create_trigger
    from .snapshots import capture_workspace
    root = selected(args)
    trigger = create_trigger("cli", root=root, actor_name="human", metadata={"operation": "snapshot"})
    emit(capture_workspace(root, label=args.label, created_by_trigger_id=trigger["trigger_id"]), as_json=True)


def status_cmd(args):
    emit(list_runs(args.limit, selected(args)), as_json=True)


def hash_cmd(args):
    from .runner import compute_hashes
    with tempfile.TemporaryDirectory(prefix="autoexp-hash-") as tmp:
        materialize_workspace(selected(args), tmp)
        emit(compute_hashes(tmp), as_json=True)


def diff_cmd(args):
    print(diff_runs(args.run_a, args.run_b, selected(args)), end="")


def restore_cmd(args):
    from .runs import restore_run_state
    run, _ = restore_run_state(args.run_id, selected(args))
    emit({"restored": run["run_id"], "repository": experiment_entry(selected(args))["repo_path"]}, as_json=True)


def report_instruction_cmd(args):
    root = selected(args)
    emit(set_report_instruction(args.path, root) if args.path else report_instruction(root), as_json=True)


def document_add_cmd(args):
    emit(add_document(args.path, kind=args.kind, title=args.title, root=selected(args), run_id=args.run_id), as_json=True)


def view_cmd(args):
    from .server import view
    view(args.host, args.port, args.allow_origin, experiment=args.experiment, open_browser=not args.no_open)


def review_cmd(args):
    from .server import ensure_server
    root = selected(args)
    token, session = create_review_session(root, ttl=args.timeout)
    base, _ = ensure_server(args.host, args.port)
    url = f"{base}/?experiment={quote(session['experiment_id'])}&review={quote(token)}"
    if args.no_open:
        print(url, file=sys.stderr)
    else:
        webbrowser.open(url)
    print("waiting for browser review...", file=sys.stderr, flush=True)
    result = wait_for_review(token, timeout=args.timeout)
    if not result or result["status"] != "completed":
        raise ValueError("review session expired before feedback was submitted")
    emit({"session_id": result["session_id"], "notes": result["notes"]}, as_json=True)


def doctor_cmd(args):
    emit(doctor(selected(args)), as_json=True)


def relink_cmd(args):
    from .store import db

    path = str(repository_root(args.path))
    conn = db()
    target = conn.execute(
        "select 1 from repositories where repo_id = ?", (args.repo_id,)
    ).fetchone()
    if not target:
        conn.close()
        raise ValueError(f"unknown repository id: {args.repo_id}")
    conflict = conn.execute(
        "select repo_id from repositories where path = ? and repo_id != ?",
        (path, args.repo_id),
    ).fetchone()
    if conflict:
        conn.close()
        raise ValueError(f"path is already registered as repository {conflict['repo_id']}")
    conn.execute(
        "update repositories set path = ?, last_opened_at = ? where repo_id = ?",
        (path, now(), args.repo_id),
    )
    conn.commit()
    conn.close()
    emit({"repo_id": args.repo_id, "path": path}, as_json=True)
def import_cmd(args):
    from .importer import import_legacy_project
    emit(import_legacy_project(args.path), as_json=True)


def research_state_cmd(args):
    from .autoresearch import for_project
    emit(for_project(selected(args)).state(), as_json=True)


def research_preflight_cmd(args):
    from .autoresearch import for_project
    emit(for_project(selected(args)).preflight(require_agent=False), as_json=True)


def research_attempt_cmd(args):
    from .autoresearch import for_project
    research = for_project(selected(args))
    started = research.begin_attempt(args.hypothesis)
    attempt = started["attempt"]
    if attempt["state"] == "running":
        attempt = research.finish_attempt(attempt["key"])
    emit(attempt, as_json=True)


def add_selector(parser):
    parser.add_argument("--experiment", help="experiment id; defaults to the latest in this repository")


def build_parser():
    parser = argparse.ArgumentParser(prog="autoexp")
    sub = parser.add_subparsers(required=True)

    experiment = sub.add_parser("experiment", help="create and inspect global experiment records")
    experiment_sub = experiment.add_subparsers(required=True)
    create = experiment_sub.add_parser("create")
    create.add_argument("objective")
    create.add_argument("--repo", default=".")
    create.add_argument("--title")
    create.add_argument("--kind", choices=("standard", "autoresearch"), default="standard")
    create.add_argument("--entrypoint")
    create.add_argument("--command")
    create.add_argument("--working-dir")
    create.add_argument("--runner", choices=("local", "docker"), default="local")
    create.add_argument("--program")
    create.add_argument("--candidate")
    create.add_argument("--evaluator")
    create.add_argument("--metric", default="score")
    create.add_argument("--direction", choices=("min", "max"), default="max")
    create.add_argument("--baseline", type=float)
    create.add_argument("--budget", type=int, default=300)
    create.add_argument("--metric-kind", choices=("json", "regex"), default="json")
    create.add_argument("--metric-path", default="metrics.json")
    create.add_argument("--metric-key", default="score")
    create.add_argument("--agent-command")
    create.add_argument("--json", action="store_true")
    create.set_defaults(fn=experiment_create_cmd)
    listing = experiment_sub.add_parser("list")
    listing.add_argument("--repo-id")
    listing.set_defaults(fn=experiment_list_cmd)
    show = experiment_sub.add_parser("show")
    add_selector(show)
    show.set_defaults(fn=experiment_show_cmd)

    files = sub.add_parser("files", help="manage the global file manifest")
    files_sub = files.add_subparsers(required=True)
    files_add = files_sub.add_parser("add")
    files_add.add_argument("path")
    files_add.add_argument("--role", choices=sorted(FILE_ROLES), required=True)
    files_add.add_argument("--description", default="")
    add_selector(files_add)
    files_add.set_defaults(fn=files_add_cmd)
    files_list = files_sub.add_parser("list")
    add_selector(files_list)
    files_list.set_defaults(fn=files_list_cmd)

    run = sub.add_parser("run")
    run.add_argument("run_id", nargs="?")
    run.add_argument("--title")
    run.add_argument("--agent", action="store_true")
    run.add_argument("--actor", default="human")
    add_selector(run)
    run.set_defaults(fn=run_cmd)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--label")
    add_selector(snapshot)
    snapshot.set_defaults(fn=snapshot_cmd)
    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=20)
    add_selector(status)
    status.set_defaults(fn=status_cmd)
    hashes = sub.add_parser("hash")
    add_selector(hashes)
    hashes.set_defaults(fn=hash_cmd)
    diff = sub.add_parser("diff")
    diff.add_argument("run_a")
    diff.add_argument("run_b")
    add_selector(diff)
    diff.set_defaults(fn=diff_cmd)
    restore = sub.add_parser("restore")
    restore.add_argument("run_id")
    add_selector(restore)
    restore.set_defaults(fn=restore_cmd)
    report = sub.add_parser("report-instruction")
    report.add_argument("path", nargs="?")
    add_selector(report)
    report.set_defaults(fn=report_instruction_cmd)

    document = sub.add_parser("document")
    document_sub = document.add_subparsers(required=True)
    document_add = document_sub.add_parser("add")
    document_add.add_argument("path")
    document_add.add_argument("--kind", choices=("report", "insight"), required=True)
    document_add.add_argument("--title")
    document_add.add_argument("--run-id")
    add_selector(document_add)
    document_add.set_defaults(fn=document_add_cmd)

    view = sub.add_parser("view")
    view.add_argument("--host", default="127.0.0.1")
    view.add_argument("--port", type=int, default=8765)
    view.add_argument("--experiment")
    view.add_argument("--allow-origin", action="append", default=[])
    view.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    view.set_defaults(fn=view_cmd)
    review = sub.add_parser("review")
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8765)
    review.add_argument("--timeout", type=int, default=900)
    review.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    add_selector(review)
    review.set_defaults(fn=review_cmd)

    research = sub.add_parser("research")
    research_sub = research.add_subparsers(required=True)
    for name, fn in (("state", research_state_cmd), ("preflight", research_preflight_cmd)):
        child = research_sub.add_parser(name)
        add_selector(child)
        child.set_defaults(fn=fn)
    attempt = research_sub.add_parser("attempt")
    attempt.add_argument("hypothesis")
    add_selector(attempt)
    attempt.set_defaults(fn=research_attempt_cmd)

    doctor_parser = sub.add_parser("doctor")
    add_selector(doctor_parser)
    doctor_parser.set_defaults(fn=doctor_cmd)
    relink = sub.add_parser("relink")
    relink.add_argument("repo_id")
    relink.add_argument("path")
    relink.set_defaults(fn=relink_cmd)
    importer = sub.add_parser("import")
    importer.add_argument("path")
    importer.set_defaults(fn=import_cmd)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError) as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
