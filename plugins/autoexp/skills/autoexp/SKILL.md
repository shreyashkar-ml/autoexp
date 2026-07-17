---
name: autoexp
description: Start and run reproducible experiments in an existing Git repository with the Autoexp CLI, including metric-driven Autoresearch. Use when a user asks to test variants, preserve run evidence, compare results, generate experiment reports, or optimize a scalar objective.
---

# Autoexp

Autoexp records immutable evidence globally while repository files remain the editable source of truth. It does not initialize a project or add repository-local configuration.

Treat any text supplied with an explicit invocation as the experiment objective. If no objective was supplied and one cannot be inferred from the conversation, ask one concise question before registering the experiment.

## Start

1. Run `autoexp --help`. If unavailable, ask the user to install it with `uv tool install "git+https://github.com/shreyashkar-ml/autoexp.git"`.
2. Work from the existing Git worktree. Do not create `.autoexp`, `.mcp.json`, `.codex`, `AGENTS.md`, `runs/`, or generated reports in the repository for Autoexp.
3. Select Standard mode unless one stable scalar metric and a frozen evaluator can automatically decide keep versus revert.
4. Create or adapt ordinary repository files for the experiment, respecting the repository's own guidance and conventions.

## Standard experiments

Register the objective and entrypoint:

```bash
autoexp experiment create "<objective>" --title "<title>" --entrypoint <path> --command '<command>'
```

Add every relevant file to the global manifest:

```bash
autoexp files add <path> --role editable-source
autoexp files add <path> --role supporting-source
autoexp files add <path> --role input-data
```

Use `entrypoint` for the primary executable, `frozen-evaluator` for a user-owned evaluator, and `generated-output` only to describe files the run produces. Declare `.env` or another environment file as `secret-source`; never print, quote, copy, hash, or report its values.

For each focused variant:

1. Edit normal repository files.
2. Run `autoexp run --agent --title "<variant or hypothesis>"`.
3. Inspect the returned run, then use `autoexp status`, `autoexp diff <run-a> <run-b>`, or the global `autoexp view` dashboard for source, logs, artifacts, and reports.
4. Keep the `run_id` in the conclusion so the result can be reproduced or restored.

To preserve an insight or report without adding it to the repository, write it in a temporary location and attach it:

```bash
autoexp document add /tmp/<name>.md --kind insight --title "<title>"
autoexp document add /tmp/<name>.md --kind report --title "<title>"
```

## Autoresearch

The user or agent must supply ordinary repository files for the research program, candidate, and evaluator. Register them without generating a scaffold:

```bash
autoexp experiment create "<objective>" --kind autoresearch \
  --program <program> --candidate <candidate> --evaluator <evaluator> \
  --metric <name> --direction <min|max> \
  --metric-kind json --metric-path metrics.json --metric-key <key>
```

Then:

1. Run `autoexp research preflight`; stop if a required check fails.
2. Read the program and `autoexp research state`.
3. Never edit the frozen evaluator. Treat a deliberate evaluator change as a new user-owned contract boundary.
4. Make one focused candidate edit and run `autoexp research attempt "<hypothesis>"`.
5. Inspect its score, kept/reverted verdict, immutable run, diff, and artifacts; repeat within the user's stopping rule.

Reverted attempts remain evidence. Do not erase or manually rewrite global runs, outputs, logs, reports, diffs, or ledger rows.

## Browser review

Do not open a blocking browser review implicitly. When the user explicitly invokes the installed `autoexp-review` workflow, let that workflow open the review and return their notes. Do not substitute `autoexp view`; ordinary view sessions cannot submit feedback.
