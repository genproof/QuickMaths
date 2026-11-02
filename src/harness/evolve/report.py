"""Aggregate round features (extract.py output) into the evolve report: what happened, and how the
progress gate's stop rule would have fared on this history at other thresholds.

Everything here is computed from the JSONL records alone, so a report can be rebuilt and compared
across harness versions. The gate replay is exact for stops (a lineage cut at round r loses rounds
r+1..n); the effect of running a lineage longer than it ran is unknown and never claimed.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

PASS = "official_local_pass"
# The feeder's stall rule (fleet/docs/FEEDER-POLICY.md): the open-rows estimate fell by less than
# 25% over two rounds and no RESIDUALS row was proved meanwhile.
CURRENT_KEEP_RATIO = 0.75
SWEEP = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
# A gate proposal needs at least this many lineages with estimates and this many rounds saved.
MIN_LINEAGES_FOR_GATE, MIN_ROUNDS_SAVED = 20, 5


@dataclass
class Lineage:
    host: str
    base: str
    problem: str
    rounds: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return any(r.get("verification") == PASS for r in self.rounds)

    @property
    def first(self) -> dict:
        return self.rounds[0]

    @property
    def order(self) -> tuple[int, str]:
        return (self.first.get("order") or 0, self.first.get("runner") or "")


def load(paths: Iterable[Path]) -> list[dict]:
    records = []
    for path in paths:
        for line in Path(path).read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def lineages(records: list[dict]) -> list[Lineage]:
    groups: dict[tuple[str, str], Lineage] = {}
    for record in records:
        key = (record.get("host") or "", record["lineage"])
        group = groups.setdefault(key, Lineage(key[0], key[1], record.get("problem") or ""))
        group.rounds.append(record)
    for group in groups.values():
        group.rounds.sort(key=lambda r: (r.get("round") or 0, r.get("order") or 0))
    return sorted(groups.values(), key=lambda g: (g.host, g.order))


def ratios(group: Lineage) -> list[float]:
    """est_r / est_{r-1} for consecutive rounds that both report a positive estimate."""
    out = []
    for before, after in zip(group.rounds, group.rounds[1:], strict=False):
        a, b = before.get("estimate"), after.get("estimate")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 0:
            out.append(b / a)
    return out


def stop_round(group: Lineage, keep_ratio: float) -> int | None:
    """Index (0-based) of the round after which the stall rule stops the lineage, or None. Rounds 1-2 always
    run; at round r >= 3 the rule looks at r-2 -> r: estimate kept above keep_ratio and no row proved."""
    rows = group.rounds
    for i in range(2, len(rows)):
        old, new = rows[i - 2].get("estimate"), rows[i].get("estimate")
        if not (isinstance(old, (int, float)) and isinstance(new, (int, float)) and old > 0):
            continue
        proved = (rows[i].get("proved_rows") or 0) > (rows[i - 2].get("proved_rows") or 0)
        if rows[i].get("verification") == PASS or rows[i].get("sorry") == 0:
            return None  # complete: the verify phase takes over, never a stall
        if new > keep_ratio * old and not proved:
            return i
    return None


def replay(groups: list[Lineage], keep_ratio: float) -> dict:
    """Stops the rule makes at `keep_ratio`: how many lineages that passed it would have cut before their
    passing round (false stops), and how many rounds of lineages that never passed it saves."""
    false_stops, saved, stopped = [], 0, 0
    for group in groups:
        index = stop_round(group, keep_ratio)
        if index is None:
            continue
        stopped += 1
        pass_index = next((i for i, r in enumerate(group.rounds) if r.get("verification") == PASS), None)
        if pass_index is not None and pass_index > index:
            false_stops.append(group.problem)
        elif pass_index is None:
            saved += len(group.rounds) - index - 1
    return {"keep_ratio": keep_ratio, "stopped": stopped, "false_stops": len(false_stops),
            "false_stop_problems": sorted(set(false_stops)), "rounds_saved": saved}


def ending(record: dict) -> str:
    """How a round ended: the supervisor's stop reason, else READY or a completed turn, else no record."""
    if record.get("stop_reason"):
        return str(record["stop_reason"])
    if record.get("ready"):
        return "ready_for_verification"
    if record.get("model_status"):
        return "turn_completed"
    return "no_record" if not record.get("phase") else str(record["phase"])


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def lane_calibration(lanes: list[dict]) -> dict:
    """Lines written against the architect's estimate, per program lane (records from `lane_records`)."""
    def ratio(r: dict) -> float | None:
        return round(r["lines"] / r["estimate"], 2) if r.get("estimate") else None
    done = [r for r in lanes if r.get("done") and ratio(r) is not None]
    open_ = [r for r in lanes if not r.get("done") and ratio(r) is not None]
    by_kind: dict[str, list[float]] = defaultdict(list)
    for r in done:
        by_kind[r.get("kind") or "unmarked"].append(ratio(r))
    refuted = sorted({s for r in lanes for s in (r.get("refuted") or [])})
    return {
        "lanes": len(lanes), "done_with_estimate": len(done),
        "median_ratio_done": median([ratio(r) for r in done]),
        "done_over_3x": sum(1 for r in done if ratio(r) > 3), "done_under_1x": sum(1 for r in done if ratio(r) < 1),
        "by_kind": {k: {"lanes": len(v), "median_ratio": median(v)} for k, v in sorted(by_kind.items())},
        "open_already_over_estimate": sorted(f"{r['program']}:{r['lane']}" for r in open_ if ratio(r) > 1),
        "refuted_statements": refuted,
        "worst_done": sorted(({"lane": f"{r['program']}:{r['lane']}", "estimate": r["estimate"], "lines": r["lines"],
                               "ratio": ratio(r)} for r in done), key=lambda x: -x["ratio"])[:8],
    }


