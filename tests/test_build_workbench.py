"""Offline controls for runtime/tools/build_workbench.py.

The checkout runs for real against a local git repository pinned in a test resources directory. Lake, elan,
go, the bubblewrap build and the downloads are stubbed: these tests cover the plan, the refusals and the
layout, not the builds themselves.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime/tools/build_workbench.py"
spec = importlib.util.spec_from_file_location("build_workbench", SCRIPT)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

TOOLCHAIN = "leanprover/lean4:v4.35.0-rc3"
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main"]


def repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    subprocess.run([*GIT, "init", "-q", str(path)], check=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    subprocess.run([*GIT, "-C", str(path), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(path), "commit", "-q", "-m", "pin"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A local benchmark repository and a resources directory that pins it."""
    src = tmp_path / "upstream"
    rev = repo(src / "lean-eval", {"lean-toolchain": TOOLCHAIN + "\n", "Main.lean": "",
                                   "lake-manifest.json": json.dumps({"packages": []})})
    pins = json.loads((ROOT / "runtime/leaneval/resources/verifier-pins.json").read_text())
    pins["benchmark"] = {"url": (src / "lean-eval").as_uri(), "rev": rev}
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "verifier-pins.json").write_text(json.dumps(pins))
    (resources / "profile.toml").write_text(f'[benchmark]\ncommit = "{rev}"\ntoolchain = "{TOOLCHAIN}"\n')
    return resources, pins


