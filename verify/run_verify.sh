#!/usr/bin/env bash
# run_verify.sh RUN MEM CPUS NAME : run RUN/verify_candidate.py (a run directory from verify_run.py prepare)
# with the official toolchain and tools, bounded to MEM (e.g. 64G) and the CPU list CPUS (e.g. 0-15), and
# write RUN/RUN-DONE: `rc N wall Ns peak BYTES oom N scope S mem MEM cpus CPUS`.
#
# This is the run wrapper vcur's run_lane.sh calls (VCUR_RUN_VERIFY); verify_tree.sh calls it too.
# Scope (LEH_VERIFY_SCOPE): `systemd` runs it in a transient user scope named NAME with MemoryMax=MEM,
# MemorySwapMax=0 and AllowedCPUs=CPUS and reads the scope's memory.peak and oom_kill count; `none` runs
# it under `taskset -c CPUS` only, without a memory cap (peak unknown); `auto` (default) takes systemd when
# a user manager answers. Never kills anything; verify_candidate.py applies its own lake test timeout.
#
# Environment of the run, by the config's evaluation_flow:
#   lake-comparator  PATH = RUN/bwrap-bin (the cache wrapper around the pinned bubblewrap), the benchmark
#                    toolchain's bin (from RUN/benchmark-clean/lean-toolchain under ELAN_HOME, default ~/.elan;
#                    it must ship nanoda_bin, which WorkspaceTest hands to `lake comparator`), /usr/bin:/bin.
#   comparator       earlier benchmarks: PATH = the toolchain's bin, the workbench tools (config tools_bin),
#                    /usr/bin:/bin; COMPARATOR_BIN, COMPARATOR_LANDRUN, COMPARATOR_LEAN4EXPORT, COMPARATOR_NANODA.
# VCUR_PY (default python3.12) runs the verifier.
set -uo pipefail
RUN=${1:?run dir}; MEM=${2:?memory, e.g. 64G}; CPUS=${3:?cpu list, e.g. 0-15}; NAME=${4:-leh-verify}
RUN=$(cd "$RUN" && pwd) || exit 2
PY=${VCUR_PY:-python3.12}
export ELAN_HOME=${ELAN_HOME:-$HOME/.elan}
TOOLCHAIN=$(tr -d '[:space:]' < "$RUN/benchmark-clean/lean-toolchain") || exit 2
LEAN_BIN=$ELAN_HOME/toolchains/$(printf %s "$TOOLCHAIN" | sed 's|/|--|; s|:|---|')/bin
read -r TOOLS FLOW PREFIX < <("$PY" -c 'import json, sys; c = json.load(open(sys.argv[1]))
print(c["tools_bin"], c.get("evaluation_flow", "comparator"), c.get("path_prefix", "-"))' "$RUN/config.json") || exit 2
[ -x "$LEAN_BIN/lean" ] || { echo "run_verify: no toolchain $TOOLCHAIN at $LEAN_BIN" >&2; exit 2; }
PYBIN=$(command -v "$PY") || { echo "run_verify: no $PY on PATH" >&2; exit 2; }
if [ "$FLOW" = lake-comparator ]; then
  [ -x "$LEAN_BIN/nanoda_bin" ] || { echo "run_verify: toolchain $TOOLCHAIN has no nanoda_bin" >&2; exit 2; }
  [ -x "$PREFIX/bwrap" ] || { echo "run_verify: no bubblewrap wrapper in $PREFIX" >&2; exit 2; }
  export PATH="$PREFIX:$LEAN_BIN:/usr/bin:/bin"
  unset COMPARATOR_BIN COMPARATOR_LANDRUN COMPARATOR_LEAN4EXPORT COMPARATOR_NANODA
else
  export PATH="$LEAN_BIN:$TOOLS:/usr/bin:/bin"
  export COMPARATOR_BIN=$TOOLS/comparator COMPARATOR_LANDRUN=$TOOLS/landrun
  export COMPARATOR_LEAN4EXPORT=$TOOLS/lean4export COMPARATOR_NANODA=$TOOLS/nanoda_bin
fi

SCOPE=${LEH_VERIFY_SCOPE:-auto}
if [ "$SCOPE" = auto ]; then
  if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    SCOPE=systemd
  else
    SCOPE=none
  fi
fi
# Inside the scope: run the verifier, then read this cgroup's peak and OOM kills before leaving it.
INNER='cd "$1" || exit 2; "$2" -B verify_candidate.py; rc=$?
cg=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup 2>/dev/null)
peak=$(cat "$cg/memory.peak" 2>/dev/null || echo unknown)
oom=$(awk "/^oom_kill /{print \$2}" "$cg/memory.events" 2>/dev/null)
echo "$rc $peak ${oom:-unknown}" > "$1/.run-measure"; exit $rc'
START=$(date +%s)
case "$SCOPE" in
  systemd)
    systemd-run --user --scope --quiet --unit="$NAME-$START" -p MemoryMax="$MEM" -p MemorySwapMax=0 \
      -p AllowedCPUs="$CPUS" -- bash -c "$INNER" run-verify "$RUN" "$PYBIN"
    RC=$? ;;
  none)
    if command -v taskset >/dev/null 2>&1; then
      taskset -c "$CPUS" bash -c "$INNER" run-verify "$RUN" "$PYBIN"
    else
      bash -c "$INNER" run-verify "$RUN" "$PYBIN"
    fi
    RC=$? ;;
  *) echo "run_verify: LEH_VERIFY_SCOPE must be auto, systemd or none" >&2; exit 2 ;;
esac
WALL=$(( $(date +%s) - START ))
read -r MRC PEAK OOM < "$RUN/.run-measure" 2>/dev/null || { MRC=$RC; PEAK=unknown; OOM=unknown; }
[ "$SCOPE" = none ] && { PEAK=unknown; OOM=unknown; }  # no cgroup of its own: the numbers would be the session's
echo "rc ${MRC:-$RC} wall ${WALL}s peak $PEAK oom $OOM scope $SCOPE mem $MEM cpus $CPUS" > "$RUN/RUN-DONE"
rm -f "$RUN/.run-measure"
exit "${MRC:-$RC}"
