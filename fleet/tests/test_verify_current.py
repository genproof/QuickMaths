"""Offline controls for the vcur automation (local/verify_current.py, local/vcur/*).

No ssh, no GitHub, no network: the "GitHub" side is a local bare repository, the "host" runs the remote
programs locally against a temporary home directory, and lanes are simulated.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
LOCAL = HERE.parent / "local"
SPEC = importlib.util.spec_from_file_location("verify_current", LOCAL / "verify_current.py")
vc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vc)
sc = vc.stage_current

BENCH = vc.BENCH
OLD_BENCH = "0e093293afbe96be91504ae6e44947876d3fc84e"
BASE = "lean-eval-vcur"
CONFIG = vc.VCUR / "config.example.json"
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "vcur test", "GIT_AUTHOR_EMAIL": "vcur-test@example.com",
           "GIT_COMMITTER_NAME": "vcur test", "GIT_COMMITTER_EMAIL": "vcur-test@example.com"}
README = ("# demo — private LeanEval submission\n\nThe proof was written by agents.\n\n"
          "Fresh source-only official local verification passed both Nanoda and the Lean kernel against benchmark "
          f"commit `{OLD_BENCH}`. Local verification is distinct from organizer acceptance.\n\n"
          "Publication status: Private, with no current publication plan.\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(cwd, *args, date: str | None = None) -> str:
    env = {**os.environ, **GIT_ENV, **({"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {})}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def old_verification(pid: str, wall: float = 100.0, **extra) -> dict:
    record = {"problem_id": pid, "statement_revision": 1, "benchmark_commit": OLD_BENCH,
              "status": "official_local_pass", "accepted": True, "nanoda_accepted": True,
              "lean_kernel_accepted": True, "official_lake_test_exit_code": 0,
              "receipt_run_id": "ff91717fa5d84252826e5218aaff9d05", "artifact_digest": "a6" * 32,
              "verification_wall_seconds": wall, "submission_files_sha256": {"Submission.lean": "00" * 32},
              "note": "Fresh source-only local verification of exactly these sources."}
    record.update(extra)
    return record


def solution_files(pid: str, verification=None, readme: str = README) -> dict[str, str]:
    files = {"Submission.lean": f"import Submission.A\ntheorem {pid} : True := trivial\n",
             "Submission/A.lean": "def a := 1\n", "Submission/B/C.lean": "def c := 2\n",
             "Submission/data.json": "{}\n", "lakefile.toml": 'name = "demo"\n', "README.md": readme}
    if verification is not False:
        files["VERIFICATION.json"] = json.dumps(verification or old_verification(pid), indent=2) + "\n"
    return files


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit_branch(bare: Path, branch: str, files: dict[str, str], *, follow: bool = False,
                  date: str | None = None) -> str:
    """Publish files as `branch` in the bare repo: an orphan commit, or (follow) a fast-forward on top."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        if follow:
            git(None, "clone", "-q", "--single-branch", "--branch", branch, str(bare), str(work))
        else:
            work.mkdir()
            git(work, "init", "-q")
        write_files(work, files)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", f"package {branch}", date=date)
        git(work, "push", "-q", str(bare), f"HEAD:refs/heads/{branch}")
        return git(work, "rev-parse", "HEAD")


def make_bundle(hashes: dict, pid: str, commit: str, receipt_path: str, receipt_sha: str = "ab" * 32) -> dict:
    status = {"phase": "finished", "accepted": True, "receipt": receipt_path, "receipt_sha256": receipt_sha,
              "elapsed_seconds": 540.2, "sources_unchanged": True, "protected_unchanged": True,
              "binaries_unchanged": True, "dependency_sources_unchanged": True, "illegal_axiom": False,
              "exit_code": 0}
    config = {"commit": commit, "problem_id": pid, "benchmark_commit": BENCH, "timeout_seconds": 3600,
              "evaluator_commit": "6380db3cfabe5557cc53ce62603defa8fb01f29c", "evaluator_sha256": "46" * 32,
              "manager_sha256": "c1" * 32, "lean_pool_commit": "e9d53e9cbcff8dfd0cc816ba94db7a26b082929f",
              "binary_sha256": {"comparator": "8e" * 32, "nanoda_bin": "3e" * 32, "lean4export": "71" * 32,
                                "landrun": "be" * 32}}
    receipt = {"accepted": True, "nanoda_accepted": True, "lean_kernel_accepted": True, "exit_code": 0,
               "timed_out": False, "illegal_sorry_axiom": False, "benchmark_commit": BENCH,
               "started_at": "2026-10-14T15:20:48.944674+00:00", "elapsed_seconds": 536.78,
               "log_sha256": "ca" * 32, "source_sha256": hashes}
    return {"status": status, "config": config, "hashes": hashes, "receipt": receipt,
            "run_done": "rc 0 wall 540s peak 9177800704 oom 0 oom_kill 0 oom_group_kill 0"}


