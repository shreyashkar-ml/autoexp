from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agent_contract import CONTRACT_VERSION, build_agent_contract
from .config import RepoPaths, SCHEMA_VERSION, ensure_repo_layout, ensure_user_layout, read_json, utc_now_iso, write_json
from .harness_tools import tool_catalog_payload, write_tool_catalog
from .rpi import artifact_instruction_payload

PROVIDER_SURFACE_VERSION = "1.0.0"


class SessionSurface(BaseModel):
    id: str = "autoeval_provider_session"
    version: str = PROVIDER_SURFACE_VERSION


class ProviderBinding(BaseModel):
    name: str
    tool_transport: str = "shell_cli"
    execution_model: str = "external_agent"


class RunBinding(BaseModel):
    run_id: str
    mode: str
    task: str
    repo_root: str
    created_at: str


class ActiveContextBinding(BaseModel):
    file: str
    payload: dict[str, Any]


class ToolCatalogBinding(BaseModel):
    file: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    loop_steps: list[str] = Field(default_factory=list)


class ArtifactInstructionBinding(BaseModel):
    artifact_name: str
    target_file: str
    template_file: str
    instructions: str
    artifact_state: str


class ProviderSessionEnvelope(BaseModel):
    schema_version: int = SCHEMA_VERSION
    contract_version: str = CONTRACT_VERSION
    surface: SessionSurface = Field(default_factory=SessionSurface)
    provider: ProviderBinding
    run: RunBinding
    artifacts: dict[str, str]
    active_context: ActiveContextBinding
    tool_catalog: ToolCatalogBinding
    artifact_generation: list[ArtifactInstructionBinding] = Field(default_factory=list)
    harness_contract: dict[str, Any]
    instructions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderLaunchRequest(BaseModel):
    provider: str
    run_id: str
    session_file: str
    sandbox_mode: str = "workspace-write"
    timeout_sec: int | None = None
    model: str | None = None
    config_profile: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedProviderEvent(BaseModel):
    sequence: int
    provider: str
    transport: str
    raw_type: str
    normalized_type: str
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ProviderExecutionResult(BaseModel):
    ok: bool
    provider: str
    transport: str
    command: list[str]
    session_file: str
    prompt_file: str
    raw_trace_file: str
    normalized_trace_file: str
    last_message_file: str
    exit_code: int | None = None
    final_output: str = ""
    error: str | None = None
    event_count: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def detect_capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def launch(
        self,
        *,
        paths: RepoPaths,
        session: ProviderSessionEnvelope,
        request: ProviderLaunchRequest,
    ) -> ProviderExecutionResult:
        raise NotImplementedError


def provider_dir(paths: RepoPaths, run_id: str) -> Path:
    return paths.runs_dir / run_id / "provider"


def provider_session_file(paths: RepoPaths, run_id: str) -> Path:
    return provider_dir(paths, run_id) / "provider_session.json"


def provider_raw_trace_file(paths: RepoPaths, run_id: str, provider: str) -> Path:
    return provider_dir(paths, run_id) / f"{provider}_raw_trace.jsonl"


def provider_normalized_trace_file(paths: RepoPaths, run_id: str, provider: str) -> Path:
    return provider_dir(paths, run_id) / f"{provider}_normalized_trace.jsonl"


def provider_last_message_file(paths: RepoPaths, run_id: str, provider: str) -> Path:
    return provider_dir(paths, run_id) / f"{provider}_last_message.txt"


def provider_prompt_file(paths: RepoPaths, run_id: str, provider: str) -> Path:
    return provider_dir(paths, run_id) / f"{provider}_prompt.txt"


def provider_result_file(paths: RepoPaths, run_id: str, provider: str) -> Path:
    return provider_dir(paths, run_id) / f"{provider}_result.json"


def _provider_name_from_session(session_payload: dict[str, Any], fallback: str) -> str:
    raw_provider = session_payload.get("provider")
    if isinstance(raw_provider, dict):
        name = str(raw_provider.get("name", "")).strip()
        if name:
            return name
    elif isinstance(raw_provider, str):
        name = raw_provider.strip()
        if name:
            return name
    return fallback


