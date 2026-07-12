---
name: autoexp
description: Run, compare, inspect, and report on local experiments with Autoexp, including metric-driven Autoresearch loops. Use when a user asks to experiment, evaluate variants, inspect Autoexp runs, generate a report, optimize a measurable objective, or work in an Autoexp project.
---

# Autoexp

## Start

1. Confirm `autoexp` is installed and the current directory is inside an Autoexp project.
2. Read `AGENTS.md`, `.autoexp/instructions.md`, and `.autoexp/project.json` before changing experiment behavior.
3. Use the Autoexp MCP tools for runs and artifact inspection. If they are missing:
   - Run `autoexp --help`. If the command is unavailable, ask the user to install it with `uv tool install "git+https://github.com/shreyashkar-ml/autoexp.git"` and restart the coding agent.
   - Confirm the current project contains `.autoexp/project.json`. If not, initialize or open an Autoexp project.
   - If both checks pass, ask the user to restart the coding agent from the project root so Claude can load `.mcp.json` or Codex can load the installed Autoexp plugin.
   - Do not ask the user to run `autoexp mcp` directly; the coding agent launches that stdio process.

If no project exists, choose the mode from the objective:

- Use `autoexp init <name>` for software, exploratory, qualitative, comparative, or multi-variant work.
- Use `autoexp init <name> --autoresearch` only when one stable scalar metric and a frozen evaluator can decide keep versus revert.

Never initialize over a non-empty directory. Ask for a project name or location when it is unclear.

## Standard experiments

1. Read the workspace contract, script manifest, parameters, and recent runs.
2. Make one focused change under `experiment/` or update parameters through MCP. Use separate, descriptively named scripts when distinct variants need separate reports.
3. Run the experiment through the `run` MCP tool with a concise title.
4. Inspect its metadata, output files, and logs before deciding the next change.
5. Keep the returned `run_id` in the summary so the result can be reproduced or compared.

For reports, read the run's report bundle and active report instruction, then write generated files under `runs/<run_id>/report/`. Change `.autoexp/report-instructions.md` only when the user wants different future guidance.

## Autoresearch

1. Call `research_preflight`; do not start while a required check fails.
2. Read `experiment/program.md` and call `research_state`.
3. If the user provided a reference training script and no attempts exist yet, save it into the file marked `agent` as the baseline.
4. Form one concrete hypothesis and save one focused edit to the file marked `agent`.
5. Call `research_begin_attempt` with that hypothesis.
6. Call `research_finish_attempt` with the returned attempt ID.
7. Inspect the score, verdict, candidate diff, immutable run, and artifacts, then repeat within the user's stopping rule.

Do not edit files marked `human` or `frozen`. A deliberate evaluator change is a user-owned contract boundary, not an agent experiment. Keep reverted attempts in the ledger; their snapshots, runs, and artifacts are part of the research record.

## Milestones and project report

- Mark only decision-changing results, surprising failures, and new best results with `mark_milestone`; do not mark every run.
- Use `project_summary` to fetch milestones, linked report excerpts, runs, and Autoresearch state without relying on the UI.
- At the end of either mode, synthesize the entire project and write `.autoexp/project-report.md` with `write_project_report`. This complements, rather than replaces, per-run reports.

## Boundaries

- Do not hand-edit run outputs, logs, ledger rows, or stored diffs.
- Do not expose values from `.env`.
- Prefer one interpretable change per run or attempt.
- Report failures with their run ID and relevant log evidence.
