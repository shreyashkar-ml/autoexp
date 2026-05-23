# Autoeval

Autoeval is a small CLI for creating versioned, Docker-backed black-box
workflow projects.

An Autoeval project is a visible directory with three executable stages:

- `input/` collects or normalizes inputs.
- `script/` performs the main computation.
- `report/` renders the final report.

Runs are recorded under `runs/`, while explicit stage versions are stored with
`autoeval storage`.

## Requirements

- Python 3.11 or newer
- Git
- Docker Engine for `autoeval run`

Docker must be installed and usable by your user:

```bash
docker run --rm hello-world
```

## Install

From a local checkout:

```bash
uv tool install .
```

From GitHub:

```bash
uv tool install git+https://github.com/shreyashkar-sahu/autoeval.git
```

After install:

```bash
autoeval --help
```

## Quick Start

Create a project:

```bash
autoeval start demo_eval
cd demo_eval
```

Store the initial input/script/report versions:

```bash
autoeval storage --label initial
```

Run the workflow:

```bash
autoeval run
```

Inspect runs:

```bash
autoeval status
```

Start the local API server for a future browser UI:

```bash
autoeval serve
```

## Project Layout

```text
demo_eval/
  autoeval.md
  autoeval.json
  input/
    stage.json
    params.schema.json
    params.json
    input.py
  script/
    stage.json
    script.py
  report/
    stage.json
    report.py
  runs/
  index.sqlite
  .git/
```

`autoeval.json` contains project metadata and sandbox settings. By default,
stages run in `python:3.12-slim` with Docker networking disabled.

## Input Parameters

User-editable inputs live in:

```text
input/params.json
```

The future UI should render controls from:

```text
input/params.schema.json
```

Editing `input/params.json` changes the input hash without changing the script
or report hashes.

## Storage vs Runs

`autoeval storage` explicitly stores the current stage/config state as
versioned assets.

`autoeval run` executes the current state and records a run, but does not store
new stage versions automatically.

This keeps storage aligned with user intent.

## Development

Run syntax checks:

```bash
python3 -m compileall autoeval
```

Run tests:

```bash
uv run --extra test pytest
```

Build package artifacts:

```bash
uv build
```
