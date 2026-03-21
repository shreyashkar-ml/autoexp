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
- `verifier.yaml` is developer/end-user authored from a fixed template.
- by default it lives at the repository root (`./verifier.yaml`), outside `.autoeval`.
- It links individual test files or entire test directories.
- `autoeval verifier path --repo .` prints the resolved absolute verifier path and whether that file currently exists.

Machine/runtime files under `.autoeval/runtime/`:
- `tool_calls.json` (callable harness tool contract)

Run-scoped provider integration files under `.autoeval/runs/<run_id>/provider/`:
- `provider_session.json` (standardized provider-facing session envelope)
- `<provider>_prompt.txt` (provider adapter launch prompt)
- `<provider>_raw_trace.jsonl` (raw provider event/log stream)
- `<provider>_normalized_trace.jsonl` (normalized provider events)
- `<provider>_last_message.txt` (provider-emitted last message text when available)
- `<provider>_result.json` (provider execution result summary)

## Harness loop

1. Initialize artifacts and validate verifier links from the user-owned `verifier.yaml`.
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

`autoeval run` is the single execution entry point for external coding agents. It will:
- ensure the `.autoeval` layout exists
- require a developer-authored repository-root `verifier.yaml`
- scan instruction artifacts and create only what is missing by default
- rewrite instruction artifacts only when `--force` is set
- create/update the run context and `provider_session.json`
- launch the configured provider unless `--no-launch` is set

The provider session surface sits between the harness contract and provider-specific adapters. It keeps the harness contract authoritative while exposing the current run's provider envelope, artifact instructions, and provider trace locations.
It also carries `artifact_generation` entries so the coding agent receives template-backed instructions for authoring the instruction artifacts.
`autoeval provider session --repo .` is an inspection command for the current run's provider envelope; provider execution still starts from `autoeval run`.
`autoeval provider files --repo .` prints the canonical provider artifact paths for the active run (or for `--run-id` when supplied), using the run's recorded provider from `provider_session.json` and including session, prompt, raw trace, normalized trace, last message, and result entries even before optional provider outputs exist.
`autoeval provider result --repo .` prints the saved provider execution result for the active run (or for `--run-id` when supplied) and fails clearly if the provider has not produced a saved result yet.
For the current Codex proto smoke in this environment, use `--sandbox-mode danger-full-access`.

## Complete usage

`autoeval run` is the only normal execution entry point. There is no separate `init` or `launch` step in the operator workflow.

### 1. Create the repository-root verifier file

`autoeval` reads verifier links from `./verifier.yaml` at the repository root. For a repo at `/path/to/project`, the default verifier path is `/path/to/project/verifier.yaml`.

Use these commands to inspect the path and print the fixed template:

```bash
uv run autoeval verifier path --repo .
uv run autoeval verifier template
```

Example `verifier.yaml`:

```yaml
schema_version: 1
tests:
  - path: tests/test_run_cli.py
    scope: file
    framework: pytest
    pattern: "test_*.py"
    recursive: false
    mcp_profiles: []
prompts: []
connections: []
```

`verifier.yaml` is user-owned. Keep it at the repository root outside `.autoeval`. The coding agent consumes it but should not author it.

### 2. Start a run

If `.autoeval` does not exist, `autoeval run` creates it during execution. If `.autoeval` already exists, the command preserves existing instruction artifacts by default and only creates or refreshes what is missing.

Example:

```bash
uv run autoeval run \
  --repo . \
  --provider codex \
  --task "Add a provider summary command, document it, and add regression tests" \
  --mode planning \
  --sandbox-mode danger-full-access
```

What `autoeval run` does:
- validates the repository-root `verifier.yaml`
- creates `.autoeval` when missing
- syncs verifier-linked pytest targets into the runtime/autocheck map
- creates run-scoped files such as `.autoeval/runs/<run_id>/provider/provider_session.json`
- launches the configured provider unless `--no-launch` is used

Use `--force` only when you intentionally want to rewrite the instruction artifacts under `.autoeval/instructions/`. Without `--force`, existing authored artifacts are preserved.

### 3. Inspect the run

After `autoeval run`, inspect the current run with:

```bash
uv run autoeval status --repo .
uv run autoeval tools run-status --repo .
uv run autoeval tools run-eval --repo .
uv run autoeval provider session --repo .
uv run autoeval provider files --repo .
uv run autoeval provider result --repo .
```

Useful verifier inspection commands:

```bash
uv run autoeval verifier path --repo .
uv run autoeval verifier show --repo .
uv run autoeval verifier sync --repo .
```

### 4. Continue an existing run

To continue the latest run:

```bash
uv run autoeval resume --repo .
```

Add `--force` only if you intentionally want `resume` to rewrite the instruction artifacts before continuing.

Use `--run-id <id>` only when you deliberately want to target a specific past run. It is not part of the default flow.

### 5. Command reference

Common `autoeval run` options:
- `--repo <path>`: target repository root, required
- `--task "<text>"`: concrete task text for the run, required
- `--provider codex`: provider name, defaults to `codex`
- `--mode planning|instant`: execution mode selected from the inline policy
- `--force`: rewrite instruction artifacts instead of preserving existing files
- `--launch/--no-launch`: launch provider after preparing the run, defaults to `--launch`
- `--sandbox-mode <mode>`: provider sandbox mode, defaults to `workspace-write`
- `--timeout-sec <seconds>`: optional provider execution timeout; omitted means no timeout
- `--model <name>`: optional provider model override
- `--profile <name>`: optional Codex config profile
- `--extra-arg <value>`: repeatable passthrough provider argument
- `--run-autocheck-now/--no-run-autocheck-now`: run harness autocheck during setup, defaults to `--run-autocheck-now`
- `--autocheck-timeout-sec <seconds>`: autocheck timeout, defaults to `300`

Common harness inspection commands:

```bash
uv run autoeval tools list --repo .
uv run autoeval tools autocheck --repo .
uv run autoeval tools guardrail-check --command "pytest -q tests/test_api.py::test_ok"
```

## MCP lifecycle

```bash
uv run autoeval mcp add --scope user --name playwright --transport stdio --command "playwright-mcp" --tool-namespace playwright --repo .
uv run autoeval mcp connect --repo . --name playwright
uv run autoeval mcp list --scope effective --repo .
```

## License

Apache License 2.0. See `LICENSE`.
