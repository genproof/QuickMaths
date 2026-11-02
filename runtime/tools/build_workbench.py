#!/usr/bin/env python3
"""Build the trusted workbench that leaneval.verifier checks (docs/CONFIGURATION.md, "Layout on a runner host").

Usage: python3.12 build_workbench.py [--workbench DIR] [--dry-run] [--lean-pool]

The workbench (default $LEANEVAL_WORKBENCH, else ~/projects/lean-eval-workbench) gets:

  pins.json                    a copy of leaneval/resources/verifier-pins.json
  lean-toolchain               the benchmark toolchain, so `lean --print-prefix` resolves at the root
  vendor/lean-eval             the benchmark at its pinned commit, with its Lake packages fetched and built:
                               Mathlib from its cache, TauCeti from its artifact cache, the EvalTools
                               library and the lean-eval executable, the ProofWidgets widget targets.
                               Never the root LeanEval library or lean-pool's Challenge/Solution libraries:
                               their .olean files are on every problem's search path and shadow the
                               problem's own Challenge module (lean4export then panics "Constant ... not
                               found"; found by the 2026-10-20 live run). lean-pool is not prebuilt by
                               default (the official flow builds it inside each workspace); --lean-pool
                               prebuilds its other libraries
  .tools/go                    Go as the benchmark CI uses it, checked against its SHA-256
  .tools/bin/landrun           the agent's sandbox, from `go install ...@<pin>`
  .tools/bin/bwrap             bubblewrap at the pinned version, the sandbox of the official evaluation, built
                               by build_bwrap.sh with the organizers' install script (no root needed)

The evaluation itself (lean-eval #665 on) is the toolchain's `lake comparator`: leanexport, the Lean checker
and the toolchain's nanoda_bin, so the benchmark toolchain must ship nanoda_bin; elan installs it.

Every step checks before it acts, so a second run only rebuilds what Lake or go consider stale.
It refuses (exit 2) when an existing checkout is at another commit, has another origin or has modified
tracked files, or when pins.json or lean-toolchain differ: a workbench at other pins may be in use, so
move it away yourself. It ends with leaneval.verifier.tool_identity on the result. --dry-run prints the
plan, runs only the read-only checks and changes nothing.

Prerequisites: Linux x86_64/aarch64, Python 3.12, git, bash, elan (~/.elan/bin or PATH; this installs
the toolchain), for bubblewrap a C compiler, pkg-config, curl and xz (build_bwrap.sh lists the rest),
network access to GitHub, go.dev and the Mathlib and TauCeti caches, about 25 GB of disk. The steps come from the benchmark's
README and CI at the pinned commit and from the checks in leaneval/verifier.py; docs/OPERATIONS.md says which
of them ran on a real host.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
RESOURCES = RUNTIME / "leaneval" / "resources"
# lean-pool libraries never prebuilt, and build outputs removed from the benchmark root and lean-pool: a
# problem workspace has its own Challenge/Solution modules, and these .olean files would shadow them.
SHADOWING = frozenset({"Challenge", "Solution"})
SHADOWING_OUTPUTS = frozenset({"Challenge", "Solution", "LeanEval"})
# The benchmark CI (.github/workflows/ci.yml at the pin) installs landrun with this Go release.
GO_VERSION = "go1.25.12"
GO_SHA256 = {"amd64": "234828b7a89e0e303d2556310ee549fbcf253d28de937bac3da13d6294262ac1"}
GO_METADATA = "https://go.dev/dl/?mode=json&include=all"
LANDRUN_MODULE = "github.com/zouuup/landrun/cmd/landrun"
CHECKOUTS = (("benchmark", "vendor/lean-eval"),)
PROOFWIDGETS_FILES = ("widget/package.json", "widget/package-lock.json")
# .tools/bin names that leaneval.verifier.tool_identity hashes (verifier.TOOLS)
TOOLS = ("landrun", "bwrap")
BUILD_BWRAP = Path(__file__).resolve().with_name("build_bwrap.sh")


class Refused(Exception):
    """The workbench is not at the pins and is left untouched."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def go_arch() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), platform.machine())


