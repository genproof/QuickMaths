"""harness.evolve: features from a fake host layout, the report, the gate replay and the proposals."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from harness.cli import main
from harness.evolve import report
from harness.evolve.extract import lineage_of, pause_seconds, rounds

ROOT = Path(__file__).resolve().parents[1]


def rec(lineage, rnd, est, problem=None, verification="not_verified_snapshot_only", proved=0, sorry=3, **extra):
    return {"host": "h", "lineage": lineage, "round": rnd, "order": rnd, "problem": problem or lineage,
            "estimate": est, "proved_rows": proved, "sorry": sorry, "verification": verification,
            "workspace": True, "tokens": 1000, "elapsed": 3600, "pause_seconds": 0, **extra}


def history():
    shrinking = [rec("a", 1, 1000), rec("a", 2, 500), rec("a", 3, 200), rec("a", 4, 0, sorry=0, verification=report.PASS)]
    flat = [rec("b", i, e) for i, e in enumerate((1000, 950, 920, 910, 900), 1)]
    late = [rec("c", 1, 1000), rec("c", 2, 990), rec("c", 3, 980), rec("c", 4, 970),
            rec("c", 5, 0, sorry=0, verification=report.PASS)]
    retried = [rec("d1", 1, 800, problem="d"), rec("d1", 2, 790, problem="d"),
               rec("d2", 1, 400, problem="d", seeded=True, order=10), rec("d2", 2, 0, problem="d", sorry=0, order=11,
                                                                            verification=report.PASS, seeded=True)]
    return shrinking + flat + late + retried


def test_gate_replay_counts_false_stops_and_saved_rounds():
    groups = report.lineages(history())
    current = report.replay(groups, 0.75)
    assert current["false_stops"] == 1 and current["false_stop_problems"] == ["c"]
    assert current["rounds_saved"] == 2  # b stops after round 3 of 5
    loose = report.replay(groups, 0.5)
    assert loose["false_stops"] == 1  # c is flat at any ratio below 0.98
    assert report.replay(groups, 0.99)["false_stops"] == 0


def test_summary_and_markdown():
    summary = report.summarize(history())
    assert summary["solved_problems"] == ["a", "c", "d"]
    assert summary["solved_after_restart"] == ["d"]
    assert summary["convergence"]["median_ratio_passed"] < summary["convergence"]["median_ratio_unpassed"]
    assert summary["rounds_to_pass_median"] == 4
    text = report.markdown(summary)
    assert "| 0.75 (current) |" in text and "## Proposals" in text
    proposals = report.proposals(summary)
    assert any("later passed" in p for p in proposals)
    assert any("not evidence for a threshold change" in p for p in proposals)  # 5 lineages only
    assert summary["stop_reasons"] == {"no_record": len(history())}


def test_endings_and_unverified_rounds():
    rows = [rec("x", 1, 10, stop_reason="wall_limit"), rec("x", 2, 5, ready=True, model_status="awaiting_verifier"),
            rec("x", 3, 5, model_status="awaiting_verifier", verification=None)]
    summary = report.summarize(rows)
    assert summary["stop_reasons"] == {"wall_limit": 1, "ready_for_verification": 1, "turn_completed": 1}
    assert summary["verification"]["none"] == 1


def test_lineage_and_pause_parsing():
    assert lineage_of({"attempt_id": "x-r3"}) == ("x", 3)
    assert lineage_of({"attempt_id": "y", "auto_continue": {"base": "y0", "round": 2}}) == ("y0", 2)
    assert lineage_of({"attempt_id": "z"}) == ("z", 1)
    assert pause_seconds([{"started_at": 10.0, "ended_at": 70.0}, {"started_at": 5, "until": 15}, {}]) == 70.0


def test_extract_reads_queues_with_the_feeder_parsers(tmp_path):
    spec = importlib.util.spec_from_file_location("feeder_for_test", ROOT / "fleet/server/feeder.py")
    feeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feeder)
    queue = tmp_path / "campaigns/fleet-1-wave101"
    workspace = queue / "jobs/fleet-1-w101-001-p/workspace"
    (workspace / "Submission").mkdir(parents=True)
    (workspace / "Submission.lean").write_text("import Submission.A\ntheorem t : True := sorry\n")
    (workspace / "Submission/A.lean").write_text("theorem a : True := trivial\n")
    (workspace / "NOTES.md").write_text("## RESIDUALS\n| name | status | source | est. lines |\n|---|---|---|---|\n"
                                        "| `A` | open | x | 300 |\n| `B` | proved | y | 0 |\n")
    (queue / "manifest.json").write_text(json.dumps({"assignments": [
        {"attempt_id": "fleet-1-w101-001-p", "problem_id": "p", "kind": "fresh", "lineage_role": "FRESH",
         "auto_continue": {"base": "fleet-1-w101-001-p", "round": 1}, "wall_seconds": 100}]}))
    (queue / "state.json").write_text(json.dumps({"history": {"fleet-1-w101-001-p": {
        "phase": "done", "model": {"status": "awaiting_verifier", "observed_tokens": 5, "elapsed_seconds": 9,
                                   "pauses": [{"started_at": 0, "ended_at": 30}]},
        "verification": {"status": "rejected"}}}}))
    own = tmp_path / "own"
    own.mkdir()
    (own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "p", "ref_min_modules": 40}]}))
    [row] = list(rounds(tmp_path / "campaigns", own, feeder, "h1"))
    assert (row["sorry"], row["estimate"], row["open_rows"], row["proved_rows"]) == (1, 300, 1, 1)
    assert (row["role"], row["verification"], row["pause_seconds"], row["ref_min_modules"]) == ("FRESH", "rejected", 30, 40)
    assert row["lineage"] == "fleet-1-w101-001-p" and row["round"] == 1


def test_cli_report(tmp_path, capsys):
    data = tmp_path / "r.jsonl"
    data.write_text("".join(json.dumps(r) + "\n" for r in history()))
    assert main(["evolve", "report", str(data), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out/report.md").read_text().startswith("# Evolve report")
    assert json.loads((tmp_path / "out/summary.json").read_text())["solved"] == 3


def test_lane_records_and_calibration(tmp_path):
    from harness.evolve.extract import lane_records
    own = tmp_path / "own"
    base = tmp_path / "jobs/arch/workspace/Submission/Lanes"
    for lane, est, kind in (("Tool", "Estimate: 2.5k lines; critical lemma x", "Kind: new"),
                            ("Case", "Estimate: 1000", "Kind: reuse"), ("Open", "Estimate: 500", "")):
        (base / lane).mkdir(parents=True)
        (base / lane / "Main.lean").write_text(f"/-! CONTRACT\n{est}\n{kind}\n-/\ntheorem main : True := sorry\n")
    done = tmp_path / "jobs/tool/workspace/Submission/Lanes/Tool"
    done.mkdir(parents=True)
    (done / "A.lean").write_text("theorem a : True := trivial\n" * 10000)
    (done / "LaneNotes.lean").write_text("/- notes -/\n" * 500)  # not counted
    case = tmp_path / "jobs/case/workspace/Submission/Lanes/Case"
    case.mkdir(parents=True)
    (case / "A.lean").write_text("theorem a : True := trivial\n" * 300)
    (own / "programs").mkdir(parents=True)
    (own / "programs/demo.json").write_text(json.dumps({
        "base_job": str(tmp_path / "jobs/arch"), "refuted": {"ToolStatement": {"lane": "Case"}},
        "lanes": {"Tool": {"dir": "Submission/Lanes/Tool", "done": True, "done_job": str(tmp_path / "jobs/tool")},
                  "Case": {"dir": "Submission/Lanes/Case", "done": True, "done_job": str(tmp_path / "jobs/case")},
                  "Open": {"dir": "Submission/Lanes/Open", "done": False}}}))
    records = list(lane_records(own, "h"))
    by = {r["lane"]: r for r in records}
    assert (by["Tool"]["estimate"], by["Tool"]["kind"], by["Tool"]["lines"]) == (2500.0, "new", 10000)
    assert (by["Case"]["estimate"], by["Case"]["kind"], by["Case"]["lines"]) == (1000.0, "reuse", 300)
    assert by["Open"]["lines"] == 0 and by["Open"]["done"] is False
    summary = report.summarize(history() + records)
    cal = summary["lane_calibration"]
    assert cal["done_with_estimate"] == 2 and cal["done_over_3x"] == 1 and cal["done_under_1x"] == 1
    assert cal["by_kind"]["new"]["median_ratio"] == 4.0 and cal["refuted_statements"] == ["ToolStatement"]
    assert summary["rounds"] == len(history())  # lane records are not rounds
    text = report.markdown(summary)
    assert "## Lane estimate calibration" in text and "Refuted interface statements: ToolStatement" in text
    assert any("Audit shared tool statements" in p for p in report.proposals(summary))
