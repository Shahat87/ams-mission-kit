#!/usr/bin/env bash
# SessionStart hook: active only in projects that opted in with a .mission-control marker file.
[ -f "${CLAUDE_PROJECT_DIR:-.}/.mission-control" ] || exit 0
SK="${CLAUDE_PLUGIN_ROOT}/skills/mission-control"
cat "$SK/CORE.md" || exit 0
printf '\nFull rules: %s/SKILL.md\nRouting table: %s/routing.md\nOr invoke skill: super-board:mission-control\n' "$SK" "$SK"
