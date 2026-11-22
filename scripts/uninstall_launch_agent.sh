#!/usr/bin/env bash
# Boot out the LaunchAgent and remove its plist. Does not change scheduler.yaml.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO}/.venv/bin/python"
LABEL="com.investing-agents.pipeline"
if [[ -x "${PY}" ]]; then
  cd "${REPO}"
  export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"
  RESOLVED="$("${PY}" -c 'from scheduler.host_schedule import load_host_schedule; from shared.paths import SCHEDULER_CONFIG_PATH; print(load_host_schedule(SCHEDULER_CONFIG_PATH).label)' 2>/dev/null || true)"
  if [[ -n "${RESOLVED}" ]]; then
    LABEL="${RESOLVED}"
  fi
fi

DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
if command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
fi
if [[ -f "${DEST}" ]]; then
  rm -f "${DEST}"
  echo "Removed ${DEST}"
else
  echo "No plist at ${DEST}"
fi
echo "config/scheduler.yaml was not modified."
