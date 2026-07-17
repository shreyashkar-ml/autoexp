#!/usr/bin/env bash
set -euo pipefail

for command in awk curl git install uv; do
  command -v "$command" >/dev/null || {
    echo "autoexp installer requires $command" >&2
    exit 1
  }
done

repo="https://github.com/shreyashkar-ml/autoexp"
ref="${AUTOEXP_REF:-main}"
raw="https://raw.githubusercontent.com/shreyashkar-ml/autoexp/${ref}/plugins/autoexp/skills"
tmp="$(mktemp -d)"
trap "rm -rf \"$tmp\"" EXIT

for skill in autoexp autoexp-review; do
  mkdir -p "$tmp/$skill/agents"
  curl -fsSL "$raw/$skill/SKILL.md" -o "$tmp/$skill/SKILL.md"
  curl -fsSL "$raw/$skill/agents/openai.yaml" -o "$tmp/$skill/agents/openai.yaml"
done

uv tool install --force "git+${repo}.git@${ref}"

codex_skills="${AUTOEXP_CODEX_SKILLS_DIR:-$HOME/.agents/skills}"
claude_skills="${AUTOEXP_CLAUDE_SKILLS_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills}"
for root in "$codex_skills" "$claude_skills"; do
  for skill in autoexp autoexp-review; do
    mkdir -p "$root/$skill/agents"
    install -m 0644 "$tmp/$skill/SKILL.md" "$root/$skill/SKILL.md"
    install -m 0644 "$tmp/$skill/agents/openai.yaml" "$root/$skill/agents/openai.yaml"
  done
done

# Claude should never open the blocking review unless the user invokes it.
awk "1; /^description:/ { print \"disable-model-invocation: true\" }" \
  "$tmp/autoexp-review/SKILL.md" > "$claude_skills/autoexp-review/SKILL.md"
chmod 0644 "$claude_skills/autoexp-review/SKILL.md"

printf "%s\n" \
  "Installed Autoexp and its agent commands." \
  "Codex: \$autoexp, \$autoexp-review" \
  "Claude Code: /autoexp, /autoexp-review" \
  "Restart your agent to load the skills."
