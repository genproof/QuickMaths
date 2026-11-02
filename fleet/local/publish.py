#!/usr/bin/env python3
"""fleet publisher (runs on the operator machine, e.g. every 10 minutes).

Takes branch packages that the feeders on every host in ``fleet.json`` built from
official_local_pass receipts (``<own root>/outbox/<problem>/<digest16>/``), groups the ready ones
by problem across hosts and publishes one canonical package per problem to the private repository
named by fleet.json ``publish.repo`` as ``claude/<problem>-private``, with the existing gh/git
transport:

* canonical package: verification on the current benchmark first, then CI fit, fewer predicted
  CI minutes, smaller archive, older; the others of the group are ``superseded`` by it;
* no branch yet: push it as a single-commit orphan branch;
* the branch already carries this artifact digest: ``already_published``;
* our branch (its head is a commit this publisher pushed, a vcur commit on top of one, or a
  commit trusted with ``--trust``): if the package is strictly better than the branch's
  VERIFICATION.json (same order, without age), fast-forward it with
  ``git commit-tree <tree> -p <remote head>`` and a plain push; otherwise ``superseded``;
* any other head (foreign or unknown history): hold with an alert, nothing pushed or marked.

Every file hash is verified against package.json before a push; the branch is read back
(head == our commit, remote tree == local tree) and ``published.json`` is recorded next to each
package. Never force-pushes, never creates ``-own-`` branches; a problem already credited to us
is skipped (fleet.json ``publish.results_account``: the account whose results file in the
intake repository lists credited problems). Submitting is a separate step outside this tool;
nothing is submitted here.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = _load("leh_fleet_for_publish", HERE / "fleet.py")  # fleet config, ssh options, remote layout
FLEET = fleet.FLEET_PATH
PUBLISH = fleet.FLEET.get("publish") or {}
REPO = PUBLISH.get("repo")  # owner/name of the private repository that receives the branches
REMOTE = f"https://github.com/{REPO}.git" if REPO else None
INTAKE = PUBLISH.get("intake") or "leanprover/lean-eval-submissions"
RESULTS_ACCOUNT = PUBLISH.get("results_account")  # results/<account>.json in the intake repository
STATE = Path(os.environ.get("LEH_STATE_DIR") or Path.home() / ".local/state/leh-fleet")
OUTBOX = f"{fleet.OWN}/outbox"  # relative to the remote home unless absolute
SSH = list(fleet.SSH)
ENV = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           GIT_TERMINAL_PROMPT="0")
# The intake's audit archiver uploads source.tar.gz (gzip of the whole branch tree) through
# GitHub's Contents API. Its stated cap is 100 MB, but the API rejected 42.2 MB (#1997) and
# 46.8 MB (#2056) with 422 "file is too large", while 29.5 MB (#1994) went through.
ARCHIVE_CAP_BYTES = 29_500_000  # the largest size seen to pass
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
SHA = re.compile(r"[0-9a-f]{40}")

LIST_PROGRAM = r'''
import json, pathlib, sys
out = []
for p in sorted(pathlib.Path(sys.argv[1]).glob("*/*/package.json")):
    if (p.parent / "published.json").exists():
        continue
    d = json.loads(p.read_text())
    d["dir"] = str(p.parent)
    try:
        d["benchmark_commit"] = json.loads((p.parent / "tree" / "VERIFICATION.json").read_text()).get("benchmark_commit")
    except (OSError, ValueError, AttributeError):
        d["benchmark_commit"] = None
    out.append(d)
print(json.dumps(out))
'''


def current_bench() -> str | None:
    """The benchmark commit submissions are verified on (vcur/stage_current.py BENCH)."""
    try:
        match = re.search(r'^BENCH = "([0-9a-f]{40})"', (HERE / "vcur" / "stage_current.py").read_text(), re.M)
    except OSError:
        return None
    return match.group(1) if match else None


def log(message: str) -> None:
    line = f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "publisher.log").open("a") as stream:
        stream.write(line + "\n")


def run(args: list[str], **kw) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, env=ENV,
                          timeout=kw.pop("timeout", 300), **kw).stdout


def hosts() -> list[str]:
    try:
        return list(json.loads(FLEET.read_text())["hosts"])
    except (OSError, ValueError, KeyError):
        return []


def remote(host: str, command: str, stdin: str | None = None) -> str:
    return subprocess.run([*SSH, host, command], check=True, capture_output=True, text=True,
                          input=stdin, env=ENV, timeout=300).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gh_json(path: str, *extra: str):
    return json.loads(run(["gh", "api", path, *extra]))


def archive_size(tree: Path) -> int:
    """Size of the intake's source.tar.gz for this tree (tarfile w:gz, as fetch_submission.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "source.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            tf.add(tree, arcname="source", filter=lambda ti: None if ti.name.startswith("source/.git") else ti)
        return out.stat().st_size


