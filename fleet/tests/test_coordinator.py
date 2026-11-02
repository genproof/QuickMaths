"""Offline controls for the fleet coordinator (no ssh, no hosts): plan(), ledger, executor, gather."""
from __future__ import annotations

import datetime
import importlib.util
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coordinator = load("leh_coordinator", HERE.parent / "local/coordinator.py")
gather = load("leh_coordinator_gather", HERE.parent / "local/coordinator_gather.py")
feeder = load("leh_feeder_for_gather", HERE.parent / "server/feeder.py")

NOW = 1_800_000_000.0
CAMP = "/srv/fleet/projects/lean-eval-campaigns"
SEEDS = "/srv/fleet/leh-fleet/seeds"


def job(wave: str, name: str) -> str:
    return f"{CAMP}/fleet-1-{wave}/jobs/{name}"


def runner(account: str, idle: bool = True, **extra) -> dict:
    return dict({"account": account, "idle": idle, "stop": False, "held": False,
                 "note": "idle" if idle else "assigned x"}, **extra)


def lineage(pid: str, wave: str = "wave113", account: str = "account-g", status: str = "running",
            closeness: float = 0.4, **extra) -> dict:
    base = f"fleet-1-w{wave[4:]}-002-{pid}"
    return dict({"key": pid, "problem_id": pid, "status": status, "role": None, "tier": "medium",
                 "runner": wave, "account": account, "base": base, "closeness": closeness,
                 "round_open": False, "handoff_at": None, "handoff_window": None, "rounds_done": 3,
                 "source_job": job(wave, f"{base}-r3"), "source_sorry": 2, "source_build_ok": True,
                 "source_complete": False, "active_tactic": None, "trying": [], "verifies": 0,
                 "verify_exhausted": False, "route_review": False, "handoffs": 0,
                 "trajectory": [[base, 3, 4000, None], [f"{base}-r2", 2, 3000, "residual"]]}, **extra)


def host(runners=None, lineages=None, **extra) -> dict:
    return dict({"ok": True, "checked_ts": NOW - 60, "inbox_supported": True, "own_root": SEEDS.rsplit("/", 1)[0],
                 "runners": runners or {},
                 "account_blocks": {}, "lineages": lineages or [], "passed": [], "programs": {}, "best": {},
                 "claims": {}, "targets": {}, "our_solved": [], "inbox": {"pending": [], "results": {}}}, **extra)


def policy(**overrides) -> dict:
    p = coordinator.merged_policy({"defaults": {"max_rounds": 10}, "coordinator": {
        "hosts": {"host-1": {"speed": 0.33, "heavy_single": False}}}})
    p.update(overrides)
    return p


def directives(actions, kind=None, host=None):
    return [a for a in actions if a["kind"] == "directive"
            and (kind is None or a["directive"]["type"] == kind) and (host is None or a["host"] == host)]


