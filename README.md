# autoeval

`autoeval` is a harness layer for coding agents.
It manages artifacts, loop control, verifier checks, guardrails, and evidence.
It does not perform coding edits/patch execution itself.

## Core artifacts

Under `.autoeval/instructions/`:
- `research.md`
- `implementation.md`
- `plan.md`
- `review.md` (Lessons + final Review sections)
- `feature_list.json`

At bootstrap, the markdown files are target files for coding-agent authorship. Their creation instructions come from `autoeval/templates/*.md` through the provider session surface; bootstrap does not copy template prose into those files.

Verifier link config:
- `.autoeval/verifier.yaml` is developer/end-user authored from fixed template.
- It links individual test files or entire test directories.

Machine/runtime files under `.autoeval/runtime/`:
- `tool_calls.json` (callable harness tool contract)

Run-scoped provider integration files under `.autoeval/runs/<run_id>/provider/`:
- `provider_session.json` (standardized provider-facing session envelope)
- `<provider>_prompt.txt` (provider adapter launch prompt)
- `<provider>_raw_trace.jsonl` (raw provider event/log stream)
- `<provider>_normalized_trace.jsonl` (normalized provider events)
- `<provider>_result.json` (provider launch/result summary)

## Harness loop

1. Initialize artifacts and validate verifier links from `verifier.yaml`.
2. Coding agent calls `tools decide-mode` first and selects an explicit mode from the inline policy embedded in `workflow.decide_mode`.
3. If mode is `instant`, skip harness loop and execute directly.
4. If mode is `planning`, continue harness loop:
5. Coding agent reads instruction artifacts plus `.autoeval/runtime/tool_calls.json`.
6. When it needs resolved pytest targets, it calls `autoeval verifier sync --repo .`.
7. Coding agent checks terminal commands with guardrail tools.
8. Coding agent executes implementation outside harness.
9. Coding agent runs `autocheck` for linked targets referenced by typed feature verifications and updates status.
10. Repeat until all sub-tasks pass.

## Guardrails

Two-layer harness safety:
- `security.py`: allowlist + sensitive command validation
- `policy.py`: action gating over security outcomes

## Callable tools for coding agent

Use `autoeval tools list --repo .` to inspect tool details.

Available tool commands:
- `autoeval tools decide-mode`
- `autoeval tools guardrail-check`
- `autoeval tools feature-list-generate`
- `autoeval tools feature-status-set`
- `autoeval tools feature-status-get`
- `autoeval tools autocheck`
- `autoeval tools run-status`
- `autoeval tools run-eval`
- `autoeval tools append-lesson`
- `autoeval tools append-review`

## Provider integration surface

`autoeval` now exposes a standardized provider-facing session surface for external coding agents:
- run `autoeval provider session --repo .` to emit/update the current run's `provider_session.json`
- run `autoeval provider launch --repo . --provider codex` to launch a concrete provider adapter against that session
- use `scripts/provider_connector.py` as a standalone connector entrypoint for scripting and smoke runs

The provider session surface sits between the harness contract and provider-specific adapters. It keeps the harness contract authoritative while allowing provider-specific launch and trace handling.
It also carries `artifact_generation` entries so the coding agent receives template-backed instructions for authoring the instruction artifacts.
For the current Codex proto smoke in this environment, use `--sandbox-mode danger-full-access`.

## Codex provider example

Initialize a repo for Codex:

```bash
uv run autoeval init \
  --repo . \
  --provider codex \
  --task "Implement feature set" \
  --force
```

Available `autoeval init` options for Codex bootstrap:
- `--repo <path>`: target repository root, required
- `--provider codex`: provider name, defaults to `codex`
- `--task "<text>"`: bootstrap task text written into run/bootstrap context, defaults to `Initialize target repository execution context`
- `--force`: regenerate existing RPI artifacts

After a run exists, launch the Codex adapter:

```bash
uv run autoeval provider launch \
  --repo . \
  --provider codex \
  --run-id "$(jq -r '.last_run_id' .autoeval/state.json)" \
  --sandbox-mode danger-full-access \
  --timeout-sec 180
```

Available `autoeval provider launch` options for Codex:
- `--repo <path>`: target repository root, required
- `--provider codex`: provider name, defaults to `codex`
- `--run-id <id>`: existing run id to attach the provider session to
- `--task "<text>"`: override task text in the emitted provider session
- `--mode planning|instant`: override mode in the emitted provider session
- `--sandbox-mode <mode>`: provider sandbox mode, defaults to `workspace-write`
- `--timeout-sec <seconds>`: launch timeout, defaults to `90`
- `--model <name>`: optional provider model override
- `--profile <name>`: optional Codex config profile
- `--extra-arg <value>`: repeatable passthrough argument for the provider command

If you only need the provider-facing session envelope without launching Codex:

```bash
uv run autoeval provider session \
  --repo . \
  --provider codex \
  --run-id "$(jq -r '.last_run_id' .autoeval/state.json)"
```

## Quickstart

```bash
uv run autoeval init --repo . --provider codex --task "Implement feature set"
uv run autoeval verifier template
uv run autoeval verifier sync --repo .
# edit .autoeval/verifier.yaml to link file/directory tests, then sync again
uv run autoeval tools decide-mode --repo . --request "Implement feature set" --mode planning
uv run autoeval run --repo . --task "Implement feature set" --mode planning
uv run autoeval tools list --repo .
uv run autoeval tools feature-list-generate --repo . --input-json '{"sub_tasks":[{"sub_task_description":"Implement feature set","verifications":[{"kind":"pytest","target":"tests/test_app.py::test_feature"}]}]}'
uv run autoeval tools guardrail-check --command "pytest -q tests/test_api.py::test_ok"
uv run autoeval tools autocheck --repo .
uv run autoeval tools run-status --repo .
uv run autoeval tools run-eval --repo .
uv run autoeval provider session --repo .
uv run autoeval provider launch --repo . --provider codex --sandbox-mode danger-full-access
uv run python scripts/provider_connector.py --repo . --provider codex --run-id "$(jq -r '.last_run_id' .autoeval/state.json)" --sandbox-mode danger-full-access
```

## MCP lifecycle

```bash
uv run autoeval mcp add --scope user --name playwright --transport stdio --command "playwright-mcp" --tool-namespace playwright --repo .
uv run autoeval mcp connect --repo . --name playwright
uv run autoeval mcp list --scope effective --repo .
```

## License

Apache License 2.0. See `LICENSE`.