class Builder:
    def __init__(self, workbench: Path, resources: Path = RESOURCES, dry_run: bool = False, lean_pool: bool = False):
        self.wb, self.dry, self.lean_pool = workbench, dry_run, lean_pool
        self.pins = json.loads((resources / "verifier-pins.json").read_text())
        self.profile = tomllib.loads((resources / "profile.toml").read_text())
        self.toolchain = self.profile["benchmark"]["toolchain"]
        if self.pins["benchmark"]["rev"] != self.profile["benchmark"]["commit"]:
            raise Refused("verifier-pins.json and profile.toml name different benchmark commits")
        self.elan_home = Path(os.environ.get("ELAN_HOME") or Path.home() / ".elan")
        self.path = os.pathsep.join([str(self.elan_home / "bin"), str(self.wb / ".tools/go/bin"),
                                     os.environ.get("PATH", "/usr/bin:/bin")])

    # ------------------------------------------------------------------ helpers
    def say(self, text: str) -> None:
        print(("[dry-run] " if self.dry else "") + text, flush=True)

    def env(self, **extra: str) -> dict[str, str]:
        return dict(os.environ, PATH=self.path, **extra)

    def where(self, path: Path) -> str:
        return "." if path == self.wb else (path.relative_to(self.wb).as_posix() if self.wb in path.parents else str(path))

    def run(self, cmd: list[str], cwd: Path, **extra: str) -> None:
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items())
        self.say(f"+ (cd {self.where(cwd)} && {(prefix + ' ') if prefix else ''}{shlex.join(cmd)})")
        if not self.dry:
            subprocess.run(cmd, cwd=cwd, env=self.env(**extra), check=True)

    def output(self, cmd: list[str], cwd: Path | None = None) -> str:
        """A read-only command; runs in dry-run too."""
        return subprocess.run(cmd, cwd=cwd, env=self.env(), check=True, capture_output=True, text=True).stdout.strip()

    def git(self, folder: Path, *args: str) -> str:
        return self.output(["git", "-C", str(folder), *args])

    # ------------------------------------------------------------------ checks
    def preflight(self) -> None:
        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
            message = f"the verifier needs x86_64/aarch64 Linux with Landlock, this is {platform.system()} {platform.machine()}"
            if not self.dry:
                raise Refused(message)
            self.say("note: " + message)
        needed = ["git", "elan"]
        if not self.bwrap_ok():
            needed += ["cc", "pkg-config", "curl", "xz"]  # build_bwrap.sh
        missing = [tool for tool in needed if shutil.which(tool, path=self.path) is None]
        if missing:
            message = (f"missing on PATH: {', '.join(missing)} (elan: https://github.com/leanprover/elan, "
                       "`elan-init.sh -y --default-toolchain none`)")
            if not self.dry:
                raise Refused(message)
            self.say("note: " + message)

    def check_file(self, path: Path, content: str, compare=None) -> bool:
        """True when `path` must be written; Refused when it holds something else."""
        if not path.exists():
            return True
        same = compare(path.read_text()) if compare else path.read_text().strip() == content.strip()
        if not same:
            raise Refused(f"{path} exists with other content; move it away to build a workbench at these pins")
        return False

    def checkout_state(self, name: str, folder: Path) -> str:
        """missing / ok, or Refused: an existing checkout must be exactly at the pin, clean, same origin."""
        if not folder.exists():
            return "missing"
        if not (folder / ".git").exists():
            raise Refused(f"{folder} exists but is not a git checkout")
        url, rev = self.pins[name]["url"], self.pins[name]["rev"]
        origin = self.git(folder, "remote", "get-url", "origin")
        if origin.rstrip("/").removesuffix(".git") != url.rstrip("/").removesuffix(".git"):
            raise Refused(f"{folder}: origin is {origin}, the pin is {url}")
        head = self.git(folder, "rev-parse", "HEAD")
        if head != rev:
            raise Refused(f"{folder}: HEAD is {head}, the pin of {name} is {rev}")
        self.check_clean(name, folder)
        return "ok"

    def check_clean(self, name: str, folder: Path) -> None:
        modified = self.git(folder, "diff", "HEAD", "--name-only").splitlines()
        if modified:
            raise Refused(f"{folder}: modified tracked files {modified[:5]}; the verifier refuses them")

    # ------------------------------------------------------------------ steps
    def write_file(self, path: Path, content: str, compare=None) -> None:
        if self.check_file(path, content, compare):
            self.say(f"write {self.where(path)}")
            if not self.dry:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_text(content)
                tmp.replace(path)
        else:
            self.say(f"ok {self.where(path)}")

    def checkouts(self) -> None:
        for name, rel in CHECKOUTS:
            folder, url, rev = self.wb / rel, self.pins[name]["url"], self.pins[name]["rev"]
            if self.checkout_state(name, folder) == "ok":
                self.say(f"ok {rel} at {name} {rev}")
                continue
            self.run(["git", "clone", "--no-checkout", url, str(folder)], self.wb)
            self.run(["git", "-c", "advice.detachedHead=false", "checkout", "--detach", rev], folder)
            if not self.dry and self.git(folder, "rev-parse", "HEAD") != rev:
                raise Refused(f"{folder}: checkout did not reach {rev}")

    def toolchains(self) -> None:
        listed = self.output(["elan", "toolchain", "list"]) if shutil.which("elan", path=self.path) else ""
        installed = {line.split()[0] for line in listed.splitlines() if line.strip()}
        if self.toolchain in installed:
            self.say(f"ok toolchain {self.toolchain}")
        else:
            self.run(["elan", "toolchain", "install", self.toolchain], self.wb)
        bench_toolchain = self.wb / "vendor/lean-eval/lean-toolchain"
        if bench_toolchain.is_file() and bench_toolchain.read_text().strip() != self.toolchain:
            raise Refused(f"{bench_toolchain} names another toolchain than profile.toml")
        nanoda = self.toolchain_bin() / "nanoda_bin"
        if not self.dry and not nanoda.is_file():
            raise Refused(f"the toolchain {self.toolchain} has no bin/nanoda_bin; the official evaluation needs it")

    def toolchain_bin(self) -> Path:
        return self.elan_home / "toolchains" / self.toolchain.replace("/", "--").replace(":", "---") / "bin"

    def bwrap_ok(self) -> bool:
        target = self.wb / ".tools/bin/bwrap"
        try:
            version = subprocess.run([str(target), "--version"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return version.strip() == "bubblewrap " + self.pins["bwrap"]["version"]

    def bwrap(self) -> None:
        if self.bwrap_ok():
            self.say(f"ok .tools/bin/bwrap is bubblewrap {self.pins['bwrap']['version']}")
            return
        if (self.wb / ".tools/bin/bwrap").exists():
            raise Refused(".tools/bin/bwrap is not the pinned bubblewrap; move it away")
        self.run(["bash", str(BUILD_BWRAP), str(self.wb / ".tools/bin")], self.wb)

    def go(self) -> None:
        go = self.wb / ".tools/go/bin/go"
        if go.is_file() and self.output([str(go), "version"]).split()[2:3] == [GO_VERSION]:
            self.say(f"ok .tools/go is {GO_VERSION}")
            return
        if go.exists():
            raise Refused(f"{go} is not {GO_VERSION}; move .tools/go away")
        arch = go_arch()
        name = f"{GO_VERSION}.linux-{arch}.tar.gz"
        self.say(f"download https://go.dev/dl/{name} (SHA-256 {GO_SHA256.get(arch, 'from ' + GO_METADATA)})")
        if self.dry:
            return
        expected = GO_SHA256.get(arch) or self.go_metadata_sha(name)
        with tempfile.TemporaryDirectory(dir=self.wb / ".tools") as tmp:
            archive = Path(tmp) / name
            urllib.request.urlretrieve(f"https://go.dev/dl/{name}", archive)
            if sha256(archive) != expected:
                raise Refused(f"{name}: SHA-256 differs from {expected}")
            with tarfile.open(archive) as tar:
                tar.extractall(Path(tmp) / "x", filter="data")
            (Path(tmp) / "x/go").rename(self.wb / ".tools/go")

    @staticmethod
    def go_metadata_sha(name: str) -> str:
        with urllib.request.urlopen(GO_METADATA, timeout=60) as response:
            releases = json.load(response)
        files = [f for r in releases if r.get("version") == GO_VERSION for f in r.get("files", [])]
        match = next((f for f in files if f.get("filename") == name), None)
        if not match or not re.fullmatch(r"[0-9a-f]{64}", str(match.get("sha256"))):
            raise Refused(f"no SHA-256 for {name} in the Go release metadata")
        return match["sha256"]

    def landrun(self) -> None:
        rev, cache = self.pins["landrun"], self.wb / ".tools/cache"
        self.run(["go", "install", f"{LANDRUN_MODULE}@{rev}"], self.wb, GOBIN=str(self.wb / ".tools/bin"),
                 GOPATH=str(cache / "gopath"), GOCACHE=str(cache / "gocache"), GOFLAGS="-modcacherw")
        if not self.dry:
            info = self.output(["go", "version", "-m", str(self.wb / ".tools/bin/landrun")])
            if rev[:12] not in info:
                raise Refused(f".tools/bin/landrun was not built from landrun {rev}:\n{info}")

    def lean_pool_libs(self) -> list[str]:
        package = self.wb / "vendor/lean-eval/.lake/packages/lean-pool"
        if (package / "lakefile.toml").is_file():
            config = tomllib.loads((package / "lakefile.toml").read_text())
            return [lib["name"] for lib in config.get("lean_lib", []) if lib.get("name") not in SHADOWING]
        if (package / "lakefile.lean").is_file():
            return [n for n in re.findall(r"^\s*lean_lib\s+«?([A-Za-z_][\w.]*)»?", (package / "lakefile.lean").read_text(), re.M)
                    if n not in SHADOWING]
        return []

    def packages(self) -> None:
        bench = self.wb / "vendor/lean-eval"
        quiet = {"GIT_OPTIONAL_LOCKS": "0", "MATHLIB_NO_CACHE_ON_UPDATE": "1"}
        # benchmark README, "Install and fetch dependencies": Mathlib cache, TauCeti outputs, the library
        self.run(["lake", "exe", "cache", "get"], bench, **quiet)
        self.run(["bash", "scripts/fetch_dependency_caches.sh", "--restore", "."], bench, **quiet)
        # the targets a working production workbench has built at its root: EvalTools and lean-eval
        self.run(["lake", "build", "EvalTools", "lean-eval"], bench, **quiet)
        self.remove_shadowing(bench)
        # trusted warm-ups: worker and verifier sandboxes mount the packages read-only, and cached
        # ProofWidgets/Mathlib targets still write metadata on their first build
        self.run(["lake", "build", "Mathlib"], bench, **quiet)
        self.run(["lake", "build", "proofwidgets/widgetPackageLock", "proofwidgets/widgetJsAll"], bench, **quiet)
        if not self.dry:
            proofwidgets = bench / ".lake/packages/proofwidgets"
            changed = self.git(proofwidgets, "diff", "--name-only", "HEAD", "--", *PROOFWIDGETS_FILES)
            if changed:
                raise Refused(f"the ProofWidgets warm-up changed {changed.split()}; reset that package")
        if self.lean_pool:
            libs = self.lean_pool_libs()
            if libs:
                self.run(["lake", "build", *libs], bench, **quiet)
            elif self.dry:
                self.say("+ lake build <lean-pool's libraries, read from its lakefile after `cache get`>")
            else:
                raise Refused("no lean_lib in vendor/lean-eval/.lake/packages/lean-pool's lakefile")

    def remove_shadowing(self, bench: Path) -> None:
        """Delete build outputs that shadow a problem workspace's own modules (an earlier build of the root
        LeanEval library or of lean-pool's Challenge/Solution libraries)."""
        dirs = [bench / ".lake/build/lib/lean", bench / ".lake/packages/lean-pool/.lake/build/lib/lean",
                bench / ".lake/build/ir", bench / ".lake/packages/lean-pool/.lake/build/ir"]
        for folder in dirs:
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                stem = path.name.split(".", 1)[0]
                if stem in SHADOWING_OUTPUTS:
                    self.say(f"remove shadowing build output {path.relative_to(self.wb)}")
                    if not self.dry:
                        shutil.rmtree(path) if path.is_dir() else path.unlink()

    def check_packages(self) -> None:
        """What verifier.prepare and the worker check: every package of the benchmark manifest at its rev."""
        bench = self.wb / "vendor/lean-eval"
        manifest = json.loads(self.git(bench, "show", f"{self.pins['benchmark']['rev']}:lake-manifest.json"))
        wrong = []
        for package in manifest["packages"]:
            folder = bench / ".lake/packages" / package["name"].strip("«»")
            head = self.git(folder, "rev-parse", "HEAD") if (folder / ".git").exists() else None
            if head != package["rev"]:
                wrong.append(f"{package['name']}: {head} != {package['rev']}")
        if wrong:
            raise Refused("Lake packages differ from the benchmark manifest: " + "; ".join(wrong))
        self.say(f"ok {len(manifest['packages'])} Lake packages at the benchmark manifest revisions")

    def verify_identity(self) -> dict:
        sys.path.insert(0, str(RUNTIME))
        from leaneval.verifier import tool_identity
        return tool_identity(self.wb)

    # ------------------------------------------------------------------ all
    def build(self) -> None:
        self.say(f"workbench {self.wb}; benchmark {self.pins['benchmark']['rev']}; toolchain {self.toolchain}")
        self.preflight()
        # read-only checks first: refuse before changing anything
        self.check_file(self.wb / "pins.json", "", compare=lambda text: json.loads(text) == self.pins)
        self.check_file(self.wb / "lean-toolchain", self.toolchain)
        for name, rel in CHECKOUTS:
            self.checkout_state(name, self.wb / rel)
        if not self.dry:
            (self.wb / ".tools/bin").mkdir(parents=True, exist_ok=True)
        with self.lock():
            self.checkouts()
            self.toolchains()
            self.write_file(self.wb / "lean-toolchain", self.toolchain + "\n")
            self.go()
            self.landrun()
            self.bwrap()
            self.packages()
            if self.dry:
                self.say("then: check the Lake packages, the checkouts and leaneval.verifier.tool_identity")
                return
            self.check_packages()
            for name, rel in CHECKOUTS:
                self.check_clean(name, self.wb / rel)
            self.write_file(self.wb / "pins.json", json.dumps(self.pins, indent=2) + "\n",
                            compare=lambda text: json.loads(text) == self.pins)
            identity = self.verify_identity()
            record = {"pins": identity["pins"], "binaries": identity["binaries"],
                      "go": self.output(["go", "version"]), "lean": self.toolchain}
            (self.wb / ".tools/workbench-build.json").write_text(json.dumps(record, indent=2) + "\n")
            self.say("workbench ready; tool identity: "
                     + ", ".join(f"{k} {v[:16]}" for k, v in identity["binaries"].items()))

    @contextlib.contextmanager
    def lock(self):
        if self.dry:
            yield
            return
        with (self.wb / ".tools/build.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("another build_workbench.py runs on this workbench") from None
            yield


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], usage=__doc__.split("Usage: ")[1].split("\n")[0])
    parser.add_argument("--workbench", type=Path,
                        default=Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan and run only read-only checks")
    parser.add_argument("--lean-pool", action="store_true",
                        help="also prebuild lean-pool's libraries (never Challenge/Solution); off by default")
    parser.add_argument("--resources", type=Path, default=RESOURCES, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)
    workbench = args.workbench.expanduser().absolute()
    if not args.dry_run:
        workbench.mkdir(parents=True, exist_ok=True)
    try:
        Builder(workbench, args.resources, args.dry_run, args.lean_pool).build()
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"failed: {shlex.join(map(str, exc.cmd))} exited {exc.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