def summarize(records: list[dict]) -> dict:
    lanes = [r for r in records if r.get("record") == "lane"]
    records = [r for r in records if r.get("record") != "lane"]
    groups = lineages(records)
    passed = [g for g in groups if g.passed]
    failed = [g for g in groups if not g.passed]
    problems = sorted({g.problem for g in groups})
    solved = sorted({g.problem for g in passed})
    by_problem: dict[str, list[Lineage]] = defaultdict(list)
    for group in groups:
        by_problem[group.problem].append(group)
    restarts = sorted(p for p in solved
                      if not sorted(by_problem[p], key=lambda g: g.order)[0].passed)
    seeded = [g for g in groups if g.first.get("seeded")]
    roles: dict[str, list[Lineage]] = defaultdict(list)
    for group in groups:
        roles[group.first.get("role") or "unknown"].append(group)
    tokens = sum(r.get("tokens") or 0 for r in records)
    model_hours = sum(r.get("elapsed") or 0 for r in records) / 3600
    pause_hours = sum(r.get("pause_seconds") or 0 for r in records) / 3600
    rounds_to_pass = [next(i for i, r in enumerate(g.rounds) if r.get("verification") == PASS) + 1 for g in passed]
    complete_unverified = sum(1 for r in records if r.get("sorry") == 0 and r.get("workspace")
                              and r.get("verification") not in (PASS, "rejected"))
    stop_reasons: dict[str, int] = defaultdict(int)
    for record in records:
        stop_reasons[ending(record)] += 1
    sweep = [replay(groups, ratio) for ratio in SWEEP]
    current = next(s for s in sweep if s["keep_ratio"] == CURRENT_KEEP_RATIO)
    safe = [s for s in sweep if s["false_stops"] == 0]
    best = max(safe, key=lambda s: (s["rounds_saved"], -s["keep_ratio"])) if safe else None
    return {
        "rounds": len(records), "lineages": len(groups), "problems": len(problems), "solved": len(solved),
        "solved_problems": solved, "tokens": tokens, "model_hours": round(model_hours, 1),
        "pause_hours": round(pause_hours, 1),
        "tokens_per_solved_problem": round(tokens / len(solved)) if solved else None,
        "rounds_to_pass_median": median(rounds_to_pass),
        "convergence": {
            "median_ratio_passed": median([x for g in passed for x in ratios(g)]),
            "median_ratio_unpassed": median([x for g in failed for x in ratios(g)]),
            "lineages_with_estimates": sum(1 for g in groups if ratios(g)),
        },
        "seeded": {"lineages": len(seeded), "passed": sum(g.passed for g in seeded)},
        "fresh": {"lineages": len(groups) - len(seeded), "passed": sum(g.passed for g in groups if not g.first.get("seeded"))},
        "roles": {role: {"lineages": len(gs), "passed": sum(g.passed for g in gs)} for role, gs in sorted(roles.items())},
        "solved_after_restart": restarts,
        "verification": {status: sum(1 for r in records if (r.get("verification") or "none") == status)
                         for status in sorted({r.get("verification") or "none" for r in records})},
        "complete_unverified_rounds": complete_unverified,
        "stop_reasons": dict(sorted(stop_reasons.items(), key=lambda kv: -kv[1])),
        "gate_replay": {"current": current, "sweep": sweep, "best_without_false_stops": best},
        "lane_calibration": lane_calibration(lanes),
    }


