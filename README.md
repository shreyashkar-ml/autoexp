<p align="center">
  <img src="assets/dark.svg" alt="Autoexp - Local Autonomous Experimentation" width="480">
</p>

# autoexp

Local-first experiment workspaces for developers using coding agents.

Autoexp is for agent-led experimentation where the trail matters: keeping scripts, parameters, outputs, logs, reports, and run history tied together while an agent iterates.

Use standard Autoexp when the work is exploratory or qualitative: protein-folding experiments, microbiology protocol variants, qualitative eval suites, ablations, data checks, or any workflow where the useful output is a sequence of attempts and a good final report—not a single number to maximize.

Autoexp has two workflows:

- **Standard experiments** for running attempts, comparing artifacts, and generating reports from custom instructions.
- **Autoresearch** ([ref.](https://github.com/karpathy/autoresearch)) for metric-driven optimization where each attempt can be kept or reverted by score.

## How it fits together

An Autoexp project is just a local folder with a few conventions:

- Put the experiment implementation under `script/`.
- Keep secrets in `app.env`.
- Tune regular inputs in `script/params.json`.
- Tune report generation in `report.txt`.
- Run experiments through Autoexp so each result is recorded.
- Open the browser UI to review runs, outputs, diffs, reports, and project state.
- Open the project in a coding agent to let it use Autoexp through MCP.

If your work has a clear metric such as accuracy, loss, latency, cost, reward, or benchmark score, use Autoresearch. It adds `script/program.md`, where you describe the objective and constraints the agent should follow while optimizing that metric.

## Install

```bash
uv tool install "git+https://github.com/shreyashkar-ml/autoexp.git"
```

Check the installation:

```bash
autoexp --help
```

## Quickstart

Create a standard project:

```bash
autoexp init demo_eval
cd demo_eval
```

Initialization creates the workspace, starter files, agent instructions, and MCP config. It does not start experimenting by itself.

Open this folder in your preferred coding agent. Claude users get `CLAUDE.md`; other agents can read `AGENTS.md`. Both files tell the agent how to use Autoexp, and `.mcp.json` lets MCP-aware clients start the Autoexp tools automatically. If your client does not read `.mcp.json`, configure a stdio MCP server that runs `autoexp mcp` from the project folder.

Example prompt:

```text
Try three prompt variants for the classifier evaluation, compare the error patterns, and write a short report recommending the best variant.
```

If the experiment needs secrets or machine-specific values, put them in `app.env`:

```bash
OPENAI_API_KEY=...
DATASET_PATH=/path/to/local/data
```

For normal non-secret inputs, edit `script/params.json`. To tune how generated reports should read, edit `report.txt`.

You can also run the current experiment yourself:

```bash
autoexp run
```

Then open the browser UI to inspect runs, outputs, logs, reports, and diffs:

```bash
autoexp view
```

Autoexp prints a stable `run_id` for each run. Use that ID later when you want to rerun, restore, compare, or report on the same experiment state.

For metric-driven research loops, start with:

```bash
autoexp init metric_lab --autoresearch
cd metric_lab
```

Then edit `script/program.md` with the research objective and ask your agent to run the Autoresearch loop through Autoexp MCP.

Example Autoresearch prompt:

```text
Improve validation accuracy without increasing inference latency. Try one hypothesis at a time and stop after five attempts or once accuracy improves by 2%.
```

## Standard experiments

Standard mode is the default Autoexp workflow. It works well when you want an agent to edit a script, try parameter changes, inspect artifacts, and write reports while you keep a clean run history.

A new project starts with this shape:

```text
demo_eval/
├── script/
│   ├── stage.json          # command Autoexp runs
│   ├── params.json         # editable, non-secret inputs
│   ├── params.schema.json  # parameter descriptions for tools and UI
│   └── script.py           # experiment implementation
├── autoexp.json            # project and runner settings
├── report.txt              # project-specific report guidance
├── app.env                 # local environment values and secrets
└── runs/                   # run outputs, logs, and reports
```

Common edits:

- To change the experiment command, edit `script/stage.json`.
- To tune non-secret inputs, edit `script/params.json`.
- To change the experiment itself, edit `script/script.py`.
- To tune report generation, edit `report.txt` with the audience, structure, depth, and what the report should explain.
- To keep local secrets or machine-specific values, use `app.env`.

Your experiment receives a JSON context file through `${CTX}`. Read paths such as `output_dir`, `logs_dir`, and `script_params_path` from that context instead of hardcoding locations.

Every execution gets a new run ID. When the same pinned inputs produce the same output, Autoexp keeps both runs and links the newer run to the result it reproduces. Rerunning an earlier run creates a child run from its pinned source without changing the parent.

## Browser UI

The browser UI is the easiest way to review multiple projects. Start it with `autoexp view` from a project folder, or pass `--project /path/to/project` to open a specific one.

The UI lets you:

- switch between filtered Standard and Autoresearch project lists
- start, stop, and rerun experiments
- inspect scripts, outputs, logs, and reports
- edit a script into a new versioned snapshot
- edit parameters and report guidance
- view Autoresearch metrics, attempts, decisions, and source changes
- start or stop an Autoresearch loop

Projects may live in completely separate directories. Once initialized or opened, they remain available from the same project picker.

Standard project view:

![Autoexp standard empty project view](https://github.com/user-attachments/assets/6b030ab6-5c54-44d2-ac1e-b52df3c55bb0)

Autoresearch project view:

![Autoexp Autoresearch empty project view](https://github.com/user-attachments/assets/d8dad830-3ce6-4088-b7d0-a563df03f02f)

## Reports

Reports are meant to be generated from recorded run artifacts, not from memory. Every run includes a report bundle at:

```text
runs/<run_id>/report/report_bundle.json
```

The bundle lists the run ID, script name, available environment variable names, run metadata, and project-relative paths to parameters, outputs, logs, report guidance, and the report directory.

Generated reports belong under:

```text
runs/<run_id>/report/
```

The preferred main report is:

```text
runs/<run_id>/report/report.md
```

To tune report generation, edit `report.txt` from the project or browser UI. Keep that file focused on what the report should say: subject matter, depth, audience, analysis, and structure. Autoexp adds the standard artifact-handling and secret-safety instructions internally.

If you want to point the project at a different report guidance file, use `autoexp report-instruction <path>`.

## Coding agents and MCP

MCP is how a coding agent talks to Autoexp directly instead of guessing shell commands and file paths.

### Optional Claude Code or Codex plugin

The plugin adds the Autoexp workflow as an agent skill and wires the MCP server into Codex. The `autoexp` command must already be installed and available on `PATH`.

For Claude Code:

```bash
claude plugin marketplace add https://github.com/shreyashkar-ml/autoexp
claude plugin install autoexp@autoexp
```

For Codex:

```bash
codex plugin marketplace add https://github.com/shreyashkar-ml/autoexp
codex plugin add autoexp@autoexp
```

The plugin is optional. Projects created with `autoexp init` still include their own agent instructions and project-local MCP configuration. Pi and other agents that discover `AGENTS.md` can use that project guidance without a plugin; MCP-only operations still require an MCP-capable client.

`autoexp init` creates project-local agent configuration:

```text
CLAUDE.md
AGENTS.md
.mcp.json
```

Claude Code reads `CLAUDE.md`. Other coding agents can read `AGENTS.md`. The generated `.mcp.json` starts the MCP server with:

```json
{
  "mcpServers": {
    "autoexp": {
      "command": "autoexp",
      "args": ["mcp"]
    }
  }
}
```

Open the project in a coding agent that can launch project-local stdio MCP servers. Clients that do not read `.mcp.json` can be configured manually to run `autoexp mcp`. The `autoexp` command must be available on the agent's `PATH`.

A useful prompt is:

```text
Run two dataset-cleaning experiments, compare the output artifacts, and write a report explaining which cleanup was more useful.
```

Through MCP, an agent can:

- read the project contract, script manifest, and parameters
- list runs and inspect their source, outputs, logs, reports, and report bundles
- create edited script snapshots
- run, rerun, restore, and compare experiments
- update parameters and project-specific report guidance
- drive the complete Autoresearch attempt cycle

Autoexp also includes MCP prompts for improving a script, debugging a failed run, and writing a report.

## Autoresearch mode

Use Autoresearch when the project has a metric that can decide whether an attempt improved the result: accuracy, loss, latency, cost, reward, benchmark score, or any JSON value your evaluator writes.

Create an Autoresearch project:

```bash
autoexp init metric_lab --autoresearch
cd metric_lab
autoexp view
```

An Autoresearch project starts with three clear file roles:

- `script/program.md` — your objective, research directions, and loop rules
- `script/train.py` — the implementation the coding agent improves
- `script/evaluate.py` — the stable evaluator that produces the metric

On a fresh Autoresearch project, the browser view can import an existing Python script as the starting `script/train.py`.

Edit `script/program.md` to change the research direction, constraints, allowed strategies, or stopping criteria. The agent reads this file before proposing and finishing attempts.

Configure the metric direction and agent command in the `autoresearch` section of `autoexp.json`:

```json
{
  "objective": {
    "metric": "score",
    "direction": "max",
    "baseline": null,
    "budget_sec": 300
  },
  "metric": {
    "kind": "json",
    "path": "metrics.json",
    "key": "score"
  },
  "agent": {
    "cmd": ["codex", "exec", "Read script/program.md and run the autoresearch loop using the Autoexp MCP tools."]
  }
}
```

Use `"direction": "max"` when larger scores are better and `"direction": "min"` when smaller scores are better.

Start the loop from the browser UI. For each attempt, the agent:

1. checks research preflight and reads the current contract state
2. proposes one hypothesis
3. saves a candidate snapshot from `script/train.py`
4. executes that snapshot as a new immutable run
5. reads the configured metric from indexed run artifacts
6. advances the best snapshot only for an improvement

Autoexp persists the research contract, loop session, and every attempt in SQLite. Each attempt records its hypothesis, base and candidate snapshots, immutable run, score, and verdict. Reverted candidates remain inspectable through their Diff, Run, and Artifacts tabs; only the current-best pointer moves backward to the prior source.

The evaluator is frozen within a contract. If you intentionally change `script/evaluate.py` outside the active loop, Autoexp starts a new contract boundary with a new evaluator fingerprint and attempt numbering while retaining the earlier contract's attempts. Start Loop stays disabled when research preflight finds a missing runner, file, objective, budget, or agent executable.

## Execution runners

During initialization, Autoexp checks whether Docker and its daemon are available.

- With Docker available, new projects use the Docker runner and the limits in `autoexp.json`.
- Without Docker, new projects use the local runner and print instructions for enabling Docker later.

To change an existing project, edit the runner setting in `autoexp.json`:

```json
"runner": "local"
```

or:

```json
"runner": "docker"
```

Declare versioned inputs that live outside the source snapshot when you have them:

```json
"external_inputs": [
  {"name": "DATASET_PATH", "kind": "file", "path": "/data/eval.jsonl", "version": "eval-v3"}
]
```

`app.env` values are passed to the selected runner but remain local and excluded from Autoexp's saved source history. Autoexp records key presence and declared versions or safe file fingerprints, never secret values. Undeclared or unversioned inputs appear as reproducibility warnings.

## Common commands

| Task | Command |
| --- | --- |
| Create a standard project | `autoexp init <project_name>` |
| Create an Autoresearch project | `autoexp init <project_name> --autoresearch` |
| Run the current experiment | `autoexp run` |
| Rerun an earlier run | `autoexp run <run_id>` |
| List recent runs | `autoexp status` |
| Restore a run's script and config | `autoexp restore <run_id>` |
| Compare two run snapshots | `autoexp diff <run_a> <run_b>` |
| Open the browser UI | `autoexp view` |
| Change the report guidance file | `autoexp report-instruction <path>` |
| Check local setup | `autoexp doctor` |
| Start the MCP server manually | `autoexp mcp` |

Run `autoexp <command> --help` for command-specific usage.
