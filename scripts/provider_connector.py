#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

from autoeval.config import RepoPaths, ensure_repo_layout, ensure_user_layout, read_json
from autoeval.provider_surface import ProviderLaunchRequest, build_provider_session_payload, write_provider_session
from autoeval.providers import resolve_provider_adapter


def _resolve_run_id(paths: RepoPaths, run_id: str | None) -> str:
    if run_id:
        return run_id
    state = read_json(paths.state_file, {"last_run_id": None})
    active_run = state.get("last_run_id")
    if not active_run:
        raise ValueError("no active run found; provide --run-id explicitly")
    return str(active_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a standardized autoeval provider session through a concrete provider adapter."
    )
    parser.add_argument("--repo", required=True, help="Repository root")
    parser.add_argument("--provider", default="codex", help="Provider adapter to launch")
    parser.add_argument("--run-id", default=None, help="Existing autoeval run id")
    parser.add_argument("--task", default=None, help="Task override if the run context does not carry it")
    parser.add_argument("--mode", default=None, help="planning or instant")
    parser.add_argument("--sandbox-mode", default="workspace-write", help="Provider sandbox mode")
    parser.add_argument("--timeout-sec", type=int, default=90, help="Provider execution timeout in seconds")
    parser.add_argument("--model", default=None, help="Optional provider model override")
    parser.add_argument("--profile", default=None, help="Optional provider profile")
    parser.add_argument("--extra-arg", action="append", default=[], help="Extra argument forwarded to the provider")
    args = parser.parse_args()

    paths = RepoPaths.from_repo(Path(args.repo))
    ensure_repo_layout(paths)
    ensure_user_layout(paths)

    active_run = _resolve_run_id(paths, args.run_id)
    session_info = write_provider_session(
        paths=paths,
        run_id=active_run,
        provider=args.provider,
        task=args.task,
        mode=args.mode,
    )
    session = build_provider_session_payload(
        paths=paths,
        run_id=active_run,
        provider=args.provider,
        task=args.task,
        mode=args.mode,
    )
    adapter = resolve_provider_adapter(args.provider)
    result = adapter.launch(
        paths=paths,
        session=session,
        request=ProviderLaunchRequest(
            provider=args.provider,
            run_id=active_run,
            session_file=session_info["session_file"],
            sandbox_mode=args.sandbox_mode,
            timeout_sec=int(args.timeout_sec),
            model=args.model,
            config_profile=args.profile,
            extra_args=list(args.extra_arg),
            metadata={"transport_preference": "proto" if args.provider == "codex" else "default"},
        ),
    )
    print(json.dumps(result.model_dump(), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
