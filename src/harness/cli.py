"""`leh`: one entry point for the harness's tools.

Each subcommand runs a component of this repository as it is documented in its own file; `leh` adds no
behaviour of its own except `doctor` (read-only host checks) and `evolve` (log features and the report).
It expects a checkout of the repository (an editable install: `pip install -e .`).

    leh doctor [--workbench DIR]           what this host still needs for rounds and official verification
    leh workbench [ARGS]                   runtime/tools/build_workbench.py: build the trusted workbench
    leh controls [CASE ...]                the five official-verification controls on the workbench
    leh verify PROBLEM SOURCE [ARGS]       verify/verify_tree.sh: official verification of one proof
    leh bank add|list|check|remove ...     runtime/tools/bank.py: the solution bank (licensed formalizations)
    leh evolve extract HOST|--local [ARGS] round features of a runner host (JSONL on stdout)
    leh evolve report FILE... [--out DIR]  the evolve report and proposals from extracted features
    leh map build TREE --problem ID        Plan-vs-Lean map of a merged proof tree (what is left, dead code,
    leh map summarize MAP.tsv --final NAME duplicates, refutations the proof relies on)
    leh fleet|coordinator|publish [ARGS]   fleet/local/{fleet,coordinator,publish}.py on the operator machine
    leh op HOST TOOL [ARGS]                fleet/operator/run.py: one operator tool on a fleet host
    leh scrub [ARGS]                       tools/scrub.py
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from harness import __version__

ROOT = Path(__file__).resolve().parents[2]
PASSTHROUGH = {
    "workbench": ["python", "runtime/tools/build_workbench.py"],
    "bank": ["python", "runtime/tools/bank.py"],
    "verify": ["bash", "verify/verify_tree.sh"],
    "fleet": ["python", "fleet/local/fleet.py"],
    "coordinator": ["python", "fleet/local/coordinator.py"],
    "publish": ["python", "fleet/local/publish.py"],
    "op": ["python", "fleet/operator/run.py"],
    "scrub": ["python", "tools/scrub.py"],
}
CONTROL_CASES = ("positive", "sorry", "axiom", "weakened", "missing")


def workbench_default() -> Path:
    return Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench")


def passthrough(name: str, args: list[str]) -> int:
    kind, script = PASSTHROUGH[name]
    path = ROOT / script
    if not path.is_file():
        print(f"leh {name}: {script} not found; leh needs a checkout of the repository", file=sys.stderr)
        return 2
    command = [sys.executable, "-I", str(path)] if kind == "python" else ["bash", str(path)]
    return subprocess.run([*command, *args]).returncode


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if ok else '--'}] {label}{(': ' + detail) if detail else ''}")
    return ok


def doctor(workbench: Path) -> int:
    """Read-only checks of what rounds and the official verification need on this host."""
    sys.path.insert(0, str(ROOT / "runtime"))
    from leaneval.planning import profile

    toolchain = profile()["benchmark"]["toolchain"]
    elan_home = Path(os.environ.get("ELAN_HOME") or Path.home() / ".elan")
    toolchain_bin = elan_home / "toolchains" / toolchain.replace("/", "--").replace(":", "---") / "bin"
    print(f"leh {__version__}; benchmark {profile()['benchmark']['commit'][:12]}; toolchain {toolchain}")
    results = []
    linux = platform.system() == "Linux" and platform.machine() in ("x86_64", "aarch64")
    results.append(check("x86_64/aarch64 Linux", linux, f"{platform.system()} {platform.machine()}"))
    if linux:
        abi = ctypes.CDLL(None, use_errno=True).syscall(444, 0, 0, 1)
        results.append(check("Landlock (landrun sandbox)", abi > 0, f"ABI {abi}"))
        userns = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        restricted = userns.is_file() and userns.read_text().strip() == "1"
        results.append(check("unprivileged user namespaces (bubblewrap)", not restricted,
                             "AppArmor restricts them: load the bwrap profile (needs root)" if restricted else ""))
        maps = Path("/proc/sys/vm/max_map_count")
        value = int(maps.read_text()) if maps.is_file() else 0
        results.append(check("vm.max_map_count >= 262144 (large proofs)", value >= 262144, str(value)))
    results.append(check("python >= 3.12", sys.version_info >= (3, 12), platform.python_version()))
    for tool in ("git", "rg"):
        results.append(check(tool, shutil.which(tool) is not None))
    results.append(check(f"toolchain {toolchain} with nanoda_bin", (toolchain_bin / "nanoda_bin").is_file(),
                         str(toolchain_bin)))
    results.append(check(f"workbench {workbench}", (workbench / "pins.json").is_file(),
                         "" if (workbench / "pins.json").is_file() else "run `leh workbench`"))
    if (workbench / "pins.json").is_file():
        from leaneval.verifier import tool_identity
        try:
            identity = tool_identity(workbench)
            results.append(check("workbench tool identity", True,
                                 ", ".join(f"{k} {v[:12]}" for k, v in identity["binaries"].items())))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            results.append(check("workbench tool identity", False, str(exc)))
        bank = workbench / "bank/SOURCES.json"
        count = len(json.loads(bank.read_text()).get("sources", [])) if bank.is_file() else 0
        check("solution bank (optional)", True, f"{count} source(s)" if count else "empty; see `leh bank`")
    disk = shutil.disk_usage(workbench if workbench.exists() else Path.home())
    results.append(check("free disk >= 30 GB", disk.free >= 30 * 1024**3, f"{disk.free / 1024**3:.0f} GB"))
    missing = results.count(False)
    print("ready" if not missing else f"{missing} check(s) failed")
    return 0 if not missing else 1


def controls(cases: list[str], workbench: Path, runs: Path, seconds: int) -> int:
    failed = []
    env = dict(os.environ, PYTHONPATH=str(ROOT / "runtime"))
    for case in cases or CONTROL_CASES:
        result = subprocess.run([sys.executable, "-m", "leaneval.controls", case, "--workbench", str(workbench),
                                 "--runs", str(runs), "--seconds", str(seconds)], env=env)
        if result.returncode:
            failed.append(case)
    print("controls passed" if not failed else f"controls failed: {', '.join(failed)}")
    return 1 if failed else 0


def ssh_command() -> list[str]:
    """ssh with the fleet tools' options: host keys checked, never interactive, LEH_SSH_CONFIG and
    LEH_SSH_KNOWN_HOSTS honoured."""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    if os.environ.get("LEH_SSH_CONFIG"):
        ssh += ["-F", os.environ["LEH_SSH_CONFIG"]]
    if os.environ.get("LEH_SSH_KNOWN_HOSTS"):
        ssh += ["-o", f"UserKnownHostsFile={os.environ['LEH_SSH_KNOWN_HOSTS']}"]
    return ssh


EVOLVE_USAGE = """usage: leh evolve extract (HOST | --local) [EXTRACT ARGS ...]
       leh evolve report FILE ... [--out DIR]

