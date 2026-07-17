---
name: autoexp-review
description: Open the Autoexp browser feedback review for the active experiment and return submitted notes to the agent. Use only when the user explicitly invokes this review workflow.
---

# Autoexp review

Open a blocking browser review for the active Autoexp experiment.

1. Run `autoexp --help`. If unavailable, ask the user to install it with `uv tool install "git+https://github.com/shreyashkar-ml/autoexp.git"`.
2. Work from the current Git worktree. Use an explicitly supplied experiment ID if present; otherwise let Autoexp resolve the latest experiment in this repository.
3. Run `autoexp review`, or `autoexp review --experiment <id>` when an experiment ID was supplied.
4. Wait for the review to complete. Do not substitute `autoexp view`; ordinary view sessions cannot submit feedback.
5. Read the returned JSON note batch and treat it as the next user instruction. Apply or answer those notes within the current task scope.
6. If the review expires or the command fails, report the exact error. Never invent feedback.

The review URL is short-lived and local. The command blocks until one feedback batch is submitted; review tokens are stored only as hashes and cannot be submitted twice.
