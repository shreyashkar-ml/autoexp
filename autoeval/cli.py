import argparse
import shutil
import sys
import uuid
from pathlib import Path

from .agent import install_agent_files
from .project import (
    RUN_CONTEXT,
    STORAGE_PATHS,
    autoeval_git,
    compute_hashes,
    copy_run_source,
    create_project,
    current_autoeval_commit,
    db,
    die,
    find_duplicate_output_run,
    get_run,
    git_commit_run,
    git_commit_storage,
    git_status,
    hash_run_output,
    init_db,
    insert_run,
    is_project_root,
    new_run_id,
    now,
    project_root,
    register_project,
    require_autoeval_git_repo,
    report_instruction,
    restore_run_state,
    local_run_context,
    run_script,
    run_script_local,
    run_stage_commit,
    runner_type,
    script_name,
    set_report_instruction,
    source_root_for_run,
    update_run,
    warn_docker_unavailable,
    upsert_stage_versions,
    write_report_bundle,
    write_json,
)
from .runtime import doctor


def init_cmd(args):
    root = create_project(Path(args.project_name).expanduser(), args.title or Path(args.project_name).name)
    register_project(root)
    warn_docker_unavailable()
    print(f"initialized autoeval project: {root}")


def print_run(run, duplicate=False):
    print(f"run_id: {run['run_id']}")
    print(f"status: {'duplicate' if duplicate else run['status']}")
    if duplicate:
        print(f"duplicate_of: {run['run_id']}")
    print(f"capsule_hash: {run['capsule_hash']}")
    print(f"output_hash: {run['output_hash']}")
    print(f"created: {'no' if duplicate else 'yes'}")
    if not duplicate:
        print(f"stored: {'yes' if run.get('stored') else 'no'}")
    print(f"run_dir: {run['run_dir']}")
    if run.get("report_path"):
        print(f"report: {run['report_path']}")


def run_cmd(args):
    root = project_root()
    require_autoeval_git_repo(root)
    init_db(root)

    source_run = get_run(args.run_id, root) if args.run_id else None
    source_root = source_root_for_run(source_run, root) if source_run else root
    if source_run:
        print(f"refreshing run artifacts for {args.run_id}")

    stage_commit = run_stage_commit(source_run) if source_run else current_autoeval_commit(root)
    unstored = bool(source_run and source_run.get("unstored_stage_changes"))
    if not source_run and git_status(STORAGE_PATHS, root=root):
        unstored = True
        print("note: executing unstored script/config changes; run `autoeval storage` to store them.", file=sys.stderr)

    if source_run:
        run_id = args.run_id
        run_dir = root / (source_run.get("run_dir") or f"runs/{run_id}")
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
    else:
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
            "unstored_stage_changes": unstored,
            "git_commit": "",
            "stored": False,
            "stored_at": None,
            "storage_label": None,
            "status": "running",
            "stage_status": {"script": "running"},
            "created_at": created_at,
        }

    write_json(run_dir / "run.json", meta)
    if source_run:
        update_run(meta, root)
    else:
        insert_run(meta, root)
    write_report_bundle(run_id, root)

    try:
        runner = runner_type(root)
        write_json(run_dir / "ctx.json", RUN_CONTEXT if runner == "docker" else local_run_context(run_dir, run_dir, root))
        code = run_script(run_dir, root=root, source_root=run_dir) if runner == "docker" else run_script_local(run_dir, root=root, source_root=run_dir)
    except SystemExit:
        meta.update({
            "output_hash": hash_run_output(run_dir),
            "status": "failed",
            "stage_status": {"script": "failed:preflight"},
        })
        write_json(run_dir / "run.json", meta)
        update_run(meta, root)
        write_report_bundle(run_id, root)
        print_run(meta)
        raise

    status = "success" if code == 0 else "failed"
    output_hash = hash_run_output(run_dir)

    if status == "success" and not source_run:
        duplicate = find_duplicate_output_run(hashes, output_hash, root=root)
        if duplicate:
            meta.update({
                "output_hash": output_hash,
                "status": "duplicate",
                "stage_status": {"script": "duplicate"},
            })
            write_json(run_dir / "run.json", meta)
            update_run(meta, root)
            write_report_bundle(duplicate["run_id"], root)
            run_dir = root / (duplicate.get("run_dir") or f"runs/{duplicate['run_id']}")
            duplicate = {**duplicate, "run_dir": str(run_dir.relative_to(root))}
            report_path = duplicate.get("report_path")
            if report_path and (root / report_path).exists():
                duplicate["report_path"] = report_path
            print_run(duplicate, duplicate=True)
            return

    meta.update({
        "output_hash": output_hash,
        "status": status,
        "stage_status": {"script": "success" if code == 0 else f"failed:{code}"},
    })
    write_json(run_dir / "run.json", meta)
    update_run(meta, root)
    write_report_bundle(run_id, root)
    print_run(meta)


