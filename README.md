# Autoeval

Local-first experiment workspaces for scripts, runs, reports, and coding agents.

Autoeval turns black-box experiments into traceable workspaces. Each run connects its script and configuration snapshot with outputs, logs, report context, and a stable run ID. The package includes a CLI, local browser UI, private experiment history, and project-scoped MCP tools.

## Why Autoeval

When experiments are just scripts and folders, the boring parts become fragile:

- Which script version produced this output?
- Where are the logs?
- Which params were used?
- What should the report include?
- How should an agent inspect or rerun this reliably?

Autoeval gives every experiment a small workspace with repeatable runs, stable run IDs, local artifacts, and agent-readable context.

## Features

- **CLI workflow**: initialize projects, run experiments, list runs, restore old state, and diff runs.
- **Local browser UI**: inspect runs, scripts, outputs, logs, reports, and edit script snapshots.
- **Run snapshots**: every run keeps the script/config state that produced it.
- **Private Autoeval history**: each execution records its source and configuration in isolated, project-local history.
- **Report bundles**: every run gets project-relative pointers to its params, outputs, logs, and reporting instructions.
- **MCP server**: coding agents can inspect runs, edit scripts, run experiments, and write reports through structured tools.
- **Flexible execution**: run Python, JavaScript, shell, or other commands through Docker isolation or the local host environment.

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

Put experiment code under `script/` and set its command in:

```text
script/stage.json
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

Autoeval prints a `run_id`. Use that ID to rerun, inspect, restore, diff, or report on the experiment.

Every run is immediately indexed in `index.sqlite`, retained under `runs/<run_id>/`, and available in the browser UI. Its source and configuration are snapshotted automatically when execution starts.

Rerun an existing run snapshot:

```bash
autoeval run <run_id>
```

Restore script/config from a run:

```bash
autoeval restore <run_id>
```

Compare source and configuration from two executed runs:

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

## Reports

Autoeval prepares each run for a human- or agent-generated report.

For each run, `report_bundle.json` points to the associated params, outputs, logs, report instruction, and report directory:

```text
runs/<run_id>/report/report_bundle.json
```

Write final report files under:

```text
runs/<run_id>/report/
```

Autoeval looks for these report entry points first:

```text
report.md
report.txt
index.md
```

Other report filenames are discovered as a fallback.

Each workspace starts with an editable report instruction file:

```text
report.txt
```

Use a different instruction file:

```bash
autoeval report-instruction path/to/report.md
```

## MCP For Agents

Autoeval includes a project-scoped MCP server for coding agents.

`autoeval init` automatically creates:

```text
AGENTS.md
.mcp.json
```

The generated `.mcp.json` contains:

```json
{
  "mcpServers": {
    "autoeval": {
      "command": "autoeval",
      "args": ["mcp"]
    }
  }
}
```

Open the project in a coding agent that supports project-local `.mcp.json` configuration. The client starts `autoeval mcp` from that workspace. Clients that read `AGENTS.md` also receive the Autoeval workflow and project contract in `autoeval.md`.

The server resolves the Autoeval project from its working directory, keeping tools and resources scoped to the current workspace. Ensure the installed `autoeval` command is available on the agent's `PATH`.

Through MCP, agents can:

- read workspace context
- list and inspect runs
- read source snapshots, outputs, logs, and report bundles
- edit scripts into versioned snapshots
- update params
- run experiments
- restore and diff runs
- read and update report instructions

Agents run experiments through Autoeval tools or commands, preserving run IDs, snapshots, logs, and outputs.

### Agent Workflow

1. Create a project with `autoeval init <project_name>`.
2. Open the project in the coding agent.
3. Ask the agent to edit the experiment and run it through Autoeval.
4. Inspect the returned `run_id`, outputs, logs, and report in the UI or through MCP.
5. Restore a run snapshot or compare two executed runs by their `run_id` values.

## Command Reference

```bash
autoeval init <project_name> [--title TITLE]
autoeval run [run_id]
autoeval status [--limit N]
autoeval hash
autoeval view [--host HOST] [--port PORT] [--project PROJECT] [--allow-origin ORIGIN]
autoeval restore <run_id>
autoeval diff <run_a> <run_b>
autoeval report-instruction [path]
autoeval mcp
autoeval doctor
```

## Execution

Autoeval uses Docker as the sandbox boundary for experiment execution. When you create a project, `autoeval init` checks whether the host Docker command and daemon are available.

- When Docker is available, the project starts with `runner: "docker"` and runs scripts inside the configured container limits.
- When Docker is unavailable, the project starts with `runner: "local"` and initialization prints a short instruction for enabling sandboxing later.

The selected runner is stored in `autoeval.json`, keeping project execution predictable across runs. After installing Docker, enable sandboxing by changing:

```json
"runner": "local"
```

to:

```json
"runner": "docker"
```

## Notes

- Runs, logs, outputs, reports, SQLite metadata, and source history remain local to the project.
- Autoeval's private history lives under `.autoeval/`, isolated from the developer-managed git repository.
- `app.env` keeps environment values machine-local and outside Autoeval history.