def mark(host: str, package_dir: str, record: dict) -> None:
    body = json.dumps(record, indent=2) + "\n"
    remote(host, f"cat > {package_dir}/.published.tmp && mv {package_dir}/.published.tmp "
           f"{package_dir}/published.json", stdin=body)


# ---------------------------------------------------------------------- choice (pure)

def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def created_ts(package: dict) -> float:
    try:
        return datetime.datetime.fromisoformat(str(package.get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def package_metrics(package: dict, bench: str | None) -> dict:
    gate = package.get("ci_gate") or {}
    return {"current": bool(bench) and package.get("benchmark_commit") == bench, "fit": bool(gate.get("fit")),
            "predicted": number(gate.get("predicted_ci_minutes")), "archive": number(gate.get("archive_bytes"))}


def branch_metrics(verification: Any, bench: str | None) -> dict:
    """The same measures from a branch's VERIFICATION.json (a vcur record keeps the packaging one
    under earlier_verification)."""
    v = verification if isinstance(verification, dict) else {}
    earlier = v.get("earlier_verification") if isinstance(v.get("earlier_verification"), dict) else {}
    gate = v.get("ci_fit_estimate") or earlier.get("ci_fit_estimate") or {}
    return {"current": bool(bench) and v.get("benchmark_commit") == bench and v.get("status") == "official_local_pass",
            "fit": bool(gate.get("fit")), "predicted": number(gate.get("predicted_ci_minutes")),
            "archive": number(gate.get("archive_bytes")),
            "digest": v.get("artifact_digest") or earlier.get("artifact_digest")}


def quality(m: dict) -> tuple:
    """Higher is better: current-benchmark verification, CI fit, fewer predicted CI minutes,
    smaller archive (unknown numbers rank last)."""
    inf = float("inf")
    return (m["current"], m["fit"], -(m["predicted"] if m["predicted"] is not None else inf),
            -(m["archive"] if m["archive"] is not None else inf))


def group_packages(packages: list[dict]) -> dict[str, list[dict]]:
    """Ready packages by problem; the same artifact on two hosts counts once."""
    groups: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for package in packages:
        key = (package["problem_id"], package["artifact_digest"])
        if key not in seen:
            seen.add(key)
            groups[package["problem_id"]].append(package)
    return dict(groups)


def choose(group: list[dict], bench: str | None) -> tuple[dict, list[dict]]:
    """(canonical, the rest): best quality, then the older package."""
    ordered = sorted(group, key=lambda p: (p.get("host", ""), p["artifact_digest"]))
    ordered.sort(key=lambda p: (*quality(package_metrics(p, bench)), -created_ts(p)), reverse=True)
    return ordered[0], ordered[1:]


def decide(package: dict, branch: dict | None, known: set[str], bench: str | None) -> str:
    """create / already_published / fast_forward / superseded / hold:foreign_history."""
    if branch is None:
        return "create"
    metrics = branch_metrics(branch.get("verification"), bench)
    if metrics["digest"] and metrics["digest"] == package["artifact_digest"]:
        return "already_published"
    if branch["head"] not in known:
        return "hold:foreign_history"
    if quality(package_metrics(package, bench)) > quality(metrics):
        return "fast_forward"
    return "superseded"


def known_commits(state: Path = STATE) -> set[str]:
    """Commits whose history is ours: pushed by this publisher (ledger.jsonl), vcur commits made on
    top of one of them (vcur-ledger.json, transitively), and commits trusted with --trust."""
    known: set[str] = set()
    try:
        for line in (state / "ledger.jsonl").read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("status") == "published" and SHA.fullmatch(str(entry.get("commit") or "")):
                known.add(entry["commit"])
    except OSError:
        pass
    try:
        trusted = json.loads((state / "publisher-trusted.json").read_text())
        known |= {t["commit"] for t in trusted if SHA.fullmatch(str(t.get("commit") or ""))}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        items = json.loads((state / "vcur-ledger.json").read_text()).get("items", {}).values()
    except (OSError, ValueError, AttributeError):
        items = []
    pairs = [(i.get("commit"), i.get("new_head")) for i in items
             if isinstance(i, dict) and i.get("state") in ("pushed", "done") and i.get("new_head")]
    grown = True
    while grown:
        grown = False
        for parent, child in pairs:
            if parent in known and child not in known:
                known.add(child)
                grown = True
    return known


# ---------------------------------------------------------------------- git / GitHub

class Hub:
    """The private repository: git over the existing credential helper, gh api for metadata."""

    def __init__(self, remote_url: str = REMOTE, repo: str = REPO, intake: str = INTAKE) -> None:
        self.remote, self.repo, self.intake = remote_url, repo, intake

    def ensure_private(self) -> None:
        if gh_json(f"repos/{self.repo}").get("private") is not True:
            raise RuntimeError("target repository is not private")

    def credited(self) -> set[str]:
        if not RESULTS_ACCOUNT:
            return set()
        results = json.loads(run(["gh", "api", f"repos/{self.intake}/contents/results/{RESULTS_ACCOUNT}.json",
                                  "-H", "Accept: application/vnd.github.raw+json"]))
        return {r["problem_id"] for r in results.get("results", [])}

    def head(self, branch: str) -> str | None:
        for line in run(["git", "ls-remote", self.remote, f"refs/heads/{branch}"], timeout=120).splitlines():
            commit, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}":
                return commit
        return None

    def fetch(self, repo: Path, branch: str) -> str:
        run(["git", "-C", str(repo), "fetch", "-q", "--depth=1", self.remote, f"refs/heads/{branch}"], timeout=1800)
        return run(["git", "-C", str(repo), "rev-parse", "FETCH_HEAD"]).strip()

    def push(self, repo: Path, commit: str, branch: str) -> None:
        """A plain push of an exact commit: the remote refuses anything but a fast-forward or a new
        branch. Never --force."""
        run(["git", "-C", str(repo), "push", "-q", self.remote, f"{commit}:refs/heads/{branch}"], timeout=1800)

    def remote_tree(self, commit: str) -> str:
        return gh_json(f"repos/{self.repo}/commits/{commit}")["commit"]["tree"]["sha"]


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return run(["git", "-C", str(repo), *args], input=stdin).strip()


def branch_state(hub: Hub, branch: str, work: Path, cache: dict | None = None) -> dict | None:
    """Head, tree and VERIFICATION.json of the remote branch (a depth-1 fetch), or None. `cache`
    maps a head commit to its facts (commits are immutable), so a held branch is not fetched
    again every run."""
    head = hub.head(branch)
    if not head:
        return None
    if cache is not None and isinstance(cache.get(head), dict):
        return dict(cache[head], head=head)
    repo = work / "branch"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    fetched = hub.fetch(repo, branch)
    if fetched != head:
        raise RuntimeError(f"{branch} moved while reading it ({head[:12]} -> {fetched[:12]})")
    try:
        verification = json.loads(git(repo, "show", f"{head}:VERIFICATION.json"))
    except (subprocess.CalledProcessError, ValueError):
        verification = None
    facts = {"tree": git(repo, "rev-parse", f"{head}^{{tree}}"), "verification": verification}
    shutil.rmtree(repo, ignore_errors=True)
    if cache is not None:
        cache[head] = facts
    return dict(facts, head=head)


def fetch_tree(package: dict, tree: Path) -> None:
    run(["rsync", "-a", "-e", " ".join(SSH), f"{package['host']}:{package['dir']}/tree/", f"{tree}/"], timeout=1800)


def stage(package: dict) -> tuple[Path, dict, int]:
    """The package tree on the Mac, every hash checked; (tree, hashes, archive bytes)."""
    pid, digest = package["problem_id"], package["artifact_digest"]
    work = STATE / "staging" / f"{pid}-{digest[:16]}"
    if work.exists():
        shutil.rmtree(work)
    tree = work / "tree"
    tree.mkdir(parents=True)
    fetch_tree(package, tree)
    local = {p.relative_to(tree).as_posix(): sha256(p) for p in tree.rglob("*") if p.is_file()}
    if local != package["files"]:
        raise RuntimeError("package files differ from package.json hashes")
    if package.get("status") != "ready" or not package.get("ci_gate", {}).get("fit"):
        raise RuntimeError(f"package not ready: {package.get('status')}")
    size = archive_size(tree)
    if size > ARCHIVE_CAP_BYTES:
        raise RuntimeError(f"source.tar.gz would be {size} bytes, over the {ARCHIVE_CAP_BYTES}-byte "
                           "audit-archive upload limit; shrink it with local/repack_numerals.py")
    return tree, local, size


def commit_tree(tree: Path, message: str, hub: Hub | None = None, branch: str | None = None,
                parent: str | None = None) -> tuple[str, str]:
    """(commit, tree sha) of the package tree: an orphan commit, or one whose only parent is the
    remote head `parent` (fetched first, so the push sends just the new commit)."""
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    tree_sha = git(tree, "write-tree")
    args = ["commit-tree", tree_sha]
    if parent:
        fetched = hub.fetch(tree, branch)
        if fetched != parent:
            raise RuntimeError(f"{branch} moved ({parent[:12]} -> {fetched[:12]})")
        args += ["-p", parent]
    commit = git(tree, *args, "-F", "-", stdin=message)
    return commit, tree_sha


def readback(hub: Hub, branch: str, commit: str, tree_sha: str) -> None:
    head = hub.head(branch)
    if head != commit or hub.remote_tree(commit) != tree_sha:
        raise RuntimeError("remote readback mismatch")


def update_message(package: dict, head: str) -> str:
    pid, digest = package["problem_id"], package["artifact_digest"]
    return (f"Update {pid} to the better verified package {digest[:16]}\n\n"
            f"The branch head {head[:12]} is replaced by a package that ranks higher (current-benchmark\n"
            f"verification, CI fit, predicted CI minutes, archive size). Its sources passed a fresh official\n"
            f"local verification (comparator, Lean kernel, Nanoda); see VERIFICATION.json.\n\n{TRAILER}\n")


def publish_problem(pid: str, group: list[dict], hub: Hub, dry_run: bool, dedup: bool, known: set[str],
                    bench: str | None, credited: set[str], cache: dict | None = None) -> list[tuple[dict, dict]]:
    """Outcome records for every package of one problem; a `hold` record is never marked."""
    canonical, rest = choose(group, bench)
    if dedup and pid in credited:
        record = {"status": "skipped_already_accepted_for_us"}
        return [(p, dict(record)) for p in [canonical, *rest]]
    branch = f"claude/{pid}-private"  # never an -own- branch
    work = STATE / "staging" / f"{pid}-branch"
    remote_branch = branch_state(hub, branch, work, cache) if dedup else None
    decision = decide(canonical, remote_branch, known, bench)
    by = {"by": canonical["artifact_digest"], "branch": branch}
    if decision.startswith("hold:"):
        return [(canonical, {"status": "hold", "reason": decision[5:], "branch": branch,
                             "head": remote_branch["head"]})]
    if decision == "already_published":
        return ([(canonical, {"status": "already_published", "branch": branch, "commit": remote_branch["head"],
                              "tree": remote_branch["tree"]})]
                + [(p, dict(by, status="superseded")) for p in rest])
    if decision == "superseded":
        record = {"status": "superseded", "branch": branch, "by": f"branch head {remote_branch['head']}"}
        return [(p, dict(record)) for p in [canonical, *rest]]
    tree, local, size = stage(canonical)
    hub.ensure_private()
    if decision == "fast_forward":
        parent = remote_branch["head"]
        commit, tree_sha = commit_tree(tree, update_message(canonical, parent), hub, branch, parent)
    else:
        parent = None
        commit, tree_sha = commit_tree(tree, canonical["commit_message"])
    record = {"status": "dry_run" if dry_run else "published", "mode": decision, "branch": branch,
              "commit": commit, "tree": tree_sha, "parent": parent, "files": len(local), "archive_bytes": size}
    if not dry_run:
        hub.push(tree, commit, branch)
        readback(hub, branch, commit, tree_sha)
    return [(canonical, record)] + [(p, dict(by, status="superseded")) for p in rest]


def trust(spec: str) -> dict:
    """--trust BRANCH@COMMIT: after review, treat this branch head as ours (fast-forwards allowed)."""
    branch, _, commit = spec.partition("@")
    if not branch.startswith("claude/") or not SHA.fullmatch(commit):
        raise SystemExit("--trust needs claude/<branch>@<40-hex commit>")
    path = STATE / "publisher-trusted.json"
    try:
        trusted = json.loads(path.read_text())
    except (OSError, ValueError):
        trusted = []
    entry = {"branch": branch, "commit": commit, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    trusted.append(entry)
    STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trusted, indent=1) + "\n")
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--outbox", default=OUTBOX, help="server outbox (tests use a temp copy)")
    parser.add_argument("--no-dedup", action="store_true", help="dry-run tests only")
    parser.add_argument("--trust", metavar="BRANCH@COMMIT", help="treat a reviewed branch head as ours")
    args = parser.parse_args()
    if args.no_dedup and not args.dry_run:
        parser.error("--no-dedup requires --dry-run")
    STATE.mkdir(parents=True, exist_ok=True)
    if args.trust:
        print(json.dumps(trust(args.trust)))
        return 0
    if not REPO:
        parser.error(f"no publish.repo in {FLEET}; see fleet.example.json")
    with open(STATE / "publisher.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        packages, failures, alerts = [], 0, []
        for host in hosts():
            try:
                found = json.loads(remote(host, f"python3 -c '{LIST_PROGRAM}' {args.outbox}") or "[]")
            except (subprocess.SubprocessError, ValueError) as exc:
                failures += 1
                log(f"FAILED listing {host}: {type(exc).__name__}: {exc}")
                continue
            packages += [dict(p, host=host) for p in found]
        groups = group_packages([p for p in packages if p.get("status") == "ready"])
        if not groups:
            return 1 if failures else 0
        hub, bench, known = Hub(), current_bench(), known_commits()
        try:
            credited = hub.credited() if not args.no_dedup else set()
        except (subprocess.SubprocessError, ValueError, KeyError) as exc:
            log(f"FAILED reading the intake results: {type(exc).__name__}: {exc}")
            return 1
        cache_path = STATE / "publisher-branch-cache.json"
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
        for pid, group in sorted(groups.items()):
            try:
                outcomes = publish_problem(pid, group, hub, args.dry_run, not args.no_dedup, known, bench, credited,
                                           cache)
            except Exception as exc:  # noqa: BLE001 - next run retries; never force
                failures += 1
                log(f"FAILED {pid} ({len(group)} package(s)): {type(exc).__name__}: {exc}")
                continue
            for package, record in outcomes:
                record.update(problem_id=pid, artifact_digest=package["artifact_digest"],
                              at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                log(f"{record['status']} {pid} {package['artifact_digest'][:16]} ({package['host']}) -> "
                    f"{record.get('branch')} {str(record.get('commit') or '')[:12]} "
                    f"{record.get('mode') or record.get('reason') or record.get('by') or ''}")
                if record["status"] == "hold":
                    alerts.append(f"{pid}: {record['branch']} head {record['head'][:12]} has a foreign or unknown "
                                  f"history; package {package['artifact_digest'][:16]} held. Review, then "
                                  f"publish.py --trust {record['branch']}@{record['head']}")
                    continue
                if not args.dry_run:
                    mark(package["host"], package["dir"], record)
                    with (STATE / "ledger.jsonl").open("a") as stream:
                        stream.write(json.dumps(record) + "\n")
                    if record["status"] == "published":
                        known.add(record["commit"])
        for alert in alerts:
            log(f"ALERT {alert}")
        if not args.dry_run:
            (STATE / "publisher-alerts.json").write_text(json.dumps(alerts, indent=1) + "\n")
        cache_path.write_text(json.dumps(dict(list(cache.items())[-200:]), indent=1) + "\n")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
