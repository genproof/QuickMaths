"""Offline controls for the host-side operator tools in operator/ (no host, no ssh, no model).

Each tool runs as it does on a host (`python3 - ARGS < tool.py`, importing the deployed feeder from
LEH_OWN_ROOT) against a program built by the feeder itself with the fixtures of test_feeder.py: an
architect round, then lanes Alpha (wave101) and Beta (wave102) running.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_feeder as tf  # noqa: E402

OPERATOR = HERE.parent / "operator"
TOOLS = sorted(p for p in OPERATOR.glob("*.py") if p.name != "run.py")
feeder = tf.feeder
DONE = {"phase": "done", "verification": {"status": "not_verified_snapshot_only"}}
BETA_NOTES = """# HANDOFF
- Next step: close part_one by induction on the chart
## RESIDUALS
| residual | status | est. lines |
|---|---|---|
| `Submission.Lanes.Beta.part_one` | open | 400 |
| `Submission.Lanes.Beta.part_two` | proved | 0 |
| `Submission.Lanes.Alpha.other` | open | 100 |
"""


def lane_ws(root, job, lanes, solved=()):
    return tf.FeederTest.lane_ws(None, root, job, lanes, solved=solved)


def events(*items) -> str:
    return "".join(json.dumps(e) + "\n" for e in items)


def say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class Host:
    """One fleet host: own root (with the deployed feeder), queue roots, config."""

    def __init__(self, base: Path, waves: list[str], tokens: dict[str, str]):
        self.base, self.own, self.campaigns = base, base / "own", base / "campaigns"
        (self.own / "briefs").mkdir(parents=True)
        shutil.copy2(HERE.parent / "server/feeder.py", self.own / "feeder.py")
        shutil.copy2(HERE.parent / "server/briefs/COMMON.md", self.own / "briefs" / "COMMON.md")
        shutil.copytree(HERE.parent / "server/briefs/roles", self.own / "briefs" / "roles")
        self.roots = [tf.queue_root(self.campaigns, w) for w in waves]
        (self.own / "config.json").write_text(json.dumps({
            "host": "host-1", "runners": [str(r) for r in self.roots], "tokens": tokens, "min_free_gb": 0}))
        (self.own / "targets.json").write_text(json.dumps({"targets": [{"problem_id": "hadwiger"}]}))

    def patches(self):
        return [mock.patch.object(feeder, "OWN", self.own), mock.patch.object(feeder, "CAMPAIGNS", self.campaigns),
                mock.patch.object(feeder, "HOST", "host-1"), mock.patch.object(feeder, "controller_pids", lambda r: [1]),
                mock.patch.object(feeder, "our_solved", lambda alerts: set())]

    def tick(self):
        patches = self.patches()
        for p in patches:
            p.start()
        try:
            return feeder.tick()
        finally:
            for p in patches:
                p.stop()

    def run(self, tool: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {"LEH_OWN_ROOT": str(self.own), "LEH_CAMPAIGNS_ROOT": str(self.campaigns),
               "HOME": str(self.base), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        with (OPERATOR / tool).open("rb") as script:
            out = subprocess.run([sys.executable, "-B", "-", *args], stdin=script, capture_output=True,
                                 env=env, cwd=self.base, timeout=120)
        if check and out.returncode:
            raise AssertionError(f"{tool} {args}: exit {out.returncode}\n{out.stderr.decode()[-2000:]}")
        return out

    def manifest(self, root):
        return json.loads((root / "manifest.json").read_text())

    def set_state(self, root, history, status="idle_waiting_for_assignments", current=None):
        state = {"status": status, "history": history}
        if current:
            state["current_job"] = str(current)
        (root / "state.json").write_text(json.dumps(state))

    def program(self):
        return json.loads((self.own / "programs" / "hadwiger.json").read_text())


def output_json(out: subprocess.CompletedProcess):
    """The JSON a tool prints after any feeder log lines: the first line that starts a JSON value."""
    text = out.stdout.decode()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(("{", "[")))
    return json.loads("\n".join(lines[start:]))


class OperatorToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = Host(Path(self.tmp.name) / "h1", ["wave101", "wave102"],
                         {"wave101": "file:~/.config/leh/accounts/account-a.token", "wave102": "env:ACCOUNT_B"})
        self.r1, self.r2 = self.host.roots
        h = self.host
        seed = lane_ws(self.r2, "seedjob", []).parent
        (seed / "result.json").write_text("{}")
        for p in h.patches():
            p.start()
            self.addCleanup(p.stop)
        feeder.create_program("hadwiger", str(seed))
        feeder.tick()
        arch = h.manifest(self.r1)["assignments"][0]
        lane_ws(self.r1, arch["attempt_id"], ["Alpha", "Beta"])
        h.set_state(self.r1, {arch["attempt_id"]: DONE})
        feeder.tick()
        mock.patch.stopall()
        self.arch = arch["attempt_id"]
        self.lane_a = h.manifest(self.r1)["assignments"][-1]["attempt_id"]
        self.lane_b = h.manifest(self.r2)["assignments"][-1]["attempt_id"]
        assert self.lane_a.endswith("-laneAlpha") and self.lane_b.endswith("-laneBeta"), (self.lane_a, self.lane_b)
        # both lane rounds are running with partial work, notes and a model stream
        self.ja = lane_ws(self.r1, self.lane_a, ["Alpha", "Beta"]).parent
        self.jb = lane_ws(self.r2, self.lane_b, ["Alpha", "Beta"]).parent
        (self.jb / "workspace/Submission/Lanes/Beta/Part.lean").write_text("theorem part : True := trivial\n")
        (self.jb / "workspace/NOTES.md").write_text(BETA_NOTES)
        (self.jb / "events.jsonl").write_text(events(
            say("first look"), say("working on part_one\nwith the chart"),
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1760000000, "privateField": "x",
                "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1760000000, "extra": 1}}}}))
        h.set_state(self.r1, {self.arch: DONE}, "model_running", self.ja)
        h.set_state(self.r2, {}, "model_running", self.jb)

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def idle(self):
        self.host.set_state(self.r1, {self.arch: DONE})
        self.host.set_state(self.r2, {})

    def digest(self) -> str:
        files = sorted(p for p in self.host.base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        return hashlib.sha256(b"".join(p.as_posix().encode() + p.read_bytes() for p in files)).hexdigest()

    def test_every_tool_parses_and_prints_its_usage(self):
        self.assertGreaterEqual(len(TOOLS), 20)
        for tool in [*TOOLS, OPERATOR / "run.py"]:
            source = tool.read_text()
            ast.parse(source, str(tool))
            self.assertIn("Usage", source.split('"""')[1], tool.name)
            self.assertNotIn("/home/", source, tool.name)
        for tool in TOOLS:
            out = self.host.run(tool.name, "--help", check=False)
            self.assertEqual(out.returncode, 1, tool.name)
            self.assertIn("Usage", out.stderr.decode(), tool.name)
            self.assertNotIn("Traceback", out.stderr.decode(), tool.name)

    def test_read_only_tools_report_the_program_and_change_nothing(self):
        before = self.digest()
        h = self.host
        out = h.run("programs.py").stdout.decode()
        self.assertIn("hadwiger lanes", out)
        self.assertIn("Alpha:wave101:running", out)
        self.assertIn("Beta:wave102:running", out)
        out = h.run("lanes.py", "hadwiger").stdout.decode()
        self.assertIn("## hadwiger lanes", out)
        self.assertIn("files=2 ", out)  # Beta: Main.lean + Part.lean
        self.assertIn("> working on part_one with the chart", out)
        out = h.run("residuals.py", "hadwiger").stdout.decode()
        self.assertIn("est=   400 [('part_one', 400)] other_open=1", out)
        out = h.run("residuals.py", str(self.jb)).stdout.decode()
        self.assertIn("open=2 est=500", out)
        out = h.run("hedges.py", f"{self.lane_b}:Beta").stdout.decode()
        self.assertIn("files=2 ", out)
        self.assertIn("sorry=1", out)
        out = h.run("messages.py", self.lane_b, "2").stdout.decode()
        self.assertEqual(out.count("\n- "), 2)
        self.assertIn("first look", out)
        out = h.run("accounts.py").stdout.decode()
        self.assertIn("wave101 account-a model_running", out)
        self.assertIn("wave102 ACCOUNT_B model_running", out)
        self.assertIn("five_hour=0.4@", out)
        data = output_json(h.run("accounts.py", "--json"))
        self.assertEqual(data["runners"][1]["rate_limit"]["windows"], {"five_hour": {"utilization": 0.4, "resetsAt": 1760000000}})
        text = json.dumps(data) + out
        for secret_like in ("privateField", "extra", "file:", ".config", ".token"):
            self.assertNotIn(secret_like, text)
        # lineages.py lists plain claims only; a program's lanes are not claims
        original = (h.own / "claims.json").read_bytes()
        claims = json.loads(original)
        claims["linnik"] = {"status": "running", "runner": str(self.r2), "base": self.lane_b, "role": "RESUME"}
        (h.own / "claims.json").write_text(json.dumps(claims))
        out = h.run("lineages.py").stdout.decode()
        self.assertIn("linnik", out)
        self.assertIn("sorry=2 lines=10 role=RESUME", out)  # both lane stubs
        self.assertIn("next: close part_one by induction on the chart", out)
        self.assertFalse([line for line in out.splitlines() if line.startswith("hadwiger")])  # the program claim
        (h.own / "claims.json").write_bytes(original)
        self.assertEqual(self.digest(), before)

    def test_stop_lane_then_relane_resumes_it_on_its_files(self):
        h = self.host
        out = output_json(h.run("stop_lane.py", "hadwiger", "Beta", "operator check"))
        self.assertEqual(out["base"], self.lane_b)
        self.assertTrue((self.jb / "STOP").exists())
        self.assertEqual(h.program()["lanes"]["Beta"]["claim"]["status"], "stopped")
        self.assertEqual(h.run("relane.py", "hadwiger", "Alpha", str(self.ja), "x", check=False).returncode, 1)  # running
        out = output_json(h.run("relane.py", "hadwiger", "Beta", str(self.jb), "back to work"))
        self.assertEqual(out, {"Beta": str(self.jb)})
        prog = h.program()
        self.assertNotIn("claim", prog["lanes"]["Beta"])
        self.assertEqual(prog["lanes"]["Beta"]["previous_claims"][0]["base"], self.lane_b)
        h.set_state(self.r2, {self.lane_b: DONE})  # the stopped round has finished
        h.tick()
        lane = [a for r in (self.r1, self.r2) for a in h.manifest(r)["assignments"]
                if a["attempt_id"].endswith("-laneBeta") and a["attempt_id"] != self.lane_b][0]
        root = self.r1 if lane in h.manifest(self.r1)["assignments"] else self.r2
        self.assertTrue((root / lane["overlay_dir"] / "Submission/Lanes/Beta/Part.lean").is_file())
        self.assertIn("close part_one by induction", (root / lane["overlay_dir"] / "NOTES.md").read_text())

    def test_split_lane_requests_a_split_architect(self):
        h = self.host
        self.idle()
        out = output_json(h.run("split_lane.py", "hadwiger", "Beta", "part_one stuck at 400 lines"))
        self.assertEqual((out["from_job"], out["runner_idle"]), (self.lane_b, True))
        prog = h.program()
        self.assertEqual(prog["split"]["lane"], "Beta")
        self.assertEqual(prog["lanes"]["Alpha"]["claim"]["status"], "running")
        self.assertEqual(h.run("split_lane.py", "hadwiger", "Alpha", "again", check=False).returncode, 1)
        h.tick()
        split = [a for a in h.manifest(self.r2)["assignments"] if "-splitBeta" in a["attempt_id"]]
        self.assertTrue(split)
        self.assertIn("part_one stuck at 400 lines", (self.r2 / split[0]["brief_file"]).read_text())

    def test_rearchitect_merges_every_lane_and_its_notes(self):
        h = self.host
        self.idle()
        (h.base / "reason.md").write_text("BetaStatement is false for the boundary case; use the open chart.\n")
        out = output_json(h.run("rearchitect.py", "hadwiger", "Beta", f"@{h.base / 'reason.md'}"))
        seed = Path(out["seed"])
        self.assertEqual(sorted(out["lanes"]), ["Alpha", "Beta"])
        self.assertTrue((seed / "workspace/Submission/Lanes/Beta/Part.lean").is_file())
        notes = (seed / "workspace/NOTES.md").read_text()
        self.assertIn("## OPERATOR RE-ARCHITECTURE (trigger: lane Beta)", notes)
        self.assertIn("BetaStatement is false", notes)
        self.assertIn("## LANE Beta: its own notes", notes)
        self.assertIn("#### RESIDUALS", notes)  # demoted, as the feeder does
        prog = h.program()
        self.assertEqual((prog["status"], prog["rearchitected"], prog["rearchitect"]["lane"]), ("architect", 1, "Beta"))
        self.assertEqual({k: v["claim"]["status"] for k, v in prog["previous_lanes"].items()},
                         {"Alpha": "stopped", "Beta": "stopped"})
        h.tick()
        arch = [a for r in (self.r1, self.r2) for a in h.manifest(r)["assignments"]
                if a["attempt_id"] == h.program()["architect"]["base"]][0]
        root = self.r1 if arch in h.manifest(self.r1)["assignments"] else self.r2
        self.assertIn("BetaStatement is false", (root / arch["brief_file"]).read_text())
        self.assertTrue((root / arch["overlay_dir"] / "Submission/Lanes/Beta/Part.lean").is_file())

    def test_export_adopt_and_hedge_on_other_hosts(self):
        h = self.host
        blob = h.run("export_prog.py", "hadwiger").stdout
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            names = set(tar.getnames())
            man = json.loads(tar.extractfile("MANIFEST.json").read())
        self.assertIn("base/workspace/Submission/Lanes/Interfaces.lean", names)
        self.assertIn("Beta/workspace/Submission/Lanes/Beta/Part.lean", names)
        self.assertIn("Beta/workspace/NOTES.md", names)
        self.assertEqual(sorted(man["lanes"]), ["Alpha", "Beta"])
        # host 2 adopts the program: both lanes continue on their exported files
        h2 = Host(Path(self.tmp.name) / "h2", ["wave201"], {})
        (h2.own / "uploads").mkdir()
        (h2.own / "uploads/export.tgz").write_bytes(blob)
        out = output_json(h2.run("adopt_prog.py", "uploads/export.tgz", "host 1 drained"))
        self.assertEqual(out["lanes"]["Beta"]["resume_job"], str(Path(out["seed"]) / "Beta"))
        self.assertEqual(h2.run("adopt_prog.py", "uploads/export.tgz", "twice", check=False).returncode, 1)
        h2.tick()
        lane = h2.manifest(h2.roots[0])["assignments"][-1]
        overlay = h2.roots[0] / lane["overlay_dir"]
        self.assertTrue(lane["attempt_id"].endswith("-laneAlpha") or lane["attempt_id"].endswith("-laneBeta"))
        self.assertTrue((overlay / "Submission/Lanes/Interfaces.lean").is_file())
        # host 3 hedges lane Beta on a different route; its feeder accepts the directive
        h3 = Host(Path(self.tmp.name) / "h3", ["wave301"], {})
        (h3.base / "export.tgz").write_bytes(blob)
        self.assertEqual(h3.run("hedge_lane.py", "hadwiger", "Beta", "wave301", str(h3.base / "export.tgz"), " ",
                                check=False).returncode, 1)  # no route
        out = output_json(h3.run("hedge_lane.py", "hadwiger", "Beta", "wave301", str(h3.base / "export.tgz"),
                                 "prove part_one from the closed chart instead", "--rounds", "2"))
        self.assertTrue(out["lane_notes"])
        seed_notes = (Path(out["seed"]) / "NOTES.md").read_text()
        self.assertIn("## OPERATOR HEDGE", seed_notes)
        self.assertIn("Route of this hedge: prove part_one from the closed chart instead", seed_notes)
        self.assertIn("## LANE Beta: its own notes", seed_notes)
        h3.tick()
        start = h3.manifest(h3.roots[0])["assignments"][-1]
        self.assertEqual(start["auto_continue"]["max_rounds"], 2)
        brief = (h3.roots[0] / start["brief_file"]).read_text()
        self.assertIn("parallel lineage", brief)
        self.assertIn("closed chart", brief)
        done = json.loads((h3.own / "inbox-processed.json").read_text())
        self.assertEqual([v["status"] for v in done.values()], ["applied"])
        self.assertEqual(h.run("hedge_lane.py", "hadwiger", "Beta", "wave101", str(h3.base / "export.tgz"), "r",
                               check=False).returncode, 1)  # the program's own host

    def test_hand_done_switch_lanes_unblock_and_resume(self):
        h = self.host
        hand = h.base / "hand/Beta"
        hand.mkdir(parents=True)
        (hand / "Main.lean").write_text("theorem Submission.Lanes.Beta.main : Submission.Lanes.BetaStatement := sorry\n")
        self.assertEqual(h.run("hand_done.py", "hadwiger", "Beta", str(hand), "x", check=False).returncode, 1)
        (hand / "Main.lean").write_text("theorem Submission.Lanes.Beta.main : Submission.Lanes.BetaStatement := trivial\n")
        out = output_json(h.run("hand_done.py", "hadwiger", "Beta", str(hand), "proved on a laptop"))
        self.assertEqual(out["stopped"], [self.lane_b])
        self.assertTrue(h.program()["lanes"]["Beta"]["done"])
        self.idle()
        out = output_json(h.run("switch_lanes.py", "hadwiger:Alpha"))
        self.assertEqual(out["lanes"], {"hadwiger:Alpha": self.lane_a})
        self.assertEqual(h.program()["lanes"]["Alpha"]["resume_job"], str(self.ja))
        self.assertEqual(h.run("switch_lanes.py", "hadwiger:Alpha", "--restart", "bad", check=False).returncode, 1)
        (h.own / "account-blocks.json").write_text(json.dumps({"account-a": {"until": 2e9}, "ACCOUNT_B": {"until": 2e9}}))
        out = output_json(h.run("unblock.py", "account-a", "switched"))
        self.assertEqual(out["left"], ["ACCOUNT_B"])
        prog = h.program()
        prog["status"] = "stopped"
        (h.own / "programs/hadwiger.json").write_text(json.dumps(prog))
        out = output_json(h.run("resume_prog.py", "hadwiger", "quota is back"))
        self.assertEqual(out, {"Alpha": str(self.ja)})
        self.assertEqual(h.program()["status"], "lanes")
        self.assertEqual(h.run("resume_prog.py", "hadwiger", "again", check=False).returncode, 1)

    def test_stop_lineage_endround_and_seed_program(self):
        h = self.host
        claims = json.loads((h.own / "claims.json").read_text())
        claims["linnik"] = {"status": "running", "runner": str(self.r2), "base": self.lane_b}
        (h.own / "claims.json").write_text(json.dumps(claims))
        self.assertEqual(h.run("stop_lineage.py", "linnik", "fleet-1-w999-001-linnik", "x", check=False).returncode, 1)
        did = h.run("stop_lineage.py", "linnik", self.lane_b, "different route won").stdout.decode().strip()
        d = json.loads((h.own / "inbox" / f"{did}.json").read_text())
        self.assertEqual((d["type"], d["base"], d["mark"]), ("stop_lineage", self.lane_b, "stopped"))
        h.tick()
        self.assertEqual(json.loads((h.own / "claims.json").read_text())["linnik"]["status"], "stopped")
        # endround: the running round finishes, its successor is dropped
        claims = json.loads((h.own / "claims.json").read_text())
        claims["green_tao"] = {"status": "running", "runner": str(self.r1), "base": self.lane_a}
        (h.own / "claims.json").write_text(json.dumps(claims))
        m = h.manifest(self.r1)
        m["assignments"].append(dict(m["assignments"][-1], attempt_id=self.lane_a + "-r2"))
        (self.r1 / "manifest.json").write_text(json.dumps(m))
        out = output_json(h.run("endround.py", "green_tao", "focus elsewhere"))
        self.assertEqual(out["dropped"], [self.lane_a + "-r2"])
        self.assertIsNone(h.manifest(self.r1)["assignments"][-1]["auto_continue"])
        self.assertEqual(json.loads((h.own / "claims.json").read_text())["green_tao"]["status"], "stopped")
        # seed_program refuses an active program, archives a stopped one
        self.assertEqual(h.run("seed_program.py", "hadwiger", str(self.jb), check=False).returncode, 1)
        prog = h.program()
        prog["status"] = "stopped"
        (h.own / "programs/hadwiger.json").write_text(json.dumps(prog))
        h.run("seed_program.py", "hadwiger", str(self.jb))
        self.assertEqual(h.program()["seed_job"], str(self.jb))
        self.assertEqual(len(list((h.own / "programs-archive").glob("hadwiger-*.json"))), 1)