extract: round features of one runner host as JSONL on stdout; EXTRACT ARGS go to extract.py
         (--campaigns DIR, --own DIR, --feeder FILE, --host NAME; on a remote host, relative to its home)
report:  report.md and summary.json from extracted files (printed without --out)"""


def evolve(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help") or args[0] not in ("extract", "report"):
        print(EVOLVE_USAGE)
        return 0 if args and args[0] in ("-h", "--help") else 2
    action, rest = args[0], args[1:]
    if action == "extract":
        if not rest or rest[0] in ("-h", "--help"):
            print(EVOLVE_USAGE)
            return 0 if rest else 2
        target, extra = rest[0], [a for a in rest[1:] if a != "--"]
        script = Path(__file__).with_name("evolve") / "extract.py"
        if target == "--local":
            return subprocess.run([sys.executable, "-I", str(script), *extra]).returncode
        with script.open("rb") as stream:  # the script goes over stdin; no file is left on the host
            return subprocess.run([*ssh_command(), target, shlex.join(["python3", "-I", "-", *extra])],
                                  stdin=stream).returncode
    parser = argparse.ArgumentParser(prog="leh evolve report")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, help="directory for report.md and summary.json (default: print)")
    parsed = parser.parse_args(rest)
    from harness.evolve.report import load, markdown, summarize

    summary = summarize(load(parsed.files))
    text = markdown(summary)
    if parsed.out:
        parsed.out.mkdir(parents=True, exist_ok=True)
        (parsed.out / "report.md").write_text(text)
        (parsed.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"wrote {parsed.out / 'report.md'} and summary.json")
    else:
        print(text, end="")
    return 0


MAP_USAGE = """usage: leh map build TREE --problem ID [--final NAME] [--workbench DIR] [--threads N]
       leh map summarize MAP_TSV --final NAME [--lanes LANES_JSON] [--json]

