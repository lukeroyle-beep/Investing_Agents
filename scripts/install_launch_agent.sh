#!/usr/bin/env bash
# Install the LaunchAgent plist into ~/Library/LaunchAgents.
# Does not bootstrap the agent and does not change host_schedule.enabled.
# A previous job with the same label is booted out so an old plist cannot keep firing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "error: ${PY} is missing. From the repo root run: uv sync --frozen" >&2
  exit 1
fi

cd "${REPO}"
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

LABEL="$("${PY}" -c 'from scheduler.host_schedule import load_host_schedule; from shared.paths import SCHEDULER_CONFIG_PATH; print(load_host_schedule(SCHEDULER_CONFIG_PATH).label)')"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
mkdir -p "${HOME}/Library/LaunchAgents"

TMP="$(mktemp "${TMPDIR:-/tmp}/investing-agents-plist.XXXXXX")"
cleanup() { rm -f "${TMP}"; }
trap cleanup EXIT

"${PY}" -m scheduler.host_schedule render-plist --output "${TMP}"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "${TMP}"
fi
cp "${TMP}" "${DEST}"
chmod 644 "${DEST}"

if command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
else
  echo "warning: launchctl is not available; the plist was written but not registered" >&2
fi

cat <<EOF
Installed ${DEST}
The agent is not loaded. host_schedule.enabled was not changed.

Enable only after you set host_schedule.enabled: true and store secrets:
  launchctl bootstrap gui/${UID_NUM} ${DEST}

Disable without uninstalling:
  launchctl bootout gui/${UID_NUM}/${LABEL}

Status:
  ${PY} ${REPO}/scripts/scheduler_status.py
EOF
