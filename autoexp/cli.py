import argparse
import shutil
import sys
import uuid
from pathlib import Path

from .reports import report_instruction, set_report_instruction, write_report_bundle
from .runner import (
    RUN_CONTEXT,
    compute_hashes,
    docker_ready,
    find_duplicate_output_run,
    hash_run_output,
    local_run_context,
    run_script,
    run_script_local,
    runner_type,
)
from .runs import (
    copy_run_source,
    get_run,
    new_run_id,
    restore_run_state,
    run_stage_commit,
    script_name,
    source_root_for_run,
)
from .runtime import doctor
from .store import (
    autoexp_git,
    db,
    git_commit_source,
    init_db,
    insert_run,
    require_autoexp_git_repo,
    update_run,
)
from .workspace import (
    create_project,
    die,
    is_project_root,
    project_root,
    register_project,
    run_dir_for,
    source_paths,
    write_json,
)


def init_cmd(args):
    docker, _ = docker_ready()
    runner = "docker" if docker else "local"
    name = Path(args.project_name)
    root = create_project(name.expanduser(), args.title or name.name, runner)
    register_project(root)
    print(f"initialized autoexp project: {root}")
    print(f"runner: {runner}")
    if not docker:
        print('sandboxing: install Docker, then set "runner": "docker" in autoexp.json', file=sys.stderr)


def print_run(run, duplicate=False):
    print(f"run_id: {run['run_id']}")
    print(f"status: {'duplicate' if duplicate else run['status']}")
    if duplicate:
        print(f"duplicate_of: {run['run_id']}")
    print(f"capsule_hash: {run['capsule_hash']}")
    print(f"output_hash: {run['output_hash']}")
    print(f"created: {'no' if duplicate else 'yes'}")
    print(f"run_dir: {run['run_dir']}")
    if run.get("report_path"):
        print(f"report: {run['report_path']}")


# --- run: the core command --------------------------------------------------

def _prepare_fresh(root, source_root, stage_commit):
    """Build a new run snapshot in runs/<id>/ and its initial metadata."""
    tmp = root / "runs" / f".tmp_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    for name in ("output", "logs", "report"):
        (tmp / name).mkdir()
    copy_run_source(source_root, tmp)

    hashes = compute_hashes(tmp)
    run_id, created_at = new_run_id(hashes, root)
    run_dir = root / "runs" / run_id
    tmp.rename(run_dir)

    meta = {
        "run_id": run_id,
        "run_dir": f"runs/{run_id}",
        "report_path": "",
        "output_hash": "",
        **hashes,
        "script_name": script_name(run_id, run_dir),
        "stage_commit": stage_commit,
        "status": "running",
        "stage_status": {"script": "running"},
        "created_at": created_at,
    }
    return run_id, run_dir, meta, hashes


def _prepare_rerun(root, source_run, source_root):
    """Reuse an existing run's directory, clearing its output/logs for a fresh execution."""
    run_id = source_run["run_id"]
    run_dir = run_dir_for(source_run, root)
    if not run_dir.exists():
        die(f"missing run directory: {run_dir.relative_to(root)}")
    for name in ("output", "logs"):
        shutil.rmtree(run_dir / name, ignore_errors=True)
        (run_dir / name).mkdir(parents=True)

    hashes = compute_hashes(source_root)
    meta = {
        **source_run,
        **hashes,
        "output_hash": "",
        "script_name": script_name(run_id, source_root),
        "status": "running",
        "stage_status": {"script": "running"},
    }
    return run_id, run_dir, meta, hashes


def _print_duplicate(duplicate, root):
    run_dir = run_dir_for(duplicate, root)
    duplicate = {**duplicate, "run_dir": str(run_dir.relative_to(root))}
    report_path = duplicate.get("report_path")
    if report_path and (root / report_path).exists():
        duplicate["report_path"] = report_path
    print_run(duplicate, duplicate=True)