def status_cmd(args):
    init_db()
    conn = db()
    rows = conn.execute(
        "select run_id, status, stored, capsule_hash, git_commit, created_at from runs order by created_at desc limit ?",
        (args.limit,),
    ).fetchall()
    conn.close()

    if not rows:
        print("no runs")
        return

    for row in rows:
        storage = "stored" if row["stored"] else "cache"
        print(f"{row['created_at']}  {row['status']:<7}  {storage:<6}  {row['run_id']}  {row['capsule_hash'][:12]}  {row['git_commit'][:12]}")


def hash_cmd(args):
    for key, value in sorted(compute_hashes().items()):
        print(f"{key}: {value}")


def storage_cmd(args):
    root = project_root()
    require_autoeval_git_repo(root)
    init_db(root)
    created_at = now()

    if args.run_id:
        run = get_run(args.run_id, root)
        run_dir = root / (run.get("run_dir") or f"runs/{args.run_id}")
        if not run_dir.exists():
            die(f"missing run directory: {run_dir.relative_to(root)}")

        run["output_hash"] = run.get("output_hash") or hash_run_output(run_dir)
        run.update({"stored": True, "stored_at": created_at, "storage_label": args.label})
        write_json(run_dir / "run.json", run)

        conn = db(root)
        exists = conn.execute("select 1 from runs where run_id = ?", (run["run_id"],)).fetchone()
        conn.close()
        if not exists:
            insert_run(run, root=root)

        commit, committed = git_commit_run(args.run_id, root=root)
        conn = db(root)
        conn.execute(
            "update runs set stored = 1, stored_at = ?, storage_label = ?, git_commit = ?, output_hash = ? where run_id = ?",
            (created_at, args.label, commit, run["output_hash"], args.run_id),
        )
        conn.commit()
        conn.close()
        write_report_bundle(args.run_id, root)
        inserted = upsert_stage_versions(run, commit, created_at, label=args.label, root=root, metadata_root=source_root_for_run(run, root))
        print(f"run_id: {args.run_id}")
    else:
        hashes = compute_hashes(root)
        commit, committed = git_commit_storage(args.message or "autoeval storage", root)
        inserted = upsert_stage_versions(hashes, commit, created_at, label=args.label, root=root)
        run = hashes

    print(f"storage_commit: {commit}")
    print(f"committed: {'yes' if committed else 'no'}")
    print(f"script_hash: {run['script_hash']}")
    print(f"capsule_hash: {run['capsule_hash']}")
    print(f"script_version: {'stored' if inserted['script'] else 'existing'}")


def diff_cmd(args):
    a = get_run(args.run_a)
    b = get_run(args.run_b)
    autoeval_git(["diff", run_stage_commit(a), run_stage_commit(b), "--", *STORAGE_PATHS], check=False)


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


def agent_install_cmd(args):
    result = install_agent_files(args.target, args.force)
    print(f"agent_target: {result['target']}")
    for path in result["written"]:
        print(f"created: {path}")


def mcp_cmd(args):
    from .mcp import serve

    serve()


def build_parser():
    parser = argparse.ArgumentParser(prog="autoeval")
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

    storage = sub.add_parser("storage")
    storage.add_argument("run_id", nargs="?")
    storage.add_argument("--label")
    storage.add_argument("--message")
    storage.set_defaults(fn=storage_cmd)

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

    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(required=True)
    install = agent_sub.add_parser("install")
    install.add_argument("--target", choices=("codex", "claude", "all"), default="all")
    install.add_argument("--force", action="store_true")
    install.set_defaults(fn=agent_install_cmd)

    mcp_parser = sub.add_parser("mcp")
    mcp_parser.set_defaults(fn=mcp_cmd)

    return parser


def main():
    args = build_parser().parse_args()
    args.fn(args)
