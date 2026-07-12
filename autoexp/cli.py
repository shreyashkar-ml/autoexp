import argparse
import sys
from pathlib import Path

from .provenance import create_trigger, reproducibility_summary
from .reports import report_instruction, set_report_instruction
from .runner import compute_hashes, docker_ready
from .runs import restore_run_state
from .runtime import diff_runs, doctor, list_runs
from .snapshots import capture_workspace
from .store import init_db, require_autoexp_git_repo
from .workspace import create_project, die, is_project_root, project_root, register_project


def init_cmd(args):
    docker, _ = docker_ready()
    runner = "docker" if docker else "local"
    name = Path(args.project_name)
    root = create_project(
        name.expanduser(),
        args.title or name.name,
        runner,
        autoresearch=args.autoresearch,
    )
    register_project(root)
    print(f"initialized autoexp project: {root}")
    print(f"runner: {runner}")
    if args.autoresearch:
        print("mode: autoresearch")
    if not docker:
        print(
            'sandboxing: install Docker, then set "runner": "docker" in .autoexp/project.json',
            file=sys.stderr,
        )


def print_run(run, root=None):
    print(f"run_id: {run['run_id']}")
    print(f"status: {run['status']}")
    print(f"capsule_hash: {run['capsule_hash']}")
    print(f"output_hash: {run['output_hash']}")
    print("created: yes")
    print(f"run_dir: {run['run_dir']}")
    for key in (
        "source_snapshot_id",
        "parent_run_id",
        "reproduces_run_id",
        "runner",
        "runner_identity",
        "exit_code",
    ):
        if run.get(key) is not None:
            print(f"{key}: {run[key]}")
    print(f"reproducibility: {reproducibility_summary(run['run_id'], root)['state']}")
    if run.get("failure_message"):
        print(f"failure: {run.get('failure_kind')}: {run['failure_message']}")


def run_cmd(args):
    from .execution import execute

    root = project_root()
    run = execute(
        root=root,
        run_id=args.run_id,
        snapshot_id=args.snapshot_id,
        trigger_kind=args.trigger_kind,
        actor_name=args.actor_name,
        session_id=args.session_id,
        request_id=args.request_id,
    )
    print_run(run, root)
    if run["status"] != "success":
        if run["status"] == "canceled":
            raise SystemExit(130)
        code = run.get("exit_code")
        raise SystemExit(code if isinstance(code, int) and 0 < code < 256 else 1)


def snapshot_cmd(args):
    root = project_root()
    require_autoexp_git_repo(root)
    init_db(root)
    trigger = create_trigger(
        "cli",
        root=root,
        actor_name="human",
        metadata={"operation": "snapshot"},
    )
    snapshot = capture_workspace(
        root,
        label=args.label,
        created_by_trigger_id=trigger["trigger_id"],
    )
    print(f"snapshot_id: {snapshot['snapshot_id']}")
    print(f"source_hash: {snapshot['source_hash']}")


def status_cmd(args):
    init_db()
    runs = list_runs(args.limit)
    if not runs:
        print("no runs")
        return
    for run in runs:
        detail = [f"repro={reproducibility_summary(run['run_id'])['state']}"]
        if run.get("parent_run_id"):
            detail.append(f"parent={run['parent_run_id']}")
        if run.get("reproduces_run_id"):
            detail.append(f"reproduces={run['reproduces_run_id']}")
        if run.get("exit_code") is not None:
            detail.append(f"exit={run['exit_code']}")
        print(
            f"{run['created_at']}  {run['status']:<8}  {run['run_id']}  "
            f"{run['capsule_hash'][:12]}  {' '.join(detail)}"
        )


def hash_cmd(args):
    for key, value in sorted(compute_hashes().items()):
        print(f"{key}: {value}")


def diff_cmd(args):
    print(diff_runs(args.run_a, args.run_b), end="")


def restore_cmd(args):
    restore_run_state(args.run_id)
    print(f"restored script/config from {args.run_id}")


def report_instruction_cmd(args):
    if args.path:
        print(f"report_instruction_file: {set_report_instruction(args.path)}")
        return
    print(f"report_instruction_source: {report_instruction()['source']}")


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


def build_parser():
    parser = argparse.ArgumentParser(prog="autoexp")
    sub = parser.add_subparsers(required=True)

    init = sub.add_parser("init")
    init.add_argument("project_name")
    init.add_argument("--title")
    init.add_argument("--autoresearch", action="store_true")
    init.set_defaults(fn=init_cmd)

    run = sub.add_parser("run")
    run.add_argument("run_id", nargs="?")
    run.add_argument("--snapshot-id", help=argparse.SUPPRESS)
    run.add_argument("--trigger-kind", default="cli", help=argparse.SUPPRESS)
    run.add_argument("--actor-name", default="human", help=argparse.SUPPRESS)
    run.add_argument("--session-id", help=argparse.SUPPRESS)
    run.add_argument("--request-id", help=argparse.SUPPRESS)
    run.set_defaults(fn=run_cmd)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--label")
    snapshot.set_defaults(fn=snapshot_cmd)

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
