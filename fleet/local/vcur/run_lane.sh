#!/bin/bash
# run_lane.sh LANE CPUS MEM KEY... : official current verification (verify_candidate.py, benchmark 208b6b22) of
# each run KEY in turn, in $B/runs/KEY from $B/srcs/KEY and the KEY row of $B/list.tsv:
#   KEY  COMMIT  FILES  OTHER  FLEET_SECS  [PROBLEM_ID]  [TIMEOUT]
# PROBLEM_ID defaults to KEY (the manual rows), TIMEOUT to max(4 x FLEET_SECS, 3600).
# Appends "start" / "done" / "prep failed" lines to $B/lane-LANE.log; the done line carries RUN-DONE
# (rc, wall, cgroup peak, oom events) and "accepted <bool> receipt <sha16>". Never kills anything.
# Env: VCUR_BASE (default ~/lean-eval-vcur), VCUR_PY (python3.12), VCUR_PREP (prep_current.py next to this
# script), VCUR_RUN_VERIFY (the run wrapper `RUN_VERIFY V MEM CPUS NAME`; default verify's
# run_verify.sh next to this script, as verify_current.py installs it, or in the repository, else
# $VCUR_BASE/run_verify.sh).
B=${VCUR_BASE:-$HOME/lean-eval-vcur}
PY=${VCUR_PY:-python3.12}
HERE=$(cd "$(dirname "$0")" && pwd)
PREP=${VCUR_PREP:-$HERE/prep_current.py}
if [ -n "${VCUR_RUN_VERIFY:-}" ]; then RV=$VCUR_RUN_VERIFY
elif [ -f "$HERE/run_verify.sh" ]; then RV=$HERE/run_verify.sh
elif [ -f "$HERE/../../../verify/run_verify.sh" ]; then RV=$(cd "$HERE/../../../verify" && pwd)/run_verify.sh
else RV=$B/run_verify.sh; fi
export VCUR_PY="$PY"
cd "$B" || exit 1
[ -f "$RV" ] || { echo "$(date -u +%FT%TZ) no run wrapper $RV" >> "$B/lane-${1:-x}.log"; exit 1; }
LANE=$1; CPUS=$2; MEM=$3; shift 3
LOG=$B/lane-$LANE.log
col() { awk -v p="$1" -v c="$2" '$1==p {v=$c} END {print v}' list.tsv; }
for P in "$@"; do
  C=$(col "$P" 2); W=$(col "$P" 5); ID=$(col "$P" 6); T=$(col "$P" 7)
  if [ -z "$C" ]; then echo "$(date -u +%FT%TZ) no row $P" >> "$LOG"; continue; fi
  W=${W%%.*}; W=${W:-0}; ID=${ID:-$P}
  [ -n "$T" ] || T=$(( W * 4 > 3600 ? W * 4 : 3600 ))
  V=$B/runs/$P
  echo "$(date -u +%FT%TZ) start $P $C timeout $T" >> "$LOG"
  "$PY" "$PREP" "$V" "$B/srcs/$P" "$C" "$ID" "$T" >> "$LOG" 2>&1 || { echo "prep failed $P" >> "$LOG"; continue; }
  bash "$RV" "$V" "$MEM" "$CPUS" "vcur-$LANE-$(printf %s "$P" | tr -c 'A-Za-z0-9' '-' | cut -c1-64)"
  R=$("$PY" -c 'import json, sys; d = json.load(open(sys.argv[1])); print("accepted", d.get("accepted"), "receipt", str(d.get("receipt_sha256"))[:16])' "$V/status.json" 2>/dev/null || echo "accepted unknown")
  echo "$(date -u +%FT%TZ) done $P $(cat "$V/RUN-DONE" 2>/dev/null) $R" >> "$LOG"
done
echo "$(date -u +%FT%TZ) lane done" >> "$LOG"
