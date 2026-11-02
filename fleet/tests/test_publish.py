"""Offline controls for the publisher: grouping, canonical choice, branch decisions, fast-forward
(a local bare repository stands in for GitHub; no network, no ssh)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("leh_publish_test", HERE.parent / "local/publish.py")
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)

BENCH = "208b6b22d396f28771dba56414b19f47cae5f2a7"
OLD_BENCH = "0e093293" + "0" * 32


def package(digest: str, host: str = "host-2", predicted: int = 120, archive: int = 1000, fit: bool = True,
            bench: str = OLD_BENCH, created: str = "2026-10-14T10:00:00+00:00", pid: str = "green_tao") -> dict:
    return {"problem_id": pid, "artifact_digest": digest, "host": host, "status": "ready" if fit else "hold_ci_review",
            "ci_gate": {"fit": fit, "predicted_ci_minutes": predicted, "archive_bytes": archive},
            "benchmark_commit": bench, "created_at": created, "dir": f"/outbox/{pid}/{digest[:16]}",
            "branch": f"claude/{pid}-private",
            "commit_message": f"Package {pid}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"}


def verification(digest: str, predicted: int = 120, fit: bool = True, bench: str = OLD_BENCH) -> dict:
    return {"benchmark_commit": bench, "status": "official_local_pass", "artifact_digest": digest,
            "ci_fit_estimate": {"fit": fit, "predicted_ci_minutes": predicted, "archive_bytes": 1000}}


class ChoiceTest(unittest.TestCase):
    def test_current_bench_is_read_from_vcur(self):
        self.assertEqual(publish.current_bench(), BENCH)

    def test_grouping_and_canonical_choice(self):
        packages = [package("a" * 64, predicted=100), package("a" * 64, host="host-4", predicted=100),
                    package("b" * 64, predicted=90, created="2026-10-14T12:00:00+00:00"),
                    package("c" * 64, predicted=90, created="2026-10-14T11:00:00+00:00"),
                    package("d" * 64, predicted=90, archive=500, created="2026-10-14T13:00:00+00:00"),
                    package("e" * 64, pid="hadwiger")]
        groups = publish.group_packages(packages)
        self.assertEqual(sorted(groups), ["green_tao", "hadwiger"])
        self.assertEqual([p["artifact_digest"][0] for p in groups["green_tao"]], ["a", "b", "c", "d"])  # a once
        best, rest = publish.choose(groups["green_tao"], BENCH)
        # fewer predicted minutes, then the smaller archive beat age
        self.assertEqual((best["artifact_digest"][0], [p["artifact_digest"][0] for p in rest]), ("d", ["c", "b", "a"]))
        # a verification on the current benchmark beats every CI number
        current = package("f" * 64, predicted=250, archive=9000, bench=BENCH)
        self.assertEqual(publish.choose(groups["green_tao"] + [current], BENCH)[0]["artifact_digest"][0], "f")
        # equal in everything: the older package
        twins = [package("g" * 64, created="2026-10-14T12:00:00+00:00"), package("h" * 64, created="2026-10-14T09:00:00+00:00")]
        self.assertEqual(publish.choose(twins, BENCH)[0]["artifact_digest"][0], "h")

    def test_branch_decisions(self):
        new = package("a" * 64, predicted=100)
        head = "1" * 40
        known = {head}
        branch = {"head": head, "tree": "t", "verification": verification("z" * 64, predicted=200)}
        self.assertEqual(publish.decide(new, None, known, BENCH), "create")
        self.assertEqual(publish.decide(new, branch, known, BENCH), "fast_forward")  # fewer predicted minutes
        self.assertEqual(publish.decide(new, branch, set(), BENCH), "hold:foreign_history")
        same = dict(branch, verification=verification("a" * 64))
        self.assertEqual(publish.decide(new, same, set(), BENCH), "already_published")
        vcur = dict(branch, verification={"benchmark_commit": BENCH, "status": "official_local_pass",
                                          "earlier_verification": verification("a" * 64)})
        self.assertEqual(publish.decide(new, vcur, set(), BENCH), "already_published")
        equal = dict(branch, verification=verification("z" * 64, predicted=100))
        self.assertEqual(publish.decide(new, equal, known, BENCH), "superseded")  # not strictly better
        current = dict(branch, verification=dict(verification("z" * 64, predicted=280), benchmark_commit=BENCH))
        self.assertEqual(publish.decide(new, current, known, BENCH), "superseded")
        unfit = dict(branch, verification=verification("z" * 64, predicted=40, fit=False))
        self.assertEqual(publish.decide(new, unfit, known, BENCH), "fast_forward")
        unreadable = dict(branch, verification=None)
        self.assertEqual(publish.decide(new, unreadable, known, BENCH), "fast_forward")

    def test_list_program_reports_unpublished_packages_with_their_benchmark(self):
        self.assertNotIn("'", publish.LIST_PROGRAM)  # it goes into python3 -c '...' over ssh
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp)
            for digest, bench in (("aaaa", OLD_BENCH), ("bbbb", None), ("cccc", BENCH)):
                (outbox / "p" / digest / "tree").mkdir(parents=True)
                (outbox / "p" / digest / "package.json").write_text(json.dumps({"problem_id": "p"}))
                if bench:
                    (outbox / "p" / digest / "tree" / "VERIFICATION.json").write_text(
                        json.dumps({"benchmark_commit": bench}))
            (outbox / "p" / "cccc" / "published.json").write_text("{}")
            out = subprocess.run([sys.executable, "-c", publish.LIST_PROGRAM, str(outbox)], check=True,
                                 capture_output=True, text=True).stdout
        listed = {Path(p["dir"]).name: p["benchmark_commit"] for p in json.loads(out)}
        self.assertEqual(listed, {"aaaa": OLD_BENCH, "bbbb": None})

    def test_known_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            ours, foreign, vcur_ours, vcur_foreign, trusted = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)
            (state / "ledger.jsonl").write_text(
                json.dumps({"status": "published", "commit": ours}) + "\n"
                + json.dumps({"status": "already_published", "commit": foreign}) + "\nnot json\n")
            (state / "vcur-ledger.json").write_text(json.dumps({"items": {
                "a": {"commit": ours, "new_head": vcur_ours, "state": "done"},
                "b": {"commit": foreign, "new_head": vcur_foreign, "state": "done"},
                "c": {"commit": vcur_ours, "new_head": "6" * 40, "state": "pushed"}}}))
            (state / "publisher-trusted.json").write_text(json.dumps([{"branch": "claude/x-private", "commit": trusted}]))
            self.assertEqual(publish.known_commits(state), {ours, vcur_ours, "6" * 40, trusted})


class PublishGitTest(unittest.TestCase):
    """publish_problem against a local bare repository standing in for the publish repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        env = dict(publish.ENV, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=str(base / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
        (base / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        self.trees = base / "trees"
        for patch in (mock.patch.object(publish, "ENV", env), mock.patch.object(publish, "STATE", base / "state"),
                      mock.patch.object(publish, "fetch_tree", self.fetch_tree)):
            patch.start()
            self.addCleanup(patch.stop)
        hub_remote = str(self.remote)

        class LocalHub(publish.Hub):
            def ensure_private(self):
                return None

            def remote_tree(self, commit):
                return subprocess.run(["git", "--git-dir", hub_remote, "rev-parse", f"{commit}^{{tree}}"],
                                      check=True, capture_output=True, text=True).stdout.strip()

        self.hub = LocalHub(remote_url=hub_remote)

    def make_package(self, digest: str, text: str, **kw) -> dict:
        """A package tree as the feeder writes it, with package.json-style hashes."""
        tree = self.trees / digest[:16]
        files = {"Submission.lean": text, "VERIFICATION.json": json.dumps(verification(digest, kw.get("predicted", 120))),
                 "README.md": "readme\n"}
        for name, body in files.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(body)
        pkg = package(digest, **kw)
        pkg["files"] = {n: hashlib.sha256(b.encode()).hexdigest() for n, b in files.items()}
        pkg["source_tree"] = str(tree)
        return pkg

    def fetch_tree(self, pkg: dict, tree: Path) -> None:
        shutil.copytree(pkg["source_tree"], tree, dirs_exist_ok=True)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "--git-dir", str(self.remote), *args], check=True, capture_output=True,
                              text=True, env=publish.ENV).stdout.strip()

    def seed_branch(self, digest: str, predicted: int) -> str:
        """The branch as published earlier: one commit with its VERIFICATION.json."""
        pkg = self.make_package(digest, "theorem old : True := trivial\n", predicted=predicted)
        work = Path(self.tmp.name) / f"seed-{digest[:4]}"
        shutil.copytree(pkg["source_tree"], work)
        commit, _ = publish.commit_tree(work, "old package\n")
        self.hub.push(work, commit, "claude/green_tao-private")
        return commit

    def test_no_branch_creates_an_orphan_commit(self):
        pkg = self.make_package("a" * 64, "theorem a : True := trivial\n")
        [(_, record)] = publish.publish_problem("green_tao", [pkg], self.hub, False, True, set(), BENCH, set())
        self.assertEqual((record["status"], record["mode"], record["parent"]), ("published", "create", None))
        self.assertEqual(self.git("rev-parse", "refs/heads/claude/green_tao-private"), record["commit"])
        self.assertEqual(self.git("rev-list", "--count", record["commit"]), "1")

    def test_our_branch_is_fast_forwarded_by_a_strictly_better_package(self):
        head = self.seed_branch("z" * 64, predicted=250)
        best = self.make_package("a" * 64, "theorem a : True := trivial\n", predicted=100, host="host-4")
        worse = self.make_package("b" * 64, "theorem b : True := trivial\n", predicted=200)
        outcomes = publish.publish_problem("green_tao", [worse, best], self.hub, False, True, {head}, BENCH, set())
        records = {p["artifact_digest"][0]: r for p, r in outcomes}
        self.assertEqual((records["a"]["status"], records["a"]["mode"], records["a"]["parent"]),
                         ("published", "fast_forward", head))
        self.assertEqual((records["b"]["status"], records["b"]["by"]), ("superseded", "a" * 64))
        new_head = self.git("rev-parse", "refs/heads/claude/green_tao-private")
        self.assertEqual(new_head, records["a"]["commit"])
        self.assertEqual(self.git("rev-parse", f"{new_head}^"), head)  # a fast-forward, history kept
        self.assertEqual(self.git("show", f"{new_head}:Submission.lean"), "theorem a : True := trivial")
        self.assertIn("Update green_tao to the better verified package", self.git("log", "-1", "--format=%B", new_head))
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), "refs/heads/claude/green_tao-private")

    def test_a_branch_with_an_unknown_head_is_held(self):
        head = self.seed_branch("z" * 64, predicted=250)
        pkg = self.make_package("a" * 64, "theorem a : True := trivial\n", predicted=100)
        [(_, record)] = publish.publish_problem("green_tao", [pkg], self.hub, False, True, set(), BENCH, set())
        self.assertEqual((record["status"], record["reason"], record["head"]), ("hold", "foreign_history", head))
        self.assertEqual(self.git("rev-parse", "refs/heads/claude/green_tao-private"), head)
        self.assertFalse((publish.STATE / "staging" / f"green_tao-{'a' * 16}").exists())  # nothing staged

    def test_a_package_that_is_not_better_is_superseded_and_nothing_moves(self):
        head = self.seed_branch("z" * 64, predicted=100)
        pkg = self.make_package("a" * 64, "theorem a : True := trivial\n", predicted=100)
        cache: dict = {}
        [(_, record)] = publish.publish_problem("green_tao", [pkg], self.hub, False, True, {head}, BENCH, set(), cache)
        self.assertEqual((record["status"], record["by"]), ("superseded", f"branch head {head}"))
        self.assertEqual(self.git("rev-parse", "refs/heads/claude/green_tao-private"), head)
        self.assertIn(head, cache)  # the head's facts are cached: the next run does not fetch again
        with mock.patch.object(self.hub, "fetch", side_effect=AssertionError("fetched again")):
            publish.publish_problem("green_tao", [pkg], self.hub, True, True, {head}, BENCH, set(), cache)

    def test_credited_problems_are_skipped_and_dry_run_pushes_nothing(self):
        pkg = self.make_package("a" * 64, "theorem a : True := trivial\n")
        [(_, record)] = publish.publish_problem("green_tao", [pkg], self.hub, False, True, set(), BENCH, {"green_tao"})
        self.assertEqual(record["status"], "skipped_already_accepted_for_us")
        [(_, record)] = publish.publish_problem("green_tao", [pkg], self.hub, True, True, set(), BENCH, set())
        self.assertEqual(record["status"], "dry_run")
        self.assertEqual(self.git("for-each-ref"), "")

    def test_the_push_is_never_forced(self):
        head = self.seed_branch("z" * 64, predicted=250)
        pkg = self.make_package("a" * 64, "theorem a : True := trivial\n", predicted=100)
        tree, _, _ = publish.stage(pkg)
        commit, _ = publish.commit_tree(tree, "update\n", self.hub, "claude/green_tao-private", head)
        moved = self.seed_branch_on_top(head)
        with self.assertRaises(subprocess.CalledProcessError):
            self.hub.push(tree, commit, "claude/green_tao-private")  # not a fast-forward any more: refused
        self.assertEqual(self.git("rev-parse", "refs/heads/claude/green_tao-private"), moved)

    def seed_branch_on_top(self, head: str) -> str:
        work = Path(self.tmp.name) / "other"
        subprocess.run(["git", "init", "-q", str(work)], check=True, env=publish.ENV)
        self.hub.fetch(work, "claude/green_tao-private")
        (work / "x").write_text("x\n")
        publish.git(work, "add", "x")
        tree = publish.git(work, "write-tree")
        commit = publish.git(work, "commit-tree", tree, "-p", head, "-m", "someone else")
        self.hub.push(work, commit, "claude/green_tao-private")
        return commit


if __name__ == "__main__":
    unittest.main()