class GitEnvCase(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.dict(os.environ, GIT_ENV), mock.patch.object(vc, "ENV", {**vc.ENV, **GIT_ENV})):
            patch.start()
            self.addCleanup(patch.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="vcur-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ---------------------------------------------------------------- pure logic

class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = vc.load_config(None)
        self.facts = vc.facts_of(old_verification("green_tao"), "2026-10-13T08:00:00Z")

    def test_parse_branch(self):
        self.assertEqual(vc.parse_branch("claude/green_tao-private"), "green_tao")
        self.assertEqual(vc.parse_branch("claude/annals_on_property_t-own-86f418c4-private"),
                         "annals_on_property_t")
        self.assertEqual(vc.parse_branch("claude/bakerWustholz_linearForms_logs-private"),
                         "bakerWustholz_linearForms_logs")
        for branch in ["main", "claude/green_tao", "claude/green_tao-private-2", "claude/x-own-XYZ-private",
                       "codex/green_tao-private", "claude/a/b-private"]:
            self.assertIsNone(vc.parse_branch(branch), branch)

    def select(self, facts=None, branch="claude/green_tao-private", commit="f0705867b411", **kw):
        return vc.select_branch(branch, commit, facts or self.facts, self.cfg, **kw)

    def test_queue_and_skips(self):
        self.assertEqual(self.select(), "queue")
        self.assertEqual(self.select(vc.facts_of(None, None)), "skip:no_verification")
        self.assertEqual(self.select(vc.facts_of(old_verification("green_tao", benchmark_commit=BENCH), None)),
                         "current")
        self.assertEqual(self.select(vc.facts_of(old_verification("green_tao", accepted=False), None)),
                         "skip:not_a_pass")
        self.assertEqual(self.select(vc.facts_of(old_verification("green_tao", status="draft"), None)),
                         "skip:not_a_pass")
        self.assertEqual(self.select(vc.facts_of(old_verification("chen_theorem"), None)), "skip:problem_mismatch")
        self.assertEqual(self.select(branch="main"), "skip:not_a_solution_branch")
        self.assertEqual(self.select(credited={"green_tao"}), "skip:credited")
        self.cfg["skip_credited"] = False
        self.assertEqual(self.select(credited={"green_tao"}), "queue")

    def test_exclusions_and_cutoff(self):
        for key, value in [("exclude_problems", ["green_tao"]), ("exclude_branches", ["claude/green_tao-private"]),
                           ("exclude_commits", ["f0705867"])]:
            cfg = vc.load_config(None)
            cfg[key] = value
            self.assertEqual(vc.select_branch("claude/green_tao-private", "f0705867b411", self.facts, cfg),
                             "skip:excluded", key)
        self.cfg["min_commit_time"] = "2026-10-14T00:00:00Z"
        self.assertEqual(self.select(), "skip:before_cutoff")
        self.assertEqual(self.select(dict(self.facts, commit_time=None)), "skip:before_cutoff")
        self.cfg["min_commit_time"] = "2026-10-12T00:00:00Z"
        self.assertEqual(self.select(dict(self.facts, commit_time="2026-10-13T08:00:00+00:00")), "queue")

    def test_shipped_config_is_valid(self):
        cfg = vc.load_config(vc.VCUR / "config.example.json")
        self.assertEqual(cfg["host"], "verify-host")
        self.assertEqual(cfg["archive_cap_bytes"], 29500000)
        self.assertEqual(cfg["mem_overrides"]["annals_on_property_t"], "96G")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "c.json"
            bad.write_text(json.dumps({"hosts": "typo"}))
            with self.assertRaises(ValueError):
                vc.load_config(bad)

    def test_launch_agent_template(self):
        text = (LOCAL / "launchd" / "vcur.plist").read_text()
        filled = text.replace("@PYTHON@", "/usr/bin/python3").replace("@REPO@", "/repo").replace("@STATE@", "/state")
        plist = plistlib.loads(filled.encode())
        self.assertEqual(plist["Label"], "local.leh.vcur")
        self.assertEqual(plist["ProgramArguments"][-3:],
                         ["/repo/fleet/local/verify_current.py", "--apply", "--once"])
        self.assertEqual(plist["StartInterval"], 300)
        self.assertNotIn("@", filled.split("-->", 1)[1])


class TimeoutTest(unittest.TestCase):
    def test_timeout(self):
        cfg = vc.load_config(None)
        self.assertEqual(vc.compute_timeout(100, "p", cfg), 3600)
        self.assertEqual(vc.compute_timeout(None, "p", cfg), 3600)
        self.assertEqual(vc.compute_timeout(899.9, "p", cfg), 3600)
        self.assertEqual(vc.compute_timeout(901, "p", cfg), 3604)
        self.assertEqual(vc.compute_timeout(1277, "p", cfg), 5108)
        self.assertEqual(vc.compute_timeout(2450.5, "p", cfg), 9802)
        self.assertEqual(vc.compute_timeout(5400, "p", cfg), 21600)
        cfg["timeout_overrides"] = {"annals_on_property_t": 21600}
        self.assertEqual(vc.compute_timeout(100, "annals_on_property_t", cfg), 21600)
        cfg.update(timeout_factor=2, timeout_floor_seconds=600)
        self.assertEqual(vc.compute_timeout(100, "p", cfg), 600)
        self.assertEqual(vc.compute_timeout(1000, "p", cfg), 2000)

    def test_memory(self):
        cfg = vc.load_config(CONFIG)
        self.assertEqual(vc.memory_for("green_tao", cfg), "48G")
        self.assertEqual(vc.memory_for("annals_on_property_t", cfg), "96G")
        self.assertEqual(vc.parse_mem("48G"), 48 * 2 ** 30)
        self.assertEqual(vc.parse_mem("512m"), 512 * 2 ** 20)
        self.assertEqual(vc.parse_mem("1024"), 1024)
        with self.assertRaises(ValueError):
            vc.parse_mem("lots")

    def test_run_key(self):
        self.assertEqual(vc.run_key("green_tao", "f0705867b4113800"), "green_tao-f0705867")
        self.assertEqual(vc.run_key("green_tao", "f0705867b4113800", 3), "green_tao-f0705867-r3")
        with self.assertRaises(ValueError):
            vc.run_key("../x", "f0705867")


class ListTsvTest(unittest.TestCase):
    LEGACY = ("pardon_torus_knot_distortion\t39e08a05113a3a92a4431d32de51513c9bd1be33\t208\t0\t407\n"
              "green_tao\tf0705867b4113800f875a9e7749275b8f0d66adf\t92\t0\t377\n")

    def test_append(self):
        row = ["chen_theorem-46c4f946", "46c4f946ff51", "0", "0", "525", "chen_theorem", "3600"]
        self.assertEqual(vc.upsert_list_row("", row), "\t".join(row) + "\n")
        out = vc.upsert_list_row(self.LEGACY, row)
        self.assertEqual(out, self.LEGACY + "\t".join(row) + "\n")

    def test_replace_in_place_keeps_other_rows(self):
        row = ["green_tao", "f0705867b4113800f875a9e7749275b8f0d66adf", "0", "0", "380", "green_tao", "3600"]
        out = vc.upsert_list_row(self.LEGACY + "\n\n", row)
        lines = out.splitlines()
        self.assertEqual(lines[0], self.LEGACY.splitlines()[0])
        self.assertEqual(lines[1], "\t".join(row))
        self.assertEqual(len(lines), 2)
        self.assertTrue(out.endswith("\n"))

    def test_duplicate_keys_collapse(self):
        text = "k\ta\t0\t0\t1\nother\tb\t0\t0\t2\nk\tc\t0\t0\t3\n"
        out = vc.upsert_list_row(text, ["k", "d", "0", "0", "4"])
        self.assertEqual(out, "k\td\t0\t0\t4\nother\tb\t0\t0\t2\n")
        self.assertEqual(vc.upsert_list_row(out, ["k", "d", "0", "0", "4"]), out)  # idempotent


class PushDecisionTest(unittest.TestCase):
    CAP = 30932992

    def decide(self, remote="c1", archive=1000, hits=()):
        return vc.push_decision(remote, "c1", "n2", archive, self.CAP, list(hits))

    def test_push_only_as_fast_forward_of_the_verified_commit(self):
        self.assertEqual(self.decide(), "push")
        self.assertEqual(self.decide(remote="n2"), "already_pushed")
        self.assertEqual(self.decide(remote="x9"), "hold:remote_moved")
        self.assertEqual(self.decide(remote=None), "hold:branch_missing")

    def test_archive_cap_is_strict(self):
        self.assertEqual(self.decide(archive=self.CAP - 1), "push")
        self.assertEqual(self.decide(archive=self.CAP), "hold:archive_too_big")
        self.assertEqual(self.decide(archive=None), "hold:archive_too_big")
        self.assertEqual(self.decide(remote="x9", archive=self.CAP + 5), "hold:remote_moved")

    def test_secret_hits_block(self):
        self.assertEqual(self.decide(hits=["github_token (diff line 3)"]), "hold:secret_scan")


class SecretScanTest(unittest.TestCase):
    def test_flags_secret_like_added_lines(self):
        samples = {
            "github_token": "gh" + "p_" + "A1b2C3d4" * 5,
            "anthropic_key": "sk-" + "ant-" + "api03-" + "x" * 30,
            "openai_key": "sk-" + "proj-" + "Ab1" * 10,
            "langfuse_key": "sk-" + "lf-" + "1234abcd-ef56",
            "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
            "private_key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
            "bearer": "Authorization: Bearer " + "x" * 24,
            "credential_path": "see ~/.ssh/config",
            "assignment": '"api_key": "' + "q" * 20 + '"',
            "url_credentials": "https://user:" + "pw123456" + "@" + "github.com/x.git",
            "ipv4": "host 192.0.2.7 port 22",
        }
        for name, text in samples.items():
            hits = vc.secret_scan(f"+++ b/README.md\n+{text}\n")
            self.assertTrue(any(h.startswith(name) for h in hits), (name, hits))
            self.assertNotIn(text, " ".join(hits))  # the report never repeats the secret

    def test_ignores_removed_lines_and_normal_records(self):
        token = "gh" + "p_" + "A1b2C3d4" * 5
        self.assertEqual(vc.secret_scan(f"--- a/x\n+++ b/x\n-{token}\n"), [])
        record = json.dumps(make_bundle({"Submission.lean": "ab" * 32}, "green_tao", "f0" * 20,
                                        "/srv/fleet/lean-eval-vcur/runs/k/receipts/v.json"),
                            indent=2)
        diff = "\n".join("+" + line for line in record.splitlines())
        diff += ("\n+Current verification (2026-10-14): the official `verify_candidate.py` flow passed, Lean "
                 "`v4.35.0-rc3`, wall 536.78 s.\n")
        self.assertEqual(vc.secret_scan(diff), [])


class HostWaitTest(unittest.TestCase):
    def test_reasons(self):
        cfg = vc.load_config(CONFIG)
        idle = {"procs": [], "free_bytes": 100 * 2 ** 30, "mem_available": 100 * 2 ** 30}
        self.assertIsNone(vc.host_wait_reason(idle, "48G", cfg))
        busy = dict(idle, procs=[{"pid": 7, "args": ["bash", "run_lane.sh", "e", "0-15", "48G", "x"], "cwd": None}])
        self.assertIn("host busy", vc.host_wait_reason(busy, "48G", cfg))
        self.assertIn("GiB free", vc.host_wait_reason(dict(idle, free_bytes=5 * 2 ** 30), "48G", cfg))
        self.assertIn("MemAvailable", vc.host_wait_reason(idle, "128G", cfg))
        cfg["require_mem_available"] = False
        self.assertIsNone(vc.host_wait_reason(idle, "128G", cfg))
        self.assertIsNone(vc.host_wait_reason(dict(idle, mem_available=None, free_bytes=None), "48G", cfg))


class RunOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = vc.load_config(None)
        self.item = {"state": vc.RUNNING, "started_at": 1000.0, "timeout": 3600}

    def outcome(self, now=1100.0, **probe):
        return vc.run_outcome(self.item, probe, now, self.cfg)

    def test_finished(self):
        target, note = self.outcome(run_done="rc 0 wall 9s", status={"phase": "finished", "accepted": True})
        self.assertEqual(target, vc.VERIFIED)
        self.assertIn("rc 0", note)
        target, note = self.outcome(run_done="rc 1 wall 9s", status={"phase": "finished", "accepted": False})
        self.assertEqual(target, vc.FAILED)
        target, _ = self.outcome(run_done="rc 1", status={"phase": "error", "accepted": False, "error": "boom"})
        self.assertEqual(target, vc.FAILED)
        target, _ = self.outcome(run_done="rc 137", status=None)
        self.assertEqual(target, vc.FAILED)

    def test_running_and_overdue(self):
        self.assertEqual(self.outcome(lane_active=True)[0], None)
        self.item["state"] = vc.STARTING
        self.assertEqual(self.outcome(lane_active=True)[0], vc.RUNNING)
        self.item["state"] = vc.RUNNING
        target, note = self.outcome(now=1000.0 + 3600 + 3601, lane_active=True)
        self.assertEqual(target, vc.HELD)
        self.assertIn("never pkill", note)

    def test_lane_missing(self):
        self.assertEqual(self.outcome(lane_active=False)[0], None)  # within the start grace
        self.assertEqual(self.outcome(now=1000.0 + 901, lane_active=False)[0], vc.HELD)
        self.assertEqual(self.outcome(log=["prep failed k"])[0], vc.FAILED)
        self.assertEqual(self.outcome(log=["2026-10-14T00:00:00Z no row k"])[0], vc.FAILED)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vcur-ledger-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "state" / "vcur-ledger.json"
        self.ledger = vc.Ledger(self.path, clock=lambda: 1_800_000_000.0)
        self.item = vc.new_item("green_tao", "claude/green_tao-private", "f0705867b4113800f875", 377.4, 1.8e9)

    def test_add_find_persist(self):
        self.assertTrue(self.ledger.add(self.item))
        self.assertFalse(self.ledger.add(dict(self.item)))
        again = vc.Ledger(self.path)
        found = again.find("claude/green_tao-private", "f0705867b4113800f875")
        self.assertEqual(found["key"], "green_tao-f0705867")
        self.assertEqual(found["fleet_seconds"], 377)
        self.assertEqual([i["id"] for i in again.queued()], ["green_tao@f0705867b411"])
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_happy_path_transitions_and_history(self):
        self.ledger.add(self.item)
        for state in [vc.STARTING, vc.RUNNING, vc.VERIFIED, vc.STAGED, vc.PUSHED]:
            self.ledger.move(self.item, state, note=state)
            self.assertIs(self.ledger.active(), self.item)
        self.ledger.move(self.item, vc.DONE)
        self.assertIsNone(self.ledger.active())
        self.assertEqual([h["to"] for h in self.item["history"]],
                         ["queued", "starting", "running", "verified", "staged", "pushed", "done"])
        self.assertEqual(json.loads(self.path.read_text())["items"][self.item["id"]]["state"], "done")

    def test_illegal_transitions(self):
        self.ledger.add(self.item)
        for target in [vc.RUNNING, vc.PUSHED, vc.DONE, vc.FAILED]:
            with self.assertRaises(ValueError):
                self.ledger.move(self.item, target)
        self.ledger.move(self.item, vc.SUPERSEDED)
        with self.assertRaises(ValueError):
            self.ledger.move(self.item, vc.QUEUED)

    def test_requeue_only_failed_or_held_with_a_new_key(self):
        self.ledger.add(self.item)
        with self.assertRaises(ValueError):
            self.ledger.requeue(self.item["id"])
        self.ledger.move(self.item, vc.STARTING, cpus="0-15", mem="48G", started_at=1.0, timeout=3600)
        self.ledger.move(self.item, vc.FAILED)
        item = self.ledger.requeue(self.item["id"])
        self.assertEqual((item["state"], item["attempt"], item["key"]),
                         (vc.QUEUED, 2, "green_tao-f0705867-r2"))
        self.assertNotIn("started_at", item)
        with self.assertRaises(KeyError):
            self.ledger.requeue("nope@0")

    def test_single_active_and_benchmark_guard(self):
        other = vc.new_item("chen_theorem", "claude/chen_theorem-private", "46c4f946ff51", 525, 1.8e9)
        for item in (self.item, other):
            self.ledger.add(item)
            self.ledger.move(item, vc.STARTING)
        with self.assertRaises(RuntimeError):
            self.ledger.active()
        self.path.write_text(json.dumps({"benchmark_commit": OLD_BENCH, "seen": {}, "items": {}}))
        with self.assertRaises(RuntimeError):
            vc.Ledger(self.path)

    def test_no_persist_never_writes(self):
        ledger = vc.Ledger(self.path, persist=False)
        ledger.add(self.item)
        ledger.move(self.item, vc.STARTING)
        self.assertFalse(self.path.exists())


# ---------------------------------------------------------------- staging (port of stage_current's assertions)

class StageTest(GitEnvCase):
    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-q")
        write_files(self.clone, solution_files("green_tao"))
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "package")
        self.head = git(self.clone, "rev-parse", "HEAD")
        self.bundle = make_bundle(sc.source_hashes(self.clone), "green_tao", self.head,
                                  "/srv/fleet/lean-eval-vcur/runs/green_tao-x/receipts/v.json")

    def test_records_current_verification_in_one_commit(self):
        result = sc.stage_clone(self.clone, "green_tao", self.bundle)
        new = result["new_head"]
        self.assertEqual(git(self.clone, "rev-parse", f"{new}^"), self.head)
        self.assertEqual(sorted(git(self.clone, "diff", "--name-only", self.head, new).split()),
                         ["README.md", "VERIFICATION.json"])
        record = json.loads((self.clone / "VERIFICATION.json").read_text())
        self.assertEqual(record["benchmark_commit"], BENCH)
        self.assertEqual(record["source_commit"], self.head)
        self.assertEqual(record["receipt_sha256"], "ab" * 32)
        self.assertEqual(record["verification_wall_seconds"], 536.78)
        self.assertEqual(set(record["source_sha256"]),
                         {"Submission.lean", "Submission/A.lean", "Submission/B/C.lean"})
        self.assertTrue(record["inputs_unchanged"])
        self.assertEqual(record["verifier"]["host"], "verify-host")
        self.assertEqual(record["verifier"]["flow"], sc.FLOW)  # a run whose config names no flow
        earlier = record["earlier_verification"]
        self.assertEqual(earlier["benchmark_commit"], OLD_BENCH)
        self.assertNotIn("submission_files_sha256", earlier)
        self.assertIn("note", earlier)
        readme = (self.clone / "README.md").read_text()
        self.assertIn(f"Current verification (2026-10-14): the official `verify_candidate.py` flow", readme)
        self.assertIn(f"parent `{self.head}`", readme)
        self.assertIn("\n\nEarlier: Fresh source-only official local verification passed", readme)
        self.assertLess(readme.index("Current verification"), readme.index("Earlier: Fresh"))
        message = git(self.clone, "log", "-1", "--format=%B")
        self.assertTrue(message.startswith("Verify green_tao against current benchmark 208b6b22"))
        self.assertTrue(message.endswith("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"))

    def test_a_production_verify_run_records_its_own_flow(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle["config"]["flow"] = "verify verify_candidate.py, source-only, lake test"
        sc.stage_clone(self.clone, "green_tao", bundle)
        record = json.loads((self.clone / "VERIFICATION.json").read_text())
        self.assertEqual(record["verifier"]["flow"], bundle["config"]["flow"])
        self.assertEqual(record["verifier"]["evaluation_flow"], "comparator")

    def test_a_lake_comparator_run_says_so_in_record_and_readme(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle["config"].update(evaluation_flow="lake-comparator", bwrap_version="0.12.0", bwrap_sha256="cd" * 32)
        sc.stage_clone(self.clone, "green_tao", bundle)
        verifier = json.loads((self.clone / "VERIFICATION.json").read_text())["verifier"]
        self.assertEqual((verifier["evaluation_flow"], verifier["bwrap_version"]), ("lake-comparator", "0.12.0"))
        self.assertIn("flow (the toolchain's `lake comparator` under bubblewrap 0.12.0) passed",
                      (self.clone / "README.md").read_text())

    def assert_refused(self, mutate, expect: str):
        bundle = json.loads(json.dumps(self.bundle))
        mutate(bundle)
        with self.assertRaises(sc.StageError) as ctx:
            sc.stage_clone(self.clone, "green_tao", bundle)
        self.assertIn(expect, str(ctx.exception))
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(self.clone, "status", "--porcelain"), "")

    def test_refusals_leave_the_clone_untouched(self):
        cases = [
            (lambda b: b["status"].update(accepted=False), "not accepted"),
            (lambda b: b["status"].update(phase="error"), "not accepted"),
            (lambda b: b["receipt"].update(nanoda_accepted=False), "both kernels"),
            (lambda b: b["receipt"].update(timed_out=True), "timed_out True"),
            (lambda b: b["receipt"].update(exit_code=1), "receipt exit 1"),
            (lambda b: b["receipt"].update(illegal_sorry_axiom=True), "illegal True"),
            (lambda b: b["receipt"].update(benchmark_commit=OLD_BENCH), "benchmark"),
            (lambda b: b["config"].update(benchmark_commit=OLD_BENCH), "benchmark"),
            (lambda b: b["config"].update(commit="0" * 40), "run is green_tao@000"),
            (lambda b: b["config"].update(problem_id="chen_theorem"), "run is chen_theorem@"),
            (lambda b: b["hashes"].update({"Submission/A.lean": "00" * 32}), "receipt source hashes differ"),
            (lambda b: (b["hashes"].update({"Submission/A.lean": "00" * 32}), b["receipt"].pop("source_sha256")),
             "verified sources differ"),
            (lambda b: b["status"].update(protected_unchanged=False), "inputs changed: protected_unchanged"),
            (lambda b: b["status"].update(dependency_sources_unchanged=None), "dependency_sources_unchanged"),
        ]
        for mutate, expect in cases:
            with self.subTest(expect=expect):
                self.assert_refused(mutate, expect)

    def test_refuses_without_readme_marker_or_when_already_current(self):
        (self.clone / "README.md").write_text("# demo\n\nNo verification paragraph.\n")
        git(self.clone, "commit", "-q", "-am", "readme")
        self.head = git(self.clone, "rev-parse", "HEAD")
        self.bundle["config"]["commit"] = self.head
        self.assert_refused(lambda b: None, "README has no")
        write_files(self.clone, {"README.md": README, "VERIFICATION.json": json.dumps(
            old_verification("green_tao", benchmark_commit=BENCH))})
        git(self.clone, "commit", "-q", "-am", "current")
        self.head = git(self.clone, "rev-parse", "HEAD")
        self.bundle["config"]["commit"] = self.head
        self.assert_refused(lambda b: None, "already on the current benchmark")

    def test_refuses_dirty_clone(self):
        (self.clone / "Submission" / "A.lean").write_text("def a := 2\n")
        with self.assertRaises(sc.StageError):
            sc.stage_clone(self.clone, "green_tao", self.bundle)

    def test_copy_sources_takes_only_lean_under_submission(self):
        (self.clone / "tools").mkdir()
        (self.clone / "tools" / "Gen.lean").write_text("-- not a submission file\n")
        src = self.tmp / "src"
        self.assertEqual(vc.copy_sources(self.clone, src), 3)
        self.assertEqual(sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()),
                         ["Submission.lean", "Submission/A.lean", "Submission/B/C.lean"])


