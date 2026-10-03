#!/bin/sh
# PostToolUse hook for `git push`: tells Claude which commit to check CI for.
# `gh run list --commit` needs the full SHA; a short one returns nothing.
# The settings `if` filter is not precise enough, so check the command here.
grep -q '"command":"[^"]*git push' || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
sha=$(git rev-parse HEAD 2>/dev/null) || exit 0
printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Pushed %s at %s. Check CI for it now: gh run list --commit %s (retry after a few seconds if empty), then gh run watch <id> --exit-status. If it fails, fix it right away."}}\n' "$branch" "$sha" "$sha"
