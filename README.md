<p align="center">
  <img src="assets/dark.svg" alt="Autoexp" width="480">
</p>

# autoexp

Track your autonomous research and experimentation, send follow-up feedbacks, and analyze reports and insights with **`autoexp`** — a local, browser-based experimentation surface for AI coding agents: Claude Code, Codex, OpenCode, Pi.

AI agents are already good at proposing code. The harder problem is the harness around those proposals: source boundaries, reproducible execution, external inputs, evaluators, artifacts, lineage, rollback, and human review. Autoexp supplies that infrastructure while your repository remains the editable source of truth.

<table>
  <tr>
    <td width="36%">
      <h3>Standard Experiments</h3>
      <p>Compare variants, inspect immutable evidence, and turn results into a clear recommendation.</p>
    </td>
    <td width="64%">
      <img src="assets/autoexp_demo.png" alt="Autoexp dashboard showing experiment variants, immutable run evidence, milestones, and the project report">
    </td>
  </tr>
  <tr>
    <td width="36%">
      <h3>Autoresearch</h3>
      <p>Optimize a measurable objective with a frozen evaluator and a keep-or-revert loop.</p>
    </td>
    <td width="64%">
      <img src="assets/autoresearch_demo.png" alt="Autoexp Autoresearch dashboard showing the scored loop, final state, and attempt ledger">
    </td>
  </tr>
</table>

## Install and connect your agent

First, install the runtime:

```bash
curl -fsSL https://raw.githubusercontent.com/shreyashkar-ml/autoexp/main/install.sh | bash
```

Then choose your agent and install its plugin:

<details>
<summary>Codex</summary>

```bash
codex plugin marketplace add shreyashkar-ml/autoexp
codex plugin add autoexp@autoexp
```
</details>

<details>
<summary>Claude</summary>

```bash
claude plugin marketplace add shreyashkar-ml/autoexp
claude plugin install autoexp@autoexp
```
</details>

Restart your agent after plugin installation.

## Use Autoexp from your agent

Autoexp exposes two agent workflows:

| Workflow | Codex | Claude Code |
| --- | --- | --- |
| Start or continue experiments | `$autoexp <objective>` | `/autoexp <objective>` |
| Open browser feedback review | `$autoexp-review` | `/autoexp-review` |

### Open your existing repository and start experimentation or autoresearch loop with autoexp.

```text
/autoexp Compare the cache strategies in this repository. Reuse the existing
replay benchmark, preserve every run, and recommend a winner from the evidence.
```

**autoexp** inspects the current worktree, understand the experiment objective, use existing resources relevant to experimentations, and builds on top of it.

```text
You define the objective and harness boundaries
          ↓
Your agent proposes a focused repository change
          ↓
Autoexp pins execution and seals the resulting evidence
          ↓
A metric or human review decides what happens next
```

### Review results with the agent

Invoke the review workflow when you want to inspect results or steer the next step:

```text
/autoexp-review
```

`/autoexp review` opens a short-lived local browser session and blocks the agent at the review boundary. You can inspect source, rendered artifacts, CSV tables, images, logs, reports, and diffs. 
Attach feedbacks/notes and submit one structured feedback batch. Feedback returns directly to the agent for follow-up experimentation and result.

### Dashboard view

Use `autoexp view` to open and view all your autoexp experimentations/research and their results in one place in read/download-mode only.

## Direct CLI reference

Most users let the plugin drive these commands. They remain available for inspection, automation, and debugging:

| Task | Command |
| --- | --- |
| Show registered experiments | `autoexp experiment list` |
| Inspect recent runs | `autoexp status` |
| Open the global dashboard | `autoexp view` |
| Compare two immutable runs | `autoexp diff <run-a> <run-b>` |
| Restore declared source from a run | `autoexp restore <run-id>` |
| Check the selected experiment and runtime | `autoexp doctor` |
| Open a blocking agent review | `autoexp review` |

<details>
<summary>What the agent runs for a Standard experiment</summary>

```bash
autoexp experiment create "<objective>" --title "<title>" --entrypoint <path> --command "<command>"
autoexp files add <path> --role editable-source
autoexp files add <path> --role supporting-source
autoexp files add <path> --role input-data
autoexp run --agent --title "<variant or hypothesis>"
```

The agent can attach reports and insights without writing generated documents into the repository:

```bash
autoexp document add /tmp/findings.md --kind insight --title "<title>"
autoexp document add /tmp/report.md --kind report --title "<title>"
```

</details>

<details>
<summary>What the agent runs for Autoresearch</summary>

```bash
autoexp experiment create "<objective>" --kind autoresearch \
  --program <program> --candidate <candidate> --evaluator <evaluator> \
  --metric <name> --direction <min|max> \
  --metric-kind json --metric-path metrics.json --metric-key <key>

autoexp research preflight
autoexp research state
autoexp research attempt "<hypothesis>"
```

</details>

## Local data and secrets

**autoexp** stores its ledger in one global SQLite database and one private bare Git snapshot repository per registered worktree. Ordinary repository files remain where they are, and secrets are never hidden by default in view.

Default data directory:

- Linux: `$XDG_DATA_HOME/autoexp` or `~/.local/share/autoexp`
- macOS: `~/Library/Application Support/autoexp`
- Windows: `%LOCALAPPDATA%/autoexp`

Set `AUTOEXP_HOME` to override it. Use `autoexp relink <repo-id> <new-path>` if a worktree moves.

## Development

```bash
uv run pytest
cd frontend && npm install && npm run build
```

The Vite build writes the bundled dashboard to `autoexp/ui`. Autoexp itself has no runtime Python dependencies.
