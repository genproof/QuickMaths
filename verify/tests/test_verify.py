"""Offline controls for verify (no network, no Lean, no comparator).

A fake workbench holds a benchmark repository with one generated problem, a Lean Pool package checkout and
placeholder binaries; a fake toolchain's `lake test` prints what comparator prints on acceptance (or a
rejection, by a mode file next to it). The organizers' evaluator is fetched at run time and never vendored,
so a stand-in with the two functions verify_candidate.py calls replaces it here. Everything else is the
real chain: verify_run.py prepare -> run_verify.sh -> verify_candidate.py -> workspace_manager.py -> receipt.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE.parent
spec = importlib.util.spec_from_file_location("verify_run", VERIFY / "verify_run.py")
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)

TOOLCHAIN = "leanprover/lean4:v4.35.0-rc3"
GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
EVALUATOR = '''import os, pathlib
def _check_requires_match_manifest(lakefile, manifest):
    assert pathlib.Path(lakefile).is_file() and pathlib.Path(manifest).is_file()
def _share_packages(target, packages):
    link = pathlib.Path(target) / ".lake/packages"
    link.unlink()
    link.symlink_to(pathlib.Path(packages).resolve(), target_is_directory=True)
    return None
'''
LAKE = '''#!/bin/bash
# fake `lake test`: the acceptance lines of comparator, or a rejection (mode file next to this script)
here=$(dirname "$0")
[ "$1" = test ] || exit 3
[ -f Submission.lean ] || exit 4
echo "${LEAN_NUM_THREADS:-unset}" > "$here/seen-threads"
command -v bwrap > "$here/seen-bwrap" || echo none > "$here/seen-bwrap"
echo "${COMPARATOR_BIN:-unset}" > "$here/seen-comparator"
case $(cat "$here/mode" 2>/dev/null) in
  reject) echo "Illegal axiom detected: 'sorryAx'"; exit 1 ;;
  tamper) echo "-- changed" >> Submission.lean ;;
esac
echo "nanoda kernel accepts the solution"
echo "Lean default kernel accepts the solution"
echo "Your solution is okay!"
'''

LAKE_COMPARATOR_DRIVER = """def main : IO UInt32 := do
  let child <- IO.Process.spawn { cmd := lake, args := #["comparator", "--config", enforcedPath.toString], env }
"""
BWRAP = '#!/bin/bash\n[ "$1" = --version ] && echo "bubblewrap {version}"\nexit 0\n'
NO_HOST_PROBLEMS = {"problems": []}


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", message)
    return git("-C", str(repo), "rev-parse", "HEAD")


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


class Fixture:
    def __init__(self, tmp: Path):
        self.tmp, self.wb = tmp, tmp / "workbench"
        vendor = self.wb / "vendor/lean-eval"
        pool = vendor / ".lake/packages/lean-pool"
        pool.mkdir(parents=True)
        git("init", "-q", str(pool))
        write(pool, {"LeanPool.lean": "-- pool\n", "lakefile.toml": 'name = "lean-pool"\n'})
        self.pool_rev = commit_all(pool, "pool")
        (vendor / ".lake/packages/mathlib").mkdir()
        manifest = json.dumps({"version": "1.1.0", "packages": [
            {"name": "lean-pool", "type": "git", "url": "https://example.com/lean-pool.git", "rev": self.pool_rev},
            {"name": "mathlib", "type": "git", "url": "https://example.com/mathlib4.git", "rev": "5e" * 20}]})
        git("init", "-q", str(vendor))
        write(vendor, {".gitignore": ".lake/\n", "lean-toolchain": TOOLCHAIN + "\n", "lake-manifest.json": manifest,
                       "generated/two_plus_two/lakefile.toml": 'name = "two_plus_two"\n',
                       "generated/two_plus_two/lean-toolchain": TOOLCHAIN + "\n",
                       "generated/two_plus_two/Challenge.lean": "theorem two : 2 + 2 = 4 := sorry\n",
                       "generated/two_plus_two/Submission.lean": "-- stub\n",
                       "generated/two_plus_two/WorkspaceTest.lean": "-- test driver\n"})
        self.pinned = commit_all(vendor, "pinned benchmark (comparator flow)")
        write(vendor, {"README.md": "current\n", "generated/two_plus_two/WorkspaceTest.lean": LAKE_COMPARATOR_DRIVER})
        self.current = commit_all(vendor, "current benchmark (lake-comparator flow), same manifest")
        write(vendor, {"lake-manifest.json": manifest.replace("5e" * 20, "6f" * 20)})
        self.other = commit_all(vendor, "a benchmark with other packages")
        git("-C", str(vendor), "checkout", "-q", "--detach", self.pinned)  # the workbench stays at its pin
        tools = self.wb / ".tools/bin"
        tools.mkdir(parents=True)
        for name in vr.BINARIES:
            (tools / name).write_text(f"binary {name}\n")
        (tools / "bwrap").write_text(BWRAP.format(version=vr.BWRAP_VERSION))
        (tools / "bwrap").chmod(0o755)
        self.elan = tmp / "elan"
        self.bin = self.elan / "toolchains/leanprover--lean4---v4.35.0-rc3/bin"
        self.bin.mkdir(parents=True)
        (self.bin / "lake").write_text(LAKE)
        (self.bin / "lean").write_text("#!/bin/bash\nexit 0\n")
        (self.bin / "nanoda_bin").write_text("#!/bin/bash\nexit 0\n")
        for name in ("lake", "lean", "nanoda_bin"):
            (self.bin / name).chmod(0o755)
        self.evaluator = tmp / "evaluate_submission.py"
        self.evaluator.write_text(EVALUATOR)
        self.source = tmp / "proof"
        write(self.source, {"Submission.lean": "import Submission.Helpers\ntheorem two : 2 + 2 = 4 := by norm_num\n",
                            "Submission/Helpers.lean": "-- helper\n", "Submission/notes.json": "{}\n",
                            "README.md": "not a proof file\n"})

    def prepare(self, name="run", **kwargs) -> Path:
        run = self.tmp / name
        vr.prepare(run, self.source, "c0ffee" * 6 + "c0ff", "two_plus_two", 600,
                   **dict(dict(benchmark=self.current, workbench=self.wb, evaluator=self.evaluator,
                               host_report=NO_HOST_PROBLEMS), **kwargs))
        return run

    def run_verify(self, run: Path, mode: str = "") -> subprocess.CompletedProcess:
        (self.bin / "mode").write_text(mode)
        env = {"HOME": str(self.tmp), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ELAN_HOME": str(self.elan),
               "VCUR_PY": sys.executable, "LEH_VERIFY_SCOPE": "none", "LEAN_NUM_THREADS": "99", **GIT_ENV}
        return subprocess.run(["bash", str(VERIFY / "run_verify.sh"), str(run), "2G", "0", "leh-test"], env=env,
                              capture_output=True, text=True, timeout=120)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.f = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_prepare_makes_a_run_directory_from_the_workbench(self):
        run = self.f.prepare()
        config = json.loads((run / "config.json").read_text())
        self.assertEqual(config["benchmark_commit"], self.f.current)
        self.assertEqual(git("-C", config["benchmark"], "rev-parse", "HEAD"), self.f.current)
        self.assertEqual((run / "benchmark-clean/.lake/packages").resolve(),
                         (self.f.wb / "vendor/lean-eval/.lake/packages").resolve())
        self.assertEqual((config["lean_pool_commit"], config["official_private_lean_pool"]), (self.f.pool_rev, True))
        self.assertEqual((config["evaluation_flow"], set(config["binary_sha256"])), ("lake-comparator", {"bwrap"}))
        self.assertEqual(config["bwrap_version"], vr.BWRAP_VERSION)
        wrapper = (run / "bwrap-bin/bwrap").read_text()
        cache = str((self.f.wb / "vendor/lean-eval/.lake/packages").resolve())
        self.assertIn(f"--ro-bind {cache} {cache}", wrapper)  # as EvalTools/RunEval.lean withCacheSandbox
        self.assertIn(f"exec {(self.f.wb / '.tools/bin/bwrap').resolve()}", wrapper)
        self.assertEqual(config["manager_sha256"], vr.sha(VERIFY / "workspace_manager.py"))
        self.assertEqual(config["evaluator_commit"], vr.EVALUATOR["commit"])
        self.assertNotIn("lean_num_threads", config)
        hashes = json.loads((run / "source-sha256.json").read_text())
        self.assertEqual(sorted(hashes), ["Submission.lean", "Submission/Helpers.lean"])  # trimmed

    def test_prepare_refusals(self):
        self.f.prepare()
        with self.assertRaisesRegex(vr.PrepareError, "exists"):
            self.f.prepare()
        with self.assertRaisesRegex(vr.PrepareError, "other Lake packages"):
            self.f.prepare("other", benchmark=self.f.other)
        (self.f.source / "Submission/Link.lean").symlink_to(self.f.source / "Submission/Helpers.lean")
        with self.assertRaisesRegex(vr.PrepareError, "symlink"):
            self.f.prepare("linked")
        (self.f.source / "Submission/Link.lean").unlink()
        (self.f.wb / ".tools/bin/nanoda_bin").unlink()
        self.f.prepare("current-needs-no-comparator-binaries")
        with self.assertRaisesRegex(vr.PrepareError, "nanoda_bin"):
            self.f.prepare("no-nanoda", benchmark=self.f.pinned)
        (self.f.wb / ".tools/bin/bwrap").write_text(BWRAP.format(version="0.6.1"))
        with self.assertRaisesRegex(vr.PrepareError, "bubblewrap 0.6.1"):
            self.f.prepare("old-bwrap")
        (self.f.wb / ".tools/bin/bwrap").unlink()
        with self.assertRaisesRegex(vr.PrepareError, "build_bwrap.sh"):
            self.f.prepare("no-bwrap")
        self.assertFalse((self.f.tmp / "no-bwrap").exists())
        with self.assertRaisesRegex(vr.PrepareError, "preflight: TMPDIR"):
            self.f.prepare("full-disk", host_report={"problems": ["TMPDIR /tmp: 1.0 GB free"]})

    def test_the_evaluator_must_match_its_pin(self):
        cache = self.f.tmp / "cache"
        path = cache / f"evaluator-{vr.EVALUATOR['commit'][:12]}" / "evaluate_submission.py"
        path.parent.mkdir(parents=True)
        path.write_text(EVALUATOR)  # not the organizers' file
        with self.assertRaisesRegex(vr.PrepareError, "differs from the pinned evaluator"):
            vr.evaluator_file(cache)

    def test_an_accepted_run_and_its_receipt(self):
        run = self.f.prepare()
        out = self.f.run_verify(run)
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])
        done = vr.run_done_fields((run / "RUN-DONE").read_text())
        self.assertEqual((done["rc"], done["scope"], done["mem"], done["cpus"]), ("0", "none", "2G", "0"))
        status = json.loads((run / "status.json").read_text())
        self.assertTrue(status["accepted"] and status["sources_unchanged"] and status["dependency_sources_unchanged"])
        self.assertEqual((self.f.bin / "seen-threads").read_text().strip(), "unset")  # the official run strips it
        self.assertEqual((self.f.bin / "seen-bwrap").read_text().strip(), str(run / "bwrap-bin/bwrap"))
        self.assertEqual((self.f.bin / "seen-comparator").read_text().strip(), "unset")
        record = vr.receipt(run)
        self.assertTrue(record["accepted"])
        self.assertEqual((record["envelope"], record["source_files"], record["benchmark_commit"]),
                         ("official", 2, self.f.current))
        self.assertEqual((record["evaluation_flow"], record["bwrap_version"]), ("lake-comparator", vr.BWRAP_VERSION))
        hashes = json.loads((run / "source-sha256.json").read_text())
        self.assertEqual(record["source_digest"], hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())
        self.assertTrue(record["nanoda_accepted"] and record["lean_kernel_accepted"])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["official_receipt_sha256"],
                         status["receipt_sha256"])

    def test_rejected_and_tampered_runs_are_not_accepted(self):
        run = self.f.prepare("rejected")
        self.assertEqual(self.f.run_verify(run, "reject").returncode, 1)
        record = vr.receipt(run)
        self.assertFalse(record["accepted"])
        self.assertTrue(record["illegal_axiom"])
        run = self.f.prepare("tampered")
        self.assertNotEqual(self.f.run_verify(run, "tamper").returncode, 0)
        status = json.loads((run / "status.json").read_text())
        self.assertEqual((status["phase"], status["accepted"]), ("error", False))  # sources changed under it
        self.assertFalse(vr.receipt(run)["accepted"])

    def test_comparator_flow_of_earlier_benchmarks(self):
        run = self.f.prepare("legacy", benchmark=self.f.pinned)
        config = json.loads((run / "config.json").read_text())
        self.assertEqual((config["evaluation_flow"], set(config["binary_sha256"])), ("comparator", set(vr.BINARIES)))
        out = self.f.run_verify(run)
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])
        self.assertEqual((self.f.bin / "seen-comparator").read_text().strip(), str(self.f.wb.resolve() / ".tools/bin/comparator"))
        self.assertTrue(vr.receipt(run)["accepted"])

    def test_ci_envelope_and_lean_threads(self):
        run = self.f.prepare("ci", envelope="ci")
        self.assertEqual(self.f.run_verify(run).returncode, 0)
        self.assertEqual((self.f.bin / "seen-threads").read_text().strip(), "unset")
        self.assertEqual(vr.receipt(run)["envelope"], "ci")
        run = self.f.prepare("threads", lean_num_threads=4)
        self.assertEqual(self.f.run_verify(run).returncode, 0)
        self.assertEqual((self.f.bin / "seen-threads").read_text().strip(), "4")

    def test_preflight_disk_and_map_limit(self):
        free = lambda gb: (lambda path: type("U", (), {"free": int(gb * 1e9)})())  # noqa: E731
        report = vr.preflight(self.f.tmp, self.f.source, tmpdir=self.f.tmp, map_limit=1048576, free=free(500))
        self.assertEqual((report["proof_files"], report["problems"]), (2, []))
        report = vr.preflight(self.f.tmp / "not/yet", self.f.source, tmpdir=self.f.tmp, map_limit=1048576,
                              free=free(1))
        self.assertEqual(len(report["problems"]), 2)  # run directory and TMPDIR, each below the 5 GB reserve
        big = self.f.tmp / "big"
        write(big, {"Submission.lean": "-- main\n", **{f"Submission/M{i}.lean": "x\n" * 1000 for i in range(2900)}})
        report = vr.preflight(self.f.tmp, big, tmpdir=self.f.tmp, map_limit=65530, free=free(500))
        self.assertEqual(report["proof_lines"], 2_900_001)
        self.assertTrue(any("vm.max_map_count 65530" in p for p in report["problems"]))  # the Poincaré failure
        report = vr.preflight(self.f.tmp, big, tmpdir=self.f.tmp, map_limit=65530, free=free(10))
        self.assertTrue(any("12.6 GB" in p for p in report["problems"]))  # 5 GiB + 2.5 KB per line

    def test_export_takes_only_proof_files_and_refuses_symlinks(self):
        repo = self.f.tmp / "repo"
        repo.mkdir()
        git("init", "-q", str(repo))
        write(repo, {"Submission.lean": "-- main\n", "Submission/A.lean": "-- a\n", "Submission/data.json": "{}\n",
                     "README.md": "readme\n", "tools/gen.py": "print()\n"})
        good = commit_all(repo, "proof")
        (repo / "Submission/B.lean").symlink_to("A.lean")
        linked = commit_all(repo, "symlink")
        commit = vr.export_ref(str(repo), good, self.f.tmp / "out")
        self.assertEqual(commit, good)
        self.assertEqual(sorted(p.relative_to(self.f.tmp / "out").as_posix()
                                for p in (self.f.tmp / "out").rglob("*") if p.is_file()),
                         ["Submission.lean", "Submission/A.lean"])
        with self.assertRaisesRegex(vr.PrepareError, "symlink"):
            vr.export_ref(str(repo), linked, self.f.tmp / "out2")

    def test_verify_tree_plan_and_argument_checks(self):
        env = {"HOME": str(self.f.tmp), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "VCUR_PY": sys.executable}
        run = lambda *args: subprocess.run(["bash", str(VERIFY / "verify_tree.sh"), *args], env=env,  # noqa: E731
                                           capture_output=True, text=True, timeout=60)
        out = run("two_plus_two", str(self.f.source), "--ci-envelope", "--dry-run", "--workbench", str(self.f.wb))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"on benchmark {vr.CURRENT_BENCHMARK}", out.stdout)
        self.assertIn("cpus 0-15 mem 31G timeout 21600s", out.stdout)
        self.assertIn('preflight   "proof_files": 2', out.stdout)
        self.assertFalse((self.f.tmp / "projects").exists())  # a dry run creates nothing
        self.assertEqual(run("bad/id", str(self.f.source)).returncode, 2)
        self.assertEqual(run("two_plus_two", str(self.f.source), "--timeout", "soon").returncode, 2)
        self.assertEqual(run("two_plus_two").returncode, 2)


if __name__ == "__main__":
    unittest.main()
