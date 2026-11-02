#!/usr/bin/env bash
# Foreground launcher for one fleet runner queue (systemd unit leh-runner@<runner>).
# The runtime pinned in controller.py (Opus 5.5 xhigh), the runner's own subscription account and
# separate account/quota state, so runners on different accounts never block or consume each other.
set -euo pipefail
ROOT=${1:?queue root}
HOST=$(cat "$ROOT/HOST")
OWN=${LEH_OWN_ROOT:-$HOME/leh-fleet}
# Follow the runtime the controller pins, so imports and identity checks agree.
RUNTIME=$(sed -n 's/^RUNTIME = Path("\(.*\)").*/\1/p' "$ROOT/controller.py")
[ -d "$RUNTIME" ] || { echo "runtime from controller.py missing: $RUNTIME"; exit 4; }
PY="$RUNTIME/.venv/bin/python"
# Subscription account per runner: config.json "tokens" {"wave101": "file:~/.config/leh/accounts/a.token"}
# (fleet.json "accounts", a secret reference). Only file: references work here, because the
# controller starts with a cleared environment. One file per runner: the runtime adds only
# "<file name>-*" siblings, so a runner never rotates into another runner's account.
WAVE=${ROOT##*-}
TOKEN=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print((c.get("tokens") or {}).get(sys.argv[2], ""))' "$OWN/config.json" "$WAVE" 2>/dev/null || true)
[ -n "$TOKEN" ] || TOKEN="file:~/.config/leh/accounts/default.token"
case "$TOKEN" in
  env:*) echo "env: account references are not supported for fleet runners; use file:"; exit 3 ;;
  file:*) TOKEN_FILE=${TOKEN#file:} ;;
  *) TOKEN_FILE=$TOKEN ;;
esac
case "$TOKEN_FILE" in "~/"*) TOKEN_FILE="$HOME/${TOKEN_FILE#"~/"}" ;; esac
if pgrep -f "controller.py --host $HOST --root $ROOT" >/dev/null; then
  echo "already running"; exit 0
fi
if [ -f "$ROOT/STOP" ]; then echo "STOP present; not starting"; exit 0; fi
[ -f "$TOKEN_FILE" ] || { echo "own token file missing"; exit 3; }
# Optional compiler settings for the agent's ./lean calls (runtime P1): config.json
# "runtime_env", e.g. {"LRH_LEAN_NUM_THREADS": "6"}. Only these LRH_* names, digits only; the
# official verifier never reads them, and runtimes without P1 ignore them.
RUNTIME_ENV=()
while IFS= read -r line; do
  [ -n "$line" ] && RUNTIME_ENV+=("$line")
done < <(python3 -c '
import json, re, sys
c = json.load(open(sys.argv[1]))
allowed = {"LRH_LEAN_NUM_THREADS", "LRH_LAKE_JOBS", "LRH_FULL_CHECK_SECONDS"}
for k, v in sorted((c.get("runtime_env") or {}).items()):
    if k in allowed and re.fullmatch(r"[0-9]{1,6}", str(v)):
        print(k + "=" + str(v))
' "$OWN/config.json" 2>/dev/null || true)
# Host layout overrides (docs/CONFIGURATION.md), passed on only when set; none of them is a secret.
LAYOUT_ENV=()
for name in LEH_OWN_ROOT LEH_CAMPAIGNS_ROOT LEH_RUNS_ROOT LEANEVAL_WORKBENCH LEH_HOSTS LEH_TRANSFER_RECEIVER \
            LEH_DEPENDENCY_CONTEXTS; do
  [ -n "${!name:-}" ] && LAYOUT_ENV+=("$name=${!name}")
done
mkdir -p "$OWN/claude-state" "$OWN/quota-state" "$OWN/leases"
cd "$RUNTIME"
exec env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" LANG=C.UTF-8 \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONPATH="$RUNTIME" PYTHONDONTWRITEBYTECODE=1 \
  LEANEVAL_CLAUDE_TOKEN_REF="file:$TOKEN_FILE" \
  LEANEVAL_CLAUDE_STATE="$OWN/claude-state" \
  LEANEVAL_QUOTA_STATE="$OWN/quota-state" \
  "${LAYOUT_ENV[@]}" \
  "${RUNTIME_ENV[@]}" \
  "$PY" -B "$ROOT/controller.py" --host "$HOST" --root "$ROOT" \
  >> "$ROOT/controller.log" 2>&1 < /dev/null