class PlanTest(unittest.TestCase):
    def test_a_pass_stops_the_problem_on_every_other_host_once(self):
        state = {"hosts": {
            "host-2": host(passed=[{"key": "green_tao", "problem_id": "green_tao", "attempt": "w613-002-r4"}],
                           claims={"green_tao": {"problem_id": "green_tao", "status": "passed"}}),
            "host-3": host(lineages=[lineage("green_tao", "wave123")],
                           claims={"green_tao": {"problem_id": "green_tao", "status": "running"}}),
            "host-4": host(targets={"green_tao": {"problem_id": "green_tao"}}),  # unclaimed target
            "host-1": host(claims={"green_tao": {"problem_id": "green_tao", "status": "exhausted"}}),  # retryable
            "host-5": host(claims={"green_tao": {"problem_id": "green_tao", "status": "delegated"}}),
        }}
        actions = coordinator.plan(state, NOW, policy())
        stops = directives(actions, "stop_lineage")
        self.assertEqual(sorted(a["host"] for a in stops), ["host-1", "host-3", "host-4"])
        for action in stops:
            d = action["directive"]
            self.assertEqual((d["problem_id"], d["mark"], d["what"]), ("green_tao", "passed_elsewhere", "all"))
            self.assertEqual(d["id"], f"pass-stop.green_tao.{action['host']}")
            self.assertIn("passed on host-2", d["reason"])
        # sent once: the ledger knows the ids
        ledger = {"directives": {a["directive"]["id"]: {"state": "sent"} for a in stops}}
        self.assertEqual(coordinator.plan(dict(state, ledger=ledger), NOW + 300, policy()), [])

    def test_a_pass_held_at_the_ci_fit_gate_spares_ci_fit_lineages_elsewhere(self):
        passed = [{"key": "poincare", "problem_id": "poincare", "attempt": "w643-004", "ci_held": True}]
        fit = lineage("poincare", "wave133", "account-h", role="CI_FIT")
        state = {"hosts": {
            "host-5": host(passed=passed, claims={"poincare": {"problem_id": "poincare", "status": "passed"}}),
            "host-4": host(lineages=[fit], claims={"poincare": {"problem_id": "poincare", "status": "running",
                                                               "role": "CI_FIT"}}),
            "host-3": host(lineages=[lineage("poincare", "wave123")],
                           claims={"poincare": {"problem_id": "poincare", "status": "running"}}),
        }}
        stops = directives(coordinator.plan(state, NOW, policy()), "stop_lineage")
        self.assertEqual([a["host"] for a in stops], ["host-3"])
        # a pass that fits CI (not held) stops the CI_FIT lineage too
        state["hosts"]["host-5"]["passed"] = [dict(passed[0], ci_held=False)]
        stops = directives(coordinator.plan(state, NOW, policy()), "stop_lineage")
        self.assertEqual(sorted(a["host"] for a in stops), ["host-3", "host-4"])

    def test_cross_host_handoff_respects_blocks_and_host_preferences(self):
        moved = lineage("pi_sphere", "wave111", "account-a", status="handoff", handoff_at=NOW - 2000,
                        handoff_window="seven_day", tier="hard", route_review=True, active_tactic="T-REL")
        state = {"hosts": {
            "host-2": host({"wave111": runner("account-a"), "wave113": runner("account-g", idle=False)}, [moved],
                           account_blocks={"account-a": {"until": NOW + 3 * 86400, "window": "seven_day"}}),
            "host-1": host({"wave101": runner("account-d")}),          # slow, no heavy single lineages
            "host-3": host({"wave123": runner("account-g")}),          # account-g blocked, seen on host-4
            "host-4": host({"wave133": runner("account-h"), "wave137": runner("account-j")},
                           account_blocks={"account-g": {"until": NOW + 7200, "window": "five_hour"}},
                           targets={"pi_sphere": {"problem_id": "pi_sphere", "max_rounds": 8}}),
        }}
        actions = coordinator.plan(state, NOW, policy(parallel=False))
        exports = [a for a in actions if a["kind"] == "export_seed"]
        self.assertEqual(len(exports), 1)
        seed = f"{SEEDS}/pi_sphere-xfer-fleet-1-w111-002-pi_sphere-r3"
        self.assertEqual((exports[0]["src_host"], exports[0]["dest_host"], exports[0]["seed_dir"]),
                         ("host-2", "host-4", seed))
        [release] = directives(actions, "stop_lineage", "host-2")
        self.assertEqual((release["directive"]["base"], release["directive"]["mark"], release["directive"]["to_host"],
                          release["directive"]["expect_status"]),
                         (moved["base"], "delegated", "host-4", ["handoff"]))
        [start] = directives(actions, "start_lineage", "host-4")
        d = start["directive"]
        self.assertEqual((d["role"], d["prefer_runner"], d["seed_dir"], d["max_rounds"], d["route_review_done"]),
                         ("RESUME", "wave133", seed, 8 - 3, True))
        self.assertEqual(d["expires_at"], NOW + 1800)
        self.assertEqual(d["target"]["problem_id"], "pi_sphere")
        # export, then the release, then the start: one group, in that order
        group = [a for a in actions if a.get("group") == start["group"]]
        self.assertEqual([a["kind"] for a in group], ["export_seed", "directive", "directive"])
        # a light problem may go to host-1 when nothing faster is free; a heavy one never
        busy5 = dict(state["hosts"]["host-4"], runners={"wave133": runner("account-h", idle=False)})
        light = {"hosts": dict(state["hosts"], **{"host-4": busy5, "host-2": dict(
            state["hosts"]["host-2"], lineages=[dict(moved, tier="medium")])})}
        self.assertEqual([a["host"] for a in directives(coordinator.plan(light, NOW, policy(parallel=False)),
                                                         "start_lineage")], ["host-1"])
        heavy = {"hosts": dict(state["hosts"], **{"host-4": busy5})}
        actions = coordinator.plan(heavy, NOW, policy(parallel=False))
        self.assertEqual(directives(actions), [])
        self.assertTrue(any("no idle runner with quota" in a["message"] for a in actions if a["kind"] == "alert"))

    def test_handoff_stays_local_while_its_host_can_place_it(self):
        moved = lineage("linnik", "wave111", "account-a", status="handoff", handoff_at=NOW - 2000)
        far = {"host-4": host({"wave133": runner("account-h")})}
        cases = {
            "own host has an idle runner with quota": {"host-2": host({"wave112": runner("account-i")}, [moved])},
            "too recent": {"host-2": host({}, [dict(moved, handoff_at=NOW - 60)])},
            "round still closing": {"host-2": host({}, [dict(moved, round_open=True)])},
            "no source round": {"host-2": host({}, [dict(moved, source_job=None)])},
            "source feeder has no inbox": {"host-2": host({}, [moved], inbox_supported=False)},
        }
        for why, hosts in cases.items():
            actions = coordinator.plan({"hosts": dict(far, **hosts)}, NOW, policy(parallel=False))
            self.assertEqual(directives(actions), [], why)

    def test_a_complete_handoff_moves_as_verify(self):
        moved = lineage("chen_theorem", "wave111", "account-a", status="handoff", handoff_at=NOW - 2000,
                        source_complete=True, verifies=1)
        state = {"hosts": {"host-2": host({}, [moved]), "host-4": host({"wave133": runner("account-h")})}}
        [start] = directives(coordinator.plan(state, NOW, policy(parallel=False)), "start_lineage")
        self.assertEqual(start["directive"]["role"], "VERIFY")
        self.assertNotIn("max_rounds", start["directive"])

    def test_a_failed_transfer_start_is_placed_again_then_alerted(self):
        moved = lineage("linnik", "wave111", "account-a", status="handoff", handoff_at=NOW - 2000)
        state = {"hosts": {"host-2": host({}, [moved]), "host-4": host({"wave133": runner("account-h")}),
                           "host-3": host({"wave127": runner("account-i")})}}
        first = coordinator.plan(state, NOW, policy(parallel=False))
        ledger = coordinator.Ledger(Path(tempfile.mkdtemp()) / "l.json", persist=False)
        for action in directives(first):
            ledger.sending(action, NOW)
            ledger.mark(action["directive"]["id"], "sent")
        [release] = directives(first, "stop_lineage")
        [start] = directives(first, "start_lineage")
        self.assertEqual(start["host"], "host-3")  # host-3 before host-4 at equal speed
        tid = release["meta"]["transfer"]
        # the source delegated the claim; the destination let the start expire
        ledger.directives[release["directive"]["id"]]["result"] = {"status": "applied"}
        ledger.directives[start["directive"]["id"]]["result"] = {"status": "expired"}
        fresh = {name: dict(snap, checked_ts=NOW + 1900) for name, snap in state["hosts"].items()}
        gone = {"hosts": dict(fresh, **{"host-2": host({}, [], checked_ts=NOW + 1900)})}
        again = coordinator.plan(dict(gone, ledger=ledger.data), NOW + 2000, policy(parallel=False))
        self.assertEqual(directives(again, "stop_lineage"), [])  # released already
        [retry] = directives(again, "start_lineage")
        self.assertEqual(retry["directive"]["id"], coordinator.directive_id("xfer", tid, "a2"))
        self.assertEqual(retry["directive"]["from_job"], moved["source_job"])
        self.assertTrue(any(a["kind"] == "export_seed" for a in again))
        # after max_transfer_attempts failed starts: an alert, no new start
        for n in (2, 3):
            did = coordinator.directive_id("xfer", tid, f"a{n}")
            ledger.directives[did] = {"type": "start_lineage", "purpose": "handoff_start", "problem_id": "linnik",
                                      "host": "host-4", "state": "sent", "sent_at": NOW, "expires_at": NOW + 1800,
                                      "meta": {"transfer": tid}, "result": {"status": "rejected"}}
        last = coordinator.plan(dict(gone, ledger=ledger.data), NOW + 4000, policy(parallel=False))
        self.assertEqual(directives(last), [])
        self.assertTrue(any("runs nowhere" in a["message"] for a in last if a["kind"] == "alert"))

    def test_one_lane_program_per_problem_fleet_wide(self):
        prog = {"status": "lanes", "lanes": 4, "reason": None}
        state = {"hosts": {
            "host-1": host(programs={"friedlander_iwaniec": dict(prog, lanes_done=3, created_at="2026-10-14T06:30:00+00:00",
                                                                 created_ts=NOW - 30000)}),
            "host-3": host(programs={"friedlander_iwaniec": dict(prog, lanes_done=1, created_at="2026-10-14T00:00:00+00:00",
                                                                 created_ts=NOW - 50000)}),
            "host-4": host(programs={"other": dict(prog, lanes_done=0, created_ts=NOW)}),
        }}
        [stop] = directives(coordinator.plan(state, NOW, policy()), "stop_lineage")
        d = stop["directive"]
        self.assertEqual((stop["host"], d["what"], d["mark"], d["problem_id"]),
                         ("host-3", "program", "stopped", "friedlander_iwaniec"))
        self.assertIn("host-1 keeps the program", d["reason"])
        # equal lanes done: the older program stays
        state["hosts"]["host-1"]["programs"]["friedlander_iwaniec"]["lanes_done"] = 1
        [stop] = directives(coordinator.plan(state, NOW, policy()), "stop_lineage")
        self.assertEqual(stop["host"], "host-1")
        # equal lanes done, the newer program on the faster host stays (host-1 runs ~3x slower)
        state["hosts"]["host-1"]["programs"]["friedlander_iwaniec"]["created_ts"] = NOW - 60000
        slow = policy(hosts={"host-1": {"speed": 0.33}})
        [stop] = directives(coordinator.plan(state, NOW, slow), "stop_lineage")
        self.assertEqual(stop["host"], "host-1")
        # a program with its own operator route tag is not a duplicate of the problem's other program
        state["hosts"]["host-1"]["programs"]["friedlander_iwaniec"]["route"] = "qualitative"
        self.assertEqual(directives(coordinator.plan(state, NOW, slow), "stop_lineage"), [])
        state["hosts"]["host-3"]["programs"]["friedlander_iwaniec"]["route"] = "qualitative"
        [stop] = directives(coordinator.plan(state, NOW, slow), "stop_lineage")
        self.assertEqual(stop["host"], "host-1")

    def parallel_state(self):
        a_main = lineage("prob_a", "wave113", "account-g", closeness=0.5, active_tactic="T4 split Core",
                         trying=["T4 split Core", "T6 coordinates"], tier="hard")
        return {"hosts": {
            "host-2": host({"wave113": runner("account-g", idle=False), "wave119": runner("account-j")}, [
                a_main,
                lineage("prob_b", "wave114", "account-g", closeness=0.2),                     # below θ
                lineage("prob_c", "wave115", "account-g", closeness=0.6),                     # already 2 live
                lineage("prob_d", "wave116", "account-g", closeness=0.45, role="VERIFY"),     # verifying
                lineage("prob_e", "wave117", "account-i", closeness=0.5),                     # program elsewhere
            ], best={"prob_a": {"job": job("wave113", "fleet-1-w113-002-prob_a-r3"), "sorry": 2,
                                "build_ok": True, "finished_at": NOW - 100}}),
            "host-3": host({"wave127": runner("account-i"), "wave128": runner("account-i")}, [
                lineage("prob_c", "wave123", "account-i", closeness=0.5),
            ], best={"prob_a": {"job": job("wave129", "fleet-1-w129-001-prob_a-r2"), "sorry": 1,
                                "build_ok": True, "finished_at": NOW - 5000}},
                programs={"prob_e": {"status": "lanes", "lanes": 2, "lanes_done": 0}}),
            "host-1": host({"wave101": runner("account-d")}),
        }}

    def test_parallel_lineage_by_closeness_with_the_main_tactic_forbidden(self):
        state = self.parallel_state()
        candidates = coordinator.parallel_candidates(state, NOW, policy())
        self.assertEqual([c["problem_id"] for c in candidates], ["prob_a"])
        actions = coordinator.plan(state, NOW, policy())
        [start] = directives(actions, "start_lineage")
        d = start["directive"]
        # equal speed: host order (host-1 is slower and the problem is hard tier: never there)
        self.assertEqual((start["host"], d["prefer_runner"], d["role"], d["problem_id"]),
                         ("host-2", "wave119", "PARALLEL", "prob_a"))
        # seeded from the best files fleet-wide: sorry 1 on host-3 beats the main lineage's sorry 2
        [export] = [a for a in actions if a["kind"] == "export_seed"]
        self.assertEqual((export["src_host"], export["job"]), ("host-3", job("wave129", "fleet-1-w129-001-prob_a-r2")))
        self.assertEqual(d["fields"]["avoid_tactics"], ["T4 split Core", "T6 coordinates"])
        self.assertIn("host-2/wave113 fleet-1-w113-002-prob_a: T4 split Core", d["fields"]["parallel_of"])
        self.assertIn("sorry 2, estimate 3000 lines, residual", d["fields"]["history"])
        self.assertEqual(d["max_rounds"], 6)
        # in flight: it counts as the second live lineage and keeps its runner reserved
        ledger = coordinator.Ledger(Path(tempfile.mkdtemp()) / "l.json", persist=False)
        ledger.sending(start, NOW)
        ledger.mark(d["id"], "sent")
        later = dict(state, ledger=ledger.data)
        self.assertEqual(coordinator.parallel_candidates(later, NOW + 300, policy()), [])
        self.assertEqual(directives(coordinator.plan(later, NOW + 300, policy())), [])
        # once it ran and ended, the cooldown still holds the problem back
        ledger.directives[d["id"]]["result"] = {"status": "applied"}
        self.assertEqual(coordinator.parallel_candidates(later, NOW + 3600, policy()), [])
        self.assertEqual([c["problem_id"] for c in coordinator.parallel_candidates(later, NOW + 20000, policy())],
                         ["prob_a"])

    def test_parallel_counts_quota_blocked_lineages_only_when_the_block_is_short(self):
        state = self.parallel_state()
        state["hosts"]["host-3"]["best"]["prob_c"] = {"job": job("wave123", "fleet-1-w123-002-prob_c-r3"),
                                                      "sorry": 1, "build_ok": True, "finished_at": NOW - 50}
        state["hosts"]["host-2"]["account_blocks"] = {"account-g": {"until": NOW + 1200, "window": "five_hour"}}
        # prob_c: one lineage blocked briefly (still live) + one on host-3 -> still 2 live
        self.assertEqual([c["problem_id"] for c in coordinator.parallel_candidates(state, NOW, policy())], ["prob_a"])
        state["hosts"]["host-2"]["account_blocks"] = {"account-g": {"until": NOW + 3 * 86400, "window": "seven_day"}}
        self.assertEqual([c["problem_id"] for c in coordinator.parallel_candidates(state, NOW, policy())],
                         ["prob_c", "prob_a"])
        # never onto a blocked account; prefer an account the problem does not use
        actions = coordinator.plan(state, NOW, policy(max_starts_per_tick=1))
        [start] = directives(actions, "start_lineage")
        self.assertEqual((start["directive"]["problem_id"], start["host"]), ("prob_c", "host-2"))
        self.assertEqual(start["directive"]["prefer_runner"], "wave119")  # account-j, not the problem's account-i

    def test_no_directives_for_hosts_without_inbox_or_with_stale_status(self):
        state = self.parallel_state()
        for name in ("host-2", "host-3"):
            state["hosts"][name]["inbox_supported"] = False
        state["hosts"]["host-1"]["checked_ts"] = NOW - 3600
        self.assertEqual(directives(coordinator.plan(state, NOW, policy())), [])

    def test_excluded_and_credited_problems_get_nothing(self):
        state = self.parallel_state()
        state["hosts"]["host-1"]["our_solved"] = ["prob_a"]
        self.assertEqual(coordinator.parallel_candidates(state, NOW, policy()), [])
        state["hosts"]["host-1"]["our_solved"] = []
        self.assertEqual(coordinator.parallel_candidates(state, NOW, policy(exclude_problems=["prob_a"])), [])

    def test_policy_from_fleet_json(self):
        fleet_cfg = json.loads((HERE.parent / "fleet.example.json").read_text())
        p = coordinator.merged_policy(fleet_cfg)
        self.assertEqual((p["hosts"]["host-1"]["speed"], p["hosts"]["host-1"]["heavy_single"]), (0.33, False))
        self.assertEqual((p["max_live_lineages"], p["parallel_min_closeness"]), (2, 0.34))
        with self.assertRaises(ValueError):
            coordinator.merged_policy({"coordinator": {"parallell": True}})

    def test_host_must_report_its_state_directory(self):
        state = {"hosts": {"host-2": host(), "host-3": host(own_root=None), "host-4": host(own_root="relative/own")}}
        view = coordinator.View(state, NOW, policy())
        self.assertEqual([h for h in ("host-2", "host-3", "host-4") if view.usable(h)], ["host-2"])
        self.assertEqual(view.seeds("host-2"), SEEDS)
        for path in (SEEDS + "/p-xfer-j", "/a/b_c/seeds/x"):
            self.assertTrue(coordinator.SEED_RE.fullmatch(path), path)
        for path in ("/a/../seeds/x", "relative/seeds/x", "/a/.hidden/seeds/x", "/a/seeds/-x"):
            self.assertIsNone(coordinator.SEED_RE.fullmatch(path), path)
        self.assertTrue(coordinator.JOB_RE.fullmatch(job("wave113", "fleet-1-w113-002-p-r3")))
        self.assertIsNone(coordinator.JOB_RE.fullmatch("/a/../full-1/jobs/x"))

    def test_directive_ids_are_inbox_safe(self):
        did = coordinator.directive_id("xfer", "pi_sphere.host-2.fleet-1-w111-002-pi_sphere", "a1")
        self.assertRegex(did, feeder.INBOX_ID.pattern)
        long = coordinator.directive_id("x" * 300, "a/b c")
        self.assertLessEqual(len(long), 160)
        self.assertRegex(long, feeder.INBOX_ID.pattern)


class LedgerExecuteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.json"

    def test_merge_outcomes_and_start_states(self):
        ledger = coordinator.Ledger(self.path)
        for did in ("a", "b", "c", "d"):
            ledger.directives[did] = {"id": did, "host": "host-2", "type": "start_lineage", "state": "sent",
                                      "sent_at": NOW, "expires_at": NOW + 1800, "result": None}
        self.assertEqual(ledger.open_ids("host-2"), ["a", "b", "c", "d"])
        snapshots = {"host-2": {"ok": True, "inbox": {"results": {
            "a": {"status": "applied", "detail": {"runner": "x"}}, "b": {"status": "pending"},
            "c": {"status": "missing"}, "d": {"status": "rejected", "detail": "bad seed"}}}}}
        ledger.merge(snapshots, NOW + 60, 600)
        self.assertEqual(ledger.directives["a"]["result"]["status"], "applied")
        self.assertIsNone(ledger.directives["b"]["result"])
        self.assertIsNone(ledger.directives["c"]["result"])  # not yet missing: within the grace
        ledger.merge(snapshots, NOW + 900, 600)
        self.assertEqual(ledger.directives["c"]["result"]["status"], "missing")
        self.assertEqual(json.loads(self.path.read_text())["directives"]["d"]["result"]["detail"], "bad seed")
        states = {k: coordinator.start_state(r, NOW + 900, 600) for k, r in ledger.directives.items()}
        self.assertEqual(states, {"a": "applied", "b": "inflight", "c": "failed", "d": "failed"})
        self.assertEqual(coordinator.start_state(ledger.directives["b"], NOW + 2500, 600), "failed")  # expired

    def test_execute_skips_the_rest_of_a_failed_group_and_records_sends(self):
        class FakeIO:
            def __init__(self):
                self.calls = []

            def export_seed(self, src, job_path, dest, seed):
                self.calls.append(("export", dest))
                if dest == "host-9":
                    raise RuntimeError("ssh failed")
                return "ok"

            def drop_directive(self, host_name, directive):
                self.calls.append(("drop", host_name, directive["id"]))

        actions = [
            {"kind": "export_seed", "group": "g1", "src_host": "host-2", "job": "j", "dest_host": "host-9", "seed_dir": "s"},
            {"kind": "directive", "group": "g1", "host": "host-9", "purpose": "handoff_start",
             "directive": {"id": "x1", "type": "start_lineage", "problem_id": "p"}},
            {"kind": "export_seed", "group": "g2", "src_host": "host-2", "job": "j", "dest_host": "host-4", "seed_dir": "s"},
            {"kind": "directive", "group": "g2", "host": "host-4", "purpose": "handoff_start",
             "directive": {"id": "x2", "type": "start_lineage", "problem_id": "p", "expires_at": NOW + 1800}},
            {"kind": "alert", "message": "note"},
        ]
        io = FakeIO()
        ledger = coordinator.Ledger(self.path)
        with mock.patch.object(coordinator, "log"):
            failures = coordinator.execute(actions, ledger, NOW, io)
        self.assertEqual(failures, 1)
        self.assertEqual(io.calls, [("export", "host-9"), ("export", "host-4"), ("drop", "host-4", "x2")])
        self.assertNotIn("x1", ledger.directives)
        self.assertEqual((ledger.directives["x2"]["state"], ledger.directives["x2"]["host"]), ("sent", "host-4"))
        # a directive whose write fails is recorded as send_failed and may be planned again
        class Broken(FakeIO):
            def drop_directive(self, host_name, directive):
                raise RuntimeError("disk full")

        with mock.patch.object(coordinator, "log"):
            coordinator.execute(actions[2:4], ledger, NOW, Broken())
        self.assertEqual(ledger.directives["x2"]["state"], "send_failed")


