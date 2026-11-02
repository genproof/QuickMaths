"""Offline controls for the fleet feeder (no server, no model, no network)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("feeder", HERE.parent / "server/feeder.py")
feeder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feeder)

POLICY = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}


def queue_root(base: Path, wave: str) -> Path:
    root = base / f"fleet-1-{wave}"
    root.mkdir(parents=True)
    (root / "HOST").write_text("host-1\n")
    (root / "manifest.json").write_text(json.dumps({
        "campaign_id": root.name, "host": "host-1", "requested_config": POLICY,
        "excluded_problem_ids": [], "assignments": []}))
    (root / "state.json").write_text(json.dumps({"status": "idle_waiting_for_assignments",
                                                  "history": {}}))
    return root


class FeederTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.own = base / "own"
        (self.own / "briefs").mkdir(parents=True)
        (self.own / "briefs" / "COMMON.md").write_text("common brief\n")
        (self.own / "briefs" / "green_tao.md").write_text("green tao intel\n")
        shutil.copytree(HERE.parent / "server/briefs/roles", self.own / "briefs" / "roles")
        self.r1 = queue_root(base / "campaigns", "wave101")
        self.r2 = queue_root(base / "campaigns", "wave102")
        (self.own / "config.json").write_text(json.dumps({
            "runners": [str(self.r1), str(self.r2)], "min_free_gb": 0}))
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao"}, {"problem_id": "hadwiger", "max_rounds": 2},
            {"problem_id": "linnik", "disabled": True}, {"problem_id": "chen_theorem"}]}))
        patches = [mock.patch.object(feeder, "OWN", self.own), mock.patch.object(feeder, "HOST", "host-1"),
                   mock.patch.object(feeder, "controller_pids", lambda root: [1]),
                   mock.patch.object(feeder, "our_solved", lambda alerts: {"chen_theorem"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, root):
        return json.loads((root / "manifest.json").read_text())

    def set_history(self, root, history, status="idle_waiting_for_assignments"):
        (root / "state.json").write_text(json.dumps({"status": status, "history": history}))

    def test_credential_halt_stops_and_blocks_every_runner_of_the_account(self):
        self.set_history(self.r1, {}, status="halted_credential_access")
        self.set_history(self.r2, {})
        t = feeder.Tick()
        t.views = {root: feeder.runner_view(root, t.cfg, t.now) for root in t.runners}
        t.blocks = {}
        feeder.credential_halts(t)
        self.assertTrue((self.r2 / "STOP").exists() and (self.r1 / "STOP").exists())  # one shared default account
        self.assertEqual(list(t.blocks.values())[0]["window"], "credential_access")
        self.assertFalse(feeder.has_quota(t, self.r2))

    def test_assigns_two_targets_in_priority_order_with_briefs(self):
        feeder.tick()
        a1 = self.manifest(self.r1)["assignments"]
        a2 = self.manifest(self.r2)["assignments"]
        self.assertEqual([a["problem_id"] for a in a1], ["green_tao"])
        self.assertEqual([a["problem_id"] for a in a2], ["hadwiger"])
        self.assertEqual(a1[0]["attempt_id"], "fleet-1-w101-001-green_tao")
        self.assertEqual(a2[0]["auto_continue"]["max_rounds"], 2)
        self.assertEqual(a1[0]["verify_policy"], "if_ready")
        brief = (self.r1 / a1[0]["brief_file"]).read_text()
        self.assertIn("common brief", brief)
        self.assertIn("green tao intel", brief)
        # second tick: nothing new while both lineages are pending
        feeder.tick()
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 1)

    def test_rounds_keep_claim_then_pass_frees_runner_and_skips_ours(self):
        feeder.tick()
        base = "fleet-1-w101-001-green_tao"
        m = self.manifest(self.r1)
        # round 1 finished without pass; controller appended round 2
        m["assignments"].append(dict(m["assignments"][0], attempt_id=base + "-r2",
                                     kind="continuation", from_job=base))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.set_history(self.r1, {base: {"phase": "done", "verification": {
            "status": "not_verified_snapshot_only"}}})
        feeder.tick()
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertEqual(claims["green_tao"]["status"], "running")
        self.set_history(self.r1, {
            base: {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}},
            base + "-r2": {"phase": "done", "verification": {"status": "official_local_pass"}}})
        feeder.tick()
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertEqual(claims["green_tao"]["status"], "passed")
        self.assertEqual(claims["green_tao"]["passed_attempt"], base + "-r2")
        # linnik disabled, chen already ours -> skipped
        self.assertEqual(claims["chen_theorem"]["status"], "skipped_already_ours")
        self.assertNotIn("linnik", claims)
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 2)

    def test_operator_enqueue_interleaves_with_running_claim(self):
        feeder.tick()
        targets = json.loads((self.own / "targets.json").read_text())
        targets["targets"].append({"problem_id": "duffin_schaeffer"})
        (self.own / "targets.json").write_text(json.dumps(targets))
        base = feeder.enqueue("duffin_schaeffer", "wave101")
        names = [a["attempt_id"] for a in self.manifest(self.r1)["assignments"]]
        self.assertEqual(names, ["fleet-1-w101-001-green_tao", base])
        self.assertEqual(base, "fleet-1-w101-002-duffin_schaeffer")
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertEqual(claims["duffin_schaeffer"]["runner"], str(self.r1))
        with self.assertRaises(SystemExit):
            feeder.enqueue("duffin_schaeffer", "wave102")
        with self.assertRaises(SystemExit):
            feeder.enqueue("linnik", "wave102")  # disabled target
        feeder.tick()  # both claims stay running, nothing else assigned
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 2)

    def test_exhausted_and_failed(self):
        info = feeder.lineage([{"attempt_id": "b"}, {"attempt_id": "b-r2"}],
                              {"b": {"phase": "done"}, "b-r2": {"phase": "done"}}, "b")
        self.assertEqual(feeder.classify(info), "exhausted")
        info = feeder.lineage([{"attempt_id": "b"}], {"b": {"phase": "failed"}}, "b")
        self.assertEqual(feeder.classify(info), "failed")
        info = feeder.lineage([{"attempt_id": "b"}, {"attempt_id": "b-r2"}],
                              {"b": {"phase": "done"}}, "b")
        self.assertIsNone(feeder.classify(info))

    def write_ws(self, root, job, sorry, lines, full_ok=None):
        ws = root / "jobs" / job / "workspace"
        (ws / "Submission").mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"theorem t{i} : True := trivial" for i in range(lines))
        holes = "\n".join(f"theorem s{i} : True := sorry" for i in range(sorry))
        (ws / "Submission.lean").write_text("-- sorry in a comment\n/- and sorry here -/\n"
                                            + body + "\n" + holes + "\n")
        if full_ok is not None:
            (ws / ".feedback").mkdir(exist_ok=True)
            (ws / ".feedback" / "step-000001.json").write_text(json.dumps(
                {"action": "build", "query": "Submission",
                 "outcome_class": "check_succeeded" if full_ok else "lean_diagnostic"}))
        return ws

    def test_proof_stats_and_round_progress(self):
        a = self.write_ws(self.r1, "a", sorry=2, lines=10)
        self.assertEqual(feeder.proof_stats(a), {"sorry": 2, "lines": 12})
        self.assertTrue(feeder.round_progress(a, self.write_ws(self.r1, "b", 1, 10))["progress"])
        self.assertTrue(feeder.round_progress(a, self.write_ws(self.r1, "c", 2, 400, True))["progress"])
        self.assertFalse(feeder.round_progress(a, self.write_ws(self.r1, "d", 2, 400, False))["progress"])
        self.assertFalse(feeder.round_progress(a, self.write_ws(self.r1, "e", 2, 20, True))["progress"])

    def test_progress_gate_stops_next_round_after_stalled_round(self):
        feeder.tick()
        base = "fleet-1-w101-001-green_tao"
        m = self.manifest(self.r1)
        first = m["assignments"][0]
        for n in (2, 3, 4):
            m["assignments"].append(dict(first, attempt_id=f"{base}-r{n}", kind="continuation",
                                         from_job=base if n == 2 else f"{base}-r{n - 1}"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.write_ws(self.r1, base, sorry=3, lines=10)
        self.write_ws(self.r1, base + "-r2", sorry=3, lines=12, full_ok=True)  # stalled round 2
        (self.r1 / "jobs" / (base + "-r3")).mkdir(parents=True)
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        (self.r1 / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(self.r1 / "jobs" / (base + "-r3")),
            "history": {base: done, base + "-r2": done}}))
        feeder.tick()
        assignments = self.manifest(self.r1)["assignments"]
        review = "fleet-1-w101-002-green_tao"
        self.assertEqual([a["attempt_id"] for a in assignments],
                         [base, base + "-r2", base + "-r3", review])
        self.assertIsNone(assignments[2]["auto_continue"])
        self.assertTrue((self.r1 / "jobs" / (base + "-r3") / "STOP").exists())
        # the stalled lineage gets one route-review lineage seeded with round 2's files
        overlay = self.r1 / assignments[3]["overlay_dir"]
        self.assertEqual(overlay.name, f"green_tao-from-{base}-r2")
        brief = self.r1 / assignments[3]["brief_file"]
        self.assertIn("route review", brief.read_text())
        claims = json.loads((self.own / "claims.json").read_text())
        claim = claims["green_tao"]
        self.assertEqual((claim["base"], claim["status"], claim["rounds"]), (review, "running", 1))
        self.assertIn("no progress", claim["route_review"]["reason"])
        # once round 1 of the review has run, later rounds get the plain brief
        m = self.manifest(self.r1)
        m["assignments"].append(dict(m["assignments"][3], attempt_id=review + "-r2", kind="continuation"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.set_history(self.r1, {base: done, base + "-r2": done, review: done})
        feeder.tick()
        self.assertNotIn("route review", brief.read_text())

    def test_progress_gate_never_stops_a_ci_fit_lineage(self):
        # 2026-10-17: the sorry-based gate stopped poincare's CI_FIT lineage (sorry 0 -> 0) mid-work
        feeder.tick()
        base = "fleet-1-w101-001-green_tao"
        claims = json.loads((self.own / "claims.json").read_text())
        claims["green_tao"]["role"] = "CI_FIT"
        (self.own / "claims.json").write_text(json.dumps(claims))
        m = self.manifest(self.r1)
        first = m["assignments"][0]
        for n in (2, 3):
            m["assignments"].append(dict(first, attempt_id=f"{base}-r{n}", kind="continuation",
                                         from_job=base if n == 2 else f"{base}-r{n - 1}"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.write_ws(self.r1, base, sorry=0, lines=10)
        self.write_ws(self.r1, base + "-r2", sorry=0, lines=12, full_ok=True)
        (self.r1 / "jobs" / (base + "-r3")).mkdir(parents=True)
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        (self.r1 / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(self.r1 / "jobs" / (base + "-r3")),
            "history": {base: done, base + "-r2": done}}))
        feeder.tick()
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r1)["assignments"]],
                         [base, base + "-r2", base + "-r3"])
        self.assertFalse((self.r1 / "jobs" / (base + "-r3") / "STOP").exists())

    def test_frontier_kinds(self):
        a = self.write_ws(self.r1, "a", sorry=2, lines=10)
        b = self.write_ws(self.r1, "b", sorry=2, lines=12, full_ok=True)
        self.assertEqual(feeder.holes(a), ["Submission.lean:s0", "Submission.lean:s1"])
        self.assertFalse(feeder.round_progress(a, b)["progress"])
        # same sorry count, but the agent split/replaced a residual in its RESIDUALS table
        (a / "NOTES.md").write_text("# HANDOFF\nx\n## RESIDUALS\n| name | status | source | est. lines |\n"
                                    "|---|---|---|---|\n| `Core` | open | Thm 3 | 9000 |\n")
        (b / "NOTES.md").write_text("# HANDOFF\n## RESIDUALS\n| name | status | source | est. lines |\n"
                                    "|---|---|---|---|\n| Core | replaced | Thm 3 | - |\n"
                                    "| CoreSmallCase | open | Thm 3, k=2 | 2000 |\n## Next\n| Other | open | x | 1 |\n")
        self.assertEqual(feeder.open_residuals(a), ["Core"])
        self.assertEqual(feeder.open_residuals(b), ["CoreSmallCase"])
        # the estimate of the open rows fell (9000 -> 2000): residual progress
        self.assertEqual(feeder.round_progress(a, b)["kind"], "residual")
        # policy 2026-10-14: a renamed sorry'd declaration alone is churn, not progress (it was
        # `residual` before); a decomposition (more sorry, new holes, passing build) is `split`
        c = self.write_ws(self.r1, "c", sorry=0, lines=12, full_ok=True)
        (c / "Submission.lean").write_text((c / "Submission.lean").read_text()
                                           + "theorem t0' : True := sorry\ntheorem u : True := sorry\n")
        self.assertFalse(feeder.round_progress(a, c)["progress"])
        (c / "Submission.lean").write_text((c / "Submission.lean").read_text() + "theorem v : True := sorry\n")
        self.assertEqual(feeder.round_progress(a, c)["kind"], "split")
        d = self.write_ws(self.r1, "d", sorry=0, lines=12, full_ok=False)
        (d / "Submission.lean").write_text((c / "Submission.lean").read_text())
        self.assertFalse(feeder.round_progress(a, d)["progress"])
        # lane scope: only the lane's files and the lane's own module build count
        lane = self.lane_ws(self.r1, "lane", ["Alpha", "Beta"])
        ws2 = self.lane_ws(self.r1, "lane2", ["Alpha", "Beta"], solved=["Alpha"], steps=False)
        (ws2 / ".feedback").mkdir()
        (ws2 / ".feedback" / "step-000001.json").write_text(json.dumps(
            {"action": "build", "query": "Submission.Lanes.Alpha.Main", "outcome_class": "check_succeeded"}))
        got = feeder.round_progress(lane, ws2, "Submission/Lanes/Alpha")
        self.assertEqual((got["kind"], got["before"]["sorry"], got["after"]["sorry"]), ("sorry", 1, 0))
        self.assertFalse(feeder.round_progress(lane, ws2, "Submission/Lanes/Beta")["progress"])

    def test_two_rounds_of_lines_only_stop_the_lineage(self):
        feeder.tick()
        base = "fleet-1-w101-001-green_tao"
        m = self.manifest(self.r1)
        first = m["assignments"][0]
        for n in (2, 3, 4):
            m["assignments"].append(dict(first, attempt_id=f"{base}-r{n}", kind="continuation"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.write_ws(self.r1, base, sorry=1, lines=10)
        self.write_ws(self.r1, base + "-r2", sorry=1, lines=400, full_ok=True)
        self.write_ws(self.r1, base + "-r3", sorry=1, lines=800, full_ok=True)
        (self.r1 / "jobs" / (base + "-r4")).mkdir(parents=True)
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        claims = {"green_tao": dict(json.loads((self.own / "claims.json").read_text())["green_tao"],
                                    route_review={"from": "earlier"})}
        (self.own / "claims.json").write_text(json.dumps(claims))
        (self.r1 / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(self.r1 / "jobs" / (base + "-r4")),
            "history": {base: done, base + "-r2": done, base + "-r3": done}}))
        feeder.tick()
        claim = json.loads((self.own / "claims.json").read_text())["green_tao"]
        self.assertEqual([claim["progress"][base + f"-r{n}"]["kind"] for n in (2, 3)], ["lines", "lines"])
        self.assertIn("frontier unchanged", claim["stopped_no_progress"])
        self.assertTrue((self.r1 / "jobs" / (base + "-r4") / "STOP").exists())
        # the route review was already used: no new lineage
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 4)

    def test_lane_writing_much_code_progresses_without_a_recorded_build(self):
        def lane_ws(job, sorry, lines):
            ws = self.r1 / "jobs" / job / "workspace"
            (ws / "Submission/Lanes/X").mkdir(parents=True)
            (ws / "Submission.lean").write_text("theorem a : True := trivial\n")
            body = "\n".join(f"theorem t{i} : True := trivial" for i in range(lines))
            holes = "\n".join(f"theorem s{i} : True := sorry" for i in range(sorry))
            (ws / "Submission/Lanes/X/Main.lean").write_text(body + "\n" + holes + "\n")
            return ws
        a = lane_ws("la", 1, 10)
        p = feeder.round_progress(a, lane_ws("lb", 2, 1000), "Submission/Lanes/X")
        self.assertEqual((p["progress"], p["kind"]), (True, "lines"))
        self.assertFalse(feeder.round_progress(a, lane_ws("lc", 2, 400), "Submission/Lanes/X")["progress"])
        self.assertFalse(feeder.round_progress(a, lane_ws("ld", 2, 1000))["progress"])  # not a lane

    def test_lines_only_rounds_do_not_stop_a_program_lane(self):
        names = ["b", "b-r2", "b-r3", "b-r4"]
        info = {"names": names, "entries": [(n, {"phase": "done"}) for n in names[:3]], "pending": ["b-r4"]}
        lines = {"progress": True, "kind": "lines", "after": {"sorry": 1}}
        for scope in ("Submission/Lanes/X", None):
            claim = {"scope": scope, "progress": {n: dict(lines) for n in names[:3]}}
            with mock.patch.object(feeder, "stop_lineage") as stop:
                reason = feeder.gate_lineage(self.r1, claim, info, {})
            self.assertEqual((reason is None, stop.called), (scope is not None, scope is None))

    def test_a_lane_needs_two_rounds_without_progress(self):
        names = ["b", "b-r2", "b-r3", "b-r4"]
        info = {"names": names, "entries": [(n, {"phase": "done"}) for n in names[:3]], "pending": ["b-r4"]}
        none = {"progress": False, "kind": None, "after": {"sorry": 1}}
        some = {"progress": True, "kind": "sorry", "after": {"sorry": 1}}
        for scope, before, stops in (("Submission/Lanes/X", some, False), ("Submission/Lanes/X", none, True),
                                     (None, some, True)):
            claim = {"scope": scope, "progress": {"b": some, "b-r2": dict(before), "b-r3": dict(none)}}
            with mock.patch.object(feeder, "stop_lineage") as stop:
                reason = feeder.gate_lineage(self.r1, claim, info, {})
            self.assertEqual((reason is not None, stop.called), (stops, stops), (scope, before))

    def test_lane_round_lost_with_its_supervisor_is_held_not_rearchitected(self):
        # 2026-10-16 05:35Z: an external SIGTERM of all host-2 runner services made every lane round
        # "fail" and the feeder re-architected linnik and wilkies
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        lane_b = self.manifest(self.r2)["assignments"][-1]
        self.lane_ws(self.r2, lane_b["attempt_id"], ["Alpha", "Beta"])
        killed = {"phase": "failed", "error": "RuntimeError: Supervisor produced no result; job requires reconciliation"}
        self.set_history(self.r2, {lane_b["attempt_id"]: killed})
        status = feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "lanes")  # no re-architecture
        self.assertNotIn("rearchitect", prog)
        self.assertEqual(prog["lanes"]["Beta"]["claim"]["status"], "held")
        self.assertTrue(any("HELD" in a for a in status["alerts"]))
        # a genuine failure still goes back to the architect
        other = {"phase": "failed", "error": "RuntimeError: model refused"}
        self.assertFalse(feeder.infra_failure(other))
        self.assertTrue(feeder.infra_failure(killed))

    def test_large_problem_stalled_after_route_review_becomes_program(self):
        cfg = json.loads((self.own / "config.json").read_text())
        cfg["program_min_ref_modules"] = 250
        (self.own / "config.json").write_text(json.dumps(cfg))
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 600}]}))
        base = "fleet-1-w101-009-green_tao"
        m = self.manifest(self.r1)
        m["assignments"] = [{"attempt_id": base, "problem_id": "green_tao"}]
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        job = self.write_ws(self.r1, base, sorry=1, lines=5).parent
        (job / "result.json").write_text("{}")
        (self.own / "claims.json").write_text(json.dumps({"green_tao": {
            "status": "running", "runner": str(self.r1), "base": base, "route_review": {"from": "x"}}}))
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {base: done})
        feeder.tick()
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertEqual(claims["green_tao"]["status"], "program")
        prog = json.loads((self.own / "programs" / "green_tao.json").read_text())
        self.assertEqual((prog["status"], prog["seed_job"]), ("architect", str(job)))
        feeder.tick()  # the architect role is assigned on the next tick
        self.assertIn("architect", json.loads((self.own / "programs" / "green_tao.json").read_text()))

    def test_retries_have_no_size_cap_but_a_count(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 900},
            {"problem_id": "hadwiger", "ref_min_modules": 300},
            {"problem_id": "linnik", "ref_min_modules": 10}]}))
        old = "fleet-1-w102-006-hadwiger"
        self.write_ws(self.r2, old, sorry=1, lines=5)
        self.write_ws(self.r2, "gt", sorry=1, lines=5)
        (self.own / "claims.json").write_text(json.dumps({
            "hadwiger": {"status": "exhausted", "runner": str(self.r2), "base": old, "rounds": 1},
            "green_tao": {"status": "exhausted", "runner": str(self.r2), "base": "gt", "rounds": 1,
                          "retries": 2},
            "linnik": {"status": "exhausted", "runner": str(self.r2), "base": "gt", "rounds": 1,
                       "program_status": "failed"}}))
        feeder.tick()
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertEqual((claims["hadwiger"]["kind"], claims["hadwiger"]["retries"]), ("retry", 1))
        self.assertEqual(claims["hadwiger"]["role"], "ROUTE_REVIEW")  # a retry re-decides the route
        root = Path(claims["hadwiger"]["runner"])
        retry = next(a for a in self.manifest(root)["assignments"]
                     if a["attempt_id"] == claims["hadwiger"]["base"])
        self.assertIn("ROUTE REVIEW", (root / retry["brief_file"]).read_text())
        self.assertEqual(claims["green_tao"]["status"], "exhausted")  # max_retries reached
        self.assertEqual(claims["linnik"]["status"], "exhausted")  # failed program: no retry

    def test_retry_seeds_latest_files_once(self):
        cfg = json.loads((self.own / "config.json").read_text())
        cfg["retry_max_ref_modules"] = 200
        (self.own / "config.json").write_text(json.dumps(cfg))
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 300},
            {"problem_id": "hadwiger", "ref_min_modules": 37}]}))
        old = "fleet-1-w102-006-hadwiger"
        ws = self.write_ws(self.r2, old + "-r2", sorry=1, lines=5)
        (ws / "NOTES.prev.md").write_text("handoff")
        (self.own / "claims.json").write_text(json.dumps({"hadwiger": {
            "status": "exhausted", "runner": str(self.r2), "base": old, "rounds": 2}}))
        feeder.tick()
        a1 = self.manifest(self.r1)["assignments"]
        self.assertEqual((a1[0]["problem_id"], a1[0]["kind"]), ("hadwiger", "fresh"))
        overlay = self.r1 / a1[0]["overlay_dir"]
        self.assertTrue((overlay / "Submission.lean").is_file())
        self.assertEqual((overlay / "NOTES.md").read_text(), "handoff")
        claims = json.loads((self.own / "claims.json").read_text())
        self.assertTrue(claims["hadwiger"]["retried"])
        self.assertEqual(self.manifest(self.r2)["assignments"][0]["problem_id"], "green_tao")

    def test_tactics_and_handoff_tables(self):
        ws = self.write_ws(self.r1, "t", sorry=1, lines=3)
        self.assertEqual((feeder.tactics(ws), feeder.handoff_missing(ws)), ([], list(feeder.HANDOFF_FIELDS)))
        (ws / "NOTES.prev.md").write_text(
            "# HANDOFF\nNext step: Foo in X.lean\nActive tactic: T1\nLast failure: goal\nDo not retry: y\n"
            "## TACTICS\n| tactic | status | target | expected signal | evidence |\n|---|---|---|---|---|\n"
            "| T1 special case k = 2 | failed | Core | Core closed | stuck on norms |\n"
            "| `T6 coordinates` | trying | Core | - | - |\n## Other\n| T9 | trying | x | - | - |\n")
        self.assertEqual(feeder.tactics(ws), [("T1 special case k = 2", "failed"), ("T6 coordinates", "trying")])
        self.assertEqual(feeder.handoff_missing(ws), [])
        got = feeder.round_progress(self.write_ws(self.r1, "u", sorry=2, lines=3), ws)
        self.assertEqual((got["kind"], got["tactics"], got["handoff_missing"]), ("sorry", ["T6 coordinates"], []))

    def test_route_review_brief_carries_measured_history_and_library(self):
        shutil.copy(HERE.parent / "server/briefs/TACTICS.md", self.own / "briefs" / "TACTICS.md")
        feeder.tick()
        base = "fleet-1-w101-001-green_tao"
        m = self.manifest(self.r1)
        for n in (2, 3):
            m["assignments"].append(dict(m["assignments"][0], attempt_id=f"{base}-r{n}", kind="continuation"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        self.write_ws(self.r1, base, sorry=3, lines=10)
        ws2 = self.write_ws(self.r1, base + "-r2", sorry=3, lines=12, full_ok=True)
        (ws2 / "NOTES.md").write_text("## TACTICS\n| tactic | status | target | signal | evidence |\n"
                                      "|---|---|---|---|---|\n| T4 split Core | trying | Core | - | - |\n")
        (self.r1 / "jobs" / (base + "-r3")).mkdir(parents=True)
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        (self.r1 / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(self.r1 / "jobs" / (base + "-r3")),
            "history": {base: done, base + "-r2": done}}))
        feeder.tick()
        review = self.manifest(self.r1)["assignments"][-1]
        brief = (self.r1 / review["brief_file"]).read_text()
        self.assertIn(f"- {base}-r2: no progress; sorry 3 -> 3; active tactic: T4 split Core", brief)
        self.assertIn("TACTICS table of the restored NOTES.md: T4 split Core (trying).", brief)
        self.assertIn("T7 Certificate plus checker", brief)  # the library for a route decision
        self.assertNotIn("{history}", brief)
        claim = json.loads((self.own / "claims.json").read_text())["green_tao"]
        self.assertEqual(claim["history"], [f"- {base}-r2: no progress; sorry 3 -> 3; active tactic: T4 split Core"])
        # the per-tick brief refresh keeps the fields (no KeyError, no lost history)
        feeder.tick()
        self.assertEqual((self.r1 / review["brief_file"]).read_text(), brief)
        # rounds after the first get the plain brief (no role, no library)
        common = feeder.brief_text("green_tao")
        self.assertNotIn("Tactic library", common)

    def test_every_retry_reviews_the_route_with_the_history_so_far(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        old = "fleet-1-w102-007-hadwiger"
        self.write_ws(self.r2, old + "-r2", sorry=1, lines=5)
        earlier = ["- fleet-1-w102-003-hadwiger-r2: no progress; sorry 2 -> 2; active tactic: T1"]
        (self.own / "claims.json").write_text(json.dumps({"hadwiger": {
            "status": "exhausted", "runner": str(self.r2), "base": old, "rounds": 2, "retries": 1,
            "route_review": {"from": "x"}, "history": earlier,
            "progress": {old + "-r2": {"kind": None, "before": {"sorry": 1}, "after": {"sorry": 1},
                                       "tactics": ["T3 other proof"]}}}}))
        feeder.tick()
        claim = json.loads((self.own / "claims.json").read_text())["hadwiger"]
        self.assertEqual((claim["role"], claim["retries"]), ("ROUTE_REVIEW", 2))
        self.assertEqual(claim["history"], earlier + [
            f"- {old}-r2: no progress; sorry 1 -> 1; active tactic: T3 other proof"])
        root = Path(claim["runner"])
        retry = next(a for a in self.manifest(root)["assignments"] if a["attempt_id"] == claim["base"])
        brief = (root / retry["brief_file"]).read_text()
        self.assertIn("active tactic: T1", brief)
        self.assertIn("active tactic: T3 other proof", brief)

    def test_rejected_architecture_goes_back_to_the_architect_once(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.assertEqual(json.loads(prog_path.read_text())["architect"]["contract"], feeder.ARCHITECT_CONTRACT)
        self.assertIn("Sanity.lean", (self.r1 / arch["brief_file"]).read_text())
        # the architect forgot the contract: one fix round on its files, with the reason
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"], contract=False)
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual((prog["status"], prog["architect_fixes"]), ("architect", 1))
        self.assertIn("contract fields", prog["architect_fix"]["reason"])
        fix = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
               if a["attempt_id"] == prog["architect"]["base"]][0]
        root = self.r1 if fix in self.manifest(self.r1)["assignments"] else self.r2
        brief = (root / fix["brief_file"]).read_text()
        self.assertIn("controller rejected the previous architecture", brief)
        self.assertIn("contract fields", brief)
        self.assertTrue((root / fix["overlay_dir"] / "Submission/Lanes/Alpha/Main.lean").is_file())
        # the fixed split is accepted
        self.lane_ws(root, fix["attempt_id"], ["Alpha", "Beta"])
        history = json.loads((root / "state.json").read_text())["history"]
        self.set_history(root, dict(history, **{fix["attempt_id"]: done}))
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "lanes")
        self.assertNotIn("architect_fix", prog)

    def lane_ws(self, root, job, lanes, solved=(), steps=True, contract=True):
        ws = root / "jobs" / job / "workspace"
        lanes_dir = ws / "Submission" / "Lanes"
        lanes_dir.mkdir(parents=True, exist_ok=True)
        imports = "import Submission.Lanes.Reduction\n" + ("import Submission.Lanes.Sanity\n" if contract else "")
        (ws / "Submission.lean").write_text(imports + "theorem hole : True := trivial\n")
        (lanes_dir / "Interfaces.lean").write_text("def Submission.Lanes.AStatement : Prop := True\n")
        (lanes_dir / "Reduction.lean").write_text("theorem red : True := trivial\n")
        if contract:
            (lanes_dir / "Sanity.lean").write_text("".join(
                f"theorem Submission.Lanes.{lane}.sanity : True := trivial\n" for lane in lanes))
        block = ("CONTRACT\nStatement: x\nUsed by: red\nSanity: s\nEstimate: 10\nSource: paper Thm 2\n"
                 "Strength: assumes the hole's case n=1\nReuse: none (searched: bar, baz)\n" if contract else "")
        files = {}
        for lane in lanes:
            (lanes_dir / lane).mkdir(exist_ok=True)
            body = "trivial" if lane in solved else "sorry"
            main = lanes_dir / lane / "Main.lean"
            main.write_text(f"/-! plan\n{block}-/\ntheorem Submission.Lanes.{lane}.main : "
                            f"Submission.Lanes.{lane}Statement := {body}\n")
            files[f"Submission/Lanes/{lane}/Main.lean"] = feeder.sha256_file(main)
        if steps:
            (ws / ".feedback").mkdir(exist_ok=True)
            (ws / ".feedback" / "step-000001.json").write_text(json.dumps(
                {"action": "build", "query": "Submission", "outcome_class": "check_succeeded",
                 "source_files": files}))
        return ws

    def test_validate_architecture_and_lane_done(self):
        good = self.lane_ws(self.r1, "arch", ["Alpha", "Beta"])
        self.assertEqual(feeder.validate_architecture(good), (["Alpha", "Beta"], "ok"))
        self.assertEqual(feeder.validate_architecture(good, 2), (["Alpha", "Beta"], "ok"))
        # contract 2: lane CONTRACT fields, one sanity theorem per lane, Sanity imported
        old = self.lane_ws(self.r1, "old", ["Alpha", "Beta"], contract=False)
        self.assertEqual(feeder.validate_architecture(old)[1], "ok")  # architects assigned before
        self.assertIn("contract fields", feeder.validate_architecture(old, 2)[1])
        sanity = good / "Submission/Lanes/Sanity.lean"
        sanity.write_text("theorem Submission.Lanes.Alpha.sanity : True := trivial\n")
        self.assertIn("['Beta']", feeder.validate_architecture(good, 2)[1])
        sanity.write_text("theorem Submission.Lanes.Alpha.sanity : True := trivial\n"
                          "theorem Submission.Lanes.Beta.sanity : True := sorry\n")
        self.assertIn("sorry outside lane stubs", feeder.validate_architecture(good, 2)[1])
        sanity.write_text("theorem Submission.Lanes.Alpha.sanity : True := trivial\n"
                          "theorem Submission.Lanes.Beta.sanity : True := trivial\n")
        (good / "Submission.lean").write_text("import Submission.Lanes.Reduction\n")
        self.assertIn("must import Submission.Lanes.Sanity", feeder.validate_architecture(good, 2)[1])
        (good / "Submission" / "Lanes" / "Reduction.lean").write_text("theorem red : True := sorry\n")
        self.assertIn("sorry outside", feeder.validate_architecture(good)[1])
        one = self.lane_ws(self.r1, "one", ["Alpha"])
        self.assertEqual(feeder.validate_architecture(one)[0], [])
        done = self.lane_ws(self.r1, "lane", ["Alpha", "Beta"], solved=["Alpha"])
        self.assertTrue(feeder.lane_done(done, "Alpha"))
        self.assertFalse(feeder.lane_done(done, "Beta"))
        (done / "Submission/Lanes/Alpha/Extra.lean").write_text("theorem x : True := trivial\n")
        self.assertFalse(feeder.lane_done(done, "Alpha"))  # unbuilt new file
        # 2026-10-17: a sorry-free lane needs no sanity theorem even when no recorded build saw its
        # current files (mandelbrot CritHit, proved rounds earlier, blocked a split for 2.5 h)
        (done / "Submission/Lanes/Sanity.lean").write_text("theorem Submission.Lanes.Beta.sanity : True := trivial\n")
        self.assertEqual(feeder.validate_architecture(done, 2), (["Alpha", "Beta"], "ok"))

    def test_contract_3_source_and_strength_for_new_lanes(self):
        good = self.lane_ws(self.r1, "arch3", ["Alpha", "Beta"])
        self.assertEqual(feeder.validate_architecture(good, 3), (["Alpha", "Beta"], "ok"))
        main = good / "Submission/Lanes/Beta/Main.lean"
        main.write_text(main.read_text().replace("Strength: assumes the hole's case n=1\n", ""))
        self.assertIn("['Strength:']", feeder.validate_architecture(good, 3)[1])
        self.assertEqual(feeder.validate_architecture(good, 2)[1], "ok")
        # a lane of the program before this (re-)architecture keeps its contract-2 block
        self.assertEqual(feeder.validate_architecture(good, 3, {"Beta"})[1], "ok")

    def test_contract_4_reuse_for_new_lanes(self):
        good = self.lane_ws(self.r1, "arch4", ["Alpha", "Beta"])
        self.assertEqual(feeder.validate_architecture(good, 4), (["Alpha", "Beta"], "ok"))
        main = good / "Submission/Lanes/Alpha/Main.lean"
        main.write_text(main.read_text().replace("Reuse: none (searched: bar, baz)\n", ""))
        self.assertIn("['Reuse:']", feeder.validate_architecture(good, 4)[1])
        self.assertEqual(feeder.validate_architecture(good, 3)[1], "ok")
        self.assertEqual(feeder.validate_architecture(good, 4, {"Alpha"})[1], "ok")

    def test_re_architecture_validation_cases_from_the_fleet(self):
        # 2026-10-12: bourgain/one_relator re-architectures were rejected for these, wrongly
        ws = self.lane_ws(self.r1, "rearch", ["Alpha", "Beta", "Gamma"], solved=["Alpha"])
        lanes = ws / "Submission/Lanes"
        # a lane proved under the old architecture has no CONTRACT block and no sanity theorem
        (lanes / "Alpha/Main.lean").write_text("theorem Submission.Lanes.Alpha.main : "
                                               "Submission.Lanes.AlphaStatement := trivial\n")
        step = ws / ".feedback/step-000001.json"
        data = json.loads(step.read_text())
        data["source_files"]["Submission/Lanes/Alpha/Main.lean"] = feeder.sha256_file(lanes / "Alpha/Main.lean")
        step.write_text(json.dumps(data))
        # sanity theorems written inside `namespace Submission.Lanes`, a main inside its namespace
        (lanes / "Sanity.lean").write_text(
            "namespace Submission.Lanes\nsection\ntheorem Beta.sanity : True := trivial\nend\n"
            "-- theorem Gamma.sanity is below\ntheorem Gamma.sanity : True := trivial\n"
            "end Submission.Lanes\n")
        (lanes / "Gamma/Main.lean").write_text(
            "/-! CONTRACT\nStatement: x\nUsed by: red\nSanity: s\nEstimate: 10 -/\n"
            "namespace Submission.Lanes.Gamma\ntheorem main : Submission.Lanes.GammaStatement := sorry\n"
            "end Submission.Lanes.Gamma\n")
        self.assertEqual(feeder.validate_architecture(ws, 2), (["Alpha", "Beta", "Gamma"], "ok"))
        # a failed lane kept as a library: no main theorem, no sorry
        (lanes / "Old").mkdir()
        (lanes / "Old/Main.lean").write_text("/-! notes: the old lane, now a library -/\n"
                                             "theorem Submission.Lanes.Old.helper : True := trivial\n")
        self.assertEqual(feeder.validate_architecture(ws, 2), (["Alpha", "Beta", "Gamma"], "ok"))
        (lanes / "Old/Part.lean").write_text("theorem part : True := sorry\n")
        self.assertIn("lane Old: Main.lean lacks", feeder.validate_architecture(ws, 2)[1])
        (lanes / "Old/Part.lean").unlink()
        # an open lane still needs its sanity theorem, outside comments and other namespaces
        (lanes / "Sanity.lean").write_text(
            "namespace Submission.Lanes\ntheorem Beta.sanity : True := trivial\nend Submission.Lanes\n"
            "-- theorem Gamma.sanity\nnamespace Other\ntheorem Gamma.sanity : True := trivial\nend Other\n")
        self.assertIn("for ['Gamma']", feeder.validate_architecture(ws, 2)[1])

    def test_declared_names(self):
        text = ("namespace A.B\ntheorem x : True := trivial\nsection S\nlemma y : True := trivial\nend S\n"
                "/- theorem hidden : True -/\nprotected theorem C.z : True := trivial\n"
                "theorem _root_.top : True := trivial\nend A.B\ntheorem w : True := trivial\n")
        self.assertEqual(feeder.declared_names(text), {"A.B.x", "A.B.y", "A.B.C.z", "top", "w"})

    def test_program_architect_lanes_integrator(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        prog = json.loads(prog_path.read_text())
        arch = self.manifest(self.r1)["assignments"][0]
        self.assertEqual((arch["max_nudges"], arch["auto_continue"]["max_rounds"]), (0, 1))
        self.assertIn("architect", (self.r1 / arch["brief_file"]).read_text())
        self.assertEqual(prog["architect"]["base"], arch["attempt_id"])
        # architect finished with a valid split
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "lanes")
        lane_a = self.manifest(self.r1)["assignments"][-1]
        lane_b = self.manifest(self.r2)["assignments"][-1]
        self.assertTrue(lane_a["attempt_id"].endswith("-laneAlpha"))
        self.assertEqual(lane_b["research_frontier"]["edit_scope"], ["Submission/Lanes/Beta/**/*.lean"])
        # lane Alpha solves its stub mid-round -> STOP, done; lane Beta ends its rounds solved
        ja = self.r1 / "jobs" / lane_a["attempt_id"]
        self.lane_ws(self.r1, lane_a["attempt_id"], ["Alpha", "Beta"], solved=["Alpha"])
        (self.r1 / "state.json").write_text(json.dumps({"status": "model_running", "current_job": str(ja),
                                                        "history": {arch["attempt_id"]: done}}))
        self.lane_ws(self.r2, lane_b["attempt_id"], ["Alpha", "Beta"], solved=["Beta"])
        self.set_history(self.r2, {lane_b["attempt_id"]: done})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertTrue((ja / "STOP").exists())
        self.assertEqual({k: v["done"] for k, v in prog["lanes"].items()}, {"Alpha": True, "Beta": True})
        self.assertEqual(prog["status"], "integrating")
        integ = self.manifest(self.r2)["assignments"][-1]
        merged = self.r2 / integ["overlay_dir"]
        for lane in ("Alpha", "Beta"):
            self.assertNotIn("sorry", (merged / f"Submission/Lanes/{lane}/Main.lean").read_text())
        self.assertIn("integrator", (self.r2 / integ["brief_file"]).read_text())
        self.set_history(self.r2, {lane_b["attempt_id"]: done, integ["attempt_id"]: {
            "phase": "done", "verification": {"status": "official_local_pass"}}})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "passed")
        self.assertEqual(json.loads((self.own / "claims.json").read_text())["hadwiger"]["status"], "passed")

    def test_failed_lane_sends_program_back_to_architect(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        lane_a = self.manifest(self.r1)["assignments"][-1]
        lane_b = self.manifest(self.r2)["assignments"][-1]
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["lanes"]["Beta"]["claim"]["scope"], "Submission/Lanes/Beta")
        # Alpha is proved; Beta's lineage ends without a proof but with a refutation in its dir
        self.lane_ws(self.r1, lane_a["attempt_id"], ["Alpha", "Beta"], solved=["Alpha"])
        self.set_history(self.r1, {arch["attempt_id"]: done, lane_a["attempt_id"]: done})
        wb = self.lane_ws(self.r2, lane_b["attempt_id"], ["Alpha", "Beta"])
        (wb / "Submission/Lanes/Beta/Refute.lean").write_text("theorem not_statement : True := trivial\n")
        self.set_history(self.r2, {lane_b["attempt_id"]: done})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual((prog["status"], prog["rearchitected"], prog["rearchitect"]["lane"]),
                         ("architect", 1, "Beta"))
        again = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
                 if a["attempt_id"] == prog["architect"]["base"]][0]
        root = self.r1 if again in self.manifest(self.r1)["assignments"] else self.r2
        brief = (root / again["brief_file"]).read_text()
        self.assertIn("architect of a parallel program", brief)
        self.assertIn("lane Beta ended without a proof", brief)
        overlay = root / again["overlay_dir"]
        self.assertNotIn("sorry", (overlay / "Submission/Lanes/Alpha/Main.lean").read_text())
        self.assertTrue((overlay / "Submission/Lanes/Beta/Refute.lean").is_file())
        # the new split keeps the proved lane Alpha and replaces Beta by Gamma
        self.lane_ws(root, again["attempt_id"], ["Alpha", "Gamma"], solved=["Alpha"])
        history = json.loads((root / "state.json").read_text())["history"]
        self.set_history(root, dict(history, **{again["attempt_id"]: done}))
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "lanes")
        self.assertEqual({k: v["done"] for k, v in prog["lanes"].items()}, {"Alpha": True, "Gamma": False})
        self.assertTrue(prog["lanes"]["Gamma"].get("claim"))

    def start_two_lanes(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        return arch, done, self.manifest(self.r1)["assignments"][-1], self.manifest(self.r2)["assignments"][-1]

    def test_refuting_lane_is_not_done_and_the_program_is_rearchitected(self):
        arch, done, lane_a, lane_b = self.start_two_lanes()
        prog_path = self.own / "programs" / "hadwiger.json"
        # Alpha has no sorry, but only because it proves Beta's statement false (a vacuous consumer)
        wa = self.lane_ws(self.r1, lane_a["attempt_id"], ["Alpha", "Beta"], solved=["Alpha"])
        (wa / "Submission/Lanes/Alpha/NotBeta.lean").write_text(
            "theorem Submission.Lanes.Alpha.not_beta :\n    ¬ Submission.Lanes.BetaStatement := by\n  exact fun h => h\n")
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertFalse(prog["lanes"]["Alpha"]["done"])
        self.assertEqual(prog["lanes"]["Alpha"]["claim"]["status"], "refuted")
        self.assertEqual(prog["refuted"]["BetaStatement"]["lane"], "Alpha")
        # once Beta's round ends too, the program goes back to the architect with the refutation in the brief
        self.lane_ws(self.r2, lane_b["attempt_id"], ["Alpha", "Beta"])
        self.set_history(self.r2, {lane_b["attempt_id"]: done})
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["status"], "architect")
        again = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
                 if a["attempt_id"] == prog["architect"]["base"]][0]
        root = self.r1 if again in self.manifest(self.r1)["assignments"] else self.r2
        brief = (root / again["brief_file"]).read_text()
        self.assertIn("proves `BetaStatement` false (`Submission.Lanes.Alpha.not_beta`)", brief)

    def test_interface_requests_are_collected_for_the_architect(self):
        arch, done, lane_a, lane_b = self.start_two_lanes()
        wa = self.lane_ws(self.r1, lane_a["attempt_id"], ["Alpha", "Beta"])
        (wa / "NOTES.md").write_text("# NOTES\n\n## INTERFACE REQUESTS\n\n| needed | from lane | why |\n|---|---|---|\n"
                                     "| BetaStatement with saddle types preserved | Beta | case 2 moves saddles |\n")
        feeder.tick()
        prog = json.loads((self.own / "programs" / "hadwiger.json").read_text())
        self.assertEqual(prog["interface_requests"]["Alpha"],
                         ["BetaStatement with saddle types preserved | Beta | case 2 moves saddles"])
        self.assertIn("interface request from lane Alpha", feeder.rearchitect_reason(prog))

    def test_lanes_follow_the_interfaces_order(self):
        ws = Path(self.tmp.name) / "order-ws"
        (ws / "Submission/Lanes").mkdir(parents=True)
        (ws / "Submission/Lanes/Interfaces.lean").write_text(
            "def Submission.Lanes.ToolStatement : Prop := True\n"
            "def Submission.Lanes.CaseOneStatement : Prop := True\n")
        self.assertEqual(feeder.interface_order(ws, ["CaseOne", "Extra", "Tool"]), ["Tool", "CaseOne", "Extra"])

    def test_new_lane_starts_with_the_finished_lanes(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        # lane Alpha (a shared tool) finished with an extra module; lane Beta has not started yet
        tool = self.lane_ws(self.r2, "tool-laneAlpha", ["Alpha"], solved=["Alpha"])
        (tool / "Submission/Lanes/Alpha/Tool.lean").write_text("theorem tool : True := trivial\n")
        prog = json.loads(prog_path.read_text())
        prog.update(status="lanes", base_job=str(self.r1 / "jobs" / arch["attempt_id"]),
                    lanes={"Alpha": {"dir": "Submission/Lanes/Alpha", "done": True, "done_job": str(tool.parent)},
                           "Beta": {"dir": "Submission/Lanes/Beta", "done": False}})
        prog.pop("architect", None)
        prog_path.write_text(json.dumps(prog))
        feeder.tick()
        lane = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
                if a["attempt_id"].endswith("-laneBeta")][0]
        root = self.r1 if lane in self.manifest(self.r1)["assignments"] else self.r2
        overlay = root / lane["overlay_dir"]
        self.assertTrue((overlay / "Submission/Lanes/Alpha/Tool.lean").is_file())
        self.assertNotIn("sorry", (overlay / "Submission/Lanes/Alpha/Main.lean").read_text())

    def test_resumed_lane_starts_from_its_own_files(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        # a stopped lane round left partial work; the operator re-opens the lane on those files
        old = self.lane_ws(self.r2, "old-laneAlpha", ["Alpha"])
        (old / "Submission/Lanes/Alpha/Part.lean").write_text("theorem part : True := trivial\n")
        (old / "NOTES.prev.md").write_text("# HANDOFF\nAlpha: do not retry the chart route\n")  # cut short
        prog = json.loads(prog_path.read_text())
        prog.update(status="lanes", base_job=str(self.r1 / "jobs" / arch["attempt_id"]),
                    lanes={"Alpha": {"dir": "Submission/Lanes/Alpha", "done": False,
                                     "resume_job": str(old.parent)}})
        prog.pop("architect", None)
        prog_path.write_text(json.dumps(prog))
        feeder.tick()
        lane = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
                if a["attempt_id"].endswith("-laneAlpha")][0]
        root = self.r1 if lane in self.manifest(self.r1)["assignments"] else self.r2
        self.assertTrue((root / lane["overlay_dir"] / "Submission/Lanes/Alpha/Part.lean").is_file())
        self.assertIn("Alpha: do not retry the chart route", (root / lane["overlay_dir"] / "NOTES.md").read_text())

    def test_running_lane_brief_follows_the_problem_brief(self):
        # 2026-10-16: an operator decision for mandelbrot's lanes reached only newly dispatched roles
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        self.set_history(self.r1, {arch["attempt_id"]: {"phase": "done", "verification": {
            "status": "not_verified_snapshot_only"}}})
        feeder.tick()
        lane = [(r, a) for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
                if a["attempt_id"].endswith("-laneAlpha")][0]
        brief = lane[0] / lane[1]["brief_file"]
        self.assertNotIn("operator decision", brief.read_text())
        self.assertEqual(json.loads(prog_path.read_text())["lanes"]["Alpha"]["claim"]["status"], "running")
        (self.own / "briefs" / "hadwiger.md").write_text("hadwiger intel\n\noperator decision: use R\n")
        feeder.tick()
        text = brief.read_text()
        self.assertIn("operator decision: use R", text)
        self.assertIn("Submission/Lanes/Alpha", text)  # still the lane role with its fields

    def split_case(self):
        """A program with running lanes Alpha (r1) and Beta (r2); Beta's round was stopped by the
        operator and a split of Beta requested, as scratchpad split_lane.py does."""
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        seed = self.lane_ws(self.r2, "seedjob", [], steps=False).parent
        (seed / "result.json").write_text("{}")
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        prog_path = self.own / "programs" / "hadwiger.json"
        arch = self.manifest(self.r1)["assignments"][0]
        self.lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        done = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
        self.set_history(self.r1, {arch["attempt_id"]: done})
        feeder.tick()
        lane_b = self.manifest(self.r2)["assignments"][-1]
        self.assertTrue(lane_b["attempt_id"].endswith("-laneBeta"))
        wb = self.lane_ws(self.r2, lane_b["attempt_id"], ["Alpha", "Beta"])
        (wb / "Submission/Lanes/Beta/Part.lean").write_text("theorem part : True := trivial\n")
        (wb / "NOTES.md").write_text("# HANDOFF\nroute SC11 blocked mid-eye\n## RESIDUALS\n| r | open |\n")
        self.set_history(self.r2, {lane_b["attempt_id"]: done})
        prog = json.loads(prog_path.read_text())
        prog["lanes"]["Beta"]["claim"]["status"] = "stopped"
        prog["split"] = {"lane": "Beta", "from_job": str(wb.parent), "reason": "two residuals stuck"}
        prog_path.write_text(json.dumps(prog))
        feeder.tick()
        split = [a for a in self.manifest(self.r2)["assignments"] if "-splitBeta" in a["attempt_id"]][-1]
        return prog_path, split, done

    def test_split_of_one_lane_keeps_the_others_running(self):
        prog_path, split, done = self.split_case()
        brief = (self.r2 / split["brief_file"]).read_text()
        self.assertIn("split ONE running lane, Beta", brief)
        self.assertIn("two residuals stuck", brief)
        overlay = self.r2 / split["overlay_dir"]
        self.assertTrue((overlay / "Submission/Lanes/Beta/Part.lean").is_file())  # Beta's work
        self.assertIn("sorry", (overlay / "Submission/Lanes/Alpha/Main.lean").read_text())  # Alpha's stub
        notes = (overlay / "NOTES.md").read_text()  # Beta's own notes reach the split architect
        self.assertIn("## LANE Beta: its own notes", notes)
        self.assertIn("route SC11 blocked mid-eye", notes)
        self.assertIn("#### RESIDUALS", notes)
        self.assertNotIn("\n## RESIDUALS", notes.split("## LANE Beta: its own notes")[1])  # demoted
        prog = json.loads(prog_path.read_text())
        self.assertEqual(prog["split"]["sub"]["base"], split["attempt_id"])
        self.assertEqual(prog["lanes"]["Alpha"]["claim"]["status"], "running")
        # the architect cut Beta into Gamma and Delta; Beta became a library with the glue
        ws = self.lane_ws(self.r2, split["attempt_id"], ["Alpha", "Gamma", "Delta"])
        (ws / "Submission/Lanes/Beta").mkdir(exist_ok=True)
        (ws / "Submission/Lanes/Beta/Main.lean").write_text("theorem Submission.Lanes.Beta.of_parts : True := trivial\n")
        step = ws / ".feedback/step-000001.json"
        data = json.loads(step.read_text())
        data["source_files"] = feeder.proof_inventory(ws)
        step.write_text(json.dumps(data))
        history = json.loads((self.r2 / "state.json").read_text())["history"]
        self.set_history(self.r2, dict(history, **{split["attempt_id"]: done}))
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertNotIn("split", prog)
        self.assertEqual(prog["base_job"], str(ws.parent))
        self.assertEqual((prog["lanes"]["Beta"]["done"], prog["lanes"]["Beta"]["split_into"]), (True, ["Delta", "Gamma"]))
        self.assertEqual(prog["lanes"]["Alpha"]["claim"]["status"], "running")  # untouched
        self.assertTrue(prog["lanes"]["Gamma"].get("claim") or prog["lanes"]["Delta"].get("claim"))

    def test_split_that_changes_another_lane_is_fixed_then_dropped(self):
        prog_path, split, done = self.split_case()
        ws = self.lane_ws(self.r2, split["attempt_id"], ["Alpha", "Gamma", "Delta"])
        (ws / "Submission/Lanes/Interfaces.lean").write_text("def Submission.Lanes.AlphaStatement : Prop := False\n")
        history = json.loads((self.r2 / "state.json").read_text())["history"]
        self.set_history(self.r2, dict(history, **{split["attempt_id"]: done}))
        feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertIn("changed or dropped other lanes ['Alpha']", prog["split"]["fix_reason"])
        fix = [a for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]
               if a["attempt_id"] == prog["split"]["sub"]["base"]][0]
        root = self.r1 if fix in self.manifest(self.r1)["assignments"] else self.r2
        self.assertIn("ARCHITECT_FIX", json.dumps(prog["split"]["sub"]))
        ws2 = self.lane_ws(root, fix["attempt_id"], ["Alpha", "Gamma"])
        (ws2 / "Submission/Lanes/Interfaces.lean").write_text("def Submission.Lanes.AlphaStatement : Prop := False\n")
        history = json.loads((root / "state.json").read_text())["history"]
        self.set_history(root, dict(history, **{fix["attempt_id"]: done}))
        status = feeder.tick()
        prog = json.loads(prog_path.read_text())
        self.assertNotIn("split", prog)
        self.assertTrue(prog["lanes"]["Beta"]["resume_job"].endswith("-laneBeta"))  # back on its own files
        self.assertIn("changed or dropped", prog["splits"][-1]["failed"])
        self.assertTrue(any("split of lane Beta failed" in a for a in status["alerts"]))

    def test_program_runner_cap(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "linnik"}]}))
        cfg = json.loads((self.own / "config.json").read_text())
        cfg["max_program_runners"] = 1
        (self.own / "config.json").write_text(json.dumps(cfg))
        for pid in ("hadwiger", "green_tao"):
            seed = self.lane_ws(self.r2, f"seed-{pid}", [], steps=False).parent
            (seed / "result.json").write_text("{}")
            feeder.create_program(pid, str(seed))
        feeder.tick()
        roles = [a["problem_id"] for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]]
        self.assertEqual(roles, ["hadwiger", "linnik"])  # one program role, then a target

    def test_seed_dir_target_gets_overlay(self):
        seed = Path(self.tmp.name) / "seeds" / "hadwiger-job"
        (seed / "Submission").mkdir(parents=True)
        (seed / "Submission.lean").write_text("theorem a : True := trivial\n")
        (seed / "Submission" / "X.lean").write_text("theorem b : True := trivial\n")
        (seed / "NOTES.md").write_text("handoff\n")
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "hadwiger", "seed_dir": str(seed)}]}))
        feeder.tick()
        a = self.manifest(self.r1)["assignments"][0]
        overlay = self.r1 / a["overlay_dir"]
        self.assertEqual(sorted(p.relative_to(overlay).as_posix() for p in overlay.rglob("*") if p.is_file()),
                         ["NOTES.md", "Submission.lean", "Submission/X.lean"])
        self.assertIn("ROUTE REVIEW", (self.r1 / a["brief_file"]).read_text())  # it stalled elsewhere
        claim = json.loads((self.own / "claims.json").read_text())["hadwiger"]
        self.assertEqual((claim["role"], claim["route_review"]["reason"]), ("ROUTE_REVIEW", "seeded transfer"))

    def test_runtime_pin_rewrites_controller_once(self):
        controller = self.r1 / "controller.py"
        controller.write_text('X = 1\nRUNTIME = Path("/old/runtime")  # pinned\nY = 2\n')
        wanted = Path(self.tmp.name) / "runtimes" / "lrh-test"
        alerts: list[str] = []
        self.assertFalse(feeder.ensure_runtime(self.r1, wanted, alerts))
        self.assertIn("not deployed", alerts[0])
        wanted.mkdir(parents=True)
        (wanted / "runtime-manifest.json").write_text("{}")
        (wanted / ".venv").mkdir()
        with mock.patch.object(feeder, "controller_pids", return_value=[]), \
                mock.patch.object(feeder.subprocess, "run") as run:
            self.assertTrue(feeder.ensure_runtime(self.r1, wanted, []))
            self.assertFalse(feeder.ensure_runtime(self.r1, wanted, []))
            run.assert_not_called()
        self.assertEqual(controller.read_text(),
                         f'X = 1\nRUNTIME = Path("{wanted}")  # pinned\nY = 2\n')
        controller.write_text(f'RUNTIME = Path("/old/runtime")\n')
        with mock.patch.object(feeder, "controller_pids", return_value=[123]), \
                mock.patch.object(feeder.subprocess, "run") as run:
            self.assertTrue(feeder.ensure_runtime(self.r1, wanted, []))
            self.assertIn("restart", run.call_args[0][0])

    def test_stop_file_and_disk_guard_block_assignment(self):
        (self.r1 / "STOP").touch()
        cfg = json.loads((self.own / "config.json").read_text())
        cfg["runners"] = [str(self.r1)]
        (self.own / "config.json").write_text(json.dumps(cfg))
        feeder.tick()
        self.assertEqual(self.manifest(self.r1)["assignments"], [])
        (self.r1 / "STOP").unlink()
        cfg["min_free_gb"] = 10**9
        (self.own / "config.json").write_text(json.dumps(cfg))
        status = feeder.tick()
        self.assertEqual(self.manifest(self.r1)["assignments"], [])
        self.assertTrue(any("disk free" in a for a in status["alerts"]))

    def test_assignment_validation(self):
        with self.assertRaises(ValueError):
            feeder.make_assignment({"problem_id": "../x"}, "b", None, feeder.DEFAULTS)
        with self.assertRaises(ValueError):
            feeder.make_assignment({"problem_id": "p", "kind": "bundle", "bundle_dir": "rel"},
                                   "b", None, feeder.DEFAULTS)

    def test_ci_gate_and_log_stats(self):
        log = ("✔ [1/3] Built Submission.A (2.5s)\n✔ [2/3] Built Submission.B (1.2m s)\n"
               "✔ [3/3] Built Submission (300ms)\nnanoda kernel accepts the solution\n"
               "Lean default kernel accepts the solution\nYour solution is okay!\n")
        stats = feeder.official_log_stats(log)
        self.assertEqual(len(stats["markers"]), 3)
        self.assertEqual(stats["submission_modules_built"], 2)
        self.assertTrue(feeder.ci_gate(stats, 1000)["fit"])
        big = dict(stats, submission_build_seconds_sum=60 * 3600.0)
        self.assertFalse(feeder.ci_gate(big, 1000)["fit"])
        self.assertFalse(feeder.ci_gate(stats, 40 * 2**20)["fit"])
        self.assertFalse(feeder.ci_gate(stats, 29_600_000)["fit"])  # the Contents API cap
        # the accepted Poincaré packages (CI 227 and 226 min) fit; the old model held them at 466-673 min
        for total, worst, ci_minutes in ((78_624.0, 513.0, 227), (86_071.0, 496.0, 226)):
            gate = feeder.ci_gate(dict(stats, submission_build_seconds_sum=total,
                                       slowest_modules=[["Submission.X", worst]]), 25_000_000)
            self.assertTrue(gate["fit"], gate)
            self.assertTrue(ci_minutes <= gate["predicted_ci_minutes"] <= 1.15 * ci_minutes, gate)
        self.assertFalse(feeder.ci_gate(dict(stats, submission_build_seconds_sum=120_000.0), 1000)["fit"])
        gate = feeder.ci_gate(stats, 1000, {"Submission/A.lean": b"set_option profiler true\ntheorem a : True := trivial\n",
                                            "Submission/B.lean": b"-- set_option trace.Meta true\n"})
        self.assertTrue(gate["fit"])
        self.assertEqual(len(gate["notes"]), 1)
        self.assertIn("1 proof files set profiler/trace options (e.g. Submission/A.lean)", gate["notes"][0])

    def synthetic_pass(self, digest="d" * 64, run_id="rid", text="theorem x : True := trivial\n"):
        """A receipt, its bundle and wave101's PASS-green_tao.json as the controller writes them
        (a later pass overwrites the PASS file); (runs, bench, pass_path, bundle)."""
        runs = Path(self.tmp.name) / "runs"
        bench = Path(self.tmp.name) / "bench"
        if not bench.exists():
            bench.mkdir()
            subprocess.run(["git", "init", "-q", str(bench)], check=True)
            (bench / "generated/green_tao").mkdir(parents=True)
            (bench / "generated/green_tao/lakefile.toml").write_text('name = "green_tao"\n')
            (bench / "generated/green_tao/lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc3\n")
            (bench / "LICENSE").write_text("Apache License 2.0\n")
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            subprocess.run(["git", "-C", str(bench), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(bench), "commit", "-qm", "b"], check=True, env=env)
        commit = subprocess.check_output(["git", "-C", str(bench), "rev-parse", "HEAD"],
                                         text=True).strip()
        proof = {"Submission.lean": text.encode(),
                 "Submission/Helpers.lean": b"-- helpers\n"}
        files = {n: hashlib.sha256(d).hexdigest() for n, d in proof.items()}
        bundle = runs / f"finalizations/capture-{run_id}/bundle"
        for n, d in proof.items():
            (bundle / "proof" / n).parent.mkdir(parents=True, exist_ok=True)
            (bundle / "proof" / n).write_bytes(d)
        manifest = {"schema_version": 1, "problem_id": "green_tao", "statement_revision": 1,
                    "source_commit": "a" * 40, "source_workspace": ".",
                    "benchmark_commit": commit, "files": files}
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        official = ("✔ Built Submission (1.0s)\nnanoda kernel accepts the solution\n"
                    "Lean default kernel accepts the solution\nYour solution is okay!\n").encode()
        (runs / run_id).mkdir(parents=True)
        (runs / run_id / "official.log").write_bytes(official)
        (runs / run_id / "receipt.json").write_text(json.dumps({
            "run_id": run_id, "artifact_digest": digest, "status": "official_local_pass",
            "service_acceptance": False, "artifact": manifest,
            "checks": {"official_lake_test": 0, "both_kernels_reported": True,
                       "inputs_unchanged": True},
            "logs": {"official.log": hashlib.sha256(official).hexdigest()},
            "verifier": {"pins": {"benchmark": {"rev": commit}}}}))
        pass_path = self.r1 / "PASS-green_tao.json"
        pass_path.write_text(json.dumps({
            "problem_id": "green_tao", "receipt_run_id": run_id, "effective_model": "claude-opus-5-5",
            "effective_effort": "xhigh", "model": {"observed_tokens": 5},
            "verification": {"status": "official_local_pass", "verified": True,
                             "artifact_digest": digest, "bundle": str(bundle),
                             "verification_wall_seconds": 10.0}}))
        return runs, bench, pass_path, bundle

    def test_package_pass_from_synthetic_receipt(self):
        runs, bench, pass_path, bundle = self.synthetic_pass()
        with mock.patch.object(feeder, "RUNS", runs), mock.patch.object(feeder, "BENCH_REPO", bench):
            package = feeder.package_pass(self.r1, pass_path)
            again = feeder.package_pass(self.r1, pass_path)
        self.assertEqual(package, again)
        self.assertEqual(package["status"], "ready")
        self.assertEqual(package["branch"], "claude/green_tao-private")
        tree = self.own / "outbox/green_tao" / ("d" * 16) / "tree"
        for name in ("Submission.lean", "Submission/Helpers.lean", "lakefile.toml",
                     "lean-toolchain", "LICENSE", "provenance/LEAN-EVAL-LICENSE", "README.md",
                     "PROVENANCE.json", "VERIFICATION.json"):
            self.assertTrue((tree / name).is_file(), name)
            self.assertEqual(hashlib.sha256((tree / name).read_bytes()).hexdigest(),
                             package["files"][name])
        verification = json.loads((tree / "VERIFICATION.json").read_text())
        self.assertEqual(verification["status"], "official_local_pass")
        # tampered proof file is rejected
        (bundle / "proof/Submission.lean").write_bytes(b"sorry")
        pass2 = self.r1 / "PASS-other.json"
        data = json.loads(pass_path.read_text())
        data["verification"]["artifact_digest"] = "e" * 64
        pass2.write_text(json.dumps(data))
        with mock.patch.object(feeder, "RUNS", runs), mock.patch.object(feeder, "BENCH_REPO", bench):
            with self.assertRaises(ValueError):
                feeder.package_pass(self.r1, pass2)


    # ------------------------------------------------------------------ policy 2026-10-14

    DONE = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}

    def complete_ws(self, root, job, lines=5):
        """A sorry-free workspace whose latest `build Submission` succeeded on exactly its files."""
        ws = self.write_ws(root, job, sorry=0, lines=lines)
        (ws / ".feedback").mkdir(exist_ok=True)
        (ws / ".feedback" / "step-000001.json").write_text(json.dumps(
            {"action": "build", "query": "Submission", "outcome_class": "check_succeeded",
             "source_current": True, "source_files": feeder.proof_inventory(ws)}))
        return ws

    def setup_lineage(self, root, base, finished, pending=1, claim=None, pid="green_tao", history=None):
        """`finished` done rounds of `base` plus `pending` rounds (the first of them running) and
        a running claim of `pid` on `root`."""
        names = [base] + [f"{base}-r{n}" for n in range(2, finished + pending + 1)]
        m = self.manifest(root)
        m["assignments"] = [{"attempt_id": n, "problem_id": pid, "verify_policy": "if_ready",
                             "kind": "fresh" if i == 0 else "continuation", "verification_seconds": 7200,
                             "auto_continue": {"base": base, "round": i + 1, "max_rounds": 10}}
                            for i, n in enumerate(names)]
        (root / "manifest.json").write_text(json.dumps(m))
        done = dict(history or {})
        done.update({n: dict(self.DONE, problem_id=pid, finished_at=1000 + i) for i, n in enumerate(names[:finished])})
        current = names[finished] if pending else None
        if current:
            (root / "jobs" / current).mkdir(parents=True, exist_ok=True)
        (root / "state.json").write_text(json.dumps({
            "status": "model_running" if current else "idle_waiting_for_assignments",
            "current_job": str(root / "jobs" / current) if current else None, "history": done}))
        path = self.own / "claims.json"
        claims = json.loads(path.read_text()) if path.exists() else {}
        claims[pid] = dict({"status": "running", "runner": str(root), "base": base}, **(claim or {}))
        path.write_text(json.dumps(claims))
        return names

    def claims(self):
        return json.loads((self.own / "claims.json").read_text())

    def set_config(self, **values):
        cfg = json.loads((self.own / "config.json").read_text())
        cfg.update(values)
        (self.own / "config.json").write_text(json.dumps(cfg))

    def pause(self, root, job, window, until, reason="usage_limit_pause", ended=False, account=None):
        """A checkpoint.json of a job waiting in a usage-limit pause (runtime agent_runner fields)."""
        (root / "jobs" / job).mkdir(parents=True, exist_ok=True)
        pause = {"reason": reason, "started_at": time.time() - 60, "until": until}
        if ended:
            pause["ended_at"] = time.time()
        (root / "jobs" / job / "checkpoint.json").write_text(json.dumps({
            "phase": "model_running" if ended else reason, "pauses": [pause],
            # a resumed call starts after the block was observed
            "invocations": [{"started_at": time.time() + 5 if ended else time.time() - 600,
                             **({"account": account} if account else {})}],
            "rate_limit": {"status": "rejected", "rateLimitType": window, "resetsAt": until,
                           "unifiedWindows": {window: {"utilization": 1.0, "resetsAt": until}}}}))

    # 1. runtime switching

    def test_runtime_switch_deferred_while_busy_then_applied_when_idle(self):
        wanted = Path(self.tmp.name) / "runtimes" / "lrh-0f1345d4"
        (wanted / ".venv").mkdir(parents=True)
        (wanted / "runtime-manifest.json").write_text("{}")
        self.set_config(runners=[str(self.r1)], runtimes={"wave101": str(wanted)})
        (self.r1 / "controller.py").write_text('RUNTIME = Path("/old/runtime")\n')
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1)
        with mock.patch.object(feeder.subprocess, "run") as run:
            status = feeder.tick()
            run.assert_not_called()
        self.assertIn('"/old/runtime"', (self.r1 / "controller.py").read_text())
        self.assertTrue(any("runtime switch deferred to lineage boundary" in a for a in status["alerts"]))
        # the lineage ends: the idle runner switches first, then gets its next lineage
        self.set_history(self.r1, {n: dict(self.DONE, problem_id="green_tao") for n in names})
        with mock.patch.object(feeder.subprocess, "run") as run:
            feeder.tick()
            self.assertIn("restart", run.call_args_list[0][0][0])
        self.assertIn(str(wanted), (self.r1 / "controller.py").read_text())
        self.assertGreater(len(self.manifest(self.r1)["assignments"]), len(names))

    def test_merge_host_config_keeps_pins_and_takes_policy_keys(self):
        spec = importlib.util.spec_from_file_location("fleet_local", HERE.parent / "local/fleet.py")
        fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        old = {"host": "host-2", "runners": [fleet.root_of("wave111", "/srv/fleet")], "custom": 1,
               "runtimes": {"wave111": "/rt/lrh-20261012-0f1345d4"}, "tokens": {"wave111": "/old"}}
        host = {"runners": {"wave111": "account-a", "wave113": "account-g"}, "max_active_runners": 3}
        merged = fleet.merge_host_config(old, "host-2", host, "/rt/lrh-abcd1234",
                                         fleet={"churn_rounds": 3, "defaults": {"verification_seconds": 21600}},
                                         home="/srv/fleet")
        self.assertEqual(merged["runtimes"], {"wave111": "/rt/lrh-20261012-0f1345d4", "wave113": "/rt/lrh-abcd1234"})
        self.assertEqual((merged["custom"], merged["max_active_runners"], merged["churn_rounds"]), (1, 3, 3))
        self.assertEqual(merged["defaults"]["verification_seconds"], 21600)
        self.assertEqual(merged["tokens"]["wave113"], "file:~/.config/leh/accounts/account-g.token")
        self.assertEqual(merged["runners"], ["/srv/fleet/projects/lean-eval-campaigns/fleet-1-wave111",
                                             "/srv/fleet/projects/lean-eval-campaigns/fleet-1-wave113"])
        refs = fleet.merge_host_config({}, "host-2", host, "/rt/x", home="/srv/fleet",
                                       fleet={"accounts": {"account-a": "file:/keys/a"}})["tokens"]
        self.assertEqual(refs, {"wave111": "file:/keys/a", "wave113": "file:~/.config/leh/accounts/account-g.token"})
        self.assertEqual(old["runtimes"], {"wave111": "/rt/lrh-20261012-0f1345d4"})  # input untouched
        pinned = fleet.merge_host_config(old, "host-2", dict(host, runtimes={"wave111": "/rt/v78"}), "/rt/new")
        self.assertEqual(pinned["runtimes"]["wave111"], "/rt/v78")
        # every policy key of fleet.json reaches config.json on deploy ("coordinator" and "publish" are read
        # on the operator machine only)
        structural = {"note", "runtime", "accounts", "hosts", "coordinator", "publish"}
        keys = {k for k in json.loads((HERE.parent / "fleet.example.json").read_text())
                if k not in structural and not k.startswith("policy_note")}
        self.assertEqual(keys - set(fleet.POLICY_KEYS), set())

    def test_run_controller_passes_only_lrh_runtime_env(self):
        text = (HERE.parent / "server/run_controller.sh").read_text()
        snippet = text.split("done < <(python3 -c '")[1].split("' \"$OWN/config.json\" 2>/dev/null")[0]
        self.assertLess(text.index('"${RUNTIME_ENV[@]}"'), text.index('"$PY" -B "$ROOT/controller.py"'))
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"runtime_env": {
                "LRH_LEAN_NUM_THREADS": "6", "LRH_LAKE_JOBS": 4, "LRH_FULL_CHECK_SECONDS": "9000",
                "LEAN_NUM_THREADS": "2", "PATH": "/evil", "LRH_LAKE_JOBS_X": "1"}}))
            out = subprocess.run([sys.executable, "-c", snippet, str(config)], capture_output=True, text=True,
                                 check=True).stdout
            self.assertEqual(out.split(), ["LRH_FULL_CHECK_SECONDS=9000", "LRH_LAKE_JOBS=4",
                                           "LRH_LEAN_NUM_THREADS=6"])
            config.write_text(json.dumps({"runtime_env": {"LRH_LAKE_JOBS": "4; rm -rf /"}}))
            out = subprocess.run([sys.executable, "-c", snippet, str(config)], capture_output=True, text=True,
                                 check=True).stdout
            self.assertEqual(out, "")

    def test_deploy_runtime_id_has_no_date(self):
        text = (HERE.parent / "server/deploy_runtime.sh").read_text()
        self.assertIn('ID="lrh-$HASH"', text)
        self.assertNotIn("date -u +%Y%m%d", text)

    # 2. verification phase

    def test_complete_kind_needs_a_build_of_exactly_the_current_files(self):
        before = self.write_ws(self.r1, "a", sorry=1, lines=5)
        ws = self.complete_ws(self.r1, "b")
        self.assertTrue(feeder.round_complete(ws))
        got = feeder.round_progress(before, ws)
        self.assertEqual((got["kind"], got["complete"], got["progress"]), ("complete", True, True))
        (ws / "Submission" / "Extra.lean").write_text("theorem e : True := trivial\n")
        self.assertFalse(feeder.round_complete(ws))  # built files differ from the current ones
        self.assertEqual(feeder.round_progress(before, ws)["kind"], "sorry")
        # a later failed full check does not undo the build; a later failed build does
        (ws / "Submission" / "Extra.lean").unlink()
        (ws / ".feedback" / "step-000002.json").write_text(json.dumps(
            {"action": "check", "query": "", "outcome_class": "timeout"}))
        self.assertTrue(feeder.round_complete(ws))
        # lrh P1 records without a compiler verdict (round-wall refusal, a superseded build) and a
        # passing light check (it builds Submission on these files) keep the round complete
        (ws / ".feedback" / "step-000003.json").write_text(json.dumps(
            {"action": "build", "query": "Submission", "outcome_class": "invalid_request",
             "phase": "round_wall_gate", "source_files": feeder.proof_inventory(ws)}))
        (ws / ".feedback" / "step-000004.json").write_text(json.dumps(
            {"action": "build", "query": "Submission", "outcome_class": "superseded",
             "source_files": feeder.proof_inventory(ws)}))
        self.assertTrue(feeder.round_complete(ws))
        self.assertTrue(feeder.last_build_ok(ws))
        (ws / ".feedback" / "step-000005.json").write_text(json.dumps(
            {"action": "check", "query": "", "check_mode": "light", "outcome_class": "check_succeeded",
             "source_current": True, "source_files": feeder.proof_inventory(ws)}))
        self.assertTrue(feeder.round_complete(ws))
        (ws / ".feedback" / "step-000006.json").write_text(json.dumps(
            {"action": "build", "query": "Submission", "outcome_class": "lean_diagnostic",
             "source_files": feeder.proof_inventory(ws)}))
        self.assertFalse(feeder.round_complete(ws))

    def test_complete_round_starts_a_verify_lineage_instead_of_continuing(self):
        self.start_verify_case()

    def start_verify_case(self):
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1)
        self.complete_ws(self.r1, base)
        feeder.tick()
        assignments = self.manifest(self.r1)["assignments"]
        self.assertTrue((self.r1 / "jobs" / names[1] / "STOP").exists())
        self.assertIsNone(assignments[1]["auto_continue"])
        verify = assignments[-1]
        self.assertEqual((verify["verify_policy"], verify["auto_continue"]["max_rounds"], verify["wall_seconds"],
                          verify["verification_seconds"], verify["max_nudges"]), ("always", 1, 900, 21600, 0))
        self.assertTrue((self.r1 / verify["overlay_dir"] / "Submission.lean").is_file())
        brief = (self.r1 / verify["brief_file"]).read_text()
        self.assertIn("verification only", brief)
        self.assertIn("Change nothing", brief)
        self.assertNotIn("{digest}", brief)
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["role"], claim["verifies"], claim["base"]), ("VERIFY", 1, verify["attempt_id"]))
        self.assertNotIn("retries", claim)
        self.assertEqual(claim["last_decision"]["decision"], "VERIFY")
        return names, verify

    def test_a_complete_ci_fit_round_is_not_sent_to_verify(self):
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1, claim={"role": "CI_FIT"})
        self.complete_ws(self.r1, base)
        feeder.tick()
        self.assertFalse((self.r1 / "jobs" / names[1] / "STOP").exists())
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["role"]), ("running", "CI_FIT"))
        self.assertNotIn("verifies", claim)

    def test_an_exhausted_complete_ci_fit_lineage_is_not_verified(self):
        base = "fleet-1-w101-009-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0, claim={"role": "CI_FIT"})
        self.complete_ws(self.r1, base)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertNotEqual(claim.get("role"), "VERIFY")
        self.assertNotIn("verifies", claim)

    def test_verify_timeout_goes_to_ci_fit_with_measured_modules(self):
        names, verify = self.start_verify_case()
        self.complete_ws(self.r1, verify["attempt_id"])
        runs = Path(self.tmp.name) / "runs"
        (runs / "rid1").mkdir(parents=True)
        (runs / "rid1" / "official.log").write_text(
            "✔ [1/3] Built Submission.Heavy (1500s)\n✔ [2/3] Built Submission.Light (2.5s)\n")
        history = json.loads((self.r1 / "state.json").read_text())["history"]
        history[names[1]] = dict(self.DONE, problem_id="green_tao")
        history[verify["attempt_id"]] = {"phase": "done", "problem_id": "green_tao", "receipt_run_id": "rid1",
                                         "verification": {"status": "timeout", "verification_wall_seconds": 21600.0}}
        self.set_history(self.r1, history)
        with mock.patch.object(feeder, "RUNS", runs):
            feeder.tick()
        ci = self.manifest(self.r1)["assignments"][-1]
        brief = (self.r1 / ci["brief_file"]).read_text()
        self.assertIn("CI fit", brief)
        self.assertIn("Submission.Heavy 1500 s", brief)
        self.assertIn("timed out after 21600 s", brief)
        self.assertEqual((ci["verify_policy"], ci["auto_continue"]["max_rounds"]), ("if_ready", 2))
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["role"], claim["verifies"]), ("CI_FIT", 1))
        # the CI_FIT round left a complete, unverified tree and the lineage ended: VERIFY #2
        self.complete_ws(self.r1, ci["attempt_id"])
        self.complete_ws(self.r1, ci["attempt_id"] + "-r2")
        m = self.manifest(self.r1)
        m["assignments"].append(dict(ci, attempt_id=ci["attempt_id"] + "-r2", kind="continuation"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        history.update({ci["attempt_id"]: dict(self.DONE, problem_id="green_tao"),
                        ci["attempt_id"] + "-r2": dict(self.DONE, problem_id="green_tao")})
        self.set_history(self.r1, history)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["role"], claim["verifies"]), ("VERIFY", 2))
        # VERIFY #2 times out too; the second CI_FIT ends complete: no third VERIFY, operator alert
        v2 = claim["base"]
        self.complete_ws(self.r1, v2)
        history[v2] = {"phase": "done", "problem_id": "green_tao",
                       "verification": {"status": "rejected"}, "receipt_run_id": "none"}
        self.set_history(self.r1, history)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual(claim["role"], "REPAIR")
        repair = self.manifest(self.r1)["assignments"][-1]
        self.assertIn("no official log was recorded", (self.r1 / repair["brief_file"]).read_text())
        self.complete_ws(self.r1, repair["attempt_id"])
        self.complete_ws(self.r1, repair["attempt_id"] + "-r2")
        m = self.manifest(self.r1)
        m["assignments"].append(dict(repair, attempt_id=repair["attempt_id"] + "-r2", kind="continuation"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        history.update({repair["attempt_id"]: dict(self.DONE, problem_id="green_tao"),
                        repair["attempt_id"] + "-r2": dict(self.DONE, problem_id="green_tao")})
        self.set_history(self.r1, history)
        status = feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["verifies"], claim.get("verify_exhausted")), ("exhausted", 2, True))
        self.assertTrue(any("not verified after 2 VERIFY" in a for a in status["alerts"]))
        self.assertFalse((self.own / "programs" / "green_tao.json").exists())

    def test_verify_rejection_goes_to_repair_with_the_official_log_tail(self):
        names, verify = self.start_verify_case()
        self.complete_ws(self.r1, verify["attempt_id"])
        runs = Path(self.tmp.name) / "runs"
        (runs / "rid2").mkdir(parents=True)
        (runs / "rid2" / "official.log").write_text("noise\n" * 100 + "error: theorem_1 does not match Challenge\n")
        history = json.loads((self.r1 / "state.json").read_text())["history"]
        history[verify["attempt_id"]] = {"phase": "done", "problem_id": "green_tao", "receipt_run_id": "rid2",
                                         "verification": {"status": "rejected"}}
        self.set_history(self.r1, history)
        with mock.patch.object(feeder, "RUNS", runs):
            feeder.tick()
        repair = self.manifest(self.r1)["assignments"][-1]
        brief = (self.r1 / repair["brief_file"]).read_text()
        self.assertIn("returned\n`rejected`", brief)
        self.assertIn("    error: theorem_1 does not match Challenge", brief)
        self.assertEqual(self.claims()["green_tao"]["role"], "REPAIR")

    def test_at_most_max_verifies_then_the_lineage_continues(self):
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1, claim={"verifies": 2})
        self.complete_ws(self.r1, base)
        feeder.tick()
        self.assertFalse((self.r1 / "jobs" / names[1] / "STOP").exists())
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 2)
        self.assertEqual(self.claims()["green_tao"]["base"], base)

    def test_exhausted_complete_lineage_verifies_instead_of_becoming_a_program(self):
        self.set_config(program_min_ref_modules=250)
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 600}]}))
        base = "fleet-1-w101-009-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0, claim={"route_review": {"from": "x"}})
        self.complete_ws(self.r1, base)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["role"]), ("running", "VERIFY"))
        self.assertFalse((self.own / "programs" / "green_tao.json").exists())

    def test_verify_backfill_of_exhausted_and_route_review_claims(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao"}, {"problem_id": "hadwiger"}]}))
        old = "fleet-1-w102-003-green_tao"
        self.complete_ws(self.r2, old)
        self.set_history(self.r2, {old: dict(self.DONE, problem_id="green_tao")})
        exhausted = {"green_tao": {"status": "exhausted", "runner": str(self.r2), "base": old, "rounds": 3}}
        (self.own / "claims.json").write_text(json.dumps(exhausted))
        self.set_config(runners=[str(self.r1)])
        feeder.tick()  # without the flag: the old behaviour, a route-review retry
        self.assertEqual(self.claims()["green_tao"]["role"], "ROUTE_REVIEW")
        # with verify_backfill the running route review of a complete source is replaced by VERIFY
        self.set_config(verify_backfill=True)
        retry = self.manifest(self.r1)["assignments"][-1]["attempt_id"]
        (self.r1 / "state.json").write_text(json.dumps({"status": "model_running", "history": {},
                                                        "current_job": str(self.r1 / "jobs" / retry)}))
        (self.r1 / "jobs" / retry).mkdir(parents=True)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["role"], claim["verifies"], claim["retries"]), ("VERIFY", 1, 1))
        self.assertTrue((self.r1 / "jobs" / retry / "STOP").exists())
        self.assertEqual(claim["verify"]["from"], str(self.r2 / "jobs" / old))
        # an exhausted claim is picked up directly by an idle runner
        (self.own / "claims.json").write_text(json.dumps(exhausted))
        self.set_config(runners=[str(self.r2)])
        self.set_history(self.r2, {old: dict(self.DONE, problem_id="green_tao")})
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["role"], claim["runner"], claim.get("retries")), ("VERIFY", str(self.r2), None))

    def test_common_brief_ready_rule_and_ci_limits(self):
        text = (HERE.parent / "server/briefs/COMMON.md").read_text()
        self.assertIn("`./lean check` before that is optional", text)
        self.assertIn("6 hours in total", text)
        self.assertNotIn("2 CPU-hours", text)

    # 3. verification budget

    def test_migrate_verification_seconds_touches_only_unfinished_assignments(self):
        self.assertEqual(feeder.DEFAULTS["verification_seconds"], 21600)
        fleet = json.loads((HERE.parent / "fleet.example.json").read_text())
        self.assertEqual(fleet["defaults"]["verification_seconds"], 21600)
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=2)
        state_before = (self.r1 / "state.json").read_text()
        changed = feeder.migrate_verification_seconds(21600)
        self.assertEqual(changed, {self.r1.name: 2, self.r2.name: 0})
        got = {a["attempt_id"]: a["verification_seconds"] for a in self.manifest(self.r1)["assignments"]}
        self.assertEqual(got, {names[0]: 7200, names[1]: 21600, names[2]: 21600})
        self.assertEqual((self.r1 / "state.json").read_text(), state_before)

    # 4. quota

    def test_runner_quota_from_checkpoint(self):
        now = time.time()
        state = {"status": "model_running", "current_job": str(self.r1 / "jobs" / "j")}
        self.pause(self.r1, "j", "seven_day", now + 3 * 86400)
        q = feeder.runner_quota(self.r1, state, now)
        self.assertEqual((q["paused"], q["window"]), (True, "seven_day"))
        self.pause(self.r1, "j", "five_hour", now + 1800)
        self.assertEqual(feeder.runner_quota(self.r1, state, now)["window"], "five_hour")
        self.pause(self.r1, "j", "five_hour", now - 10)  # reset passed
        self.assertFalse(feeder.runner_quota(self.r1, state, now)["paused"])
        self.pause(self.r1, "j", "five_hour", now + 1800, ended=True)
        q = feeder.runner_quota(self.r1, state, now)
        self.assertEqual((q["paused"], bool(q["active_since"])), (False, True))
        self.assertFalse(feeder.runner_quota(self.r1, dict(state, status="finalizing"), now)["paused"])
        # without a rejection record the window comes from the pause length
        self.assertEqual(feeder.pause_window({}, {"started_at": now, "until": now + 6 * 86400}), "seven_day")
        self.assertEqual(feeder.pause_window({"status": "allowed_warning", "rateLimitType": "seven_day"},
                                             {"started_at": now, "until": now + 3600}), "five_hour")

    def test_blocked_account_gets_no_new_work_until_its_runner_resumes(self):
        self.set_config(tokens={"wave101": "/x/account-g", "wave102": "/x/account-g"})
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1)
        self.write_ws(self.r1, base, sorry=1, lines=5)
        self.pause(self.r1, names[1], "five_hour", time.time() + 1200)
        status = feeder.tick()
        self.assertEqual(self.manifest(self.r2)["assignments"], [])
        self.assertIn("quota-blocked", status["runners"][self.r2.name]["assignment"])
        self.assertIn("account-g", json.loads((self.own / "account-blocks.json").read_text()))
        self.assertEqual(self.claims()["green_tao"]["status"], "running")  # short pause: no handoff
        self.pause(self.r1, names[1], "five_hour", time.time() + 1200, ended=True)  # resumed
        feeder.tick()
        self.assertEqual(len(self.manifest(self.r2)["assignments"]), 1)

    def test_a_pause_blocks_the_account_that_hit_the_limit_not_the_runner_s_new_one(self):
        # 2026-10-19: two runners moved from an exhausted account to a fresh one while still paused
        self.set_config(tokens={"wave101": "/x/account-fresh", "wave102": "/x/account-fresh"})
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1)
        self.write_ws(self.r1, base, sorry=1, lines=5)
        self.pause(self.r1, names[1], "five_hour", time.time() + 1200, account="account-spent")
        feeder.tick()
        blocks = json.loads((self.own / "account-blocks.json").read_text())
        self.assertEqual(sorted(blocks), ["account-spent"])
        self.assertEqual(len(self.manifest(self.r2)["assignments"]), 1)  # the fresh account still works

    def test_seven_day_pause_hands_the_lineage_off_to_a_runner_with_quota(self):
        self.set_config(tokens={"wave101": "/x/account-a", "wave102": "/x/account-h"})
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=2,
                                   claim={"route_review": {"from": "x"}, "retries": 1})
        self.write_ws(self.r1, base, sorry=2, lines=5)
        self.pause(self.r1, names[1], "seven_day", time.time() + 3 * 86400)
        status = feeder.tick()
        self.assertTrue((self.r1 / "jobs" / names[1] / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r1)["assignments"]], names[:2])
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["handoff"]["window"]), ("handoff", "seven_day"))
        # wave102 is kept free for it while the stopped round closes (no new target meanwhile)
        self.assertEqual(self.manifest(self.r2)["assignments"], [])
        self.assertEqual(status["runners"][self.r2.name]["assignment"], "held for a handoff")
        # the stopped round closed with a snapshot: it is the source of the RESUME lineage
        self.write_ws(self.r1, names[1], sorry=1, lines=50)
        self.set_history(self.r1, {base: dict(self.DONE, problem_id="green_tao"),
                                   names[1]: dict(self.DONE, problem_id="green_tao")})
        feeder.tick()
        resume = self.manifest(self.r2)["assignments"][0]
        self.assertEqual(Path(resume["overlay_dir"]).name, f"green_tao-from-{names[1]}")
        brief = (self.r2 / resume["brief_file"]).read_text()
        self.assertIn("role in this round: resume", brief)
        self.assertIn("Do not re-decide the route", brief)
        self.assertEqual(resume["auto_continue"]["max_rounds"], 1)  # 3 rounds by default, 2 done
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["runner"], claim["role"], claim["handoffs"], claim["retries"]),
                         ("running", str(self.r2), "RESUME", 1, 1))
        self.assertEqual(claim["route_review"], {"from": "x"})
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 2)  # account-a stays blocked

    def test_handoff_of_a_first_round_resumes_from_its_seed(self):
        self.set_config(tokens={"wave101": "/x/account-a", "wave102": "/x/account-h"})
        seed = self.write_ws(self.r2, "fleet-1-w102-000-green_tao", sorry=3, lines=5).parent
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=0, pending=1, claim={"seeded_from": str(seed)})
        self.pause(self.r1, names[0], "seven_day", time.time() + 86400)
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "handoff")
        self.set_history(self.r1, {base: {"phase": "failed", "error": "RuntimeBlocked"}})
        feeder.tick()
        resume = self.manifest(self.r2)["assignments"][0]
        self.assertEqual(Path(resume["overlay_dir"]).name, f"green_tao-from-{seed.name}")
        self.assertEqual(self.claims()["green_tao"]["role"], "RESUME")

    def test_five_hour_handoff_only_when_far_and_a_runner_is_free(self):
        self.set_config(tokens={"wave101": "/x/account-a", "wave102": "/x/account-h"})
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=1)
        self.write_ws(self.r1, base, sorry=2, lines=5)
        self.pause(self.r1, names[1], "five_hour", time.time() + 1800)
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "running")  # reset in 30 min: wait
        (self.r2 / "STOP").touch()
        self.pause(self.r1, names[1], "five_hour", time.time() + 7200)
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "running")  # no free runner with quota
        (self.r2 / "STOP").unlink()
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "handoff")

    def test_operator_paused_claims_are_adopted_only_with_the_flag(self):
        base = "fleet-1-w101-001-green_tao"
        self.write_ws(self.r1, base, sorry=1, lines=5)
        self.set_history(self.r1, {base: dict(self.DONE, problem_id="green_tao")})
        m = self.manifest(self.r1)
        m["assignments"] = [{"attempt_id": base, "problem_id": "green_tao"}]
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        (self.r1 / "STOP").touch()  # parked: the account is out of its weekly window
        (self.own / "claims.json").write_text(json.dumps({"green_tao": {
            "status": "paused", "runner": str(self.r1), "base": base, "rounds": 1, "paused_reason": "weekly"}}))
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao"}, {"problem_id": "hadwiger"}]}))
        feeder.tick()
        self.assertEqual([a["problem_id"] for a in self.manifest(self.r2)["assignments"]], ["hadwiger"])
        self.set_history(self.r2, {a["attempt_id"]: dict(self.DONE, problem_id="hadwiger")
                                   for a in self.manifest(self.r2)["assignments"]})
        self.set_config(adopt_paused_claims=True)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["role"], claim["runner"]), ("running", "RESUME", str(self.r2)))

    def test_closeness_and_finished_round_job(self):
        self.assertEqual(feeder.closeness({"complete": True}), 1.0)
        self.assertEqual(feeder.closeness({}, role="VERIFY"), 1.0)
        self.assertEqual(feeder.closeness({"after": {"sorry": 2}, "frontier_size": 2, "kind": "sorry"}), 0.35)
        self.assertEqual(feeder.closeness({"after": {"sorry": 2}, "frontier": ["a", "b"], "kind": "lines"}), 0.25)
        self.assertEqual(feeder.closeness({"after": {"sorry": 0}, "kind": "residual"}), 0.99)
        base = "fleet-1-w101-001-green_tao"
        for job in (base, base + "-r2"):
            self.write_ws(self.r1, job, sorry=1, lines=1)
        self.set_history(self.r1, {base: self.DONE, base + "-r2": self.DONE, base + "-r3": {"phase": "failed"}})
        claim = {"runner": str(self.r1), "base": base, "rounds": 4}  # r4 never started
        self.assertIsNone(feeder.last_round_job(claim))
        self.assertEqual(feeder.finished_round_job(claim), self.r1 / "jobs" / (base + "-r2"))

    # 5. stall rules

    def stalled_rounds(self, sorries, notes=None, pending=1):
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=len(sorries), pending=pending)
        for i, (name, sorry) in enumerate(zip(names, sorries)):
            ws = self.write_ws(self.r1, name, sorry=sorry, lines=10 + 400 * i, full_ok=True)
            if notes:
                (ws / "NOTES.md").write_text(notes[i])
        return names

    def test_churn_rule_three_rounds_without_a_new_sorry_minimum(self):
        names = self.stalled_rounds([3, 3, 4, 5])  # lines, split, split: each round is progress
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertTrue((self.r1 / "jobs" / names[4] / "STOP").exists())
        self.assertIn("churn: 3 rounds without a new sorry minimum", claim["route_review"]["reason"])
        self.assertEqual([claim["history"][i].split(": ")[1].split(";")[0] for i in range(3)],
                         ["lines", "split", "split"])
        self.assertEqual(claim["role"], "ROUTE_REVIEW")

    def test_churn_rule_spares_decomposition_before_a_new_minimum(self):
        names = self.stalled_rounds([2, 3, 4])  # bounded_mult: 2, 3, 4 and then 0
        feeder.tick()
        self.assertFalse((self.r1 / "jobs" / names[3] / "STOP").exists())
        self.assertEqual(self.claims()["green_tao"]["base"], names[0])

    def table(self, *rows):
        return ("# HANDOFF\n## RESIDUALS\n| name | status | source | est. lines |\n|---|---|---|---|\n"
                + "".join(f"| `{k}` | {s} | src | {e} |\n" for k, s, e in rows))

    def test_estimate_stall_rule(self):
        notes = [self.table(("Core", "open", 50000)), self.table(("Core", "open", 48000)),
                 self.table(("Core", "replaced", 0), ("CoreA", "open", 47000))]
        names = self.stalled_rounds([1, 1, 1], notes)
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertTrue((self.r1 / "jobs" / names[3] / "STOP").exists())
        self.assertIn("estimate stall: remaining-work estimate 50000 -> 48000 -> 47000", claim["route_review"]["reason"])
        self.assertEqual(claim["trajectory"][-1][1:], [1, 47000, "residual"])

    def test_estimate_rule_spares_closed_rows_and_overrides_churn(self):
        notes = [self.table(("Core", "open", 50000), ("Side", "open", 100)),
                 self.table(("Core", "open", 48000), ("Side", "proved", 0)),
                 self.table(("Core", "open", 47000))]
        names = self.stalled_rounds([1, 1, 1], notes)
        feeder.tick()
        self.assertFalse((self.r1 / "jobs" / names[3] / "STOP").exists())
        # falling estimate (>25% a round) while the sorry count churns: not a stall
        notes = [self.table(("Core", "open", 40000)), self.table(("Core", "open", 20000)),
                 self.table(("Core", "open", 10000)), self.table(("Core", "open", 5000))]
        (self.own / "claims.json").unlink()
        names = self.stalled_rounds([3, 4, 5, 6], notes)
        feeder.tick()
        self.assertFalse((self.r1 / "jobs" / names[4] / "STOP").exists())

    # 6. retry seeds

    def test_retry_and_route_review_seed_from_the_best_round_on_the_host(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))
        jobs = {"fleet-1-w102-001-hadwiger": (2, True, 100), "fleet-1-w102-002-hadwiger": (2, True, 200),
                "fleet-1-w102-003-hadwiger": (1, False, 300),
                "fleet-1-w102-004-hadwiger-laneAlpha": (0, True, 400)}
        history = {}
        for name, (sorry, ok, at) in jobs.items():
            self.write_ws(self.r2, name, sorry=sorry, lines=5, full_ok=ok)
            history[name] = dict(self.DONE, problem_id="hadwiger", finished_at=at)
        self.set_history(self.r2, history)
        self.assertEqual(feeder.best_round_job("hadwiger", [self.r1, self.r2]),
                         self.r2 / "jobs" / "fleet-1-w102-002-hadwiger")
        (self.own / "claims.json").write_text(json.dumps({"hadwiger": {
            "status": "exhausted", "runner": str(self.r2), "base": "fleet-1-w102-003-hadwiger", "rounds": 1}}))
        feeder.tick()
        claim = self.claims()["hadwiger"]
        self.assertEqual(claim["seeded_from"], str(self.r2 / "jobs" / "fleet-1-w102-002-hadwiger"))

    # 7. parallel lineages

    def test_parallel_targets_tell_route_review_and_resume_what_to_avoid(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "avoid_tactics": ["T4 split Core", "T6 coordinates"],
             "parallel_of": "host-2 w617 lineage: special case k = 2"}]}))
        rr = feeder.sync_brief(self.r1, "b1", "green_tao", "ROUTE_REVIEW", {"history": "h"})
        text = (self.r1 / rr).read_text()
        self.assertIn("a sibling lineage is already following: host-2 w617 lineage: special case k = 2;", text)
        self.assertIn("do not use these tactics: T4 split Core; T6 coordinates; choose a different tactic.", text)
        text = (self.r1 / feeder.sync_brief(self.r1, "b2", "green_tao", "RESUME", {"reason": "r"})).read_text()
        self.assertIn("choose a different tactic", text)
        self.assertNotIn("sibling", (self.r1 / feeder.sync_brief(self.r1, "b3", "green_tao")).read_text())
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        self.assertNotIn("sibling", (self.r1 / feeder.sync_brief(self.r1, "b4", "green_tao", "ROUTE_REVIEW")).read_text())

    def test_a_pass_stops_the_problems_other_lineages_on_the_host(self):
        base = "fleet-1-w101-001-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0)
        self.set_history(self.r1, {base: {"phase": "done", "problem_id": "green_tao",
                                          "verification": {"status": "official_local_pass"}}})
        lane = "fleet-1-w102-001-green_tao-laneAlpha"
        m = self.manifest(self.r2)
        m["assignments"] = [{"attempt_id": lane, "problem_id": "green_tao"},
                            {"attempt_id": lane + "-r2", "problem_id": "green_tao"}]
        (self.r2 / "manifest.json").write_text(json.dumps(m))
        (self.r2 / "jobs" / lane).mkdir(parents=True)
        (self.r2 / "state.json").write_text(json.dumps({"status": "model_running", "history": {},
                                                        "current_job": str(self.r2 / "jobs" / lane)}))
        (self.own / "programs").mkdir()
        (self.own / "programs" / "green_tao.json").write_text(json.dumps({
            "problem_id": "green_tao", "status": "lanes", "created_at": "x",
            "lanes": {"Alpha": {"done": False, "dir": "Submission/Lanes/Alpha",
                                "claim": {"runner": str(self.r2), "base": lane, "status": "running"}}}}))
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "passed")
        self.assertTrue((self.r2 / "jobs" / lane / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r2)["assignments"]], [lane])
        prog = json.loads((self.own / "programs" / "green_tao.json").read_text())
        self.assertEqual((prog["status"], prog["lanes"]["Alpha"]["claim"]["status"]), ("stopped", "stopped"))

    # 8. host admission

    def fake_proc(self, available_kb, full_avg60):
        proc = Path(self.tmp.name) / "proc"
        (proc / "pressure").mkdir(parents=True, exist_ok=True)
        (proc / "meminfo").write_text(f"MemTotal:       134217728 kB\nMemAvailable:   {available_kb} kB\n")
        (proc / "pressure" / "memory").write_text(
            f"some avg10=1.00 avg60=2.00 avg300=0.50 total=1\nfull avg10=0.50 avg60={full_avg60} avg300=0.20 total=1\n")
        return mock.patch.object(feeder, "PROC", proc)

    def test_memory_admission_and_status_metrics(self):
        self.set_config(min_mem_available_gb=16)
        with self.fake_proc(8 * 2**20, 1.5):
            status = feeder.tick()
        self.assertEqual(self.manifest(self.r1)["assignments"], [])
        self.assertTrue(any("MemAvailable 8.0 GB below min_mem_available_gb 16" in a for a in status["alerts"]))
        self.assertEqual((status["host"]["mem_available_gb"], status["host"]["mem_total_gb"],
                          status["host"]["memory_full_avg60"]), (8.0, 128.0, 1.5))
        self.set_config(min_mem_available_gb=0, max_memory_pressure_avg60=1.0)
        with self.fake_proc(64 * 2**20, 1.5):
            status = feeder.tick()
        self.assertEqual(self.manifest(self.r1)["assignments"], [])
        self.assertTrue(any("memory pressure" in a for a in status["alerts"]))
        self.set_config(max_memory_pressure_avg60=5.0)
        with self.fake_proc(64 * 2**20, 1.5):
            feeder.tick()
        self.assertEqual(len(self.manifest(self.r1)["assignments"]), 1)

    def test_max_active_runners(self):
        self.set_config(max_active_runners=1)
        status = feeder.tick()
        counts = [len(self.manifest(r)["assignments"]) for r in (self.r1, self.r2)]
        self.assertEqual(counts, [1, 0])
        self.assertEqual(status["runners"][self.r2.name]["assignment"], "max_active_runners reached")
        self.assertEqual(status["host"]["active_runners"], 1)

    # addendum (a)-(e)

    def test_residual_parser_on_real_row_formats(self):
        notes = (
            "# HANDOFF\nNext step: x\n## RESIDUALS\n| name | status | source | est. lines |\n|---|---|---|---|\n"
            "| `hMain`: `∀ ε > 0, ∀ᶠ t, Submission.FI.Final.offPartMain 1 156 310 73 t ≤ ε * main` "
            "(hyp. of `friedlander_iwaniec_of_r14r`) | open | FI §5 | 10k |\n"
            "| `hGap`: `∀ ε > 0, mmGap t ≤ ε * main` | **open** | FI §4 | ~1,500 |\n"
            "| Submission.FI.Final.ComplGResidual 1 156 310 73 | replaced | - | 0 |\n"
            "| `hPrime`: `∀ ε > 0, ∀ᶠ t, mmPrime t ≤ ε * main` | proved/replaced | - | 0 |\n"
            "| h3 of Submission.theorem_1_of_three (SegOK B3) | open | Thm 3 | 200 |\n"
            "| `abs_le`: `|x| ≤ 1` | open | lemma | 2-4k |\n"
            "| `broken: unbalanced | open | y | 100 |\n"
            "| (older rows: all proved or replaced; see git history of NOTES.md) | - | - | - |\n"
            "### RESIDUALS (history)\n| `Old` | open | x | 99999 |\n"
            "## RESIDUALS (lane Beta)\n| residual | est. lines | status |\n|---|---|---|\n| `BetaCore` | 3000 | OPEN |\n"
            "## Next\n| Other | open | x | 1 |\n")
        rows = feeder.residual_rows(notes)
        self.assertEqual([(r["key"], r["status"], r["est"]) for r in rows], [
            ("hMain", "open", 10000), ("hGap", "open", 1500), ("Submission.FI.Final.ComplGResidual", "replaced", 0),
            ("hPrime", "proved", 0), ("h3", "open", 200), ("abs_le", "open", 2000), ("broken", "open", 100),
            ("BetaCore", "open", 3000)])
        ws = self.write_ws(self.r1, "n", sorry=1, lines=1)
        (ws / "NOTES.md").write_text(notes)
        summary = feeder.residual_summary(ws)
        self.assertEqual((summary["open"], summary["estimate"], summary["max"]), (6, 16800, 10000))
        self.assertEqual(feeder.open_residuals(ws), ["BetaCore", "abs_le", "broken", "h3", "hGap", "hMain"])
        self.assertEqual(feeder.parse_estimate("≈ 2,500 lines"), 2500)
        self.assertEqual(feeder.parse_estimate("1.5k"), 1500)
        self.assertIsNone(feeder.parse_estimate("-"))

    def test_residual_progress_needs_a_closed_row_or_a_lower_estimate(self):
        a = self.write_ws(self.r1, "a", sorry=1, lines=10)
        (a / "NOTES.md").write_text(self.table(("Core", "open", 9000)))
        renamed = self.write_ws(self.r1, "b", sorry=1, lines=12, full_ok=True)
        (renamed / "NOTES.md").write_text(self.table(("Core", "replaced", 0), ("CoreSmall", "open", 9000)))
        self.assertFalse(feeder.round_progress(a, renamed)["progress"])  # wilkies/milnor-style churn
        closed = self.write_ws(self.r1, "c", sorry=1, lines=12, full_ok=True)
        (closed / "NOTES.md").write_text(self.table(("Core", "proved", 0), ("Next", "open", 12000)))
        got = feeder.round_progress(a, closed)
        self.assertEqual((got["kind"], got["closed"], got["estimate"]), ("residual", ["Core"], 12000))
        broken = self.write_ws(self.r1, "d", sorry=1, lines=12, full_ok=False)
        (broken / "NOTES.md").write_text(self.table(("Core", "proved", 0)))
        self.assertFalse(feeder.round_progress(a, broken)["progress"])

    def test_one_program_per_problem_per_host(self):
        self.set_config(program_min_ref_modules=250)
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 600}]}))
        (self.own / "programs").mkdir()
        active = {"problem_id": "green_tao", "status": "waiting_seed", "lanes": {}, "created_at": "x",
                  "seed_job": str(self.r1 / "jobs" / "none")}
        (self.own / "programs" / "green_tao.json").write_text(json.dumps(active))
        self.assertTrue(feeder.program_active("green_tao"))
        with self.assertRaises(ValueError):
            feeder.start_program("green_tao", self.r1, {})
        base = "fleet-1-w101-009-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0, claim={"route_review": {"from": "x"}})
        self.write_ws(self.r1, base, sorry=1, lines=5)
        feeder.tick()
        self.assertEqual(self.claims()["green_tao"]["status"], "exhausted")  # no second program, no retry
        (self.own / "programs" / "green_tao.json").write_text(json.dumps(dict(active, status="stopped")))
        self.assertFalse(feeder.program_active("green_tao"))

    def test_status_signals(self):
        self.assertEqual([feeder.tier_of(t) for t in (
            {"ref_min_modules": 40, "n_solvers": 4}, {"ref_min_modules": 44, "n_solvers": 3},
            {"ref_min_modules": 300, "n_solvers": 3}, {"ref_min_modules": 700, "n_solvers": 5},
            {"ref_min_modules": 100, "n_solvers": 1}, {})],
            ["easy", "medium", "hard", "extreme", "extreme", None])
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 194, "n_solvers": 6}]}))
        notes = [self.table(("Core", "open", 9000)), self.table(("Core", "open", 4000))]
        self.stalled_rounds([2, 1], notes)
        status = feeder.tick()
        signal = status["signals"]["green_tao"]
        self.assertEqual((signal["tier"], signal["estimates"], signal["status"]), ("medium", [9000, 4000], "running"))
        self.assertEqual(signal["closeness"], round(1 / (1 + 1 + 0.5 * 2) + 0.1, 3))
        self.assertIsNotNone(json.loads((self.own / "status.json").read_text())["signals"]["green_tao"])

    # ------------------------------------------------------------------ coordinator inbox, claims v2 (P1)

    def inbox(self, **directive):
        """Write <OWN>/inbox/<id>.json as the coordinator does; later files get later mtimes."""
        folder = self.own / "inbox"
        folder.mkdir(exist_ok=True)
        path = folder / f"{directive.get('file', directive['id'])}.json"
        directive.pop("file", None)
        path.write_text(json.dumps(directive))
        stamp = time.time() - 1000 + len(list(folder.glob("*.json")))
        os.utime(path, (stamp, stamp))
        return path

    def seed(self, name, sorry=1, notes="## HANDOFF\n- Active tactic: T4 split Core\n"):
        seed = self.own / "seeds" / name
        (seed / "Submission").mkdir(parents=True, exist_ok=True)
        (seed / "Submission.lean").write_text("import Submission.A\n" + "theorem s : True := sorry\n" * sorry)
        (seed / "Submission" / "A.lean").write_text("theorem a : True := trivial\n")
        (seed / "NOTES.md").write_text(notes)
        return seed

    def processed(self):
        return json.loads((self.own / "inbox-processed.json").read_text())

    def test_inbox_parallel_start_is_a_second_claim_with_a_divergent_brief(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        base = "fleet-1-w101-001-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=1)
        seed = self.seed("green_tao-par-fleet-1-w113-002-green_tao-r3")
        did = "par.green_tao.20261014T1800Z"
        self.inbox(id=did, type="start_lineage", problem_id="green_tao", role="PARALLEL", seed_dir=str(seed),
                   prefer_runner="wave102", max_rounds=6, expires_at=time.time() + 600,
                   fields={"avoid_tactics": ["T4 split Core"], "parallel_of": "host-2/wave113 b: T4 split Core",
                           "history": "Sibling lineage b on host-2/wave113 (round, sorry, estimate, kind):"},
                   reason="parallel lineage: closeness 0.4")
        status = feeder.tick()
        [a] = self.manifest(self.r2)["assignments"]
        self.assertEqual((a["problem_id"], a["auto_continue"]["max_rounds"]), ("green_tao", 6))
        self.assertEqual(Path(a["overlay_dir"]).name, f"green_tao-seed-{seed.name}")
        self.assertTrue((self.r2 / a["overlay_dir"] / "Submission" / "A.lean").is_file())
        brief = (self.r2 / a["brief_file"]).read_text()
        self.assertIn("role in this round: parallel lineage", brief)
        self.assertIn("a sibling lineage is already following: host-2/wave113 b: T4 split Core;", brief)
        self.assertIn("do not use these tactics: T4 split Core;", brief)
        self.assertIn("Sibling lineage b on host-2/wave113", brief)
        claims = self.claims()
        self.assertEqual(claims["green_tao"]["runner"], str(self.r1))  # the main lineage is untouched
        second = claims["green_tao~2"]
        self.assertEqual((second["status"], second["runner"], second["problem_id"], second["key"], second["role"]),
                         ("running", str(self.r2), "green_tao", "green_tao~2", "PARALLEL"))
        self.assertTrue(second["route_review"])
        self.assertEqual(second["directive"], did)
        self.assertEqual(self.processed()[did]["status"], "applied")
        self.assertEqual(self.processed()[did]["detail"]["key"], "green_tao~2")
        self.assertFalse((self.own / "inbox" / f"{did}.json").exists())
        self.assertTrue((self.own / "inbox" / "processed" / f"{did}.json").exists())
        self.assertEqual(status["signals"]["green_tao~2"]["problem_id"], "green_tao")
        self.assertEqual(status["inbox"]["recent"][did]["status"], "applied")
        # a re-sent id is applied once: the duplicate is dropped
        self.set_history(self.r2, {a["attempt_id"]: dict(self.DONE, problem_id="green_tao")})
        self.inbox(id=did, type="start_lineage", problem_id="green_tao", role="PARALLEL", seed_dir=str(seed))
        feeder.tick()
        self.assertFalse((self.own / "inbox" / f"{did}.json").exists())
        self.assertEqual(sum(x["problem_id"] == "green_tao" for x in self.manifest(self.r2)["assignments"]), 1)

    def test_inbox_ci_fit_of_a_pass_held_by_the_ci_gate(self):
        # 2026-10-16: poincare_3d_smooth passed locally but its package waits on the CI gate (460 min)
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        claims = {"green_tao": {"status": "passed", "runner": str(self.r1), "base": "fleet-1-w101-001-green_tao"}}
        (self.own / "claims.json").write_text(json.dumps(claims))
        seed = self.seed("green_tao-ci-module", sorry=0)
        self.inbox(id="ci.early", type="start_lineage", problem_id="green_tao", role="CI_FIT", seed_dir=str(seed))
        self.inbox(id="par.passed", type="start_lineage", problem_id="green_tao", role="PARALLEL", seed_dir=str(seed))
        feeder.tick()
        done = self.processed()
        self.assertIn("the problem passed", str(done["ci.early"]["detail"]))  # no held package yet
        self.assertIn("the problem passed", str(done["par.passed"]["detail"]))
        package = self.own / "outbox" / "green_tao" / "791b4817023576e6" / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"problem_id": "green_tao", "status": "hold_ci_review"}))
        self.inbox(id="ci.held", type="start_lineage", problem_id="green_tao", role="CI_FIT", seed_dir=str(seed),
                   fields={"measured": "module format"})
        feeder.tick()
        self.assertEqual(self.processed()["ci.held"]["status"], "applied")
        claims = self.claims()
        self.assertEqual(claims["green_tao"]["status"], "passed")  # the pass stays recorded
        self.assertEqual((claims["green_tao~2"]["role"], claims["green_tao~2"]["status"]), ("CI_FIT", "running"))
        # once a package is ready, a passed problem takes no further CI_FIT
        package.write_text(json.dumps({"problem_id": "green_tao", "status": "ready"}))
        self.inbox(id="ci.ready", type="start_lineage", problem_id="green_tao", role="CI_FIT", seed_dir=str(seed))
        feeder.tick()
        self.assertIn("the problem passed", str(self.processed()["ci.ready"]["detail"]))

    def test_ci_fit_brief_stays_in_later_rounds(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        seed = self.seed("green_tao-ci", sorry=0)
        self.inbox(id="ci.keep", type="start_lineage", problem_id="green_tao", role="CI_FIT", seed_dir=str(seed),
                   max_rounds=3, fields={"measured": "ComplexFirst is 55k module-seconds"})
        feeder.tick()
        a = [x for r in (self.r1, self.r2) for x in self.manifest(r)["assignments"]][0]
        root = self.r1 if a in self.manifest(self.r1)["assignments"] else self.r2
        m = self.manifest(root)
        m["assignments"].append(dict(a, attempt_id=a["attempt_id"] + "-r2", kind="continuation"))
        (root / "manifest.json").write_text(json.dumps(m))
        (root / "jobs" / (a["attempt_id"] + "-r2")).mkdir(parents=True)
        (root / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(root / "jobs" / (a["attempt_id"] + "-r2")),
            "history": {a["attempt_id"]: dict(self.DONE, problem_id="green_tao")}}))
        brief = root / a["brief_file"]
        brief.write_text("stale")
        feeder.tick()
        self.assertIn("ComplexFirst is 55k module-seconds", brief.read_text())

    def test_inbox_start_validation_and_waiting(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao"}, {"problem_id": "linnik", "disabled": True}]}))
        (self.r1 / "STOP").touch()
        (self.r2 / "STOP").touch()
        seed = self.seed("duffin_schaeffer-xfer-j")
        elsewhere = Path(self.tmp.name) / "elsewhere"
        shutil.copytree(seed, elsewhere)
        (self.own / "seeds" / "evil").symlink_to(elsewhere)
        good = dict(type="start_lineage", problem_id="duffin_schaeffer", role="RESUME", seed_dir=str(seed))
        self.inbox(id="bad-outside", **dict(good, seed_dir=str(elsewhere)))
        self.inbox(id="bad-symlink", **dict(good, seed_dir=str(self.own / "seeds" / "evil")))
        self.inbox(id="bad-relative", **dict(good, seed_dir="seeds/duffin_schaeffer-xfer-j"))
        self.inbox(id="bad-role", **dict(good, role="ARCHITECT"))
        self.inbox(id="bad-disabled", **dict(good, problem_id="linnik"))
        self.inbox(id="bad-type", type="set_runtime", runner="wave101")
        self.inbox(id="bad-id", file="file-name", **good)
        self.inbox(id="late", expires_at=time.time() - 5, **good)
        self.inbox(id="xfer.ds.a1", expires_at=time.time() + 600, prefer_runner="wave101", max_rounds=3,
                   reason="seven_day pause of account-a on host-2", from_host="host-2", route_review_done=True,
                   target={"problem_id": "duffin_schaeffer", "ref_min_modules": 300, "n_solvers": 3,
                           "max_rounds": 5, "evil": "x"}, fields={"reason": "seven_day pause of account-a on host-2"},
                   **good)
        status = feeder.tick()
        done = self.processed()
        for did, word in (("bad-outside", "not under"), ("bad-symlink", "not under"), ("bad-relative", "absolute"),
                          ("bad-role", "role must be"), ("bad-disabled", "disabled"), ("bad-type", "unknown type"),
                          ("file-name", "differs from the file name")):
            self.assertEqual(done[did]["status"], "rejected", did)
            self.assertIn(word, str(done[did]["detail"]), did)
        self.assertEqual(done["late"]["status"], "expired")
        # no runner can take the valid one: it waits in the inbox, status.json says why
        self.assertTrue((self.own / "inbox" / "xfer.ds.a1.json").exists())
        [waiting] = status["inbox"]["pending"]
        self.assertEqual((waiting["id"], waiting["waiting"]), ("xfer.ds.a1", "no idle runner with quota"))
        # wave102 frees up; the start prefers wave101 (still stopped), so wave102 takes it
        (self.r2 / "STOP").unlink()
        feeder.tick()
        [a] = self.manifest(self.r2)["assignments"]
        self.assertEqual((a["problem_id"], a["auto_continue"]["max_rounds"]), ("duffin_schaeffer", 3))
        brief = (self.r2 / a["brief_file"]).read_text()
        self.assertIn("role in this round: resume", brief)
        self.assertIn("seven_day pause of account-a on host-2", brief)
        claim = self.claims()["duffin_schaeffer"]
        self.assertEqual((claim["role"], claim["handoffs"], claim["problem_id"]), ("RESUME", 1, "duffin_schaeffer"))
        self.assertTrue(claim["route_review"])  # its route review ran on the old host: a stall ends it
        target = next(t for t in json.loads((self.own / "targets.json").read_text())["targets"]
                      if t["problem_id"] == "duffin_schaeffer")
        self.assertEqual(target, {"problem_id": "duffin_schaeffer", "ref_min_modules": 300, "n_solvers": 3,
                                  "max_rounds": 5})
        self.assertEqual(self.processed()["xfer.ds.a1"]["status"], "applied")

    def test_inbox_stop_marks_claims_blocks_targets_and_ends_programs(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao"}, {"problem_id": "hadwiger"}, {"problem_id": "duffin_schaeffer"}]}))
        base = "fleet-1-w101-001-green_tao"
        names = self.setup_lineage(self.r1, base, finished=1, pending=2)
        lane = "fleet-1-w102-001-hadwiger-laneAlpha"
        m = self.manifest(self.r2)
        m["assignments"] = [{"attempt_id": lane, "problem_id": "hadwiger"},
                            {"attempt_id": lane + "-r2", "problem_id": "hadwiger"}]
        (self.r2 / "manifest.json").write_text(json.dumps(m))
        (self.r2 / "jobs" / lane).mkdir(parents=True)
        (self.r2 / "state.json").write_text(json.dumps({"status": "model_running", "history": {},
                                                        "current_job": str(self.r2 / "jobs" / lane)}))
        (self.own / "programs").mkdir()
        (self.own / "programs" / "hadwiger.json").write_text(json.dumps({
            "problem_id": "hadwiger", "status": "lanes", "created_at": "x",
            "lanes": {"Alpha": {"done": False, "dir": "Submission/Lanes/Alpha",
                                "claim": {"runner": str(self.r2), "base": lane, "status": "running"}}}}))
        claims = self.claims()
        claims["hadwiger"] = {"status": "program", "program": str(self.own / "programs" / "hadwiger.json")}
        (self.own / "claims.json").write_text(json.dumps(claims))
        self.inbox(id="pass-stop.green_tao.host-1", type="stop_lineage", problem_id="green_tao", what="all",
                   mark="passed_elsewhere", reason="green_tao passed on host-4 (x)")
        self.inbox(id="program-dedup.hadwiger.host-1.0", type="stop_lineage", problem_id="hadwiger",
                   what="program", mark="stopped", reason="duplicate lane program; host-3 keeps it")
        self.inbox(id="pass-stop.duffin_schaeffer.host-1", type="stop_lineage", problem_id="duffin_schaeffer",
                   mark="passed_elsewhere", reason="passed on host-2")
        self.inbox(id="par.green_tao.late", type="start_lineage", problem_id="green_tao", role="PARALLEL",
                   seed_dir=str(self.seed("green_tao-par-j")))
        feeder.tick()
        # green_tao: the running round got a controlled STOP, the unstarted one was dropped
        self.assertTrue((self.r1 / "jobs" / names[1] / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r1)["assignments"]], names[:2])
        claims = self.claims()
        self.assertEqual((claims["green_tao"]["status"], claims["green_tao"]["previous_status"],
                          claims["green_tao"]["stopped_by"]),
                         ("passed_elsewhere", "running", "pass-stop.green_tao.host-1"))
        # the program and its running lane stop; the claim stays a program claim
        prog = json.loads((self.own / "programs" / "hadwiger.json").read_text())
        self.assertEqual((prog["status"], prog["lanes"]["Alpha"]["claim"]["status"]), ("stopped", "stopped"))
        self.assertTrue((self.r2 / "jobs" / lane / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r2)["assignments"]], [lane])
        self.assertEqual((claims["hadwiger"]["status"], claims["hadwiger"]["program_status"]), ("program", "stopped"))
        # a problem without a claim here is blocked, so no feeder path picks it up
        self.assertEqual(claims["duffin_schaeffer"]["status"], "passed_elsewhere")
        done = self.processed()
        self.assertEqual(done["pass-stop.green_tao.host-1"]["detail"]["marked"], ["green_tao"])
        self.assertEqual(done["par.green_tao.late"]["status"], "rejected")
        self.assertIn("passed", done["par.green_tao.late"]["detail"])
        # both runners free: nothing of these problems is assigned again
        self.set_history(self.r1, {n: dict(self.DONE, problem_id="green_tao") for n in names[:2]})
        self.set_history(self.r2, {lane: dict(self.DONE, problem_id="hadwiger")})
        feeder.tick()
        assigned = [a["attempt_id"] for r in (self.r1, self.r2) for a in self.manifest(r)["assignments"]]
        self.assertEqual(assigned, names[:2] + [lane])
        self.assertEqual(feeder.tick()["runners"][self.r1.name]["assignment"], "idle")

    def test_inbox_delegate_by_base_checks_the_expected_status(self):
        self.set_config(tokens={"wave101": "/x/account-a", "wave102": "/x/account-h"})
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        base = "fleet-1-w101-001-green_tao"
        self.setup_lineage(self.r1, base, finished=2, pending=0,
                           claim={"status": "handoff", "handoff": {"window": "seven_day", "at_ts": time.time() - 4000}})
        self.write_ws(self.r1, base + "-r2", sorry=1, lines=5)
        release = dict(type="stop_lineage", problem_id="green_tao", base=base, mark="delegated", to_host="host-4",
                       expect_status=["handoff"], reason="moved to host-4/wave133")
        self.inbox(id="xfer.t.release", **release)
        self.inbox(id="xfer.t2.release", **release)
        self.inbox(id="xfer.t3.release", **dict(release, base="fleet-1-w101-009-green_tao"))
        feeder.tick()
        claim = self.claims()["green_tao"]
        self.assertEqual((claim["status"], claim["host"], claim["previous_status"]), ("delegated", "host-4", "handoff"))
        self.assertEqual(self.manifest(self.r2)["assignments"], [])  # no local RESUME of a delegated lineage
        done = self.processed()
        self.assertEqual(done["xfer.t.release"]["status"], "applied")
        self.assertEqual(done["xfer.t2.release"]["status"], "rejected")
        self.assertIn("is delegated, expected ['handoff']", done["xfer.t2.release"]["detail"])
        self.assertIn("no claim with base", done["xfer.t3.release"]["detail"])

    def test_hold_and_release_runner(self):
        self.inbox(id="hold.wave102", type="hold_runner", runner="wave102", reason="operator test")
        self.inbox(id="hold.nobody", type="hold_runner", runner="wave199")
        status = feeder.tick()
        self.assertEqual([a["problem_id"] for a in self.manifest(self.r1)["assignments"]], ["green_tao"])
        self.assertEqual(self.manifest(self.r2)["assignments"], [])
        self.assertEqual(status["runners"][self.r2.name]["assignment"], "held (operator test)")
        self.assertTrue(status["runners"][self.r2.name]["held"])
        self.assertIn("wave102", json.loads((self.own / "holds.json").read_text()))
        self.assertEqual(self.processed()["hold.nobody"]["status"], "rejected")
        self.inbox(id="release.wave102", type="release_runner", runner="fleet-1-wave102")
        feeder.tick()
        self.assertEqual([a["problem_id"] for a in self.manifest(self.r2)["assignments"]], ["hadwiger"])
        self.assertEqual(json.loads((self.own / "holds.json").read_text()), {})

    def test_second_lineage_ends_on_a_stall_and_never_becomes_a_program(self):
        self.set_config(program_min_ref_modules=1)
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "green_tao", "ref_min_modules": 500}, {"problem_id": "hadwiger"}]}))
        base = "fleet-1-w102-001-green_tao"
        names = self.setup_lineage(self.r2, base, finished=2, pending=1)
        self.write_ws(self.r2, names[0], sorry=3, lines=5)
        self.write_ws(self.r2, names[1], sorry=3, lines=6, full_ok=True)  # no progress in round 2
        claims = self.claims()
        claims["green_tao~2"] = dict(claims.pop("green_tao"), problem_id="green_tao", key="green_tao~2",
                                     role="PARALLEL")
        # the main lineage ended here earlier; it may not be retried while the second one runs
        claims["green_tao"] = {"status": "exhausted", "runner": str(self.r1), "base": "b0", "retries": 0}
        (self.own / "claims.json").write_text(json.dumps(claims))
        feeder.tick()
        second = self.claims()["green_tao~2"]
        self.assertTrue((self.r2 / "jobs" / names[2] / "STOP").exists())
        self.assertEqual(second["last_decision"]["decision"], "stop")  # no route review for a second lineage
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r2)["assignments"]], names)
        self.assertEqual([a["problem_id"] for a in self.manifest(self.r1)["assignments"]], ["hadwiger"])  # no retry
        self.set_history(self.r2, {n: dict(self.DONE, problem_id="green_tao") for n in names})
        feeder.tick()
        self.assertEqual(self.claims()["green_tao~2"]["status"], "exhausted")
        self.assertFalse((self.own / "programs" / "green_tao.json").exists())

    def test_a_pass_on_the_host_stops_the_second_lineage(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        base = "fleet-1-w101-001-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0)
        self.set_history(self.r1, {base: {"phase": "done", "problem_id": "green_tao",
                                          "verification": {"status": "official_local_pass"}}})
        second = "fleet-1-w102-001-green_tao"
        names = self.setup_lineage(self.r2, second, finished=0, pending=2)
        claims = self.claims()
        claims["green_tao~2"] = dict(claims["green_tao"], problem_id="green_tao", key="green_tao~2")
        claims["green_tao"] = {"status": "running", "runner": str(self.r1), "base": base}
        (self.own / "claims.json").write_text(json.dumps(claims))
        feeder.tick()
        claims = self.claims()
        self.assertEqual((claims["green_tao"]["status"], claims["green_tao~2"]["status"]), ("passed", "stopped"))
        self.assertTrue((self.r2 / "jobs" / names[0] / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r2)["assignments"]], names[:1])

    def test_a_ci_fit_pass_keeps_a_parallel_ci_fit_lineage(self):
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "green_tao"}]}))
        base = "fleet-1-w101-001-green_tao"
        self.setup_lineage(self.r1, base, finished=1, pending=0)
        self.set_history(self.r1, {base: {"phase": "done", "problem_id": "green_tao",
                                          "verification": {"status": "official_local_pass"}}})
        second = "fleet-1-w102-001-green_tao"
        names = self.setup_lineage(self.r2, second, finished=0, pending=2)
        claims = self.claims()
        claims["green_tao~2"] = dict(claims["green_tao"], problem_id="green_tao", key="green_tao~2", role="CI_FIT")
        claims["green_tao"] = {"status": "running", "runner": str(self.r1), "base": base, "role": "CI_FIT"}
        (self.own / "claims.json").write_text(json.dumps(claims))
        feeder.tick()
        claims = self.claims()
        self.assertEqual((claims["green_tao"]["status"], claims["green_tao~2"]["status"]), ("passed", "running"))
        self.assertFalse((self.r2 / "jobs" / names[0] / "STOP").exists())
        self.assertEqual([a["attempt_id"] for a in self.manifest(self.r2)["assignments"]], names)

    def test_claim_key_and_pid_helpers(self):
        claims = {"x": {"status": "running"}, "x~2": {"status": "exhausted", "problem_id": "x"},
                  "y": {"status": "passed"}}
        self.assertEqual(feeder.claim_key(claims, "x"), "x~2")  # a finished second claim is reused
        claims["x~2"]["status"] = "handoff"
        self.assertEqual(feeder.claim_key(claims, "x"), "x~3")
        self.assertEqual(feeder.claim_key(claims, "y"), "y")
        self.assertEqual(feeder.claim_pid("x~2", {}), "x")
        self.assertEqual(feeder.claim_pid("odd", {"problem_id": "x"}), "x")
        self.assertEqual([k for k, _ in feeder.claims_of(claims, "x")], ["x", "x~2"])

    def test_every_pass_is_kept_and_packaged_by_digest(self):
        runs, bench, first, _ = self.synthetic_pass(digest="a" * 64, run_id="run-a")
        with mock.patch.object(feeder, "RUNS", runs), mock.patch.object(feeder, "BENCH_REPO", bench):
            feeder.tick()
            kept = self.r1 / "passes" / f"green_tao-{'a' * 16}.json"
            self.assertEqual(kept.read_bytes(), first.read_bytes())
            # a later pass of the problem on this runner overwrites PASS-green_tao.json
            self.synthetic_pass(digest="b" * 64, run_id="run-b", text="theorem y : True := trivial\n")
            status = feeder.tick()
        self.assertEqual(sorted(p.name for p in (self.r1 / "passes").glob("*.json")),
                         [f"green_tao-{'a' * 16}.json", f"green_tao-{'b' * 16}.json"])
        self.assertTrue((self.own / "outbox/green_tao" / ("a" * 16) / "package.json").is_file())
        package = json.loads((self.own / "outbox/green_tao" / ("b" * 16) / "package.json").read_text())
        self.assertEqual(Path(package["pass_file"]).parent.name, "passes")
        self.assertEqual(sorted(p["artifact_digest"][:1] for p in status["packages"]), ["a", "b"])

    def test_provenance_names_the_runtime_the_job_ran_with(self):
        runtimes = Path(self.tmp.name) / "runtimes"
        for rid in ("lrh-abc12345", "lrh-pin00000"):
            (runtimes / rid).mkdir(parents=True)
            (runtimes / rid / "runtime-manifest.json").write_text(json.dumps({"runtime_id": rid}))
        attempt = "fleet-1-w101-003-green_tao-r2"
        (self.r1 / "jobs" / attempt).mkdir(parents=True)
        (self.r1 / "jobs" / attempt / "worker.json").write_text(json.dumps(
            {"remote_agent": str(runtimes / "lrh-abc12345")}))
        self.set_history(self.r1, {attempt: {"phase": "done", "receipt_run_id": "rid",
                                             "verification": {"artifact_digest": "d" * 64}}})
        (self.r1 / "controller.py").write_text(f'RUNTIME = Path("{runtimes / "lrh-pin00000"}")\n')
        runs, bench, pass_path, _ = self.synthetic_pass()
        with mock.patch.object(feeder, "RUNS", runs), mock.patch.object(feeder, "BENCH_REPO", bench):
            feeder.package_pass(self.r1, pass_path)
            prov = json.loads((self.own / "outbox/green_tao" / ("d" * 16) / "tree/PROVENANCE.json").read_text())
            self.assertIn("campaign runtime lrh-abc12345)", prov["system"])
            self.assertNotIn("v16", prov["system"])
            self.assertEqual((prov["runtime"]["runtime_id"], prov["runtime"]["attempt"]), ("lrh-abc12345", attempt))
            self.assertIn("worker.json", prov["runtime"]["source"])
            # no job record of the pass: the queue's pin at packaging time, and it says so
            _, _, other, _ = self.synthetic_pass(digest="e" * 64, run_id="rid-e", text="theorem z : True := trivial\n")
            feeder.package_pass(self.r1, other)
            prov = json.loads((self.own / "outbox/green_tao" / ("e" * 16) / "tree/PROVENANCE.json").read_text())
            self.assertEqual(prov["runtime"]["runtime_id"], "lrh-pin00000")
            self.assertEqual(prov["runtime"]["source"], "queue controller pin at packaging time")
            (self.r1 / "controller.py").unlink()
            _, _, third, _ = self.synthetic_pass(digest="f" * 64, run_id="rid-f", text="theorem w : True := trivial\n")
            feeder.package_pass(self.r1, third)
            prov = json.loads((self.own / "outbox/green_tao" / ("f" * 16) / "tree/PROVENANCE.json").read_text())
            self.assertIn("campaign runtime not recorded)", prov["system"])


class ReuseNotesTest(unittest.TestCase):
    def test_reuse_section_of_the_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "NOTES.md").write_text("## HANDOFF\nx\n## REUSE\n- repo a@1234, files B.lean (Apache-2.0)\n"
                                         "### details\nnot this\n## RESIDUALS\n")
            self.assertEqual(feeder.reuse_notes(ws), "- repo a@1234, files B.lean (Apache-2.0)")
            (ws / "NOTES.md").write_text("## HANDOFF\n")
            self.assertEqual(feeder.reuse_notes(ws), "")


class LegacyKeysTest(unittest.TestCase):
    def test_renamed_keys_are_read_under_their_new_names(self):
        self.assertEqual(feeder.upgrade_keys({"rival_min_modules": 300, "program_min_rival_modules": 250}),
                         {"ref_min_modules": 300, "program_min_ref_modules": 250})
        self.assertEqual(feeder.upgrade_keys({"rival_min_modules": 1, "ref_min_modules": 2}), {"ref_min_modules": 2})


class ControllerHaltTest(unittest.TestCase):
    def test_security_event_halts_even_without_a_model_record(self):
        spec = importlib.util.spec_from_file_location("ctl3", HERE.parent / "server/controller.py")
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            self.assertIsNone(ctl.halt_status(job, {"phase": "failed"}))
            self.assertEqual(ctl.halt_status(job, {"model": {"stop_reason": "authentication_failure"}}),
                             "halted_authentication_failure")
            (job / "SECURITY-EVENT.json").write_text("{}")
            self.assertEqual(ctl.halt_status(job, {"phase": "failed"}), "halted_credential_access")


class CredentialHaltTest(unittest.TestCase):
    def test_a_runner_halted_for_credential_access_is_never_restarted(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(feeder, "OWN", Path(tmp)), \
                mock.patch.object(feeder, "controller_pids", lambda root: []), \
                mock.patch.object(feeder.subprocess, "run") as run:
            root = Path(tmp) / "fleet-1-wave101"
            root.mkdir()
            alerts = []
            note = feeder.ensure_running(root, {"status": "halted_credential_access"}, alerts)
            self.assertIn("waiting for operator", note)
            self.assertIn("leh-runner@wave101", alerts[0])
            run.assert_not_called()


class ControllerDeriveTest(unittest.TestCase):
    def test_lease_is_per_queue_root(self):
        text = (HERE.parent / "server/controller.py").read_text()
        self.assertIn('LEASES / (root.name + ".lock")', text)
        self.assertNotIn("host-leases", text)
        compile(text, "controller.py", "exec")

    def test_builtin_overlay_copies_only_proof_files(self):
        spec = importlib.util.spec_from_file_location("ctl", HERE.parent / "server/controller.py")
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LEH_TRANSFER_RECEIVER": ""}):
            source, workspace = Path(tmp) / "seed", Path(tmp) / "ws"
            (source / "Submission/A").mkdir(parents=True)
            (source / "Submission.lean").write_text("import Submission.A.B\n")
            (source / "Submission/A/B.lean").write_text("theorem b : True := trivial\n")
            (source / "NOTES.md").write_text("## HANDOFF\n")
            (source / "lakefile.toml").write_text("evil\n")
            workspace.mkdir()
            (workspace / "lakefile.toml").write_text("trusted\n")
            result = ctl.overlay(source, workspace)
            self.assertEqual(sorted(result["files"]), ["NOTES.md", "Submission.lean", "Submission/A/B.lean"])
            self.assertEqual((workspace / "lakefile.toml").read_text(), "trusted\n")
            self.assertEqual((workspace / "Submission/A/B.lean").read_text(), "theorem b : True := trivial\n")
            self.assertIs(result["official_verification"], False)
            with self.assertRaises(ValueError):
                ctl.overlay(Path(tmp) / "ws/.empty-does-not-exist", workspace)

    def test_controller_layout_and_host_allowlist(self):
        spec = importlib.util.spec_from_file_location("ctl2", HERE.parent / "server/controller.py")
        ctl = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"LEH_HOSTS": "host-1,host-2", "LEH_RUNS_ROOT": "/srv/runs"}):
            spec.loader.exec_module(ctl)
        self.assertEqual(ctl.HOSTS, {"host-1", "host-2"})
        self.assertEqual(ctl.RUNS, Path("/srv/runs"))
        manifest = {"requested_config": ctl.POLICY, "host": "host-3", "assignments": []}
        with self.assertRaises(ValueError):
            ctl.validate(manifest, "host-3", Path("/x"))
        ctl.HOSTS = set()
        self.assertEqual(ctl.validate(manifest, "host-3", Path("/x")), [])


class AccountLabelTest(unittest.TestCase):
    def test_labels_match_the_runtime(self):
        self.assertEqual(feeder.account_label("file:~/.config/leh/accounts/account-a.token"), "account-a")
        self.assertEqual(feeder.account_label("env:LEH_TOKEN_B"), "LEH_TOKEN_B")
        self.assertEqual(feeder.account_label("/keys/account-c"), "account-c")
        root = Path("/srv/fleet/projects/lean-eval-campaigns/fleet-1-wave101")
        self.assertEqual(feeder.account_of(root, {"tokens": {"wave101": "file:/k/account-d.token"}}), "account-d")
        self.assertEqual(feeder.account_of(root, {}), "default")


if __name__ == "__main__":
    sys.exit(unittest.main())
