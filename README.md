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
- `autocheck_map.json`
- `tool_calls.json` (callable harness tool contract)

Verifier link config:
- `.autoeval/verifier.yaml` is developer/end-user authored from fixed template.
- It links individual test files or entire test directories.

## Harness loop

1. Initialize artifacts and verifier mapping from `verifier.yaml`.
2. Coding agent calls `tools decide-mode` first.
3. If mode is `instant`, skip harness loop and execute directly.
4. If mode is `planning`, continue harness loop:
5. Coding agent reads artifacts + `tool_calls.json`.
6. Coding agent checks terminal commands with guardrail tools.
7. Coding agent executes implementation outside harness.
8. Coding agent runs `autocheck` for linked targets referenced by feature criteria and updates status.
9. Repeat until all sub-tasks pass.

## Guardrails

Two-layer harness safety:
- `security.py`: allowlist + sensitive command validation
- `policy.py`: action gating over security outcomes

## Callable tools for coding agent

Use `autoeval tools list --repo .` to inspect tool details.

Available tool commands:
- `autoeval tools decide-mode`
- `autoeval tools guardrail-check`
- `autoeval tools feature-status-set`
- `autoeval tools feature-status-get`
- `autoeval tools autocheck`
- `autoeval tools run-status`
- `autoeval tools run-eval`
- `autoeval tools append-lesson`
- `autoeval tools append-review`

## Quickstart

```bash
uv run autoeval init --repo . --provider codex --task "Implement feature set"
uv run autoeval verifier template
uv run autoeval verifier sync --repo .
# edit .autoeval/verifier.yaml to link file/directory tests, then sync again
uv run autoeval tools decide-mode --repo . --request "Implement feature set" --mode auto
uv run autoeval run --repo . --task "Implement feature set" --mode auto
uv run autoeval tools list --repo .
uv run autoeval tools guardrail-check --command "pytest -q tests/test_api.py::test_ok"
uv run autoeval tools autocheck --repo .
uv run autoeval tools run-status --repo .
uv run autoeval tools run-eval --repo .
```

## MCP lifecycle

```bash
uv run autoeval mcp add --scope user --name playwright --transport stdio --command "playwright-mcp" --tool-namespace playwright --repo .
uv run autoeval mcp connect --repo . --name playwright
uv run autoeval mcp list --scope effective --repo .
```

## License

Apache License 2.0. See `LICENSE`.
