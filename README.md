# Autoeval

Local-first experiment workspaces for scripts, runs, reports, and coding agents.

Autoeval helps you run black-box experiments without losing track of what changed, what ran, what it produced, or what an agent should inspect next. It gives you a CLI, a local browser UI, versioned run snapshots, report bundles, and MCP tools in one Python package.

```bash
pip install "git+https://github.com/shreyashkar-ml/autoeval.git"
```

Repository: https://github.com/shreyashkar-ml/autoeval

## Why Autoeval

When experiments are just scripts and folders, the boring parts become fragile:

- Which script version produced this output?
- Where are the logs?
- Which params were used?
- What should the report include?
- How should an agent inspect or rerun this without guessing?

Autoeval gives every experiment a small workspace with repeatable runs, stable run IDs, local artifacts, and agent-readable context.

## Features

- **CLI workflow**: initialize projects, run experiments, list runs, store important results, restore old state, and diff runs.
- **Local browser UI**: inspect runs, scripts, outputs, logs, reports, and edit script snapshots.
- **Run snapshots**: every run keeps the script/config state that produced it.
- **Private Autoeval history**: Autoeval keeps its own internal git history and does not commit to your project git repo.
- **Report bundles**: every run gets structured report context for humans or AI agents.
- **MCP server**: coding agents can inspect runs, edit scripts, run experiments, and write reports through structured tools.
- **Docker optional**: use Docker when available, or run locally with no Docker setup.

## Install

With pip:

```bash
pip install "git+https://github.com/shreyashkar-ml/autoeval.git"
```

With uv:

```bash
uv tool install "git+https://github.com/shreyashkar-ml/autoeval.git"
```

Check the CLI:

```bash
autoeval --help
```

## Quickstart

Create a workspace:

```bash
autoeval init demo_eval
cd demo_eval
```

Run the starter script:

```bash
autoeval run
```

Open the UI:

```bash
autoeval view
```

List runs:

```bash
autoeval status
```

## Basic Workflow

Edit your experiment script:

```text
script/script.py
```

Edit non-secret inputs:

```text
script/params.json
```

Put local secrets or machine-specific values in:

```text
app.env
```

Run:

```bash
autoeval run
```

Autoeval prints a `run_id`. Use that ID to rerun, inspect, store, restore, diff, or report on the experiment.

Rerun an existing run snapshot:

```bash
autoeval run <run_id>
```

Store an important run:

```bash
autoeval storage <run_id>
```

Restore script/config from a run:

```bash
autoeval restore <run_id>
```

Compare two runs:

```bash
autoeval diff <run_a> <run_b>
```

## Browser UI

```bash
autoeval view
```

The UI is included in the package. It runs locally and lets you:

- switch between Autoeval projects
- start and stop runs
- rerun existing runs
- inspect run status
- open script snapshots
- edit scripts into new snapshots
- view outputs and generated reports
- edit report instructions

No frontend server or Node setup is needed for normal use.

## Reports

Autoeval prepares report context, but does not write final reports by itself.

For each run, report context is available at:

```text
runs/<run_id>/report/report_bundle.json
```

Write final report files under:

```text
runs/<run_id>/report/
```

Autoeval displays reports named:

```text
report.md
report.txt
index.md
```

Each workspace starts with an editable report instruction file:

```text
report.txt
```

Use a different instruction file:

```bash
autoeval report-instruction path/to/report.md
```

## MCP For Agents

Autoeval ships with an MCP server. This lets coding agents work with your experiments through structured Autoeval tools instead of arbitrary shell commands.

Install agent files inside an Autoeval workspace:

```bash
autoeval agent install --target all
```

This writes:

```text
AGENTS.md
.mcp.json
```

For Claude-specific instructions:

```bash
autoeval agent install --target claude
```

This writes:

```text
CLAUDE.md
.mcp.json
```

The generated MCP config starts:

```bash
autoeval mcp
```

Through MCP, agents can:

- read workspace context
- list and inspect runs
- read source snapshots, outputs, logs, and report bundles
- edit scripts into versioned snapshots
- update params
- run experiments
- store, restore, and diff runs
- read and update report instructions

Agents should use Autoeval MCP tools or Autoeval commands to run experiments. They should not run `script/` files directly.

## Command Reference

```bash
autoeval init <project_name>
autoeval run [run_id]
autoeval status [--limit N]
autoeval hash
autoeval view [--host HOST] [--port PORT] [--project PROJECT]
autoeval storage [run_id] [--label LABEL] [--message MESSAGE]
autoeval restore <run_id>
autoeval diff <run_a> <run_b>
autoeval report-instruction [path]
autoeval agent install [--target claude|all] [--force]
autoeval mcp
autoeval doctor
```

## Docker

Docker is optional.

- `runner: "auto"` uses Docker when available and otherwise runs locally.
- `runner: "docker"` requires Docker.
- `runner: "local"` always runs locally.

Set this in `autoeval.json`.

## Notes

- Autoeval is local-first. Runs, logs, outputs, and reports stay on your machine unless you store or share them yourself.
- Autoeval keeps private internal history under `.autoeval/`; it does not commit to your normal git repository.
- `app.env` is for local environment values and is not stored by Autoeval.
