from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from .security import validate_command, validate_pytest_target

RuntimeApprover = Callable[[str, dict[str, Any]], bool | tuple[bool, str] | dict[str, Any]]

HIGH_RISK_TOKENS = ("rm ", "pkill ", "chmod ", "curl ", "git push", "git reset")
NETWORK_TOKENS = ("http://", "https://", "curl ", "wget ", "pip install", "npm install")


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    policy_stage: str = "static"
    runtime_approval_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyEngine:
    def __init__(self, runtime_approver: RuntimeApprover | None = None, no_network: bool = True) -> None:
        self.runtime_approver = runtime_approver
        self.no_network = no_network

    def evaluate_terminal_command(
        self,
        command: str,
        *,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        info = dict(metadata or {})
        command_text = command.strip()
        if not command_text:
            return PolicyDecision(allowed=False, reason="empty command", policy_stage="static", metadata=info)

        cmd_check = validate_command(command_text)
        if not cmd_check.allowed:
            info["security_check"] = "command_allowlist"
            return PolicyDecision(
                allowed=False,
                reason=cmd_check.reason,
                policy_stage="security",
                runtime_approval_required=False,
                metadata=info,
            )

        if target is not None:
            target_check = validate_pytest_target(target)
            if not target_check.allowed:
                info["security_check"] = "pytest_target"
                return PolicyDecision(
                    allowed=False,
                    reason=target_check.reason,
                    policy_stage="security",
                    runtime_approval_required=False,
                    metadata=info,
                )

        if bool(info.get("no_network", self.no_network)):
            lowered = command_text.lower()
            if any(token in lowered for token in NETWORK_TOKENS):
                info["security_check"] = "no_network"
                return PolicyDecision(
                    allowed=False,
                    reason="network command blocked by no_network constraint",
                    policy_stage="security",
                    runtime_approval_required=False,
                    metadata=info,
                )

        lowered = command_text.lower()
        runtime_required = any(token in lowered for token in HIGH_RISK_TOKENS)
        if runtime_required and self.runtime_approver is not None:
            response = self.runtime_approver(command_text, info)
            if isinstance(response, bool):
                approved = response
                reason = "runtime approver decision"
                mutation: dict[str, Any] = {}
            elif isinstance(response, tuple):
                approved = bool(response[0])
                reason = str(response[1])
                mutation = {}
            else:
                approved = bool(response.get("allowed", False))
                reason = str(response.get("reason", "runtime approver decision"))
                mutation = dict(response.get("metadata", {}))

            info.update(mutation)
            return PolicyDecision(
                allowed=approved,
                reason=reason,
                policy_stage="runtime",
                runtime_approval_required=True,
                metadata=info,
            )

        return PolicyDecision(
            allowed=True,
            reason="approved by static policy",
            policy_stage="static",
            runtime_approval_required=runtime_required,
            metadata=info,
        )


def evaluate_action(payload: dict[str, Any]) -> dict[str, Any]:
    command = str(payload.get("command", ""))
    target = payload.get("target")
    no_network = bool(payload.get("no_network", True))
    engine = PolicyEngine(no_network=no_network)
    decision = engine.evaluate_terminal_command(command=command, target=str(target) if target is not None else None)
    return decision.model_dump()