build:     TREE holds Submission.lean and Submission/ (e.g. a program's base plus every lane's latest files);
           it is made a Lake workspace of problem ID against the workbench's built packages, `Submission` is
           built, and TREE/map.tsv lists every Submission declaration: own sorry, sorry-dependent, reached by
           the final theorem (default: Submission.<first hole>).
summarize: per lane what is left, dead code, names defined in several lanes and refutations (`not_*`)
           the final proof relies on; LANES_JSON ({"lanes": {lane: {"done": ..., "status": ...}}}) adds status."""


def plan_map(args: list[str]) -> int:
    if not args or args[0] not in ("build", "summarize"):
        print(MAP_USAGE)
        return 0 if args and args[0] in ("-h", "--help") else 2
    from harness.map import build, summary

    parser = argparse.ArgumentParser(prog=f"leh map {args[0]}")
    if args[0] == "build":
        parser.add_argument("tree", type=Path)
        parser.add_argument("--problem", required=True)
        parser.add_argument("--final")
        parser.add_argument("--workbench", type=Path, default=workbench_default())
        parser.add_argument("--threads", type=int, default=16)
        parsed = parser.parse_args(args[1:])
        tree = parsed.tree.expanduser().resolve()
        template = build.prepare(tree, parsed.problem, parsed.workbench.expanduser())
        final = parsed.final or build.default_final(template, parsed.problem)
        out = build.build(tree, parsed.problem, parsed.workbench.expanduser(), final, parsed.threads)
        print(summary.text(summary.from_files(out, final, tree / "lanes.json")), end="")
        return 0
    parser.add_argument("map_tsv", type=Path)
    parser.add_argument("--final", required=True)
    parser.add_argument("--lanes", type=Path)
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args[1:])
    result = summary.from_files(parsed.map_tsv, parsed.final, parsed.lanes)
    print(json.dumps(result, indent=1) if parsed.json else summary.text(result), end="" if not parsed.json else "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in PASSTHROUGH:
        return passthrough(argv[0], argv[1:])
    if argv and argv[0] == "evolve":
        return evolve(argv[1:])
    if argv and argv[0] == "map":
        return plan_map(argv[1:])
    parser = argparse.ArgumentParser(prog="leh", description=__doc__.split("\n\n")[0],
                                     epilog=__doc__.split("\n\n", 2)[2], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"leh {__version__}")
    sub = parser.add_subparsers(dest="command")
    doc = sub.add_parser("doctor", help="read-only checks of this host")
    doc.add_argument("--workbench", type=Path, default=workbench_default())
    ctl = sub.add_parser("controls", help="the official-verification controls")
    ctl.add_argument("cases", nargs="*", choices=CONTROL_CASES, metavar="CASE")
    ctl.add_argument("--workbench", type=Path, default=workbench_default())
    ctl.add_argument("--runs", type=Path, default=Path.home() / "projects/lean-eval-runs")
    ctl.add_argument("--seconds", type=int, default=1800)
    for name in (*PASSTHROUGH, "evolve", "map"):
        sub.add_parser(name, help=f"see `leh {name} --help`")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor(args.workbench.expanduser())
    if args.command == "controls":
        return controls(args.cases, args.workbench.expanduser().resolve(), args.runs.expanduser().resolve(), args.seconds)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