class RunTest(unittest.TestCase):
    def test_arguments_are_quoted_and_layout_passed(self):
        spec = importlib.util.spec_from_file_location("operator_run", OPERATOR / "run.py")
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        with mock.patch.dict(os.environ, {"LEH_REMOTE_OWN_ROOT": "srv/leh", "LEH_REMOTE_CAMPAIGNS_ROOT": "/data/q"}):
            steps = run.commands("host-1", "stop_lane.py", ["p", "Beta", "a reason; rm -rf ~"], None)
            upload = run.commands("host-1", "adopt_prog.py", ["uploads/x.tgz", "r"], OPERATOR / "lanes.py")
        (cmd, stdin), = steps
        self.assertEqual(stdin, OPERATOR / "stop_lane.py")
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-2], "host-1")
        self.assertEqual(cmd[-1], "LEH_OWN_ROOT=\"$HOME\"/srv/leh LEH_CAMPAIGNS_ROOT=/data/q python3 - p Beta "
                                  "'a reason; rm -rf ~'")
        self.assertEqual(len(upload), 2)
        self.assertEqual(upload[0][0][-1], 'mkdir -p "$HOME"/srv/leh/uploads && cat > "$HOME"/srv/leh/uploads/lanes.py')
        with self.assertRaises(SystemExit):
            run.commands("host-1", "run.py", [], None)
        with self.assertRaises(SystemExit):
            run.commands("host-1", "missing.py", [], None)


if __name__ == "__main__":
    unittest.main()