# ---------------------------------------------------------------- the whole state machine, offline

class FakeHub(vc.Hub):
    def __init__(self, bare: Path, credited=()):
        super().__init__(remote=str(bare))
        self.bare, self.credited_set, self.pushes = bare, set(credited), []

    def file_json(self, commit, path):
        result = subprocess.run(["git", "--git-dir", str(self.bare), "show", f"{commit}:{path}"],
                                capture_output=True, text=True, env=vc.ENV)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def commit_info(self, commit):
        when, tree = vc.run(["git", "--git-dir", self.bare, "log", "-1", "--format=%cI %T", commit]).split()
        return {"time": when, "tree": tree}

    def credited(self):
        return set(self.credited_set)

    def push(self, clone, new_head, branch):
        self.pushes.append((branch, new_head))
        super().push(clone, new_head, branch)


class FakeHost(vc.Host):
    """Runs the remote programs locally against a temporary home; lanes are recorded, not started."""

    def __init__(self, cfg, home: Path):
        super().__init__(cfg)
        self.home, self.python = home, sys.executable
        self.started, self.commands, self.extra_procs = [], [], []

    def run(self, command, stdin=None, timeout=300):
        self.commands.append(command)
        return subprocess.run(["bash", "-c", command], cwd=self.home, env={**vc.ENV, "HOME": str(self.home)},
                              input=stdin, capture_output=True, text=True, check=True, timeout=timeout).stdout

    def rsync(self, sources, dest, delete=False):
        self.commands.append(f"rsync -> {dest}")
        target = self.home / dest
        for source in map(str, sources):
            if source.endswith("/"):
                if delete and target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / Path(source).name)

    def probe(self, key, tail, markers):
        result = super().probe(key, tail, markers)
        result["procs"] = result["procs"] + self.extra_procs
        return result

    def start_lane(self, lane, cpus, mem, key):
        self.commands.append(f"start {key}")
        self.started.append((lane, cpus, mem, key))


