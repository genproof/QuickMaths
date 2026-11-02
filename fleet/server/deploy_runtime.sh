#!/usr/bin/env bash
# Deploy runtime to a runner host as <own root>/runtimes/<id> (run on the operator machine).
# Usage: deploy_runtime.sh HOST
# Content-addressed id lrh-<hash>; never overwrites an existing deployment. Regenerates the manifest file
# hashes that the queue controller verifies, links the runtime venv, runs the runtime tests on a scratch
# copy and builds the declaration-name index for unknown-identifier hints. Prints the deployed path.
# Switching a runner is separate: set "runtimes" in <own root>/config.json; the feeder applies
# it when that runner is idle (lineage boundary), never mid-lineage.
#
# Operator machine: LEH_SSH_CONFIG, LEH_SSH_KNOWN_HOSTS (optional ssh -F / UserKnownHostsFile),
# LEH_REMOTE_OWN_ROOT (default leh-fleet, relative to the remote home).
# Runner host: LEH_RUNTIME_VENV (default ~/.venvs/leh-runtime; created with python3.12 -m venv and
# pytest when missing), LEANEVAL_WORKBENCH (default ~/projects/lean-eval-workbench).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SRC="$REPO/runtime"
HOST=${1:?usage: deploy_runtime.sh HOST}
SSH_OPTS="-o StrictHostKeyChecking=yes -o BatchMode=yes"
[ -n "${LEH_SSH_CONFIG:-}" ] && SSH_OPTS="-F $LEH_SSH_CONFIG $SSH_OPTS"
[ -n "${LEH_SSH_KNOWN_HOSTS:-}" ] && SSH_OPTS="$SSH_OPTS -o UserKnownHostsFile=$LEH_SSH_KNOWN_HOSTS"
REMOTE_OWN=${LEH_REMOTE_OWN_ROOT:-leh-fleet}
HASH=$(cd "$SRC" && find leaneval tests README.md -type f -not -path '*/__pycache__/*' -not -name '*.pyc' \
       | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-8)
# Deterministic id (no date): the same sources always deploy to the same directory, so a
# redeploy never produces a new pin for runners that run this content already.
ID="lrh-$HASH"
# shellcheck disable=SC2086
RHOME=$(ssh $SSH_OPTS "$HOST" 'printf %s "$HOME"')
case "$REMOTE_OWN" in /*) DEST="$REMOTE_OWN/runtimes/$ID" ;; *) DEST="$RHOME/$REMOTE_OWN/runtimes/$ID" ;; esac
# shellcheck disable=SC2086
if ssh $SSH_OPTS "$HOST" "test -e $DEST"; then echo "already deployed: $HOST:$DEST"; exit 0; fi
# shellcheck disable=SC2086
ssh $SSH_OPTS "$HOST" "mkdir -p $(dirname "$DEST") && rm -rf $(dirname "$DEST")/*.partial"
# shellcheck disable=SC2086
rsync -a --exclude __pycache__ --exclude '*.pyc' --exclude .pytest_cache -e "ssh $SSH_OPTS" "$SRC/" "$HOST:$DEST.partial/"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$HOST" "ID=$ID DEST=$DEST bash -s" <<'REMOTE'
set -euo pipefail
VENV=${LEH_RUNTIME_VENV:-$HOME/.venvs/leh-runtime}
if [ ! -x "$VENV/bin/python" ]; then
  python3.12 -m venv "$VENV"
  "$VENV/bin/pip" install -q pytest
fi
ln -s "$VENV" "$DEST.partial/.venv"
python3 - "$DEST.partial" "$ID" <<'PY'
import hashlib, json, sys, time
from pathlib import Path
root, rid = Path(sys.argv[1]), sys.argv[2]
manifest = json.loads((root / 'runtime-manifest.json').read_text())
files = {}
for path in sorted(root.rglob('*')):
    rel = path.relative_to(root).as_posix()
    if path.is_file() and not path.is_symlink() and rel != 'runtime-manifest.json' \
            and not rel.startswith('.venv') and '__pycache__' not in rel:
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest.update(runtime_id=rid, previous_runtime=manifest.get('runtime_id'), files=files)
manifest.setdefault('change_log', []).append({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'runtime_id': rid, 'note': 'deployed from runtime; see its git history'})
(root / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
print(len(files), 'files hashed')
PY
SCRATCH=$(mktemp -d /tmp/lrh-test.XXXXXX)
cp -a "$DEST.partial/." "$SCRATCH/"
(cd "$SCRATCH" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRATCH" "$VENV/bin/python" -m pytest -q -p no:cacheprovider tests 2>&1 | tail -5)
rm -rf "$SCRATCH"
mv "$DEST.partial" "$DEST"
INDEX=$HOME/.cache/lrh/decl-names.txt
if [ ! -s "$INDEX" ]; then
  WORKBENCH=${LEANEVAL_WORKBENCH:-$HOME/projects/lean-eval-workbench}
  PKG=$WORKBENCH/vendor/lean-eval/.lake/packages
  TOOLCHAIN=$(sed -n 's/^toolchain = "leanprover\/lean4:\(.*\)"/\1/p' "$DEST/leaneval/resources/profile.toml")
  CORE=$(ls -d "$HOME"/.elan/toolchains/*"$TOOLCHAIN"*/src/lean | head -1)
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DEST" nice -n 19 "$VENV/bin/python" -m leaneval.compact "$PKG/mathlib" "$PKG/lean-pool" "$PKG/batteries" "$CORE"
fi
echo "deployed: $DEST"
REMOTE
