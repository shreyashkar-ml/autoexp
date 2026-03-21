from typing import Any

from .config import RepoPaths
from .provider_surface import ProviderExecutionResult, ProviderLaunchRequest, build_provider_session_payload, write_provider_session
from .providers import resolve_provider_adapter


def launch_provider_run(
    paths: RepoPaths,
    *,
    provider: str,
    run_id: str,
    task: str | None = None,
    mode: str | None = None,
    sandbox_mode: str = "workspace-write",
    timeout_sec: int | None = None,
    model: str | None = None,
    config_profile: str | None = None,
    extra_args: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    session_file: str | None = None,
) -> ProviderExecutionResult:
    effective_session_file = session_file
    if not effective_session_file:
        session_info = write_provider_session(paths=paths, run_id=run_id, provider=provider, task=task, mode=mode)
        effective_session_file = str(session_info["session_file"])

    session = build_provider_session_payload(
        paths=paths,
        run_id=run_id,
        provider=provider,
        task=task,
        mode=mode,
    )
    adapter = resolve_provider_adapter(provider)
    return adapter.launch(
        paths=paths,
        session=session,
        request=ProviderLaunchRequest(
            provider=provider,
            run_id=run_id,
            session_file=str(effective_session_file),
            sandbox_mode=sandbox_mode,
            timeout_sec=timeout_sec,
            model=model,
            config_profile=config_profile,
            extra_args=list(extra_args or []),
            metadata=dict(metadata or {}),
        ),
    )
