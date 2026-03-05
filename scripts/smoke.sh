#!/usr/bin/env bash
set -euo pipefail

REPO_PATH="${1:-.}"
TASK_TEXT="${2:-demo multi-phase task}"

uv run autoeval init --repo "${REPO_PATH}" --provider codex --task "${TASK_TEXT}"
uv run autoeval verifier sync --repo "${REPO_PATH}"
uv run autoeval tools decide-mode --repo "${REPO_PATH}" --request "${TASK_TEXT}" --mode auto
uv run autoeval run --repo "${REPO_PATH}" --task "${TASK_TEXT}" --mode auto --run-autocheck-now
uv run autoeval autocheck --repo "${REPO_PATH}"
uv run autoeval status --repo "${REPO_PATH}"
uv run autoeval tools list --repo "${REPO_PATH}"
uv run autoeval tools guardrail-check --command "pytest -q tests/test_sample.py::test_ok" || true
uv run autoeval eval --repo "${REPO_PATH}" --profile default
uv run autoeval tools append-lesson --repo "${REPO_PATH}" --text "sample interruption pattern" || true
uv run autoeval tools append-review --repo "${REPO_PATH}" --text "sample final review note" || true