def proposals(summary: dict) -> list[str]:
    """Candidate policy changes the numbers support. Proposals only: a person approves each one."""
    out = []
    gate = summary["gate_replay"]
    current, best = gate["current"], gate["best_without_false_stops"]
    evidence = summary["convergence"]["lineages_with_estimates"]
    if evidence < MIN_LINEAGES_FOR_GATE:
        out.append(f"Only {evidence} lineage(s) report RESIDUALS estimates; the gate replay is not evidence for a "
                   f"threshold change below {MIN_LINEAGES_FOR_GATE}.")
        best = None
    if current["false_stops"]:
        out.append(f"The stall rule at keep ratio {current['keep_ratio']} would cut {current['false_stops']} lineage(s) "
                   f"that later passed ({', '.join(current['false_stop_problems'][:5])}); a looser ratio or a "
                   "row-level progress signal for these problems is worth a look.")
    if (best and best["keep_ratio"] != current["keep_ratio"]
            and best["rounds_saved"] >= max(MIN_ROUNDS_SAVED, current["rounds_saved"] + 1)):
        out.append(f"Keep ratio {best['keep_ratio']} stops no passing lineage on this history and saves "
                   f"{best['rounds_saved']} rounds (current {current['keep_ratio']}: {current['rounds_saved']}).")
    conv = summary["convergence"]
    if conv["median_ratio_passed"] is not None and conv["median_ratio_unpassed"] is not None \
            and conv["median_ratio_unpassed"] - conv["median_ratio_passed"] < 0.1:
        out.append("The remaining-work estimate barely separates passing from stuck lineages here "
                   f"(median ratio {conv['median_ratio_passed']} vs {conv['median_ratio_unpassed']}); do not tighten "
                   "the gate on it alone.")
    seeded, fresh = summary["seeded"], summary["fresh"]
    if seeded["lineages"] >= 5 and fresh["lineages"] >= 5:
        rs, rf = seeded["passed"] / seeded["lineages"], fresh["passed"] / fresh["lineages"]
        if rs > rf + 0.15:
            out.append(f"Seeded lineages passed {rs:.0%} vs fresh {rf:.0%}: prefer seeding retries with the best "
                       "finished files.")
    if summary["complete_unverified_rounds"]:
        out.append(f"{summary['complete_unverified_rounds']} round(s) ended sorry-free without an official verdict: "
                   "check that the verify phase picks them up.")
    cal = summary.get("lane_calibration") or {}
    if cal.get("done_with_estimate", 0) >= 5 and cal.get("median_ratio_done"):
        out.append(f"Finished program lanes wrote a median {cal['median_ratio_done']}x their estimates "
                   f"({cal['done_with_estimate']} lanes; {cal['done_over_3x']} over 3x, {cal['done_under_1x']} under 1x). "
                   "Ask architects to mark each lane `Kind: new` or `Kind: reuse` and to calibrate new-infrastructure "
                   "estimates on this ratio; a lane already over its estimate is a design-review signal.")
    if cal.get("refuted_statements"):
        out.append(f"Interface statements refuted by lanes: {', '.join(cal['refuted_statements'][:8])}. Audit shared "
                   "tool statements against every consumer's needs before the lanes start.")
    if summary["model_hours"] and summary["pause_hours"] > 0.25 * summary["model_hours"]:
        out.append(f"Usage-limit pauses took {summary['pause_hours']} h against {summary['model_hours']} model hours: "
                   "spread the load over more accounts or hand off paused lineages sooner.")
    return out


