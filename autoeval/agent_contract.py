from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import RepoPaths
from .harness_tools import tool_catalog_payload

CONTRACT_VERSION = "1.0"


class ToolParameter(BaseModel):
    name: str
    type: str
    required: bool = False


class ToolCallSpec(BaseModel):
    id: str
    description: str
    cli: str
    parameters: list[ToolParameter] = Field(default_factory=list)


class ArtifactPaths(BaseModel):
    research: str
    implementation: str
    plan: str
    review: str
    decision_prompt: str
    feature_list: str
    verifier_yaml: str
    autocheck_map: str
    tool_calls: str


class HarnessLoopContract(BaseModel):
    contract_version: str = CONTRACT_VERSION
    execution_model: str = "harness_only"
    artifact_paths: ArtifactPaths
    tool_calls: list[ToolCallSpec]
    loop_steps: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


DEFAULT_LOOP_STEPS = [
    "read research/plan/implementation/review/feature_list artifacts",
    "run guardrail.check_command before terminal command execution",
    "perform coding externally to harness",
    "run verifier.autocheck and inspect outcomes",
    "update only feature_list status via feature.status_set",
    "check run.status and run.eval",
    "repeat until all feature sub-tasks pass",
]


def build_agent_contract(paths: RepoPaths) -> HarnessLoopContract:
    catalog = tool_catalog_payload(paths)
    artifacts = catalog.get("artifact_paths", {})
    tools_raw = catalog.get("tools", [])

    tool_calls: list[ToolCallSpec] = []
    for item in tools_raw:
        if not isinstance(item, dict):
            continue
        parameters: list[ToolParameter] = []
        for parameter in item.get("parameters", []):
            if isinstance(parameter, dict):
                parameters.append(
                    ToolParameter(
                        name=str(parameter.get("name", "")),
                        type=str(parameter.get("type", "string")),
                        required=bool(parameter.get("required", False)),
                    )
                )
        tool_calls.append(
            ToolCallSpec(
                id=str(item.get("id", "")),
                description=str(item.get("description", "")),
                cli=str(item.get("cli", "")),
                parameters=parameters,
            )
        )

    return HarnessLoopContract(
        artifact_paths=ArtifactPaths(
            research=str(artifacts.get("research", paths.rpi_dir / "research.md")),
            implementation=str(artifacts.get("implementation", paths.rpi_dir / "implementation.md")),
            plan=str(artifacts.get("plan", paths.rpi_dir / "plan.md")),
            review=str(artifacts.get("review", paths.review_file)),
            decision_prompt=str(
                artifacts.get("decision_prompt", Path(__file__).resolve().parent / "prompts" / "decision.md")
            ),
            feature_list=str(artifacts.get("feature_list", paths.rpi_dir / "feature_list.json")),
            verifier_yaml=str(artifacts.get("verifier_yaml", paths.verifier_file)),
            autocheck_map=str(artifacts.get("autocheck_map", paths.autocheck_map_file)),
            tool_calls=str(paths.tool_calls_file),
        ),
        tool_calls=tool_calls,
        loop_steps=[str(step) for step in catalog.get("loop", {}).get("steps", DEFAULT_LOOP_STEPS)],
        metadata={
            "workflow_model": "research-plan-implementation",
            "catalog_version": str(catalog.get("catalog_version", "1.0.0")),
            "schema_version": int(catalog.get("schema_version", 1)),
        },
    )


def contract_schema() -> dict[str, Any]:
    return HarnessLoopContract.model_json_schema()


def contract_schema_file() -> Path:
    base = Path(__file__).resolve().parent
    return base / "schemas" / "agent_contract.v1.json"
