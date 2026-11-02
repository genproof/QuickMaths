#!/usr/bin/env bash
# Idempotent start of this runner queue (copied into each queue root).
# The controller runs in its own systemd unit, so it survives the feeder and reboots
# are handled by the feeder timer; KillMode=process keeps a running model supervisor
# alive across controller restarts (the controller adopts it).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
WAVE=${ROOT##*-}
if [ -f "$ROOT/STOP" ]; then echo "STOP present; not starting"; exit 0; fi
sudo -n systemctl start "leh-runner@${WAVE}.service"
systemctl is-active "leh-runner@${WAVE}.service"
