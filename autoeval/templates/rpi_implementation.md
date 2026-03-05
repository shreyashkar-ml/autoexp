<!-- template_id: rpi_implementation -->
<!-- template_version: 2.2.0 -->

# Implementation Artifact Instruction

Purpose:
- Define harness-aware execution behavior for the coding agent.
- Keep autoeval as orchestration and verification layer only.

## Required Workflow
1. First tool call must be `workflow.decide_mode` using `.autoeval/instructions/tool_calls.json` and `prompts/decision.md`.
2. If mode is `instant`, skip harness loop orchestration and execute changes directly.
3. If mode is `planning`, continue the harness loop with artifacts and tool calls.
4. Before terminal command execution, call `guardrail.check_command`.
5. Use verifier/autocheck to validate progress from developer-linked tests in `verifier.yaml`.
6. Create feature sub-task criteria using relevant linked pytest targets from `autocheck_map.json`.
7. Update feature status through `feature.status_set`.

## Constraints
- autoeval must not execute coding edits/patches/commands for the coding agent.
- linking tests in `verifier.yaml` is developer/end-user responsibility, not coding-agent responsibility.
- Update only `status` field in `feature_list.json`; do not mutate immutable task metadata.
- Record outcomes and review notes in run artifacts and `.autoeval/instructions/review.md`.