def read_provider_files(paths: RepoPaths, *, run_id: str, provider: str) -> dict[str, Any]:
    session_file = provider_session_file(paths, run_id)
    if not session_file.exists():
        raise ValueError(
            f"no provider session found for run_id '{run_id}' and provider '{provider}'; prepare the run first"
        )

    session_payload = read_json(session_file, {})
    effective_provider = provider
    if isinstance(session_payload, dict):
        effective_provider = _provider_name_from_session(session_payload, provider)

    return {
        "run_id": run_id,
        "provider": effective_provider,
        "session_file": str(session_file),
        "prompt_file": str(provider_prompt_file(paths, run_id, effective_provider)),
        "raw_trace_file": str(provider_raw_trace_file(paths, run_id, effective_provider)),
        "normalized_trace_file": str(provider_normalized_trace_file(paths, run_id, effective_provider)),
        "last_message_file": str(provider_last_message_file(paths, run_id, effective_provider)),
        "result_file": str(provider_result_file(paths, run_id, effective_provider)),
    }


def read_provider_result(paths: RepoPaths, *, run_id: str, provider: str) -> dict[str, Any]:
    result_file = provider_result_file(paths, run_id, provider)
    if not result_file.exists():
        raise ValueError(
            f"no saved provider result found for run_id '{run_id}' and provider '{provider}'; launch the provider first"
        )

    payload = read_json(result_file, {})
    if not isinstance(payload, dict):
        raise ValueError(f"invalid provider result payload in '{result_file}'")
    return payload


def _load_active_context(paths: RepoPaths, run_id: str) -> tuple[str, Path, dict[str, Any]]:
    run_dir = paths.runs_dir / run_id
    loop_context_file = run_dir / "loop_context.json"
    if loop_context_file.exists():
        return "planning", loop_context_file, read_json(loop_context_file, {})

    instant_context_file = run_dir / "instant_context.json"
    if instant_context_file.exists():
        return "instant", instant_context_file, read_json(instant_context_file, {})

    raise ValueError(f"no loop or instant context found for run_id '{run_id}'")


def build_provider_session_payload(
    paths: RepoPaths,
    *,
    run_id: str,
    provider: str,
    task: str | None = None,
    mode: str | None = None,
) -> ProviderSessionEnvelope:
    ensure_repo_layout(paths)
    ensure_user_layout(paths)
    write_tool_catalog(paths)

    detected_mode, context_file, context_payload = _load_active_context(paths, run_id)
    effective_mode = mode or detected_mode
    if effective_mode != detected_mode:
        raise ValueError(f"mode mismatch for run_id '{run_id}': expected {detected_mode}, got {effective_mode}")

    task_value = task or str(context_payload.get("task", "")).strip()
    if not task_value:
        raise ValueError(f"task is required to build provider session for run_id '{run_id}'")

    tool_catalog = tool_catalog_payload(paths)
    contract = build_agent_contract(paths).model_dump()
    state = read_json(paths.state_file, {"provider": provider})

    return ProviderSessionEnvelope(
        provider=ProviderBinding(name=provider),
        run=RunBinding(
            run_id=run_id,
            mode=effective_mode,
            task=task_value,
            repo_root=str(paths.repo),
            created_at=utc_now_iso(),
        ),
        artifacts={key: str(value) for key, value in contract["artifact_paths"].items()},
        active_context=ActiveContextBinding(file=str(context_file), payload=context_payload),
        tool_catalog=ToolCatalogBinding(
            file=str(paths.tool_calls_file),
            tools=list(tool_catalog.get("tools", [])),
            loop_steps=[str(step) for step in tool_catalog.get("loop", {}).get("steps", [])],
        ),
        artifact_generation=[
            ArtifactInstructionBinding.model_validate(item) for item in artifact_instruction_payload(paths)
        ],
        harness_contract=contract,
        instructions=[
            "Treat provider_session.json as the authoritative provider-facing harness contract.",
            "Read the active context file and tool catalog before taking action.",
            "Load artifact creation instructions from the template-backed artifact_generation entries in this session.",
            "Use the autoeval CLI tool surface for harness actions rather than editing harness artifacts manually.",
            "Do not mutate immutable feature_list task metadata; only status updates may flow through harness tools.",
        ],
        metadata={
            "state_provider": str(state.get("provider", provider)),
            "context_file": str(context_file),
            "tool_transport": "shell_cli",
        },
    )


def write_provider_session(
    paths: RepoPaths,
    *,
    run_id: str,
    provider: str,
    task: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    payload = build_provider_session_payload(paths, run_id=run_id, provider=provider, task=task, mode=mode)
    session_file = provider_session_file(paths, run_id)
    write_json(session_file, payload.model_dump())
    return {
        "ok": True,
        "provider": provider,
        "run_id": run_id,
        "session_file": str(session_file),
        "surface_version": PROVIDER_SURFACE_VERSION,
        "mode": payload.run.mode,
        "task": payload.run.task,
    }