class Fake(bw.Builder):
    """The real checkout, files and refusals; recorded instead of run: lake, go, elan, bubblewrap, downloads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands: list[tuple[str, ...]] = []
        self.elan_home = self.wb.parent / "elan"  # a toolchain that ships nanoda_bin, as the real one does
        self.toolchain_bin().mkdir(parents=True, exist_ok=True)
        (self.toolchain_bin() / "nanoda_bin").write_bytes(b"nanoda")

    def preflight(self):
        pass

    def run(self, cmd, cwd, **extra):
        if cmd[0] == "git":
            return super().run(cmd, cwd, **extra)
        self.commands.append((self.where(cwd), *[c.replace(str(self.wb), "<wb>") for c in cmd]))
        if cmd[0] == "bash" and cmd[1].endswith("build_bwrap.sh"):
            (self.wb / ".tools/bin/bwrap").write_bytes(b"bwrap")
        if cmd[:2] == ["go", "install"]:
            (self.wb / ".tools/bin/landrun").write_bytes(b"landrun")

    def output(self, cmd, cwd=None):
        if cmd[:3] == ["go", "version", "-m"]:
            return f"mod\t{bw.LANDRUN_MODULE}\tv0.0.0-20260504000000-{self.pins['landrun'][:12]}"
        if cmd[0] in ("elan", "go"):
            return ""
        return super().output(cmd, cwd)

    def bwrap_ok(self):
        return (self.wb / ".tools/bin/bwrap").is_file()

    def go(self):
        self.commands.append(("go-download",))

    def packages(self):
        self.commands.append(("packages",))

    def verify_identity(self):
        return {"pins": self.pins, "binaries": {n: bw.sha256(self.wb / ".tools/bin" / n) for n in bw.TOOLS}}


def test_dry_run_prints_the_pinned_plan_and_creates_nothing(tmp_path, capsys):
    wb = tmp_path / "wb"
    assert bw.main(["--dry-run", "--workbench", str(wb)]) == 0
    out = capsys.readouterr().out
    pins = json.loads((ROOT / "runtime/leaneval/resources/verifier-pins.json").read_text())
    assert f"checkout --detach {pins['benchmark']['rev']}" in out
    assert f"landrun@{pins['landrun']}" in out
    for step in ("build_bwrap.sh", "lake exe cache get", "fetch_dependency_caches.sh --restore .", "lake build Mathlib",
                 "proofwidgets/widgetJsAll", "tool_identity"):
        assert step in out, step
    assert not wb.exists()


def test_build_checks_out_the_pins_installs_binaries_and_is_idempotent(tmp_path, upstream):
    resources, pins = upstream
    wb = tmp_path / "wb"
    wb.mkdir()
    first = Fake(wb, resources)
    first.build()
    for name, rel in bw.CHECKOUTS:
        head = subprocess.run(["git", "-C", str(wb / rel), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        assert head.strip() == pins[name]["rev"]
    assert (wb / "lean-toolchain").read_text().strip() == TOOLCHAIN
    assert json.loads((wb / "pins.json").read_text()) == pins
    for name in bw.TOOLS:
        assert (wb / ".tools/bin" / name).is_file() and not (wb / ".tools/bin" / name).is_symlink()
    record = json.loads((wb / ".tools/workbench-build.json").read_text())
    assert set(record["binaries"]) == set(bw.TOOLS)
    assert ("packages",) in first.commands
    assert (".", "bash", str(bw.BUILD_BWRAP), "<wb>/.tools/bin") in first.commands
    second = Fake(wb, resources)
    second.build()  # nothing to clone, bubblewrap already built
    assert second.commands == [c for c in first.commands if c[1:2] != ("bash",)]


@pytest.mark.parametrize("damage, message", [
    (lambda wb: (wb / "vendor/lean-eval/Main.lean").write_text("changed"), "lean-eval: modified tracked files"),
    (lambda wb: (wb / "pins.json").write_text('{"other": 1}'), "pins.json exists with other content"),
    (lambda wb: (wb / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n"), "lean-toolchain exists"),
    (lambda wb: subprocess.run([*GIT, "-C", str(wb / "vendor/lean-eval"), "commit", "-q", "--allow-empty", "-m", "x"],
                               check=True), "the pin of benchmark is"),
    (lambda wb: subprocess.run(["git", "-C", str(wb / "vendor/lean-eval"), "remote", "set-url", "origin",
                                "https://example.com/other.git"], check=True), "origin is https://example.com/other.git"),
])
def test_refuses_a_workbench_that_is_not_at_the_pins(tmp_path, upstream, damage, message):
    resources, _ = upstream
    wb = tmp_path / "wb"
    wb.mkdir()
    Fake(wb, resources).build()
    damage(wb)
    again = Fake(wb, resources)
    with pytest.raises(bw.Refused, match=message):
        again.build()
    assert again.commands == []  # refused before any build step


def test_main_reports_a_refusal_with_exit_2(tmp_path, upstream, capsys):
    resources, _ = upstream
    wb = tmp_path / "wb"
    (wb / "vendor/lean-eval").mkdir(parents=True)
    assert bw.main(["--dry-run", "--workbench", str(wb), "--resources", str(resources)]) == 2
    assert "is not a git checkout" in capsys.readouterr().err


def test_lean_pool_libraries_come_from_its_lakefile(tmp_path, upstream):
    resources, _ = upstream
    package = tmp_path / "wb/vendor/lean-eval/.lake/packages/lean-pool"
    package.mkdir(parents=True)
    builder = bw.Builder(tmp_path / "wb", resources)
    assert builder.lean_pool_libs() == []
    (package / "lakefile.lean").write_text('lean_lib LeanPool where\n  roots := #[`LeanPool]\nlean_lib «Extra»\n')
    assert builder.lean_pool_libs() == ["LeanPool", "Extra"]
    (package / "lakefile.toml").write_text('[[lean_lib]]\nname = "LeanPool"\n')
    assert builder.lean_pool_libs() == ["LeanPool"]
    (package / "lakefile.toml").write_text('[[lean_lib]]\nname = "LeanPool"\n[[lean_lib]]\nname = "Challenge"\n'
                                           '[[lean_lib]]\nname = "Solution"\n')
    assert builder.lean_pool_libs() == ["LeanPool"]  # never the shadowing Challenge/Solution libraries


def test_packages_build_only_the_benchmark_root_targets(tmp_path, upstream, capsys):
    # 2026-10-20 live run: `lake build` of the root and of lean-pool's Challenge/Solution libraries put
    # Challenge.olean on every problem's search path; lean4export then panicked "Constant ... not found".
    resources, _ = upstream
    bw.Builder(tmp_path / "wb", resources, True).packages()
    plan = capsys.readouterr().out
    assert "lake build EvalTools lean-eval" in plan
    assert "lake build)" not in plan and "LeanPool" not in plan and "Challenge" not in plan


def test_shadowing_build_outputs_are_removed(tmp_path, upstream):
    resources, _ = upstream
    bench = tmp_path / "wb/vendor/lean-eval"
    root, pool = bench / ".lake/build/lib/lean", bench / ".lake/packages/lean-pool/.lake/build/lib/lean"
    for folder, names in ((root, ["EvalTools.olean", "LeanEval.olean", "LeanEval"]),
                          (pool, ["Challenge.olean", "Challenge.ilean", "Solution", "LeanPool.olean"])):
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).mkdir() if "." not in name else (folder / name).write_text("x")
    bw.Builder(tmp_path / "wb", resources).remove_shadowing(bench)
    assert sorted(p.name for p in root.iterdir()) == ["EvalTools.olean"]
    assert sorted(p.name for p in pool.iterdir()) == ["LeanPool.olean"]


def test_script_runs_standalone():
    out = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--dry-run" in out.stdout


def test_bubblewrap_pin_matches_build_bwrap_sh():
    pins = json.loads((ROOT / "runtime/leaneval/resources/verifier-pins.json").read_text())
    script = (ROOT / "runtime/tools/build_bwrap.sh").read_text()
    installer = pins["bwrap"]["installer"]
    assert f"INSTALLER_COMMIT={installer['commit']}" in script
    assert f"INSTALLER_SHA256={installer['sha256']}" in script
    assert f'"bubblewrap {pins["bwrap"]["version"]}"' in script
