# Autoeval

Autoeval is a local-first workspace for black-box experiments. It gives each experiment a stable project layout, versioned script state, repeatable run artifacts, a browser UI, and agent-friendly context files.

Use it when you want to iterate on scripts and outputs without manually organizing run folders, reports, logs, or agent instructions.

## Install

```bash
pip install autoeval
```

or:

```bash
uv tool install autoeval
```

Docker is optional. Autoeval runs locally when Docker is unavailable. If Docker is installed and usable, the default runner uses it for sandboxed execution.

## Create A Project

```bash
autoeval init demo_eval
cd demo_eval
```

This creates:

```text
demo_eval/
  .autoeval/
    git/
  .gitignore
  app.env
  autoeval.json
  autoeval.md
  report.txt
  index.sqlite
  script/
    stage.json
    params.json
    params.schema.json
    script.py
  runs/
```

The project root can also contain your own normal `.git` repository. Autoeval stores its internal history separately under `.autoeval/git`.

## Run An Experiment

```bash
autoeval run
```

Autoeval copies the current script/config state into a new `runs/<run_id>/` directory, executes it, records logs and outputs, and prints the `run_id`.

Each run directory contains:

```text
runs/<run_id>/
  ctx.json
  run.json
  script/
  output/
  logs/
  report/
    report_bundle.json
```

Refresh an existing run in place:

```bash
autoeval run <run_id>
```

List recent runs:

```bash
autoeval status
```

## Script Inputs And Environment

Put executable experiment logic in `script/`.

Put non-secret configurable parameters in:

```text
script/params.json
```

Keep credentials, tokens, endpoints, and machine-local values in:

```text
app.env
```

`app.env` is ignored by git and by Autoeval's private storage. Autoeval passes it to the selected runner during execution.

## Browser UI

Launch the local UI:

```bash
autoeval view
```

The UI shows registered Autoeval projects on the machine, recent runs, status, script files, output filenames, and generated reports. It also lets you edit a script snapshot without overwriting the original run.

## Reports

Autoeval does not generate reports automatically during `autoeval run`.

For every run, Autoeval writes report context to:

```text
runs/<run_id>/report/report_bundle.json
```

The bundle includes the run id, script name, script params, output artifacts, logs, report instructions, and `app.env` variable names only. It never includes secret values from `app.env`.

Generated reports should be written under:

```text
runs/<run_id>/report/
```

Each project starts with an editable report instruction file:

```text
report.txt
```

Edit this file from your editor or from the UI when the domain, audience, report structure, or evaluation criteria changes. Generated report output can contain more than one file, including markdown, images, tables, or other assets.

Point Autoeval to a different project-local report instruction file with:

```bash
autoeval report-instruction path/to/report.md
```

## Store A Run

Runs start as local cache entries. Promote a run into Autoeval's private storage when you want to preserve it:

```bash
autoeval storage <run_id>
```

Store the current script/config state without promoting a run:

```bash
autoeval storage --label initial
```

## Agent Workflow

Install thin adapter files for coding agents:

```bash
autoeval agent install --target all
```

This writes `AGENTS.md` and `.mcp.json` unless they already exist. `AGENTS.md` is the generic adapter used by coding agents that read that convention. Use `--target claude` when you specifically want `CLAUDE.md`.

Start the MCP server:

```bash
autoeval mcp
```

Agents should use Autoeval commands or MCP tools to inspect runs, edit scripts, execute experiments, read logs, and generate reports. They should not run files inside `script/` directly.

## Useful Commands

```bash
autoeval init <project_name>
autoeval run [run_id]
autoeval status
autoeval storage [run_id]
autoeval restore <run_id>
autoeval diff <run_a> <run_b>
autoeval report-instruction [path]
autoeval view
autoeval doctor
autoeval agent install [--target claude|all]
autoeval mcp
```