def markdown(summary: dict) -> str:
    s = summary
    lines = [
        "# Evolve report", "",
        f"{s['rounds']} rounds in {s['lineages']} lineages on {s['problems']} problems; {s['solved']} solved "
        f"({s['model_hours']} model hours, {s['pause_hours']} paused, {s['tokens']:,} tokens"
        + (f", {s['tokens_per_solved_problem']:,} per solved problem" if s["tokens_per_solved_problem"] else "") + ").",
        f"Median rounds to an official pass: {s['rounds_to_pass_median']}. Solved only after an earlier lineage "
        f"failed: {len(s['solved_after_restart'])} ({', '.join(s['solved_after_restart'][:8]) or '-'}).", "",
        "## Convergence", "",
        f"Median round-over-round ratio of the open-rows estimate: {s['convergence']['median_ratio_passed']} in "
        f"lineages that passed, {s['convergence']['median_ratio_unpassed']} in the rest "
        f"({s['convergence']['lineages_with_estimates']} lineages report estimates).", "",
        "## Gate replay (stall rule: estimate kept above the ratio over two rounds, no row proved)", "",
        "| keep ratio | lineages stopped | stopped but later passed | rounds saved in unpassed lineages |",
        "|---|---|---|---|",
    ]
    for row in s["gate_replay"]["sweep"]:
        mark = " (current)" if row["keep_ratio"] == CURRENT_KEEP_RATIO else ""
        lines.append(f"| {row['keep_ratio']}{mark} | {row['stopped']} | {row['false_stops']} | {row['rounds_saved']} |")
    lines += ["", "## Seeding and roles", "",
              f"Seeded lineages: {s['seeded']['passed']}/{s['seeded']['lineages']} passed; fresh: "
              f"{s['fresh']['passed']}/{s['fresh']['lineages']}.", "", "| role | lineages | passed |", "|---|---|---|"]
    lines += [f"| {role} | {v['lineages']} | {v['passed']} |" for role, v in s["roles"].items()]
    cal = s.get("lane_calibration") or {}
    if cal.get("lanes"):
        lines += ["", "## Lane estimate calibration (program lanes)", "",
                  f"{cal['done_with_estimate']} finished lanes with an estimate: median lines/estimate "
                  f"{cal['median_ratio_done']}; {cal['done_over_3x']} over 3x, {cal['done_under_1x']} under 1x. "
                  f"Open lanes already over their estimate: {len(cal['open_already_over_estimate'])}.", "",
                  "| kind | finished lanes | median ratio |", "|---|---|---|"]
        lines += [f"| {k} | {v['lanes']} | {v['median_ratio']} |" for k, v in cal["by_kind"].items()]
        if cal["refuted_statements"]:
            lines += ["", "Refuted interface statements: " + ", ".join(cal["refuted_statements"]) + "."]
    lines += ["", "## Verification and stops", "",
              "Verification: " + ", ".join(f"{k} {v}" for k, v in s["verification"].items()) + ".",
              f"Sorry-free rounds without an official verdict: {s['complete_unverified_rounds']}.",
              "Round endings: " + ", ".join(f"{k} {v}" for k, v in list(s["stop_reasons"].items())[:8]) + ".", "",
              "## Proposals (for review; nothing is applied automatically)", ""]
    lines += [f"- {p}" for p in proposals(s)] or ["- none"]
    return "\n".join(lines) + "\n"