class MainTickTest(unittest.TestCase):
    """main() end to end with a fake remote: dry run writes nothing, --apply sends once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        now = time.time()
        hosts = json.loads((HERE.parent / "fleet.example.json").read_text())["hosts"]
        self.snapshots = {name: host(checked_ts=now - 30) for name in hosts}
        self.snapshots["host-2"]["passed"] = [{"key": "green_tao", "problem_id": "green_tao", "attempt": "x"}]
        self.snapshots["host-3"]["lineages"] = [lineage("green_tao", "wave123")]
        self.fake = FakeRemote(self.snapshots)
        for patch in (mock.patch.object(coordinator, "RemoteIO", lambda: self.fake),
                      mock.patch.object(coordinator, "STATE", Path(self.tmp.name)),
                      mock.patch.object(coordinator, "FLEET_JSON", HERE.parent / "fleet.example.json")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *args: str) -> int:
        with mock.patch("sys.stdout"):
            return coordinator.main([*args, "--ledger", str(self.ledger)])

    def test_dry_run_then_apply_then_outcome(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.fake.dropped, [])
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.run_main("--apply", "--once"), 0)
        self.assertEqual([(h, d["id"]) for h, d in self.fake.dropped], [("host-3", "pass-stop.green_tao.host-3")])
        record = json.loads(self.ledger.read_text())["directives"]["pass-stop.green_tao.host-3"]
        self.assertEqual((record["state"], record["purpose"], record["result"]), ("sent", "pass_stop", None))
        # the host applied it; the next tick records the outcome and sends nothing again
        self.snapshots["host-3"]["inbox"]["results"] = {"pass-stop.green_tao.host-3": {"status": "applied",
                                                                                        "detail": {"marked": ["green_tao"]}}}
        self.assertEqual(self.run_main("--apply", "--once"), 0)
        self.assertEqual(len(self.fake.dropped), 1)
        self.assertEqual(self.fake.asked["host-3"][-1], ["pass-stop.green_tao.host-3"])
        record = json.loads(self.ledger.read_text())["directives"]["pass-stop.green_tao.host-3"]
        self.assertEqual(record["result"]["status"], "applied")

    def test_operator_hold_and_stop(self):
        self.assertEqual(self.run_main("--hold", "host-2:wave111", "--reason", "account-a weekly"), 0)
        self.assertEqual(self.fake.dropped, [])
        self.assertEqual(self.run_main("--hold", "host-2:wave111", "--stop", "host-3:linnik", "--reason", "x",
                                       "--apply"), 0)
        sent = {d["type"]: (h, d) for h, d in self.fake.dropped}
        self.assertEqual((sent["hold_runner"][0], sent["hold_runner"][1]["runner"]), ("host-2", "wave111"))
        self.assertEqual((sent["stop_lineage"][0], sent["stop_lineage"][1]["problem_id"], sent["stop_lineage"][1]["mark"]),
                         ("host-3", "linnik", "stopped"))
        self.assertRegex(sent["hold_runner"][1]["id"], feeder.INBOX_ID.pattern)
        with self.assertRaises(SystemExit):
            self.run_main("--hold", "host-9:wave111")


class FakeRemote:
    def __init__(self, snapshots):
        self.snapshots, self.dropped, self.asked = snapshots, [], {}

    def gather(self, host_name, mode, values):
        self.asked.setdefault(host_name, []).append(list(values))
        if mode == "best":
            return {}
        return json.loads(json.dumps(self.snapshots[host_name]))

    def export_seed(self, src, job_path, dest, seed):
        return "fake"

    def drop_directive(self, host_name, directive):
        self.dropped.append((host_name, directive))


class GatherTest(unittest.TestCase):
    """coordinator_gather.collect with the real feeder on a fake host tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.own = base / "own"
        self.own.mkdir()
        self.r1 = self.queue(base, "wave113")
        self.r2 = self.queue(base, "wave114")
        (self.own / "config.json").write_text(json.dumps({
            "host": "host-2", "runners": [str(self.r1), str(self.r2)],
            "tokens": {"wave113": "/x/account-g", "wave114": "/x/account-i"}}))

    def queue(self, base: Path, wave: str) -> Path:
        root = base / "campaigns" / f"fleet-1-{wave}"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({"assignments": []}))
        (root / "state.json").write_text(json.dumps({"status": "idle_waiting_for_assignments", "history": {}}))
        return root

    def workspace(self, root: Path, name: str, sorry: int, notes: str = "") -> Path:
        ws = root / "jobs" / name / "workspace"
        (ws / "Submission").mkdir(parents=True)
        (ws / "Submission.lean").write_text("theorem t : True := trivial\n" + "theorem s : True := sorry\n" * sorry)
        (ws / "NOTES.md").write_text(notes)
        (ws / ".feedback").mkdir()
        (ws / ".feedback" / "step-000001.json").write_text(json.dumps(
            {"action": "build", "query": "Submission", "outcome_class": "check_succeeded"}))
        return ws

    def test_snapshot_and_best_rounds(self):
        base = "fleet-1-w113-002-prob_a"
        self.workspace(self.r1, base, 3)
        self.workspace(self.r1, base + "-r2", 2, "## HANDOFF\n- **Active tactic:** T4 split Core (row 2)\n")
        (self.r1 / "jobs" / (base + "-r3")).mkdir(parents=True)
        (self.r1 / "manifest.json").write_text(json.dumps({"assignments": [
            {"attempt_id": base, "problem_id": "prob_a"}, {"attempt_id": base + "-r2", "problem_id": "prob_a"},
            {"attempt_id": base + "-r3", "problem_id": "prob_a"}]}))
        (self.r1 / "state.json").write_text(json.dumps({
            "status": "model_running", "current_job": str(self.r1 / "jobs" / (base + "-r3")),
            "history": {base: {"phase": "done", "problem_id": "prob_a", "finished_at": 1},
                        base + "-r2": {"phase": "done", "problem_id": "prob_a", "finished_at": 2}}}))
        moved = "fleet-1-w114-001-prob_b"
        self.workspace(self.r2, moved, 1)
        (self.r2 / "manifest.json").write_text(json.dumps({"assignments": [{"attempt_id": moved, "problem_id": "prob_b"}]}))
        (self.r2 / "state.json").write_text(json.dumps({"status": "idle_waiting_for_assignments", "history": {
            moved: {"phase": "done", "problem_id": "prob_b", "finished_at": 3}}}))
        (self.own / "claims.json").write_text(json.dumps({
            "prob_a": {"status": "running", "runner": str(self.r1), "base": base, "closeness": 0.4,
                       "trajectory": [[base, 3, None, None]]},
            "prob_b": {"status": "handoff", "runner": str(self.r2), "base": moved,
                       "handoff": {"window": "seven_day", "at_ts": 123.0}},
            "prob_a~2": {"status": "exhausted", "problem_id": "prob_a", "key": "prob_a~2"},
            "prob_c": {"status": "passed", "runner": str(self.r2), "passed_attempt": "x"}}))
        (self.own / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "prob_a", "ref_min_modules": 300, "n_solvers": 3, "notes": "dropped"}]}))
        (self.own / "status.json").write_text(json.dumps({
            "checked_at": "2026-10-14T12:00:00+00:00",
            "runners": {self.r1.name: {"assignment": None}, self.r2.name: {"assignment": "idle"}}}))
        (self.own / "account-blocks.json").write_text(json.dumps({
            "account-a": {"until": NOW + 100, "window": "seven_day"}, "account-b": {"until": NOW - 100}}))
        (self.own / "programs").mkdir()
        (self.own / "programs" / "prob_d.json").write_text(json.dumps({
            "problem_id": "prob_d", "status": "lanes", "created_at": "2026-10-14T06:00:00+00:00",
            "lanes": {"A": {"done": True}, "B": {"done": False}}, "route": "qualitative"}))
        (self.own / "inbox" / "processed").mkdir(parents=True)
        (self.own / "inbox" / "wait.json").write_text("{}")
        (self.own / "inbox-processed.json").write_text(json.dumps({"done": {"status": "applied", "detail": {}}}))
        snap = gather.collect(feeder, self.own, ["done", "wait", "lost"], now=NOW)
        self.assertTrue(snap["inbox_supported"])
        self.assertEqual(snap["checked_ts"], datetime.datetime(2026, 10, 14, 12, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(snap["runners"]["wave114"], {"root": str(self.r2), "account": "account-i", "note": "idle",
                                                      "idle": True, "stop": False, "held": False})
        self.assertFalse(snap["runners"]["wave113"]["idle"])
        self.assertEqual(snap["account_blocks"], {"account-a": {"until": NOW + 100, "window": "seven_day"}})
        by_key = {l["key"]: l for l in snap["lineages"]}
        self.assertEqual(sorted(by_key), ["prob_a", "prob_b"])
        a = by_key["prob_a"]
        self.assertEqual((a["runner"], a["account"], a["tier"], a["round_open"], a["rounds_done"]),
                         ("wave113", "account-g", "hard", True, 2))
        self.assertEqual((a["source_job"], a["source_sorry"], a["source_build_ok"]),
                         (str(self.r1 / "jobs" / (base + "-r2")), 2, True))
        self.assertEqual(a["active_tactic"], "T4 split Core (row 2)")
        b = by_key["prob_b"]
        self.assertEqual((b["status"], b["round_open"], b["handoff_at"], b["handoff_window"]),
                         ("handoff", False, 123.0, "seven_day"))
        self.assertEqual(snap["passed"], [{"key": "prob_c", "problem_id": "prob_c", "attempt": "x",
                                           "runner": "wave114", "ci_held": False}])
        self.assertEqual(snap["claims"]["prob_a~2"]["problem_id"], "prob_a")
        self.assertEqual((snap["programs"]["prob_d"]["lanes"], snap["programs"]["prob_d"]["lanes_done"],
                          snap["programs"]["prob_d"]["route"]), (2, 1, "qualitative"))
        self.assertEqual(snap["targets"]["prob_a"], {"problem_id": "prob_a", "ref_min_modules": 300, "n_solvers": 3})
        self.assertEqual({k: v["status"] for k, v in snap["inbox"]["results"].items()},
                         {"done": "applied", "wait": "pending", "lost": "missing"})
        best = gather.collect_best(feeder, self.own, ["prob_a", "prob_x"])
        self.assertEqual(list(best), ["prob_a"])
        self.assertEqual((best["prob_a"]["job"], best["prob_a"]["sorry"], best["prob_a"]["runner"]),
                         (str(self.r1 / "jobs" / (base + "-r2")), 2, "wave113"))
        json.dumps(snap)  # what goes over ssh

    def test_snapshot_of_a_host_with_the_pre_inbox_feeder(self):
        new = {"process_inbox", "claim_pid", "claims_of", "claim_key", "second_lineage", "keep_passes",
               "pass_runtime", "held", "wave_of", "end_program", "stop_claim_lineage", "assign_inbox",
               "inbox_start", "inbox_stop", "inbox_hold", "inbox_release", "inbox_check_start", "inbox_seed",
               "inbox_fields", "inbox_finish", "inbox_waiting", "inbox_target", "inbox_order", "inbox_runner",
               "record_ts", "LIVE_CLAIM", "INBOX_ID", "INBOX_ROLES", "INBOX_MARKS", "INBOX_HANDLERS"}
        old = type("OldFeeder", (), {k: staticmethod(getattr(feeder, k)) if callable(getattr(feeder, k))
                                     else getattr(feeder, k)
                                     for k in dir(feeder) if not k.startswith("__") and k not in new})
        (self.own / "claims.json").write_text(json.dumps({"p": {"status": "running", "runner": str(self.r1),
                                                                "base": "fleet-1-w113-001-p"}}))
        snap = gather.collect(old, self.own, [], now=NOW)
        self.assertFalse(snap["inbox_supported"])
        self.assertEqual([l["problem_id"] for l in snap["lineages"]], ["p"])

    def test_active_tactic_formats(self):
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        for text, want in (("- Active tactic: T6 coordinates\n", "T6 coordinates"),
                           ("| Active tactic | `T2 weaker` |\n", "T2 weaker"),
                           ("**Active tactic**: special case k = 2\n", "special case k = 2"),
                           ("no handoff\n", None)):
            (ws / "NOTES.md").write_text(text)
            self.assertEqual(gather.active_tactic(feeder, ws), want, text)


class EndToEndTest(unittest.TestCase):
    """gather on two fake hosts -> plan -> transport into their inboxes -> the real feeder applies
    the directives: the contract between coordinator and feeder."""

    POLICY = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        # job paths live under the temp tree here, not /srv/fleet/projects/lean-eval-campaigns
        job_re = re.compile(re.escape(str(self.base)) + r"/[^/]+/campaigns/[A-Za-z0-9_.-]+/jobs/[A-Za-z0-9_.-]+")
        for patch in (mock.patch.object(feeder, "controller_pids", lambda root: [1]),
                      mock.patch.object(feeder, "our_solved", lambda alerts: set()),
                      mock.patch.object(coordinator, "JOB_RE", job_re)):
            patch.start()
            self.addCleanup(patch.stop)

    def own(self, name: str, runners: dict[str, str]) -> tuple[Path, dict[str, Path]]:
        own = self.base / name
        (own / "briefs").mkdir(parents=True)
        shutil.copytree(HERE.parent / "server/briefs/roles", own / "briefs" / "roles")
        (own / "briefs" / "COMMON.md").write_text("common\n")
        (own / "inbox").mkdir()
        (own / "seeds").mkdir()
        roots = {}
        for wave in runners:
            root = self.base / name / "campaigns" / f"fleet-1-{wave}"
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({"campaign_id": root.name, "host": feeder.HOST,
                                                            "requested_config": self.POLICY, "assignments": []}))
            (root / "state.json").write_text(json.dumps({"status": "idle_waiting_for_assignments", "history": {}}))
            roots[wave] = root
        (own / "config.json").write_text(json.dumps({
            "host": name, "runners": [str(r) for r in roots.values()], "min_free_gb": 0,
            "tokens": {w: f"/x/{a}" for w, a in runners.items()}}))
        (own / "targets.json").write_text(json.dumps({"targets": []}))
        return own, roots

    def tick(self, own: Path) -> dict:
        with mock.patch.object(feeder, "OWN", own), mock.patch("builtins.print"):
            return feeder.tick()

    def test_cross_host_handoff_from_plan_to_feeder(self):
        src, src_roots = self.own("host-2", {"wave111": "account-a"})
        dest, dest_roots = self.own("host-4", {"wave133": "account-h"})
        root = src_roots["wave111"]
        base = "fleet-1-w111-002-pi_sphere"
        for name, sorry in ((base, 3), (base + "-r2", 2)):
            ws = root / "jobs" / name / "workspace"
            (ws / "Submission").mkdir(parents=True)
            (ws / "Submission.lean").write_text("import Submission.A\n" + "theorem s : True := sorry\n" * sorry)
            (ws / "Submission" / "A.lean").write_text("theorem a : True := trivial\n")
            (ws / "NOTES.md").write_text("## HANDOFF\n- Active tactic: T-REL\n")
        (root / "manifest.json").write_text(json.dumps({"campaign_id": root.name, "host": feeder.HOST,
                                                        "requested_config": self.POLICY, "assignments": [
            {"attempt_id": base, "problem_id": "pi_sphere"}, {"attempt_id": base + "-r2", "problem_id": "pi_sphere"}]}))
        (root / "state.json").write_text(json.dumps({"status": "idle_waiting_for_assignments", "history": {
            base: {"phase": "done", "problem_id": "pi_sphere", "finished_at": 1},
            base + "-r2": {"phase": "done", "problem_id": "pi_sphere", "finished_at": 2}}}))
        (src / "targets.json").write_text(json.dumps({"targets": [
            {"problem_id": "pi_sphere", "ref_min_modules": 300, "n_solvers": 3, "max_rounds": 6}]}))
        (src / "claims.json").write_text(json.dumps({"pi_sphere": {
            "status": "handoff", "runner": str(root), "base": base, "rounds": 2, "closeness": 0.3,
            "route_review": {"from": "x"}, "handoff": {"window": "seven_day", "at_ts": time.time() - 3600}}}))
        (src / "account-blocks.json").write_text(json.dumps({"account-a": {"until": time.time() + 86400,
                                                                         "window": "seven_day"}}))
        self.tick(src)   # host-2's own feeder: its only runner is blocked, the handoff waits
        self.tick(dest)  # host-4: an idle runner, nothing to do
        state = {"hosts": {"host-2": dict(gather.collect(feeder, src, []), ok=True),
                           "host-4": dict(gather.collect(feeder, dest, []), ok=True)}}
        self.assertTrue(state["hosts"]["host-4"]["runners"]["wave133"]["idle"])
        actions = coordinator.plan(state, time.time(), policy(parallel=False))
        hosts = {"host-2": src, "host-4": dest}

        class Transport:
            """export_seed / drop_directive on the fake hosts (/srv/fleet/leh-fleet -> their dirs)."""

            @staticmethod
            def local(host_name, path):
                return Path(str(path).replace(coordinator.OWN, str(hosts[host_name])))

            def export_seed(self, src_host, job_path, dest_host, seed):
                workspace = Path(job_path) / "workspace"
                target = self.local(dest_host, seed)
                for path in [workspace / "Submission.lean", workspace / "NOTES.md",
                             *(workspace / "Submission").rglob("*.lean")]:
                    (target / path.relative_to(workspace)).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target / path.relative_to(workspace))
                return "copied"

            def drop_directive(self, host_name, directive):
                body = dict(directive, seed_dir=str(self.local(host_name, directive["seed_dir"]))) \
                    if directive.get("seed_dir") else directive
                (hosts[host_name] / "inbox" / f"{directive['id']}.json").write_text(json.dumps(body))

        ledger = coordinator.Ledger(self.base / "ledger.json")
        with mock.patch.object(coordinator, "log"):
            self.assertEqual(coordinator.execute(actions, ledger, time.time(), Transport()), 0)
        self.tick(src)
        self.tick(dest)
        claims_src = json.loads((src / "claims.json").read_text())
        self.assertEqual((claims_src["pi_sphere"]["status"], claims_src["pi_sphere"]["host"]), ("delegated", "host-4"))
        [assignment] = json.loads((dest_roots["wave133"] / "manifest.json").read_text())["assignments"]
        self.assertEqual((assignment["problem_id"], assignment["auto_continue"]["max_rounds"]), ("pi_sphere", 6 - 2))
        self.assertTrue((dest_roots["wave133"] / assignment["overlay_dir"] / "Submission" / "A.lean").is_file())
        claim = json.loads((dest / "claims.json").read_text())["pi_sphere"]
        self.assertEqual((claim["role"], claim["runner"], claim["coordinator"]["from_host"]),
                         ("RESUME", str(dest_roots["wave133"]), "host-2"))
        # the outcomes travel back into the ledger
        ids = {"host-2": ledger.open_ids("host-2"), "host-4": ledger.open_ids("host-4")}
        snapshots = {h: dict(gather.collect(feeder, own, ids[h]), ok=True) for h, own in hosts.items()}
        ledger.merge(snapshots, time.time(), 600)
        self.assertEqual(sorted((r["purpose"], r["result"]["status"]) for r in ledger.directives.values()),
                         [("handoff_release", "applied"), ("handoff_start", "applied")])
        # and the next plan does nothing more for this lineage
        state = {"hosts": snapshots, "ledger": ledger.data}
        self.assertEqual(directives(coordinator.plan(state, time.time(), policy(parallel=False))), [])


if __name__ == "__main__":
    unittest.main()