def run_cmd(args):
    root = project_root()
    require_autoexp_git_repo(root)
    init_db(root)

    source_run = get_run(args.run_id, root) if args.run_id else None
    source_root = source_root_for_run(source_run, root) if source_run else root

    if source_run:
        print(f"refreshing run artifacts for {args.run_id}")
        stage_commit = run_stage_commit(source_run)
        run_id, run_dir, meta, hashes = _prepare_rerun(root, source_run, source_root)
    else:
        stage_commit = git_commit_source("autoexp source snapshot", root)[0]
        run_id, run_dir, meta, hashes = _prepare_fresh(root, source_root, stage_commit)

    def persist(meta, *, new=False, bundle_run_id=None):
        write_json(run_dir / "run.json", meta)
        (insert_run if new else update_run)(meta, root)
        write_report_bundle(bundle_run_id or run_id, root)

    persist(meta, new=source_run is None)

    try:
        runner = runner_type(root)
        ctx = RUN_CONTEXT if runner == "docker" else local_run_context(run_dir, run_dir, root)
        write_json(run_dir / "ctx.json", ctx)
        execute = run_script if runner == "docker" else run_script_local
        code = execute(run_dir, root=root, source_root=run_dir)
    except SystemExit:
        meta.update({
            "output_hash": hash_run_output(run_dir),
            "status": "failed",
            "stage_status": {"script": "failed:preflight"},
        })
        persist(meta)
        print_run(meta)
        raise

    output_hash = hash_run_output(run_dir)
    status = "success" if code == 0 else "failed"

    if status == "success" and source_run is None:
        duplicate = find_duplicate_output_run(hashes, output_hash, root=root)
        if duplicate:
            meta.update({
                "output_hash": output_hash,
                "status": "duplicate",
                "stage_status": {"script": "duplicate"},
            })
            persist(meta, bundle_run_id=duplicate["run_id"])
            _print_duplicate(duplicate, root)
            return

    meta.update({
        "output_hash": output_hash,
        "status": status,
        "stage_status": {"script": "success" if code == 0 else f"failed:{code}"},
    })
    persist(meta)
    print_run(meta)


# --- other commands ----------------------------------------------------------

def status_cmd(args):
    init_db()
    conn = db()
    rows = conn.execute(
        "select run_id, status, capsule_hash, created_at from runs order by created_at desc limit ?",
        (args.limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("no runs")
        return
    for row in rows:
        print(f"{row['created_at']}  {row['status']:<7}  {row['run_id']}  {row['capsule_hash'][:12]}")


def hash_cmd(args):
    for key, value in sorted(compute_hashes().items()):
        print(f"{key}: {value}")


def diff_cmd(args):
    a = get_run(args.run_a)
    b = get_run(args.run_b)
    autoexp_git(["diff", run_stage_commit(a), run_stage_commit(b), "--", *source_paths()], check=False)


def restore_cmd(args):
    restore_run_state(args.run_id)
    print(f"restored script/config from {args.run_id}")


def report_instruction_cmd(args):
    if args.path:
        path = set_report_instruction(args.path)
        print(f"report_instruction_file: {path}")
        return
    info = report_instruction()
    print(f"report_instruction_source: {info['source']}")


def view_cmd(args):
    from .server import view

    root = project_root() if is_project_root(Path.cwd()) else None
    if root:
        register_project(root)
    view(args.host, args.port, args.allow_origin, project=args.project)


def doctor_cmd(args):
    result = doctor()
    for item in result["checks"]:
        status = "ok" if item["ok"] else "fail" if item.get("required", True) else "warn"
        detail = f" - {item['detail']}" if item.get("detail") else ""
        print(f"{status}: {item['name']}{detail}")
    print(f"overall: {'ok' if result['ok'] else 'failed'}")


def mcp_cmd(args):
    from .mcp import serve

    serve()


# --- argument parser & entry point ------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="autoexp")
    sub = parser.add_subparsers(required=True)

    init = sub.add_parser("init")
    init.add_argument("project_name")
    init.add_argument("--title")
    init.set_defaults(fn=init_cmd)

    run = sub.add_parser("run")
    run.add_argument("run_id", nargs="?")
    run.set_defaults(fn=run_cmd)

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(fn=status_cmd)

    hashes = sub.add_parser("hash")
    hashes.set_defaults(fn=hash_cmd)

    diff = sub.add_parser("diff")
    diff.add_argument("run_a")
    diff.add_argument("run_b")
    diff.set_defaults(fn=diff_cmd)

    restore = sub.add_parser("restore")
    restore.add_argument("run_id")
    restore.set_defaults(fn=restore_cmd)

    report = sub.add_parser("report-instruction")
    report.add_argument("path", nargs="?")
    report.set_defaults(fn=report_instruction_cmd)

    view_parser = sub.add_parser("view")
    view_parser.add_argument("--host", default="127.0.0.1")
    view_parser.add_argument("--port", type=int, default=8765)
    view_parser.add_argument("--project")
    view_parser.add_argument("--allow-origin", action="append", default=[])
    view_parser.set_defaults(fn=view_cmd)

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.set_defaults(fn=doctor_cmd)

    mcp_parser = sub.add_parser("mcp")
    mcp_parser.set_defaults(fn=mcp_cmd)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError) as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