class FlowTest(GitEnvCase):
    def setUp(self):
        super().setUp()
        self.bare = self.tmp / "remote.git"
        git(None, "init", "-q", "--bare", str(self.bare))
        self.alpha = commit_branch(self.bare, "claude/alpha_problem-private", solution_files("alpha_problem"),
                                   date="2026-10-14T10:00:00Z")
        commit_branch(self.bare, "claude/beta_problem-private",
                      solution_files("beta_problem", old_verification("beta_problem", benchmark_commit=BENCH)))
        commit_branch(self.bare, "claude/gamma_problem-private", solution_files("gamma_problem", False))
        commit_branch(self.bare, "claude/delta_problem-private", solution_files("delta_problem"))
        commit_branch(self.bare, "main", {"README.md": "repo\n"})
        self.home = self.tmp / "home"
        (self.home / BASE).mkdir(parents=True)
        self.cfg = vc.load_config(CONFIG)
        self.cfg.update(min_commit_time=None, require_mem_available=False, min_free_gb=0)
        self.hub = FakeHub(self.bare, credited={"delta_problem"})
        self.host = FakeHost(self.cfg, self.home)
        self.ledger_path = self.tmp / "state" / "vcur-ledger.json"
        self.now = [1_800_000_000.0]
        self.lines: list[str] = []

    def runner(self, apply=True) -> vc.Runner:
        ledger = vc.Ledger(self.ledger_path, persist=apply, clock=lambda: self.now[0])
        return vc.Runner(self.cfg, ledger, self.host, self.hub, apply=apply, workdir=self.tmp / "work",
                         out=self.lines.append, clock=lambda: self.now[0])

    def remote_head(self, branch="claude/alpha_problem-private") -> str:
        return git(None, "--git-dir", str(self.bare), "rev-parse", f"refs/heads/{branch}")

    def base(self) -> Path:
        return (self.home / BASE).resolve()

    def complete_run(self, item, accepted=True):
        """What run_lane.sh + verify_candidate.py leave behind in runs/<key>."""
        run, src = self.base() / "runs" / item["key"], self.base() / "srcs" / item["key"]
        hashes = {p.relative_to(src).as_posix(): sha256(p) for p in sorted(src.rglob("*")) if p.is_file()}
        receipt = run / "receipts" / "verifier-candidate-20261014T152048944674Z.json"
        receipt.parent.mkdir(parents=True)
        bundle = make_bundle(hashes, item["problem_id"], item["commit"], str(receipt))
        receipt.write_text(json.dumps(bundle["receipt"]))
        bundle["status"].update(receipt_sha256=sha256(receipt), accepted=accepted)
        for name, data in [("status.json", bundle["status"]), ("config.json", bundle["config"]),
                           ("source-sha256.json", hashes)]:
            (run / name).write_text(json.dumps(data, indent=2))
        (run / "RUN-DONE").write_text(f"rc {0 if accepted else 1} wall 540s peak 9177800704 oom 0\n")
        build = run / "verification" / "candidate-20261014T152048944674Z" / ".lake" / "build"
        build.mkdir(parents=True)
        (build / "Submission.olean").write_bytes(b"0" * 4096)
        return build

    def test_full_cycle_pushes_one_fast_forward_commit_and_cleans_up(self):
        runner = self.runner()
        self.assertEqual(runner.tick(), 0)
        self.assertEqual(runner.decisions, {
            "claude/alpha_problem-private": "queue", "claude/beta_problem-private": "current",
            "claude/gamma_problem-private": "skip:no_verification",
            "claude/delta_problem-private": "skip:credited"})
        item = runner.ledger.items["alpha_problem@" + self.alpha[:12]]
        key = f"alpha_problem-{self.alpha[:8]}"
        self.assertEqual((item["state"], item["key"], item["timeout"], item["mem"]),
                         (vc.STARTING, key, 3600, "48G"))
        self.assertEqual(self.host.started, [("auto", "0-15", "48G", key)])
        self.assertEqual((self.base() / "list.tsv").read_text(),
                         f"{key}\t{self.alpha}\t0\t0\t100\talpha_problem\t3600\n")
        self.assertEqual(sorted(p.relative_to(self.base() / "srcs" / key).as_posix()
                                for p in (self.base() / "srcs" / key).rglob("*") if p.is_file()),
                         ["Submission.lean", "Submission/A.lean", "Submission/B/C.lean"])
        self.assertEqual(sorted(vc.LANE_SCRIPTS), ["prep_current.py", "run_lane.sh", "run_verify.sh",
                                                   "verify_candidate.py", "verify_run.py", "workspace_manager.py"])
        for name, path in vc.LANE_SCRIPTS.items():
            self.assertEqual(sha256(self.base() / "auto" / name), sha256(path))

        self.assertEqual(self.runner().tick(), 0)  # lane not visible yet, within grace: wait
        self.assertEqual(vc.Ledger(self.ledger_path).items[item["id"]]["state"], vc.STARTING)

        build = self.complete_run(item)
        runner = self.runner()
        self.assertEqual(runner.tick(), 0, self.lines)
        item = runner.ledger.items[item["id"]]
        self.assertEqual(item["state"], vc.DONE, self.lines)
        self.assertEqual([h["to"] for h in item["history"]],
                         ["queued", "starting", "verified", "staged", "pushed", "done"])
        new_head = self.remote_head()
        self.assertEqual(new_head, item["new_head"])
        self.assertEqual(git(None, "--git-dir", str(self.bare), "rev-parse", f"{new_head}^"), self.alpha)
        self.assertEqual(sorted(git(None, "--git-dir", str(self.bare), "diff", "--name-only", self.alpha,
                                    new_head).split()), ["README.md", "VERIFICATION.json"])
        record = self.hub.file_json(new_head, "VERIFICATION.json")
        self.assertEqual((record["benchmark_commit"], record["source_commit"]), (BENCH, self.alpha))
        self.assertEqual(self.hub.pushes, [("claude/alpha_problem-private", new_head)])
        self.assertFalse(build.exists())
        self.assertTrue((self.base() / "runs" / key / "receipts").is_dir())  # receipts stay, builds go
        self.assertFalse((self.tmp / "work" / key).exists())
        self.assertTrue(any(line.split()[1:3] == ["done", "alpha_problem"] for line in self.lines))

        commands = len(self.host.commands)
        runner = self.runner()
        self.assertEqual(runner.tick(), 0)  # idempotent: the branch is current now, nothing to do
        self.assertEqual(runner.decisions["claude/alpha_problem-private"], "current")
        self.assertEqual(len(runner.ledger.items), 1)
        self.assertEqual(self.host.started, [("auto", "0-15", "48G", key)])
        self.assertFalse(any(c.startswith(("start", "rsync")) for c in self.host.commands[commands:]))

    def test_dry_run_reads_only(self):
        self.assertEqual(self.runner(apply=False).tick(), 0)
        text = "\n".join(self.lines)
        self.assertIn(f"would queue alpha_problem@{self.alpha[:12]}", text)
        self.assertIn(f"would start alpha_problem@{self.alpha[:12]} as alpha_problem-{self.alpha[:8]}", text)
        self.assertIn("skip:no_verification", text)
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(sorted(p.name for p in self.base().iterdir()), [])
        self.assertEqual(self.host.started, [])
        self.assertFalse((self.tmp / "work").exists())
        self.assertEqual(self.remote_head(), self.alpha)
        self.assertTrue(all("python" in c and " - " in c for c in self.host.commands), self.host.commands)

    def test_push_refused_when_remote_moved_after_staging(self):
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        self.complete_run(item)
        runner.poll(item)
        runner.stage(item)
        self.assertEqual(item["state"], vc.STAGED)
        moved = commit_branch(self.bare, "claude/alpha_problem-private", {"Submission/A.lean": "def a := 3\n"},
                              follow=True)
        runner.guard(item, runner.push, item)
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("moved from", item["held_reason"])
        self.assertEqual(self.remote_head(), moved)
        self.assertEqual(self.hub.pushes, [])

    def test_branch_moved_to_a_current_record_supersedes(self):
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        self.complete_run(item)
        runner.poll(item)
        commit_branch(self.bare, "claude/alpha_problem-private", {"VERIFICATION.json": json.dumps(
            old_verification("alpha_problem", benchmark_commit=BENCH))}, follow=True)
        runner.guard(item, runner.stage, item)
        self.assertEqual(item["state"], vc.SUPERSEDED)
        self.assertEqual(self.hub.pushes, [])

    def test_archive_over_cap_is_held_before_and_after_verification(self):
        self.cfg["archive_cap_bytes"] = 1000
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("shrink it", item["held_reason"])
        self.assertEqual(self.host.started, [])

        self.cfg["archive_cap_bytes"] = 30932992
        runner.ledger.requeue(item["id"])
        runner.tick()
        self.complete_run(item)
        runner.poll(item)
        runner.stage(item)
        self.cfg["archive_cap_bytes"] = item["archive_bytes"]  # the staged tree is exactly at the cap
        runner.guard(item, runner.push, item)
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("source.tar.gz would be", item["held_reason"])
        self.assertEqual(self.remote_head(), self.alpha)

    def test_secret_like_metadata_is_never_pushed(self):
        token = "gh" + "p_" + "Z9y8X7w6" * 5
        commit_branch(self.bare, "claude/alpha_problem-private", {"VERIFICATION.json": json.dumps(
            old_verification("alpha_problem", note=f"debug {token}"))}, follow=True)
        alpha = self.remote_head()
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{alpha[:12]}"]
        self.complete_run(item)
        runner.tick()
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("secret-like", item["held_reason"])
        self.assertNotIn(token, json.dumps(runner.ledger.data))
        self.assertEqual(self.remote_head(), alpha)

    def test_busy_host_keeps_the_item_queued(self):
        self.host.extra_procs = [{"pid": 4242, "args": ["/usr/local/bin/python3.12", "verify_candidate.py"],
                                  "cwd": "/srv/fleet/verify-conveyor/runtime/x"}]
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        self.assertEqual(item["state"], vc.QUEUED)
        self.assertTrue(any("host busy" in line for line in self.lines))
        self.assertEqual(self.host.started, [])

        moved = commit_branch(self.bare, "claude/alpha_problem-private", {"Submission/A.lean": "def a := 4\n"},
                              follow=True)
        runner = self.runner()
        runner.tick()
        self.assertEqual(runner.ledger.items[item["id"]]["state"], vc.SUPERSEDED)
        self.assertEqual(runner.ledger.items[f"alpha_problem@{moved[:12]}"]["state"], vc.QUEUED)

    def test_failed_run_is_not_replayed_until_requeued(self):
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        build = self.complete_run(item, accepted=False)
        runner.tick()
        self.assertEqual(item["state"], vc.FAILED)
        self.assertFalse(build.exists())
        runner.tick()
        self.assertEqual(len(self.host.started), 1)
        runner.ledger.requeue(item["id"])
        runner.tick()
        self.assertEqual(self.host.started[-1][3], f"alpha_problem-{self.alpha[:8]}-r2")
        self.assertIn(f"alpha_problem-{self.alpha[:8]}-r2\t", (self.base() / "list.tsv").read_text())

    def test_prep_failure_from_the_lane_log_and_lost_lane(self):
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        (self.base() / "lane-auto.log").write_text(
            f"2026-10-14T00:00:00Z start {item['key']}-r2 c timeout 3600\nprep failed {item['key']}-r2\n")
        self.now[0] += 1000
        runner.tick()
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("lane not running", item["held_reason"])
        runner.ledger.requeue(item["id"])
        runner.tick()
        self.assertEqual(item["state"], vc.STARTING)
        runner.tick()
        self.assertEqual(item["state"], vc.FAILED)
        self.assertIn("prep failed", item["history"][-1]["note"])

    def test_existing_run_dir_is_held_not_overwritten(self):
        key = f"alpha_problem-{self.alpha[:8]}"
        (self.base() / "runs" / key).mkdir(parents=True)
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        self.assertEqual(item["state"], vc.HELD)
        self.assertEqual(self.host.started, [])

    def test_newest_sibling_branch_wins(self):
        own = commit_branch(self.bare, "claude/alpha_problem-own-0123abcd-private", solution_files("alpha_problem"),
                            date="2026-10-14T12:00:00Z")
        runner = self.runner()
        runner.tick()
        self.assertEqual(runner.decisions["claude/alpha_problem-private"], "skip:newer_branch_for_problem")
        self.assertEqual(runner.decisions["claude/alpha_problem-own-0123abcd-private"], "queue")
        self.assertEqual([i["branch"] for i in runner.ledger.items.values()],
                         ["claude/alpha_problem-own-0123abcd-private"])
        self.assertEqual(runner.ledger.items[f"alpha_problem@{own[:12]}"]["state"], vc.STARTING)

    def test_problem_with_a_current_branch_is_skipped_unless_forced(self):
        commit_branch(self.bare, "claude/alpha_problem-own-0123abcd-private",
                      solution_files("alpha_problem", old_verification("alpha_problem", benchmark_commit=BENCH)))
        runner = self.runner(apply=False)
        runner.tick()
        self.assertEqual(runner.decisions["claude/alpha_problem-private"], "skip:problem_has_current_branch")
        self.assertFalse(any(line.startswith("would queue") for line in self.lines))
        self.cfg["force_branches"] = ["claude/alpha_problem-private"]
        runner = self.runner(apply=False)
        runner.tick()
        self.assertEqual(runner.decisions["claude/alpha_problem-private"], "queue")
        self.assertTrue(any(line.startswith("would queue alpha_problem@") for line in self.lines))

    def test_transient_errors_retry_then_hold(self):
        runner = self.runner()
        runner.tick()
        item = runner.ledger.items[f"alpha_problem@{self.alpha[:12]}"]
        with mock.patch.object(FakeHost, "probe", side_effect=subprocess.CalledProcessError(255, "ssh")):
            for n in range(1, self.cfg["max_step_errors"]):
                self.assertEqual(runner.tick(), 1)
                self.assertEqual((item["state"], item["errors"]), (vc.STARTING, n))
            runner.tick()
        self.assertEqual(item["state"], vc.HELD)
        self.assertIn("consecutive errors", item["held_reason"])


