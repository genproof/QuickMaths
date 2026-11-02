#!/usr/bin/env bash
# Official verification of one proof on a benchmark commit, as the organizers' flow runs it.
#
# Usage: verify_tree.sh PROBLEM_ID SOURCE [--repo REPO] [--benchmark COMMIT] [--workbench DIR] [--out DIR]
#                       [--cpus LIST] [--mem SIZE] [--timeout SECONDS] [--ci-envelope] [--bwrap PATH]
#                       [--no-preflight] [--dry-run]
#
#   SOURCE       a directory holding Submission.lean (and Submission/), or a git ref (commit, branch, tag)
#                of REPO (a path or URL; default: the git repository of the current directory)
#   --benchmark  the benchmark commit (default: the current one, verify_run.py CURRENT_BENCHMARK)
#   --workbench  the trusted workbench (default $LEANEVAL_WORKBENCH or ~/projects/lean-eval-workbench)
#   --out        the run directory, which must not exist (default:
#                $LEH_VERIFY_ROOT/<problem>-<source>-<UTC time>[-ci], LEH_VERIFY_ROOT ~/projects/lean-eval-verify)
#   --cpus/--mem the bounds of the run (default 0-15 and 64G: a large proof's official run starts ~32 lean
#                processes; one was too slow on 4 CPUs at 48G and passed in about 2.8 h on 16 CPUs at 64G)
#   --timeout    the lake test timeout in seconds (default 21600, the organizers' 6 h)
#   --ci-envelope
#                the organizers' CI runner instead (Namespace 16 CPU / 32 GB since 2026-10-22): CPUs 0-15,
#                31G; a separate run whose wall time predicts CI (accepted Poincaré packages: CI took
#                0.95-1.01x of this run) and whose acceptance says the proof fits the runner's memory
#   --bwrap      the pinned bubblewrap 0.12.0 for the lake-comparator flow (default $LEH_BWRAP or
#                <workbench>/.tools/bin/bwrap; build_bwrap.sh builds one without root)
#   --no-preflight
#                run even when the disk or vm.max_map_count preflight fails (verify_run.py preflight)
#   --dry-run    print the plan and the preflight, and stop
#
# Steps: the proof files only (Submission.lean, Submission/**/*.lean) are exported or copied; a clean
# benchmark checkout at COMMIT gets the workbench's built Lake packages; verify_run.py prepare fetches
# the organizers' evaluator at its pinned commit and picks the benchmark's flow (lake-comparator from
# lean-eval 208b6b22 on, else the comparator binaries); run_verify.sh runs verify_candidate.py (official
# `lake test`: Lean kernel and Nanoda) in a cgroup scope; verify_run.py receipt writes
# <run>/receipt.json. Exit 0 accepted, 1 rejected or failed, 2 the run could not be prepared.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${VCUR_PY:-python3.12}
usage() { sed -n '4,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 2 ] || usage
PROBLEM=$1; SOURCE=$2; shift 2
REPO=; BENCH=; WORKBENCH=; OUT=; CPUS=; MEM=; TIMEOUT=21600; CI=; DRY=; BWRAP=(); PREFLIGHT=()
while [ $# -gt 0 ]; do
  case $1 in
    --repo) REPO=$2; shift 2 ;;
    --benchmark) BENCH=$2; shift 2 ;;
    --workbench) WORKBENCH=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --cpus) CPUS=$2; shift 2 ;;
    --mem) MEM=$2; shift 2 ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    --ci-envelope) CI=1; shift ;;
    --bwrap) BWRAP=(--bwrap "$2"); shift 2 ;;
    --no-preflight) PREFLIGHT=(--no-preflight); shift ;;
    --dry-run) DRY=1; shift ;;
    *) usage ;;
  esac
done
[[ $PROBLEM =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$ ]] || { echo "bad PROBLEM_ID" >&2; exit 2; }
[[ $TIMEOUT =~ ^[0-9]+$ ]] || { echo "bad --timeout" >&2; exit 2; }
BENCH=${BENCH:-$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import verify_run; print(verify_run.CURRENT_BENCHMARK)' "$HERE")}
WORKBENCH=${WORKBENCH:-${LEANEVAL_WORKBENCH:-$HOME/projects/lean-eval-workbench}}
if [ -n "$CI" ]; then CPUS=${CPUS:-0-15}; MEM=${MEM:-31G}; ENVELOPE=ci
else CPUS=${CPUS:-0-15}; MEM=${MEM:-64G}; ENVELOPE=official; fi

if [ -d "$SOURCE" ] && [ -z "$REPO" ]; then
  KIND=directory; COMMIT=directory; LABEL=tree
else
  KIND=git; REPO=${REPO:-$(git rev-parse --show-toplevel)}
  COMMIT=$(git -C "$REPO" rev-parse --verify "$SOURCE^{commit}" 2>/dev/null || true)
  [ -n "$COMMIT" ] || [ ! -d "$REPO" ] || { echo "no commit $SOURCE in $REPO" >&2; exit 2; }
  LABEL=${COMMIT:0:12}; LABEL=${LABEL:-ref}
fi
ROOT=${LEH_VERIFY_ROOT:-$HOME/projects/lean-eval-verify}
RUN=${OUT:-$ROOT/$PROBLEM-$LABEL-$(date -u +%Y%m%dT%H%M%SZ)${CI:+-ci}}
NAME=leh-verify-$(printf %s "$PROBLEM" | tr -c 'A-Za-z0-9' '-' | cut -c1-40)

echo "verify $PROBLEM ($ENVELOPE) on benchmark $BENCH"
if [ "$KIND" = git ]; then echo "  source   git $REPO $SOURCE${COMMIT:+ -> $COMMIT}"; else echo "  source   directory $SOURCE"; fi
echo "  run      $RUN"
echo "  bounds   cpus $CPUS mem $MEM timeout ${TIMEOUT}s"
echo "  workbench $WORKBENCH"
if [ -n "$DRY" ]; then
  [ "$KIND" = directory ] && "$PY" "$HERE/verify_run.py" preflight "$(dirname "$RUN")" "$SOURCE" 2>/dev/null \
    | sed 's/^/  preflight /' || true
  exit 0
fi
[ ! -e "$RUN" ] || { echo "$RUN exists; use another --out" >&2; exit 2; }
mkdir -p "$(dirname "$RUN")"

STAGE=$(mktemp -d "$(dirname "$RUN")/.verify-source.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
if [ "$KIND" = git ]; then
  COMMIT=$("$PY" "$HERE/verify_run.py" export "$REPO" "$SOURCE" "$STAGE/src") || exit 2
  SRC=$STAGE/src
else
  SRC=$SOURCE
fi
"$PY" "$HERE/verify_run.py" prepare "$RUN" "$SRC" "$COMMIT" "$PROBLEM" "$TIMEOUT" --benchmark "$BENCH" \
  --workbench "$WORKBENCH" --envelope "$ENVELOPE" ${BWRAP[@]+"${BWRAP[@]}"} ${PREFLIGHT[@]+"${PREFLIGHT[@]}"} \
  --flow "verify verify_tree.sh ($ENVELOPE), source-only, lake test" \
  || exit 2
set +e
VCUR_PY=$PY bash "$HERE/run_verify.sh" "$RUN" "$MEM" "$CPUS" "$NAME"
"$PY" "$HERE/verify_run.py" receipt "$RUN"
