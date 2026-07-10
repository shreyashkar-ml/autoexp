---
name: autoexp
description: Run, compare, inspect, and report on local experiments with Autoexp, including metric-driven Autoresearch loops. Use when a user asks to experiment, evaluate variants, inspect Autoexp runs, generate a run report, optimize a measurable objective, or work in a project containing autoexp.json.
---

# Autoexp

## Start

1. Confirm `autoexp` is installed and the current directory is inside an Autoexp project.
2. Read `autoexp.md`, `autoexp.json`, and the relevant instruction file before changing experiment behavior.
3. Use the Autoexp MCP tools for runs and artifact inspection. If they are missing:
   - Run `autoexp --help`. If the command is unavailable, ask the user to install it with `uv tool install "git+https://github.com/shreyashkar-ml/autoexp.git"` and restart the coding agent.
   - Confirm the current project contains `autoexp.json`. If not, ask the user to initialize or open an Autoexp project.
   - If both checks pass, ask the user to restart the coding agent from the project root so Claude can load `.mcp.json` or Codex can load the installed Autoexp plugin.
   - Do not ask the user to run `autoexp mcp` directly; the coding agent launches that stdio process.

If no project exists, choose the mode from the objective:

- Use `autoexp init <name>` for exploratory or qualitative work.
- Use `autoexp init <name> --autoresearch` only when a scalar metric decides improvement.

Never initialize over a non-empty directory. Ask for a project name or location when it is unclear.

## Standard experiments

1. Read the workspace contract, script manifest, parameters, and recent runs.
2. Make one focused change under `script/` or update `script/params.json`.
3. Run the experiment through the `run` MCP tool.
4. Inspect its metadata, output files, and logs before deciding the next change.
5. Keep the returned `run_id` in the summary so the result can be reproduced or compared.

For reports, read the run's report bundle and active report instruction, then write generated files under `runs/<run_id>/report/`. Edit `report.txt` only when the user wants to change future report guidance.

## Autoresearch

1. Read `script/program.md` and call `research_state`.
2. If the user provided a reference training script and no attempts exist yet, adapt or copy it into the file marked `agent` as the baseline.
3. Form one concrete hypothesis and edit only the file marked `agent`.
4. Call `research_begin_attempt` with that hypothesis.
5. Call `research_finish_attempt` with the returned attempt tag.
6. Inspect the score, decision, and diff, then repeat within the user's stopping rule.

Do not edit files marked `human` or `frozen`. Keep reverted attempts in the ledger; they are part of the research record.

## Boundaries

- Do not hand-edit run outputs, logs, ledger rows, or stored diffs.
- Do not expose values from `app.env`.
- Prefer one interpretable change per run or attempt.
- Report failures with their run ID and relevant log evidence.