class LaneEnvTest(unittest.TestCase):
    """config `workbench` reaches run_lane.sh as a plain variable, quoted, relative to ~."""

    def test_workbench_variable(self):
        cfg = vc.load_config(CONFIG)
        self.assertEqual(vc.lane_env(cfg), {})
        cfg.update(workbench="srv/workbench")
        self.assertEqual(vc.lane_env(cfg), {"LEANEVAL_WORKBENCH": "srv/workbench"})
        for bad in ("~/wb", "a/../b", 'x"; rm -rf ~', "$(id)"):
            with self.assertRaises(ValueError):
                vc.lane_env(dict(cfg, workbench=bad))

    def test_start_command_carries_the_variables(self):
        cfg = vc.load_config(CONFIG)
        cfg.update(host="verify-host", workbench="srv/workbench")
        host = vc.Host(cfg)
        sent = []
        host.run = lambda command, stdin=None, timeout=300: sent.append(command) or ""
        host.start_lane("auto", "0-15", "48G", "k-1")
        self.assertIn('VCUR_BASE="$PWD" LEANEVAL_WORKBENCH="$HOME/srv/workbench" nohup setsid bash '
                      "auto/run_lane.sh auto 0-15 48G k-1", sent[0])


class PrepCurrentTest(unittest.TestCase):
    def test_default_mode_prepares_with_production_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
                   "LEANEVAL_WORKBENCH": str(Path(tmp) / "no-workbench")}
            out = subprocess.run([sys.executable, str(vc.VCUR / "prep_current.py"), str(Path(tmp) / "V"), tmp,
                                  "c" * 40, "two_plus_two", "60"], env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("build the workbench first", out.stderr)  # verify_run.prepare, found in the repository


class RunLaneTest(unittest.TestCase):
    """run_lane.sh reads both the manual 5-column rows and the automation's 7-column rows."""

    PREP = ("import json, os, sys\n"
            "V, SRC, commit, problem, timeout = sys.argv[1:6]\n"
            "if problem == 'bad_p':\n    sys.exit(3)\n"
            "os.makedirs(V)\n"
            "json.dump({'src': SRC, 'commit': commit, 'problem': problem, 'timeout': int(timeout)},"
            " open(os.path.join(V, 'args.json'), 'w'))\n"
            "json.dump({'phase': 'finished', 'accepted': True, 'receipt_sha256': 'ab' * 32},"
            " open(os.path.join(V, 'status.json'), 'w'))\n")
    VERIFY = '#!/bin/bash\necho "rc 0 wall 1s mem $2 cpus $3 unit $4" > "$1/RUN-DONE"\n'

    def test_rows_timeouts_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = tmp / "base"
            base.mkdir()
            (tmp / "prep.py").write_text(self.PREP)
            (tmp / "verify.sh").write_text(self.VERIFY)
            (base / "list.tsv").write_text("legacy_p\tc1\t208\t0\t1200\n"
                                           "k-new\tc2\t0\t0\t100\tnew_p\t4000\n"
                                           "k-def\tc3\t0\t0\t536.78\tdef_p\n"
                                           "bad\tc4\t0\t0\t10\tbad_p\t3600\n")
            env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "VCUR_BASE": str(base), "VCUR_PY": sys.executable,
                   "VCUR_PREP": str(tmp / "prep.py"), "VCUR_RUN_VERIFY": str(tmp / "verify.sh")}
            subprocess.run(["bash", str(vc.VCUR / "run_lane.sh"), "t", "0-1", "1G",
                            "legacy_p", "k-new", "k-def", "bad", "missing"], env=env, check=True, timeout=60)
            args = {k: json.loads((base / "runs" / k / "args.json").read_text())
                    for k in ["legacy_p", "k-new", "k-def"]}
            self.assertEqual((args["legacy_p"]["problem"], args["legacy_p"]["timeout"]), ("legacy_p", 4800))
            self.assertEqual((args["k-new"]["problem"], args["k-new"]["timeout"], args["k-new"]["commit"]),
                             ("new_p", 4000, "c2"))
            self.assertEqual((args["k-def"]["problem"], args["k-def"]["timeout"]), ("def_p", 3600))
            self.assertEqual(args["k-new"]["src"], str(base / "srcs" / "k-new"))
            log = (base / "lane-t.log").read_text().splitlines()
            done = [line for line in log if line.split()[1] == "done"]
            self.assertEqual([line.split()[2] for line in done], ["legacy_p", "k-new", "k-def"])
            self.assertTrue(all(line.endswith("accepted True receipt " + "ab" * 8) for line in done), done)
            self.assertIn("unit vcur-t-k-new", done[1])
            self.assertIn("prep failed bad", log)
            self.assertTrue(any(line.split()[1:] == ["no", "row", "missing"] for line in log))
            self.assertTrue(log[-1].endswith("lane done"))


if __name__ == "__main__":
    unittest.main()
