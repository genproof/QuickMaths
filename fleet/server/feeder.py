#!/usr/bin/env python3
"""fleet feeder: keeps the own runner queues busy, packages official passes.

One tick (systemd timer, every few minutes; safe to run by hand, serialized by a lock):

* claims: a runner's current target finishes when its lineage (attempt + auto_continue
  rounds) has no pending round left. Outcome: ``passed`` (an official_local_pass receipt),
  ``exhausted`` (rounds used up without a pass) or ``failed`` (last round failed);
* progress gate: rounds 1-2 of a lineage always run; from round 2 on, a round is the last one
  when it made no progress (no ``sorry`` closed, no RESIDUALS row proved and no drop of the
  remaining-work estimate, no decomposition, <300 new code lines with a passing build), or when
  it is the second round in a row that only added lines (the already started next round gets
  ``<job>/STOP``, a controlled stop with a snapshot). Stall rules (policy 2026-10-14): the
  estimate (sum of ``est. lines`` over open RESIDUALS rows) fell by less than 25% in two rounds
  without a closed row, or, without estimates, ``churn_rounds`` rounds without a new sorry
  minimum. A stopped lineage gets one ROUTE_REVIEW lineage, seeded with the best finished round
  of the problem on this host, before it can end;
* verification phase: a round that is ``complete`` (no sorry, ``build Submission`` succeeded on
  exactly its files) but unverified is never a stall: its continuation stops and a 1-round
  VERIFY lineage (verify_policy always) runs the official verifier; a timeout leads to a CI_FIT
  round with the measured module times, a rejection to a REPAIR round with the official log;
* quota: an account whose runner waits in a usage-limit pause gets no new work; a lineage in a
  seven_day pause (or a long five_hour pause when a runner with quota is free) is handed off to
  an idle runner with quota on this host as a RESUME lineage from its last finished round;
* tactics: every route review (after a stall, on a retry, on a seeded transfer) gets the
  measured history of the problem's rounds (progress kind, sorry count, estimate, the TACTICS
  row marked trying in NOTES.md) and the tactic library briefs/TACTICS.md, and must switch tactic;
* assignment: an idle runner (no pending assignment, no running claim, account not quota-blocked,
  host admission ok) gets, in order: a handed-off lineage (closest to done first), a VERIFY
  backfill, a program role, a retry seeded with the best files of an exhausted target (smallest
  size hint ``ref_min_modules`` first, at most ``max_retries``, always as a route review), the
  next target from ``targets.json``. Targets already credited on the official leaderboard to the
  model named by ``leaderboard_model`` in config.json, or claimed/finished, are skipped. The brief
  is copied into the queue root (``brief_file`` must be root-relative) and refreshed for later
  rounds;
* runtime pin: ``runtimes`` in config.json points a runner's controller at a runtime dir; a
  change is applied only when the runner is idle (lineage boundary);
* liveness: a runner whose controller is not running is restarted with its ``start.sh``
  unless ``STOP`` exists; halted controllers are restarted with a cooldown;
* inbox: directives of the fleet coordinator (``<OWN>/inbox/<id>.json``, docs/COORDINATOR-POLICY.md)
  are applied under the feeder lock, each id once: ``stop_lineage`` and ``hold_runner``/
  ``release_runner`` first, ``start_lineage`` (seeded from ``<OWN>/seeds``) when an idle runner
  with quota asks for work, right after a local handoff; a start waits until it expires;
* claims v2: a second lineage of a problem on this host is the claim ``<problem>~N`` with a
  ``problem_id`` field; it ends on a stall (no route review) and never becomes a program;
* packaging: each ``PASS-<problem>.json`` is first kept as ``passes/<problem>-<digest16>.json``
  (a later pass of the problem overwrites the PASS file) and turned into a branch package in
  ``outbox/`` (exact verified proof files + benchmark lakefile/toolchain + README/PROVENANCE/
  VERIFICATION/licenses; the runtime id comes from the job's runtime) for the local publisher,
  with a CI-fit gate;
* ``status.json`` summarizes runners, host memory/load, claims and their signals, packages,
  inbox directives, runner holds and alerts.

It never edits fleet queues, never kills processes (a job STOP file is the controller's
controlled stop) and never touches credentials. Policy and config keys:
docs/FEEDER-POLICY.md, docs/COORDINATOR-POLICY.md. Python 3.10 compatible (the system python
of the oldest production host).

Host layout (docs/CONFIGURATION.md): LEH_OWN_ROOT (default ~/leh-fleet), LEH_CAMPAIGNS_ROOT
(~/projects/lean-eval-campaigns), LEH_RUNS_ROOT (~/projects/lean-eval-runs), LEANEVAL_WORKBENCH
(~/projects/lean-eval-workbench).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import textwrap
import time
import traceback
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

OWN = Path(os.environ.get("LEH_OWN_ROOT") or Path.home() / "leh-fleet")
CAMPAIGNS = Path(os.environ.get("LEH_CAMPAIGNS_ROOT") or Path.home() / "projects/lean-eval-campaigns")


def _host() -> str:
    """config.json "host" (written by deploy), else this machine's short host name."""
    try:
        name = json.loads((OWN / "config.json").read_text()).get("host")
    except (OSError, ValueError, AttributeError):
        name = None
    return name or socket.gethostname().split(".")[0]


HOST = _host()
POLICY = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
RUNS = Path(os.environ.get("LEH_RUNS_ROOT") or Path.home() / "projects/lean-eval-runs")
BENCH_REPO = Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench") / "vendor/lean-eval"
LEADERBOARD = "https://lean-lang.org/eval/site-data/v2/groups/formalization-evaluation.json"
HALTED = {"halted_authentication_failure", "halted_consecutive_failures", "halted_credential_access",
          "halted_controller_error"}
MARKERS = ("nanoda kernel accepts the solution", "Lean default kernel accepts the solution",
           "Your solution is okay!")
# verification_seconds: the official LeanEval CI allows one 6 h job (16 CPU / 32 GB since 2026-10-22);
# a complete proof must not be lost to a shorter local verifier budget (2026-10-14).
DEFAULTS = {"wall_seconds": 14400, "token_limit": 1_000_000_000, "verification_seconds": 21600,
            "worker_timeout_seconds": 1800, "verify_policy": "if_ready", "max_rounds": 3,
            # The runtime ends a session after this many early turn ends; the fleet uses 50.
            "max_nudges": 50}


# --------------------------------------------------------------------------- helpers

def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso(ts: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(ts if ts is not None else time.time(),
                                           datetime.timezone.utc).isoformat()


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def log(message: str) -> None:
    line = f"{iso()} {message}"
    print(line, flush=True)
    with contextlib.suppress(OSError):
        with (OWN / "feeder.log").open("a") as stream:
            stream.write(line + "\n")


# --------------------------------------------------------------------------- runners

def controller_pids(root: Path) -> list[int]:
    needle = f"controller.py --host {HOST} --root {root}"
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or b"python" not in Path(argv[0].decode(errors="replace")).name.encode():
            continue
        if needle in b" ".join(argv).decode(errors="replace"):
            found.append(int(proc.name))
    return found


def lineage(assignments: list[dict], history: dict, base: str) -> dict[str, Any]:
    names = [a["attempt_id"] for a in assignments
             if a["attempt_id"] == base or a["attempt_id"].startswith(base + "-r")]
    entries = [(n, history[n]) for n in names if n in history]
    pending = [n for n in names if n not in history]
    passed = [n for n, e in entries
              if (e.get("verification") or {}).get("status") == "official_local_pass"]
    return {"names": names, "entries": entries, "pending": pending, "passed": passed}


# Claims v2: claims.json is keyed by problem id; a second lineage of the same problem on this host
# (a coordinator start while one runs) is the key `<problem>~N` with a `problem_id` field.
LIVE_CLAIM = ("running", "handoff", "paused", "program")


def claim_pid(key: str, claim: Any) -> str:
    """The problem of a claim: its `problem_id` field, else the key up to `~`."""
    pid = claim.get("problem_id") if isinstance(claim, dict) else None
    return str(pid) if pid else key.split("~", 1)[0]


def claims_of(claims: dict[str, Any], pid: str) -> list[tuple[str, dict[str, Any]]]:
    """(key, claim) of every claim of `pid` on this host, the main key first."""
    found = [(k, c) for k, c in claims.items() if isinstance(c, dict) and claim_pid(k, c) == pid]
    return sorted(found, key=lambda kc: (kc[0] != pid, kc[0]))


def second_lineage(pid: str, claim: dict[str, Any]) -> bool:
    """A claims-v2 lineage (key `<pid>~N`) beside the problem's main claim."""
    return str(claim.get("key") or pid) != pid


def claim_key(claims: dict[str, Any], pid: str, keep: tuple[str, ...] = ()) -> str:
    """The key for a new lineage of `pid`: the problem id unless a live claim (or one whose status
    is in `keep`) holds it, else the first `<pid>~N` (N >= 2) that is free or holds a finished claim."""
    held = LIVE_CLAIM + keep
    if (claims.get(pid) or {}).get("status") not in held:
        return pid
    n = 2
    while (claims.get(f"{pid}~{n}") or {}).get("status") in held:
        n += 1
    return f"{pid}~{n}"


COMMENT_BLOCK = re.compile(r"/-.*?-/", re.DOTALL)
COMMENT_LINE = re.compile(r"--[^\n]*")
PROOF_FILE = re.compile(r"(Submission\.lean|Submission/.+\.lean|NOTES\.md)")
PROGRESS_MIN_LINES = 300
DECL = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|nonrec|partial)\s+)*"
                  r"(?:theorem|lemma|def|instance|abbrev|structure|class)\s+([^\s:(\[{]+)")
# RESIDUALS / TACTICS tables of NOTES.md (briefs/COMMON.md), parsed leniently: 84 of 176 real
# NOTES had rows like "| `hMain`: `∀ ε > 0, …` (hyp. of …) | open | … | 10k |" that a one-token
# name regex missed (strategy report 2026-10-14, §3.4).
RESIDUAL_STATUS = {
    **dict.fromkeys(("open", "opened", "planned", "pending", "todo", "wip", "partial", "active",
                     "unproved", "unproven", "new", "sub", "subgoal"), "open"),
    **dict.fromkeys(("proved", "proven", "closed", "done", "solved", "complete", "completed",
                     "discharged"), "proved"),
    **dict.fromkeys(("refuted", "disproved", "false"), "refuted"),
    **dict.fromkeys(("replaced", "superseded", "split", "merged", "refined"), "replaced"),
    **dict.fromkeys(("dropped", "removed", "obsolete", "abandoned"), "dropped"),
}
TACTIC_STATUS = ("trying", "worked", "failed", "dropped")
HISTORY_HEADING = re.compile(r"\b(HISTORY|ARCHIVE|OLD|PREVIOUS)\b")
ESTIMATE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:[-–]\s*\d[\d,]*(?:\.\d+)?\s*)?([kK])?(?![A-Za-z])")
# The HANDOFF contract between rounds (briefs/COMMON.md); recorded, not enforced.
HANDOFF_FIELDS = ("Next step", "Active tactic", "Last failure", "Do not retry")


def proof_paths(workspace: Path, scope: str | None = None) -> list[Path]:
    """Proof files: Submission.lean and Submission/**/*.lean, or only those under `scope`."""
    if scope:
        return [p for p in sorted((workspace / scope).rglob("*.lean")) if p.is_file() and not p.is_symlink()]
    main = workspace / "Submission.lean"
    return [p for p in [main, *sorted((workspace / "Submission").rglob("*.lean"))]
            if p.is_file() and not p.is_symlink()]


def code_text(path: Path) -> str:
    return COMMENT_LINE.sub("", COMMENT_BLOCK.sub("", path.read_text(errors="replace")))


def proof_stats(workspace: Path, scope: str | None = None) -> dict[str, int] | None:
    """Code-level `sorry` count and non-blank code lines of the proof files (comments stripped)."""
    if not (workspace / (scope or "Submission.lean")).exists():
        return None
    sorry = lines = 0
    for path in proof_paths(workspace, scope):
        text = code_text(path)
        sorry += len(re.findall(r"\bsorry\b", text))
        lines += sum(1 for line in text.splitlines() if line.strip())
    return {"sorry": sorry, "lines": lines}


def holes(workspace: Path, scope: str | None = None) -> list[str]:
    """Declarations whose body contains `sorry`, as `relative/path.lean:name`."""
    found = set()
    for path in proof_paths(workspace, scope):
        name = None
        for line in code_text(path).splitlines():
            match = DECL.match(line)
            if match:
                name = match.group(1)
            if re.search(r"\bsorry\b", line):
                found.add(f"{path.relative_to(workspace).as_posix()}:{name}")
    return sorted(found)


def notes_text(workspace: Path) -> str:
    """NOTES.md, or NOTES.prev.md where a continuation moved it."""
    for name in ("NOTES.md", "NOTES.prev.md"):
        if (workspace / name).is_file():
            return (workspace / name).read_text(errors="replace")
    return ""


def split_cells(line: str) -> list[str]:
    """Cells of a markdown table row; `|` inside backticks or escaped as `\\|` stays in its cell."""
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    protect = text.count("`") % 2 == 0  # an unbalanced backtick must not swallow the row
    cells, buf, tick, i = [], [], False, 0
    while i < len(text):
        char = text[i]
        if char == "\\" and text[i + 1:i + 2] == "|":
            buf.append("|")
            i += 2
            continue
        if char == "`" and protect:
            tick = not tick
        if char == "|" and not tick:
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def is_separator(line: str) -> bool:
    text = line.strip()
    return "-" in text and set(text) <= set("|-: ")


def reuse_notes(workspace: Path, limit: int = 4000) -> str:
    """The `## REUSE` section of NOTES.md (adapted sources: repository, commit, files, licence), as the
    agents record it for the package's provenance; empty when there is none."""
    text, out, inside = notes_text(workspace), [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = bool(re.match(r"#+\s*REUSE\b", line))
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()[:limit]


def section_tables(text: str, section: str) -> list[tuple[list[str] | None, list[list[str]]]]:
    """(header, rows) of each markdown table under headings containing `section` (not under a
    history/archive heading); the header is None for a table without a separator row."""
    tables: list[tuple[list[str] | None, list[list[str]]]] = []
    block: list[str] = []
    inside = False

    def flush() -> None:
        if len(block) >= 2 and is_separator(block[1]):
            tables.append((split_cells(block[0]), [split_cells(l) for l in block[2:] if not is_separator(l)]))
        elif block:
            tables.append((None, [split_cells(l) for l in block if not is_separator(l)]))
        block.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.upper()
            inside = section in heading and not HISTORY_HEADING.search(heading)
        elif inside and line.strip().startswith("|"):
            block.append(line)
        else:
            flush()
    flush()
    return tables


def table_rows(text: str, section: str, statuses: tuple[str, ...]) -> list[tuple[str, str]]:
    """(name, status) rows of the tables under `section` whose second cell starts with a status."""
    rows = []
    for _, body in section_tables(text, section):
        for cells in body:
            word = re.search(r"[A-Za-z]+", cells[1]) if len(cells) > 1 else None
            if word and word.group(0).lower() in statuses:
                rows.append((re.sub(r"[`*]", "", cells[0]).strip(), word.group(0).lower()))
    return rows


def row_status(cell: str) -> str | None:
    word = re.search(r"[A-Za-z]+", cell)
    if word:
        return RESIDUAL_STATUS.get(word.group(0).lower())
    return "proved" if any(mark in cell for mark in ("✅", "✓", "✔")) else None


def residual_key(cell: str) -> str:
    """A row's name: its first backticked identifier, else its first word (else the cell text)."""
    for span in re.findall(r"`([^`]+)`", cell):
        if span.strip() and not re.search(r"\s", span.strip()):
            return span.strip()[:120]
    text = re.sub(r"[`*_]", "", cell).strip()
    word = re.split(r"[\s:,;(]", text, 1)[0] if text else ""
    return (word if re.fullmatch(r"[\w.'’-]+", word or "-") else text)[:120]


def parse_estimate(cell: str) -> int | None:
    """Lines in an `est. lines` cell: 1500, 1,500, ~1.5k, 2-4k (lower end), else None."""
    match = ESTIMATE.search(cell.replace("~", " ").replace("≈", " "))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(value * (1000 if match.group(2) else 1))


def residual_rows(text: str) -> list[dict[str, Any]]:
    """Rows of the RESIDUALS tables: key, status (open/proved/refuted/replaced/dropped), est.
    Columns come from the header when it names them; rows with an unknown status are skipped."""
    rows = []
    for header, body in section_tables(text, "RESIDUALS"):
        lower = [h.lower() for h in header or []]

        def column(pattern: str, default: int | None) -> int | None:
            return next((i for i, h in enumerate(lower) if re.search(pattern, h)), default)

        name = column(r"name|residual|statement|key|lemma|hole", 0)
        status = column(r"status|state", None)
        est = column(r"\best|lines|\bloc\b|size", None if header else 3)
        candidates = [status] if status is not None else [i for i in (1, 2, 3) if i not in (name, est)]
        for cells in body:
            state = next((s for s in (row_status(cells[i]) for i in candidates if i < len(cells)) if s), None)
            key = residual_key(cells[name]) if name < len(cells) else ""
            if state and key:
                rows.append({"key": key, "status": state,
                             "est": parse_estimate(cells[est]) if est is not None and est < len(cells) else None})
    return rows


def residual_summary(workspace: Path) -> dict[str, Any]:
    """The agent's own frontier: open rows (K), their summed estimate (E; None without a table or
    when no open row has one; 0 when no row is open), the largest row, each key's status."""
    rows = residual_rows(notes_text(workspace))
    keys: dict[str, str] = {}
    for row in rows:
        if row["status"] == "open" or row["key"] not in keys:
            keys[row["key"]] = row["status"]
    opened = [r for r in rows if r["status"] == "open"]
    ests = [r["est"] for r in opened if r["est"] is not None]
    estimate = None if not rows else (0 if not opened else (sum(ests) if ests else None))
    return {"rows": len(rows), "open": len(opened) if rows else None, "estimate": estimate,
            "max": max(ests, default=None), "keys": keys}


def open_residuals(workspace: Path) -> list[str]:
    """Names marked `open` in the RESIDUALS table of NOTES.md (the agent's own frontier)."""
    return sorted({r["key"] for r in residual_rows(notes_text(workspace)) if r["status"] == "open"})


def tactics(workspace: Path) -> list[tuple[str, str]]:
    """(tactic, status) rows of the TACTICS table of NOTES.md."""
    return table_rows(notes_text(workspace), "TACTICS", TACTIC_STATUS)


def handoff_missing(workspace: Path) -> list[str]:
    """HANDOFF contract fields absent from NOTES.md."""
    text = notes_text(workspace).lower()
    return [f for f in HANDOFF_FIELDS if f.lower() not in text]


# Records of calls the runtime refused or stopped before a compiler verdict (lrh P1: round-wall
# refusal, a compile superseded by an edit; request validation): they say nothing about the proof.
NO_VERDICT = {"invalid_request", "superseded", "budget_blocked"}


def last_build_ok(workspace: Path, scope: str | None = None) -> bool:
    """The latest whole-project build succeeded (with `scope`: the latest build of the lane's
    modules or of the whole project)."""
    module = scope.replace("/", ".") if scope else None
    for path in reversed(sorted((workspace / ".feedback").glob("step-*.json"))[-300:]):
        try:
            step = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if step.get("outcome_class") in NO_VERDICT:
            continue
        query = step.get("query") or ""
        if step.get("action") == "build" and (query == "Submission"
                                              or (module and query.startswith(module + "."))):
            return step.get("outcome_class") == "check_succeeded"
    return False


def last_full_build_ok(workspace: Path) -> bool:
    return last_build_ok(workspace)


def proof_inventory(workspace: Path) -> dict[str, str]:
    return {p.relative_to(workspace).as_posix(): sha256_file(p) for p in proof_paths(workspace)}


def full_build_matches(workspace: Path) -> bool:
    """The latest whole-project build (`./lean build Submission`, or a passing full check)
    succeeded on exactly the current proof files (`source_files` of the .feedback step)."""
    current = None
    for path in reversed(sorted((workspace / ".feedback").glob("step-*.json"))[-300:]):
        step = read_json(path)
        if not isinstance(step, dict) or step.get("outcome_class") in NO_VERDICT:
            continue
        action, query = step.get("action"), step.get("query") or ""
        full_check = action == "check" or (action == "build" and not query)
        if not full_check and not (action == "build" and query == "Submission"):
            continue
        if step.get("outcome_class") == "check_succeeded" and step.get("source_current", True) is not False:
            current = proof_inventory(workspace) if current is None else current
            if step.get("source_files") == current:
                return True
        if not full_check:
            return False  # the latest whole build failed or saw other files
    return False


def round_complete(workspace: Path) -> bool:
    """No `sorry` and a successful whole build of exactly these files: only verification is left."""
    stats = proof_stats(workspace)
    return bool(stats) and stats["sorry"] == 0 and full_build_matches(workspace)


def first_round(workspace: Path, scope: str | None = None) -> dict[str, Any]:
    """Measurements of a lineage's first round (nothing to compare with; never a stall)."""
    stats = proof_stats(workspace, scope)
    if stats is None:
        return {"first": True, "progress": True, "kind": None, "reason": "stats unavailable"}
    table = residual_summary(workspace)
    complete = scope is None and stats["sorry"] == 0 and full_build_matches(workspace)
    frontier = holes(workspace, scope) + open_residuals(workspace)
    return {"first": True, "after": stats, "full_build_ok": last_build_ok(workspace, scope),
            "kind": "complete" if complete else None, "progress": True, "complete": complete,
            "estimate": table["estimate"], "open_rows": table["open"], "frontier": frontier[:20],
            "frontier_size": len(frontier), "tactics": [t for t, s in tactics(workspace) if s == "trying"][:3]}


def round_progress(before_ws: Path, after_ws: Path, scope: str | None = None) -> dict[str, Any]:
    """Progress of one round, by kind: `complete` (no sorry, whole build of exactly these files
    ok), `sorry` (fewer of them), `residual` (with a passing build: a RESIDUALS row open before is
    now proved, or the summed estimate of the open rows fell; a renamed or re-split row alone is
    not progress), `split` (a decomposition: more sorry and new frontier entries, passing build)
    or `lines` (>=300 new code lines with a passing build)."""
    before, after = proof_stats(before_ws, scope), proof_stats(after_ws, scope)
    if before is None or after is None:
        return {"progress": True, "kind": None, "reason": "stats unavailable"}
    built = last_build_ok(after_ws, scope)
    frontier_before = holes(before_ws, scope) + open_residuals(before_ws)
    frontier_after = holes(after_ws, scope) + open_residuals(after_ws)
    table_before, table_after = residual_summary(before_ws), residual_summary(after_ws)
    closed = sorted(k for k, s in table_before["keys"].items()
                    if s == "open" and table_after["keys"].get(k) == "proved")
    est_before, est_after = table_before["estimate"], table_after["estimate"]
    shrunk = est_before is not None and est_after is not None and est_after < est_before
    complete = scope is None and after["sorry"] == 0 and full_build_matches(after_ws)
    if complete:
        kind = "complete"
    elif after["sorry"] < before["sorry"]:
        kind = "sorry"
    elif built and (closed or shrunk):
        kind = "residual"
    elif built and after["sorry"] > before["sorry"] and set(frontier_after) - set(frontier_before):
        kind = "split"
    elif built and after["lines"] - before["lines"] >= PROGRESS_MIN_LINES:
        kind = "lines"
    elif scope is not None and after["lines"] - before["lines"] >= 3 * PROGRESS_MIN_LINES:
        # a program lane writing a lot of code: its passing build may not be recorded (agents run
        # long builds in the background); on 2026-10-15 this stopped linnik RealFirst and ComplexFirst
        kind = "lines"
    else:
        kind = None
    return {"before": before, "after": after, "full_build_ok": built, "kind": kind,
            "frontier": frontier_after[:20], "frontier_size": len(frontier_after),
            "progress": kind is not None, "complete": complete,
            "estimate_before": est_before, "estimate": est_after, "open_rows": table_after["open"],
            "closed": closed[:10],
            "tactics": [t for t, s in tactics(after_ws) if s == "trying"][:3],
            "handoff_missing": handoff_missing(after_ws)}


def attempt_history(claim: dict[str, Any]) -> list[str]:
    """Measured outcome of each gated round of this problem's lineages, newest last: what moved
    and which tactic was active (the input AdaEvolve's stagnation step and IDS's reloader get)."""
    lines = list(claim.get("history") or [])
    for name, p in (claim.get("progress") or {}).items():
        if p.get("first"):
            continue
        before, after = (p.get("before") or {}).get("sorry"), (p.get("after") or {}).get("sorry")
        estimate = (f"; estimate {p.get('estimate_before')} -> {p.get('estimate')}"
                    if p.get("estimate_before") is not None or p.get("estimate") is not None else "")
        line = (f"- {name}: {p.get('kind') or 'no progress'}; sorry {before} -> {after}{estimate}; "
                f"active tactic: {', '.join(p.get('tactics') or []) or 'not recorded'}")
        if line not in lines:
            lines.append(line)
    return lines[-15:]


def history_text(lines: list[str], tried: list[tuple[str, str]]) -> str:
    """The `{history}` block of the ROUTE_REVIEW brief."""
    out = []
    if tried:
        out.append("TACTICS table of the restored NOTES.md: "
                   + "; ".join(f"{name} ({status})" for name, status in tried) + ".")
    out.append("Rounds measured by the controller (progress kind; sorry count; remaining-work "
               "estimate in lines when recorded; tactic marked trying):")
    out += lines or ["- none recorded (earlier rounds ran before this record or on another host)"]
    return "\n".join(out)


def verify_pending(entry: dict[str, Any] | None, defaults: dict[str, Any]) -> bool:
    """A finished round whose files were never officially verified (or timed out with a smaller
    budget than the current one)."""
    if not entry or entry.get("phase") != "done":
        return False
    verification = entry.get("verification") or {}
    status = verification.get("status")
    if status in ("official_local_pass", "rejected"):
        return False
    if status == "timeout":
        spent = float(verification.get("verification_wall_seconds") or 0)
        return spent < 0.9 * float(defaults["verification_seconds"])
    return True


def verify_candidate(job: Path | None, entry: dict[str, Any] | None, defaults: dict[str, Any]) -> bool:
    return bool(job) and verify_pending(entry, defaults) and round_complete(job / "workspace")


def closeness(entry: dict[str, Any] | None, role: str | None = None) -> float:
    """How near a lineage is to a pass: 1.0 when complete or verifying, else
    1/(1 + sorry + 0.5 frontier), +0.1 if the round closed a sorry or a residual."""
    entry = entry or {}
    if role == "VERIFY" or entry.get("complete") or entry.get("kind") == "complete":
        return 1.0
    sorry = (entry.get("after") or {}).get("sorry")
    if sorry is None:
        return 0.0
    frontier = entry.get("frontier_size", len(entry.get("frontier") or []))
    value = 1 / (1 + sorry + 0.5 * frontier) + (0.1 if entry.get("kind") in ("sorry", "residual") else 0)
    return round(min(value, 0.99), 3)


def stall_reason(names: list[str], progress: dict[str, Any], policy: dict[str, Any]) -> str | None:
    """Stall rules on top of the per-round gate, never on a complete round: the remaining-work
    estimate fell by less than (1 - estimate_ratio) in each of the last two rounds with no
    RESIDUALS row proved and no sorry closed; or, when the estimate is not recorded for those
    rounds, `churn_rounds` rounds since the last new minimum of the sorry count (counted from
    round 1: a decomposition may raise the count, only a new minimum resets it)."""
    entries = [progress[n] for n in names if n in progress]
    if not entries or entries[-1].get("complete") or entries[-1].get("kind") == "complete":
        return None
    recent = [e for e in entries if not e.get("first")][-2:]
    ratio = float(policy.get("estimate_ratio") or 0)
    if (ratio and len(recent) == 2
            and all(e.get("estimate_before") is not None and e.get("estimate") is not None for e in recent)):
        if all(e["estimate_before"] >= float(policy["estimate_min"]) and e["estimate"] > ratio * e["estimate_before"]
               and not e.get("closed") and e.get("kind") != "sorry" for e in recent):
            trail = " -> ".join(str(v) for v in [recent[0]["estimate_before"]] + [e["estimate"] for e in recent])
            return (f"estimate stall: remaining-work estimate {trail} lines fell by less than "
                    f"{round((1 - ratio) * 100)}% in each of two rounds, no residual closed")
        return None
    churn = int(policy.get("churn_rounds") or 0)
    sorries = [(e.get("after") or {}).get("sorry") for e in entries]
    if not churn or any(s is None for s in sorries):
        return None
    best, at = sorries[0], 0
    for i, value in enumerate(sorries):
        if value < best:
            best, at = value, i
    if len(sorries) - 1 - at >= churn:
        return (f"churn: {len(sorries) - 1 - at} rounds without a new sorry minimum "
                f"(minimum {best} in round {at + 1}; sorry by round {sorries})")
    return None


def gate_lineage(root: Path, claim: dict[str, Any], info: dict[str, Any],
                 state: dict[str, Any], policy: dict[str, Any] | None = None) -> str | None:
    """Rounds 1-2 always run; from round 2 on, a round without progress is the last one, and
    so is a second round in a row whose only progress is new lines (the frontier did not move).
    With a `policy` (single lineages; program roles have none): a complete but unverified last
    round stops the continuation for a VERIFY lineage, and the stall rules apply. Sets
    claim["stop_kind"] (complete / no_progress / lines / estimate / churn) when it stops."""
    done = dict(info["entries"])
    progress = claim.setdefault("progress", {})
    scope = claim.get("scope")
    for i, name in enumerate(info["names"]):
        if name in progress or name not in done or done[name].get("phase") != "done":
            continue
        workspace = root / "jobs" / name / "workspace"
        entry = (first_round(workspace, scope) if i == 0 else
                 round_progress(root / "jobs" / info["names"][i - 1] / "workspace", workspace, scope))
        progress[name] = entry
        trajectory = claim.setdefault("trajectory", [])
        trajectory.append([name, (entry.get("after") or {}).get("sorry"), entry.get("estimate"), entry.get("kind")])
        del trajectory[:-30]
        claim["closeness"] = closeness(entry, claim.get("role"))
    finished = [n for n, _ in info["entries"]]
    if not finished or not info["pending"]:
        return None
    last_name = finished[-1]
    # a CI_FIT lineage is complete by construction: it is verified only when its agent replies READY
    # (fit targets met); an automatic VERIFY ended five partitioned CI-fit lineages and stopped their
    # siblings on 2026-10-18/19
    if (policy and policy.get("verify") and scope is None and claim.get("role") not in PERSISTENT_ROLES
            and int(claim.get("verifies", 0)) < int(policy["max_verifies"])):
        entry = progress.get(last_name)
        if entry is not None and "complete" not in entry:  # measured before the complete kind existed
            entry["complete"] = round_complete(root / "jobs" / last_name / "workspace")
        if entry and entry.get("complete") and verify_pending(done.get(last_name), policy["defaults"]):
            stop_lineage(root, info["pending"], state)
            claim.update(stop_kind="complete", last_finished=last_name)
            return f"complete and unverified: {last_name} has no sorry and built Submission on its files"
    if len(finished) < 2:
        return None
    if claim.get("role") in PERSISTENT_ROLES:
        # a CI_FIT lineage works on a sorry-free proof: the sorry/frontier progress measure is
        # always flat for it (2026-10-17: poincare M3 was stopped mid-work at 2,200/2,527 modules);
        # it runs until max_rounds or the operator stops it
        return None
    last = progress.get(last_name, {})
    before = progress.get(finished[-2], {}) if len(finished) >= 3 else {}
    if not last.get("progress", True) and (scope is None or before.get("progress", True) is False):
        # a program lane needs two rounds in a row without progress: on 2026-10-15/16 all five
        # single-round lane stops were reversed by the operator (the lane was mid-proof)
        kind, reason = "no_progress", f"no progress in {last_name}: {last}"
    elif scope is None and last.get("kind") == "lines" and before.get("kind") == "lines":
        # not for program lanes: a lane holds one large statement, so its frontier only moves when
        # the whole lane closes; on 2026-10-15 this rule stopped 3 lanes in the middle of their proofs
        kind, reason = "lines", f"frontier unchanged for 2 rounds in {last_name}: {last}"
    else:
        reason = stall_reason(info["names"], progress, policy) if policy else None
        kind = "estimate" if (reason or "").startswith("estimate") else "churn"
    if not reason:
        return None
    stop_lineage(root, info["pending"], state)
    claim.update(stopped_no_progress=reason[:2000], stop_kind=kind, last_finished=last_name)
    return reason


def stop_lineage(root: Path, pending: list[str], state: dict[str, Any]) -> None:
    """End a lineage: the running round gets a controlled STOP and no successor; unstarted
    rounds are dropped from the manifest."""
    with manifest_lock(root):
        manifest = json.loads((root / "manifest.json").read_text())
        current = Path(state.get("current_job") or "").name
        kept = []
        for a in manifest["assignments"]:
            if a["attempt_id"] in pending:
                if a["attempt_id"] == current:
                    a["auto_continue"] = None
                    (root / "jobs" / current / "STOP").touch()
                elif a["attempt_id"] not in state.get("history", {}):
                    continue  # never started: drop it
            kept.append(a)
        manifest["assignments"] = kept
        tmp = root / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.replace(root / "manifest.json")


def seed_overlay(root: Path, pid: str, source_job: Path) -> str:
    """Copy a finished job's proof files into <root>/imports/; returns the root-relative dir."""
    workspace = source_job / "workspace"
    dest = root / "imports" / f"{pid}-from-{source_job.name}"
    if dest.exists():
        return str(dest.relative_to(root))
    files = {}
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace).as_posix()
        if path.is_symlink() or not path.is_file() or not PROOF_FILE.fullmatch(rel):
            continue
        if rel.startswith((".lake/", ".incremental/", ".compiler/")):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files[rel] = sha256_file(path)
    # A continuation moves NOTES.md to NOTES.prev.md; a round cut short never rewrites it.
    if "NOTES.md" not in files and (workspace / "NOTES.prev.md").is_file():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workspace / "NOTES.prev.md", dest / "NOTES.md")
        files["NOTES.md"] = sha256_file(workspace / "NOTES.prev.md")
    write_json(dest.parent / f"{dest.name}.SOURCE.json",
               {"source_job": str(source_job), "copied_at": iso(), "files": files})
    return str(dest.relative_to(root))


def seed_overlay_dir(root: Path, pid: str, seed: Path) -> str:
    """Copy exported proof files (Submission.lean, Submission/**, NOTES.md) into <root>/imports/."""
    dest = root / "imports" / f"{pid}-seed-{seed.name}"
    if not dest.exists():
        for path in sorted(seed.rglob("*")):
            rel = path.relative_to(seed).as_posix()
            if path.is_file() and not path.is_symlink() and PROOF_FILE.fullmatch(rel):
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest / rel)
        if not dest.exists():
            raise ValueError(f"seed {seed} has no proof files")
    return str(dest.relative_to(root))


def next_base(root: Path, assignments: list[dict], pid: str) -> str:
    """Attempt name fleet-1-w<runner number>-<seq>-<problem>; runner names end in digits (wave101, r7)."""
    wave = re.search(r"-[A-Za-z_]*(\d+)$", root.name)
    seq = len({(a.get("auto_continue") or {}).get("base", a["attempt_id"]) for a in assignments}) + 1
    return f"fleet-1-w{wave.group(1) if wave else root.name}-{seq:03d}-{pid}"


def last_round_job(claim: dict[str, Any]) -> Path | None:
    if not claim.get("runner") or not claim.get("base"):
        return None
    rounds = int(claim.get("rounds") or 1)
    name = claim["base"] if rounds <= 1 else f"{claim['base']}-r{rounds}"
    job = Path(claim["runner"]) / "jobs" / name
    return job if (job / "workspace").is_dir() else None


def round_index(base: str, name: str) -> int | None:
    if name == base:
        return 1
    rest = name[len(base) + 2:] if name.startswith(base + "-r") else ""
    return int(rest) if rest.isdigit() else None


def finished_round_job(claim: dict[str, Any], history: dict[str, Any] | None = None) -> Path | None:
    """The latest round of the claim's lineage that really finished (`done` in the controller
    history, workspace present). claim["rounds"] may count rounds that never started."""
    if not claim.get("runner") or not claim.get("base"):
        return None
    root = Path(claim["runner"])
    if history is None:
        history = (read_json(root / "state.json", {}) or {}).get("history", {})
    rounds = sorted((round_index(claim["base"], n), n) for n in history
                    if round_index(claim["base"], n) is not None)
    for _, name in reversed(rounds):
        job = root / "jobs" / name
        if (history[name] or {}).get("phase") == "done" and (job / "workspace").is_dir():
            return job
    return None


def job_entry(job: Path) -> dict[str, Any] | None:
    """The controller history entry of a job (<root>/jobs/<name>)."""
    history = (read_json(job.parent.parent / "state.json", {}) or {}).get("history", {})
    return history.get(job.name)


def best_round_job(pid: str, runners: list[Path], default: Path | None = None) -> Path | None:
    """The best finished round of `pid` on this host: lowest sorry among rounds whose last
    `build Submission` succeeded, ties to the latest; program lane rounds excluded. `default`
    when no round qualifies."""
    best, rank = default, None
    for root in runners:
        history = (read_json(root / "state.json", {}) or {}).get("history", {})
        for name, entry in history.items():
            if ((entry or {}).get("problem_id") != pid or entry.get("phase") != "done"
                    or re.search(r"-lane[A-Z]", name)):
                continue
            workspace = root / "jobs" / name / "workspace"
            stats = proof_stats(workspace) if workspace.is_dir() else None
            if not stats or not last_full_build_ok(workspace):
                continue
            key = (-stats["sorry"], float(entry.get("finished_at") or 0))
            if rank is None or key > rank:
                best, rank = root / "jobs" / name, key
    return best


# ---------------------------------------------------------------------------------------------
# Programs: one large problem split into parallel lanes (architect -> lanes -> integrator).
# State lives in <OWN>/programs/<problem>.json; each role is an ordinary queue lineage.

LANES_DIR = "Submission/Lanes"
# A lane that ends without a proof (often: it refuted its statement) sends the program back to
# the architect, with the finished lanes and the failed lane's work, at most this many times.
MAX_REARCHITECT = 2
# Lane claim states that end the lane without a proof; "refuted": the lane proved an interface statement false.
LANE_FAILED = ("exhausted", "failed", "refuted")
# An architecture the validator rejects goes back to the architect with the reason this often.
MAX_ARCHITECT_FIXES = 1
# Version of the architect brief's contract (lane CONTRACT blocks + Sanity.lean); architects
# assigned before it existed are validated without it. 3 (2026-10-26) adds Source: and Strength:,
# 4 (2026-10-27) Reuse: (earlier formalizations of the step found in the bank, lean-pool or Mathlib).
ARCHITECT_CONTRACT = 4


def lane_ids(workspace: Path) -> list[str]:
    return sorted(p.parent.name for p in (workspace / LANES_DIR).glob("*/Main.lean")
                  if re.fullmatch(r"[A-Z][A-Za-z0-9]*", p.parent.name))


def interface_order(workspace: Path, lanes: list[str]) -> list[str]:
    """Lanes in the order their statements appear in Interfaces.lean (the architect lists shared tool lanes
    first, and the feeder assigns lanes in this order); lanes it does not mention keep their order at the end."""
    path = workspace / LANES_DIR / "Interfaces.lean"
    text = path.read_text(errors="replace") if path.is_file() else ""
    def position(lane: str) -> int:
        match = re.search(rf"\bdef\s+(?:Submission\.Lanes\.)?{re.escape(lane)}Statement\b", text)
        return match.start() if match else len(text) + 1
    return sorted(lanes, key=lambda lane: (position(lane), lanes.index(lane)))


def code_sorry(path: Path) -> int:
    text = COMMENT_LINE.sub("", COMMENT_BLOCK.sub("", path.read_text(errors="replace")))
    return len(re.findall(r"\bsorry\b", text))


CONTRACT_KEYS = ("Statement:", "Used by:", "Sanity:", "Estimate:")
# Contract 3: where the lane's statement comes from (the published proof step, or the inductive step of
# a stated relative induction) and why it is strictly weaker than the hole. The Cerf programs of
# 2026-10-19..21 issued >=17 invented lane statements that were false, vacuous or as strong as the whole
# problem; each was true-looking to its lane and found only by audits 4.5-8.4 h later.
CONTRACT_KEYS_3 = ("Source:", "Strength:")
# Contract 4: what `./lean search` found for the lane's step in the solution bank, lean-pool and Mathlib
# (source, commit, files, licence), or `none` with the searches made. Adapted public formalizations were
# the fastest accepted solutions; lanes that rebuilt an existing library cost days.
CONTRACT_KEYS_4 = ("Reuse:",)
SCOPE_LINE = re.compile(r"^\s*(namespace|section|end)\b[ \t]*([^\s]*)")


def declared_names(text: str) -> set[str]:
    """Full names of the declarations in Lean source, resolving `namespace X … end X` (a
    `theorem Foo.main` inside `namespace Submission.Lanes` is `Submission.Lanes.Foo.main`)."""
    names, scopes = set(), []  # scopes: (kind, name) innermost last
    for line in COMMENT_LINE.sub("", COMMENT_BLOCK.sub("", text)).splitlines():
        scope = SCOPE_LINE.match(line)
        if scope:
            kind, name = scope.groups()
            if kind != "end":
                scopes.append((kind, name))
            else:  # `end X` closes the innermost scope named X; a bare `end` the innermost one
                at = next((i for i in range(len(scopes) - 1, -1, -1) if scopes[i][1] == name), None)
                del scopes[len(scopes) - 1 if at is None else at:]
            continue
        decl = DECL.match(line)
        if decl:
            name = decl.group(1)
            prefix = [n for kind, n in scopes if kind == "namespace" and n]
            names.add(name[len("_root_."):] if name.startswith("_root_.") else ".".join([*prefix, name]))
    return names


def declares(text: str, full: str) -> bool:
    """`full` is declared in `text`, written out in full or inside its namespace."""
    return f"theorem {full}" in text or full in declared_names(text)


def validate_architecture(workspace: Path, contract: int = 1, known: Any = ()) -> tuple[list[str], str]:
    """Lane ids if the architect left a compiling split with sorry only in the lane stubs.

    A Lanes/<Id> folder whose Main.lean declares no `Submission.Lanes.<Id>.main` and has no sorry
    is a library the lanes build on (a re-architecture keeps a failed lane's work that way).

    contract >= 2 (briefs/roles/ARCHITECT.md since 2026-10-12): every open lane's docstring
    carries a CONTRACT block, and Submission/Lanes/Sanity.lean, imported by Submission.lean and
    so built with it, holds a sorry-free `Submission.Lanes.<Id>.sanity` per open lane (the lane
    statement checked on a concrete instance), because one false lane statement cost a whole
    program. Lanes already proved (kept by a re-architecture) need neither.

    contract >= 3: open lanes that are new (not in `known`, the program's lanes before this
    architecture) also carry Source: and Strength:, and from contract 4 Reuse:; lanes of an earlier
    contract keep theirs."""
    lanes = []
    for lane in lane_ids(workspace):
        folder = workspace / LANES_DIR / lane
        main = (folder / "Main.lean").read_text(errors="replace")
        if declares(main, f"Submission.Lanes.{lane}.main"):
            lanes.append(lane)
        elif any(code_sorry(p) for p in folder.rglob("*.lean") if p.is_file()):
            return [], f"lane {lane}: Main.lean lacks Submission.Lanes.{lane}.main"
    if len(lanes) < 2:
        return [], f"need >=2 lanes under {LANES_DIR}, found {lanes}"
    for name in ("Interfaces.lean", "Reduction.lean"):
        if not (workspace / LANES_DIR / name).is_file():
            return [], f"missing {LANES_DIR}/{name}"
    # a lane with no sorry left needs no contract or sanity check (2026-10-17: a split was rejected
    # 2.5 h for a missing sanity theorem of the long-proved lane CritHit); the full build still checks it
    open_lanes = [lane for lane in lanes if not lane_done(workspace, lane)
                  and any(code_sorry(p) for p in (workspace / LANES_DIR / lane).rglob("*.lean") if p.is_file())]
    if contract >= 2:
        for lane in open_lanes:
            main = (workspace / LANES_DIR / lane / "Main.lean").read_text(errors="replace")
            new = lane not in known
            keys = (*CONTRACT_KEYS, *(CONTRACT_KEYS_3 if contract >= 3 and new else ()),
                    *(CONTRACT_KEYS_4 if contract >= 4 and new else ()))
            missing = [k for k in ("CONTRACT", *keys) if k not in main]
            if missing:
                return [], f"lane {lane}: Main.lean docstring lacks the contract fields {missing}"
    if contract >= 2 and open_lanes:
        sanity = workspace / LANES_DIR / "Sanity.lean"
        if not sanity.is_file():
            return [], f"missing {LANES_DIR}/Sanity.lean (one sanity theorem per lane)"
        text = sanity.read_text(errors="replace")
        absent = [lane for lane in open_lanes if not declares(text, f"Submission.Lanes.{lane}.sanity")
                  and f"Submission.Lanes.{lane}.sanity" not in text]
        if absent:
            return [], f"{LANES_DIR}/Sanity.lean lacks Submission.Lanes.<Id>.sanity for {absent}"
        if not re.search(r"^\s*import\s+Submission\.Lanes\.Sanity\b", (workspace / "Submission.lean").read_text(
                errors="replace"), re.MULTILINE):
            return [], "Submission.lean must import Submission.Lanes.Sanity so the build checks it"
    lane_dirs = [f"{LANES_DIR}/{lane}/" for lane in lanes]
    for path in [workspace / "Submission.lean", *sorted((workspace / "Submission").rglob("*.lean"))]:
        rel = path.relative_to(workspace).as_posix()
        if not rel.startswith(tuple(lane_dirs)) and path.is_file() and code_sorry(path):
            return [], f"sorry outside lane stubs: {rel}"
    if not last_full_build_ok(workspace):
        return [], "last ./lean build Submission did not succeed"
    return lanes, "ok"


def lane_done(workspace: Path, lane: str) -> bool:
    """No sorry in the lane dir, and a successful build saw exactly the current lane files."""
    folder = workspace / LANES_DIR / lane
    files = {p.relative_to(workspace).as_posix(): sha256_file(p)
             for p in sorted(folder.rglob("*.lean")) if p.is_file() and not p.is_symlink()}
    if not files or any(code_sorry(workspace / name) for name in files):
        return False
    for path in reversed(sorted((workspace / ".feedback").glob("step-*.json"))[-100:]):
        try:
            step = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if step.get("action") != "build" or step.get("outcome_class") != "check_succeeded":
            continue
        query = step.get("query") or ""
        if query not in ("Submission", f"Submission.Lanes.{lane}.Main"):
            continue
        seen = {k: v for k, v in (step.get("source_files") or {}).items() if k.startswith(f"{LANES_DIR}/{lane}/")}
        return seen == files
    return False


# `theorem T : ¬ X.YStatement` in a lane: the lane proves an interface statement false (its own or another lane's).
NEGATED_STATEMENT = re.compile(r"\btheorem\s+([\w.'’]+)\s*:\s*¬\s*\(?\s*((?:[\w'’]+\.)*[A-Za-z][\w'’]*Statement)\s*\)?\s*:=")


def refuted_statements(workspace: Path, lane: str) -> dict[str, str]:
    """Interface statements the lane's files prove false, as {statement name: theorem}. Comments are ignored.
    A consumer that closes its own statement through `absurd` with such a theorem is vacuous, not done
    (2026-10-30: two shared tool statements were refuted four times while their consumers counted as done)."""
    found: dict[str, str] = {}
    for path in sorted((workspace / LANES_DIR / lane).rglob("*.lean")):
        text = COMMENT_LINE.sub("", COMMENT_BLOCK.sub("", path.read_text(errors="replace")))
        for theorem, statement in NEGATED_STATEMENT.findall(text):
            found.setdefault(statement.rsplit(".", 1)[-1], theorem)
    return found


def interface_requests(workspace: Path) -> list[str]:
    """Rows of the `## INTERFACE REQUESTS` table in the lane round's NOTES.md: interface changes a consumer
    needs from another lane, recorded instead of a private strengthened copy of that lane's statement."""
    notes = next((workspace / n for n in ("NOTES.md", "NOTES.prev.md") if (workspace / n).is_file()), None)
    if notes is None:
        return []
    rows = []
    for header, body in section_tables(notes.read_text(errors="replace"), "INTERFACE REQUESTS"):
        for cells in body:
            line = " | ".join(c.strip() for c in cells if c.strip())
            if line and not re.fullmatch(r"[-:| ]*", line):
                rows.append(line[:400])
    return rows[:20]


def rearchitect_reason(program: dict[str, Any]) -> str:
    """The refutations and open interface requests that a re-architecture has to resolve."""
    lines = [f"- lane {r['lane']} proves `{stmt}` false (`{r['theorem']}`): replace or correct that statement, and "
             f"treat every lane that assumed it as not done." for stmt, r in sorted((program.get("refuted") or {}).items())]
    for lane, rows in sorted((program.get("interface_requests") or {}).items()):
        lines += [f"- interface request from lane {lane}: {row}" for row in rows]
    return "\n".join(lines)


LANE_NOTES_CAP = 80_000


def append_lane_notes(dest: Path, lane: str, job: Path) -> None:
    """Append the lane's own NOTES.md (HANDOFF, STUCK, routes tried) from its last job to the overlay's
    NOTES.md. A lane's notes live in its job's NOTES.md, not in its directory, so a split, a
    re-architecture or a resume on its files used to see only the program notes (2026-10-20: three split
    architects reported "lane notes were not restored"). Headings are demoted two levels so the
    program's own HANDOFF/RESIDUALS sections stay the first ones."""
    workspace = Path(job) / "workspace"
    source = next((workspace / n for n in ("NOTES.md", "NOTES.prev.md") if (workspace / n).is_file()), None)
    if source is None:
        return
    text = re.sub(r"^(#{1,4}) ", lambda m: "#" * (len(m.group(1)) + 2) + " ",
                  source.read_text(errors="replace"), flags=re.M)
    if len(text) > LANE_NOTES_CAP:
        text = text[:LANE_NOTES_CAP] + "\n\n[... truncated by the controller]\n"
    notes = dest / "NOTES.md"
    head = notes.read_text(errors="replace").rstrip() if notes.is_file() else ""
    notes.write_text(f"{head}\n\n## LANE {lane}: its own notes from {Path(job).name} (verbatim, headings demoted)\n\n"
                     f"{text.strip()}\n")


def merged_overlay(root: Path, pid: str, base_job: Path, lanes: dict[str, Any],
                   notes_of: str | None = None) -> str:
    """Base (architect) proof files plus each lane's directory from its finishing job; `notes_of` also
    carries that lane's own NOTES.md (split, re-architecture, resume)."""
    dest = root / "imports" / f"{pid}-integrated-{time.time_ns()}"  # two merges in one second must not collide
    base_rel = seed_overlay(root, pid, base_job)
    shutil.copytree(root / base_rel, dest)
    for lane, info in lanes.items():
        source = Path(info["done_job"]) / "workspace" / LANES_DIR / lane
        target = dest / LANES_DIR / lane
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".*"))
    if notes_of:
        append_lane_notes(dest, notes_of, Path(lanes[notes_of]["done_job"]))
    write_json(dest.parent / f"{dest.name}.SOURCE.json",
               {"base_job": str(base_job), "lanes": {k: v["done_job"] for k, v in lanes.items()},
                "created_at": iso()})
    return str(dest.relative_to(root))


def current_job(sub: dict[str, Any]) -> Path | None:
    state = read_json(Path(sub["runner"]) / "state.json", {}) or {}
    job = Path(state.get("current_job") or "")
    return job if job.name.startswith(sub["base"]) and (job / "workspace").is_dir() else None


def update_sub(sub: dict[str, Any], alerts: list[str]) -> dict[str, Any]:
    """Advance one program role lineage (same bookkeeping as a claim)."""
    root = Path(sub["runner"])
    state = read_json(root / "state.json", {}) or {}
    manifest = read_json(root / "manifest.json", {}) or {}
    info = lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
    sub["rounds"] = len(info["names"])
    outcome = classify(info)
    if outcome:
        sub["status"] = outcome
    else:
        try:
            gate_lineage(root, sub, info, state)
        except Exception as exc:  # noqa: BLE001
            alerts.append(f"progress gate {sub['base']}: {type(exc).__name__}: {exc}")
    return {"info": info, "state": state}


def lane_statement(workspace: Path, lane: str) -> str | None:
    """Text of `def ...<lane>Statement` in Interfaces.lean (up to the next blank line), if any."""
    try:
        text = (workspace / LANES_DIR / "Interfaces.lean").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(rf"^def (?:Submission\.Lanes\.)?{re.escape(lane)}Statement\b.*?(?=\n\s*\n|\Z)", text,
                  re.MULTILINE | re.DOTALL)
    return m.group(0).strip() if m else None


def finish_split(program: dict[str, Any], alerts: list[str]) -> None:
    """The split architect ended: adopt its new lanes (the split lane becomes a done library with
    the glue, the base moves to the architect's files, running lanes keep their claims), give it
    one fix round, or put the lane back on its own files."""
    pid, split = program["problem_id"], program["split"]
    lane = split["lane"]
    job = last_round_job(split["sub"])
    reason = "no job"
    if job:
        ws = job / "workspace"
        lanes, reason = validate_architecture(ws, int(split["sub"].get("contract", 1)), set(program["lanes"]))
        if lanes:
            new = [l for l in lanes if l not in program["lanes"]]
            old_base = Path(program["base_job"]) / "workspace"
            changed = [l for l, i in program["lanes"].items() if l != lane
                       and lane_statement(ws, l) != lane_statement(old_base, l)]
            missing = [l for l, i in program["lanes"].items() if l != lane and not i.get("done") and l not in lanes]
            if lane_statement(ws, lane) != lane_statement(old_base, lane):
                reason = f"the split changed {lane}Statement"
            elif lane in lanes and not lane_done(ws, lane):
                reason = f"lane {lane} is still open in the split"
            elif not new:
                reason = "the split added no lane"
            elif changed or missing:
                reason = f"the split changed or dropped other lanes {sorted(set(changed + missing))}"
            else:
                info = program["lanes"][lane]
                if info.get("claim"):
                    info.setdefault("previous_claims", []).append(info.pop("claim"))
                info.update(done=True, done_job=str(job), split_into=new, split_at=iso())
                for l in new:
                    program["lanes"][l] = {"dir": f"{LANES_DIR}/{l}", "done": lane_done(ws, l), "split_from": lane}
                    if program["lanes"][l]["done"]:
                        program["lanes"][l]["done_job"] = str(job)
                program["base_job"] = str(job)
                program.setdefault("splits", []).append({"lane": lane, "into": new, "job": str(job), "at": iso()})
                program.pop("split", None)
                log(f"program {pid}: lane {lane} split into {new} ({job.name})")
                return
    if job and int(split.get("fixes", 0)) < MAX_ARCHITECT_FIXES:
        split.update(fixes=int(split.get("fixes", 0)) + 1, fix_job=str(job), fix_reason=reason)
        split.pop("sub", None)
        log(f"program {pid}: split of lane {lane} rejected ({reason}); architect fixes it")
        return
    info = program["lanes"][lane]
    if info.get("claim"):
        info.setdefault("previous_claims", []).append(info.pop("claim"))
    info["resume_job"] = split["from_job"]
    program.setdefault("splits", []).append({"lane": lane, "failed": reason, "at": iso()})
    program.pop("split", None)
    alerts.append(f"program {pid}: split of lane {lane} failed ({reason}); the lane continues on its files")
    log(f"program {pid}: split of lane {lane} failed ({reason}); lane re-opened on {Path(info['resume_job']).name}")


INFRA_ERRORS = ("Supervisor produced no result",)


def infra_failure(entry: dict[str, Any] | None) -> bool:
    """A failed round whose controller lost its supervisor (killed service, host restart)."""
    return bool(entry) and entry.get("phase") == "failed" and any(
        text in str(entry.get("error") or "") for text in INFRA_ERRORS)


def update_program(program: dict[str, Any], claims: dict[str, Any], alerts: list[str]) -> None:
    pid = program["problem_id"]
    status = program["status"]
    if status == "waiting_seed":
        if (Path(program["seed_job"]) / "result.json").is_file():
            program["status"] = "architect"
    elif status == "architect" and program.get("architect"):
        sub = program["architect"]
        update_sub(sub, alerts)
        if sub["status"] != "running":
            job = last_round_job(sub)
            lanes, reason = (validate_architecture(job / "workspace", int(sub.get("contract", 1)),
                                                   set(program.get("lanes") or {}))
                             if job else ([], "no job"))
            if lanes:
                state = {}
                for lane in interface_order(job / "workspace", lanes):
                    state[lane] = {"dir": f"{LANES_DIR}/{lane}", "done": False}
                    if lane_done(job / "workspace", lane):  # kept by a re-architecture
                        state[lane].update(done=True, done_job=str(job))
                program.update(status="lanes", base_job=str(job), lanes=state)
                for key in ("rearchitect", "architect_fix", "refuted", "interface_requests"):
                    program.pop(key, None)  # the new architecture answered them
                log(f"program {pid}: architecture ok, lanes {lanes}")
            elif job and int(program.get("architect_fixes", 0)) < MAX_ARCHITECT_FIXES:
                # the validator's reason goes back to an architect round on the same files
                program.update(architect=None, architect_fixes=int(program.get("architect_fixes", 0)) + 1,
                               architect_fix={"job": str(job), "reason": reason, "at": iso()})
                log(f"program {pid}: architecture rejected ({reason}); architect fixes it")
            else:
                program.update(status="failed", reason="architect: " + reason)
                log(f"program {pid}: architecture rejected: {reason}")
    elif status == "lanes":
        split = program.get("split") or {}
        if split.get("sub") and split["sub"].get("status") == "running":
            update_sub(split["sub"], alerts)
            if split["sub"]["status"] != "running":
                finish_split(program, alerts)
        for lane, info in program["lanes"].items():
            sub = info.get("claim")
            if info["done"] or not sub or sub["status"] != "running":
                continue
            live = update_sub(sub, alerts)
            last_entry = live["info"]["entries"][-1][1] if live["info"]["entries"] else None
            if sub["status"] == "failed" and infra_failure(last_entry):
                # the round died with its supervisor (host kill, restart): no mathematical failure, so
                # no re-architecture; the lane waits for the operator (relane.py) like any unknown cleanup
                sub.update(status="held", held_at=iso(), held_reason=str((last_entry or {}).get("error"))[:300])
                alerts.append(f"program {pid}: lane {lane} HELD after an infrastructure failure of its round")
                log(f"program {pid}: lane {lane} held: its round ended without a result ({sub['held_reason']})")
                continue
            # an operator brief update reaches the lane's later rounds, as for a running claim
            sync_brief(Path(sub["runner"]), sub["base"], pid, sub.get("role") or "LANE",
                       {"lane": lane, "dir": info["dir"]})
            job = current_job(sub) or last_round_job(sub)
            requests = interface_requests(job / "workspace") if job else []
            if requests:
                program.setdefault("interface_requests", {})[lane] = requests
            else:
                (program.get("interface_requests") or {}).pop(lane, None)
            refuted = refuted_statements(job / "workspace", lane) if job else {}
            if refuted:
                # A refuted interface statement means the split is wrong: stop the lane (it is not done, even
                # without a sorry) and re-architect once the other lanes' rounds end.
                record = program.setdefault("refuted", {})
                for statement, theorem in refuted.items():
                    record.setdefault(statement, {"lane": lane, "theorem": theorem, "job": str(job), "at": iso()})
                if live["info"]["pending"]:
                    stop_lineage(Path(sub["runner"]), live["info"]["pending"], live["state"])
                sub["status"] = "refuted"
                alerts.append(f"program {pid}: lane {lane} refutes {sorted(refuted)}; re-architecture follows")
                log(f"program {pid}: lane {lane} refutes {sorted(refuted)} in {job.name}")
                continue
            if job and lane_done(job / "workspace", lane):
                info.update(done=True, done_job=str(job))
                if live["info"]["pending"]:
                    stop_lineage(Path(sub["runner"]), live["info"]["pending"], live["state"])
                sub["status"] = "done"
                log(f"program {pid}: lane {lane} done in {job.name}")
            elif sub["status"] != "running":
                last = last_round_job(sub)
                if last and lane_done(last / "workspace", lane):
                    info.update(done=True, done_job=str(last))
                    sub["status"] = "done"
        lanes = program["lanes"]
        failed = [l for l, i in lanes.items() if i.get("claim") and not i["done"]
                  and i["claim"]["status"] in LANE_FAILED]
        running = [l for l, i in lanes.items() if i.get("claim") and i["claim"]["status"] == "running"]
        if all(i["done"] for i in lanes.values()):
            program["status"] = "integrating"
            log(f"program {pid}: all lanes done")
        elif failed and int(program.get("rearchitected", 0)) >= MAX_REARCHITECT:
            program.update(status="failed", reason=f"lane {failed[0]} ended without a proof")
            log(f"program {pid}: lane {failed[0]} ended without a proof")
        elif failed and not running:
            job = last_round_job(lanes[failed[0]]["claim"])
            program.update(status="architect", architect=None,
                           rearchitected=int(program.get("rearchitected", 0)) + 1,
                           rearchitect={"lane": failed[0], "job": str(job), "at": iso()})
            log(f"program {pid}: lane {failed[0]} ended without a proof; re-architecting")
    elif status == "integrating" and program.get("integrator"):
        sub = program["integrator"]
        update_sub(sub, alerts)
        if sub["status"] != "running":
            program["status"] = "passed" if sub["status"] == "passed" else "failed"
            if sub["status"] != "passed":
                program["reason"] = "integrator: " + sub["status"]
    if program["status"] in ("passed", "failed"):
        claims[pid] = dict(claims.get(pid) or {}, status="passed" if program["status"] == "passed"
                           else "exhausted", program_status=program["status"], finished_at=iso(),
                           runner=(program.get("integrator") or program.get("architect") or {}).get("runner"),
                           base=(program.get("integrator") or program.get("architect") or {}).get("base"))


def program_assignment(root: Path, program: dict[str, Any], targets: dict[str, Any],
                       assignments: list[dict], defaults: dict[str, Any]) -> dict[str, Any] | None:
    """Next role of this program for an idle runner, or None."""
    pid = program["problem_id"]
    target = dict(targets.get(pid) or {"problem_id": pid}, kind="fresh")
    target.pop("disabled", None)
    base = next_base(root, assignments, pid)
    if program["status"] == "architect" and not program.get("architect"):
        again, fix = program.get("rearchitect"), program.get("architect_fix")
        role, fields = "ARCHITECT", {}
        if again:
            role += "+REARCHITECT"
            fields = {"lane": again["lane"], "dir": f"{LANES_DIR}/{again['lane']}",
                      "findings": rearchitect_reason(program) or "- none recorded"}
        if fix:  # continue on the rejected architect's files with the validator's reason
            overlay = seed_overlay(root, pid, Path(fix["job"]))
            role += "+ARCHITECT_FIX"
            fields["reason"] = fix["reason"]
        elif again:
            keep = {l: i for l, i in program["lanes"].items() if i.get("done")}
            keep[again["lane"]] = {"done_job": again["job"]}
            overlay = merged_overlay(root, pid, Path(program["base_job"]), keep, notes_of=again["lane"])
        else:
            overlay = seed_overlay(root, pid, Path(program["seed_job"]))
        spec = dict(target, overlay_dir=overlay, max_rounds=1, max_nudges=0)
        key = ("architect", None)
    elif program["status"] == "lanes" and (program.get("split") or {}).get("lane") and not program["split"].get("sub"):
        split = program["split"]  # operator: split one running lane while the others keep working
        if split.get("fix_job"):
            overlay = seed_overlay(root, pid, Path(split["fix_job"]))
        else:
            keep = {l: i for l, i in program["lanes"].items() if i.get("done")}
            keep[split["lane"]] = {"done_job": split["from_job"]}
            overlay = merged_overlay(root, pid, Path(program["base_job"]), keep, notes_of=split["lane"])
        spec = dict(target, overlay_dir=overlay, max_rounds=1, max_nudges=0)
        base = f"{base}-split{split['lane']}"
        role = "ARCHITECT+SPLIT" + ("+ARCHITECT_FIX" if split.get("fix_job") else "")
        fields = {"lane": split["lane"], "dir": f"{LANES_DIR}/{split['lane']}",
                  "reason": str(split.get("fix_reason") or split.get("reason") or "operator")[:1500]}
        key = ("split", None)
    elif program["status"] == "lanes":
        lanes = program["lanes"]
        if any(i.get("claim") and not i["done"] and i["claim"]["status"] in LANE_FAILED
               for i in lanes.values()):
            return None  # a re-architecture is pending; new lanes would be wasted
        lane = next((l for l, i in lanes.items() if not i["done"] and not i.get("claim")), None)
        if lane is None:
            return None
        resume = lanes[lane].get("resume_job")  # operator: a stopped lane continues on its own files
        # A lane round starts from the base plus every finished lane, so shared tools and earlier results are
        # importable instead of re-derived inside the lane (2026-10-30: lanes seeded from the base alone
        # re-proved the same helpers; 171 names were defined in more than one lane).
        merge = {l: {"done_job": i["done_job"]} for l, i in lanes.items()
                 if l != lane and i.get("done") and i.get("done_job")}
        if resume:
            merge[lane] = {"done_job": resume}
        overlay = (merged_overlay(root, pid, Path(program["base_job"]), merge, notes_of=lane if resume else None)
                   if merge else seed_overlay(root, pid, Path(program["base_job"])))
        spec = dict(target, overlay_dir=overlay,
                    research_frontier={"schema_version": 1, "lane_kind": "research",
                                       "owner": f"{pid}:{lane}",
                                       "edit_scope": [f"{LANES_DIR}/{lane}/**/*.lean"]})
        base = f"{base}-lane{lane}"
        role, fields, key = "LANE", {"lane": lane, "dir": f"{LANES_DIR}/{lane}"}, ("lanes", lane)
    elif program["status"] == "integrating" and not program.get("integrator"):
        spec = dict(target, overlay_dir=merged_overlay(root, pid, Path(program["base_job"]), program["lanes"]),
                    max_rounds=2)
        role, fields, key = "INTEGRATOR", {}, ("integrator", None)
    else:
        return None
    assignment = make_assignment(spec, base, sync_brief(root, base, pid, role, fields), defaults)
    sub = {"runner": str(root), "base": base, "status": "running", "claimed_at": iso(), "role": role}
    if key[0] in ("architect", "split"):
        sub["contract"] = ARCHITECT_CONTRACT  # validated against the brief it was given
    if key[0] == "split":
        program["split"]["sub"] = sub
    elif key[0] == "lanes":
        sub["scope"] = f"{LANES_DIR}/{key[1]}"  # the progress gate measures the lane only
        program["lanes"][key[1]]["claim"] = sub
    else:
        program[key[0]] = sub
    return assignment


def program_subs(program: dict[str, Any]) -> list[dict[str, Any]]:
    subs = [program.get("architect"), program.get("integrator"), (program.get("split") or {}).get("sub")]
    subs += [lane.get("claim") for lane in (program.get("lanes") or {}).values()]
    return [sub for sub in subs if sub]


PROGRAM_FINAL = ("passed", "failed", "stopped")


def program_active(pid: str) -> bool:
    """One program per problem on a host (cross-host dedup is the coordinator's job, P1)."""
    path = OWN / "programs" / f"{pid}.json"
    return path.exists() and (read_json(path, {}) or {}).get("status") not in PROGRAM_FINAL


def start_program(pid: str, seed_job: Path, claims: dict[str, Any]) -> Path:
    """Write programs/<pid>.json seeded with `seed_job` and turn the claim into a program."""
    path = OWN / "programs" / f"{pid}.json"
    if program_active(pid):
        raise ValueError(f"program {pid} already active")
    path.parent.mkdir(exist_ok=True)
    status = "architect" if (seed_job / "result.json").is_file() else "waiting_seed"
    write_json(path, {"problem_id": pid, "status": status, "seed_job": str(seed_job),
                      "created_at": iso(), "lanes": {}})
    claims[pid] = {"status": "program", "program": str(path), "previous_claim": claims.get(pid)}
    log(f"program {pid} created from {seed_job} ({status})")
    return path


def create_program(pid: str, seed: str) -> Path:
    """Operator: start a lane program for PID seeded with the proof files of job SEED."""
    seed_job = Path(seed) if seed.startswith("/") else CAMPAIGNS / seed.split("/", 1)[0] / "jobs" / seed.split("/", 1)[1]
    if not (seed_job / "workspace").is_dir():
        raise SystemExit(f"seed job {seed_job} has no workspace")
    claims = read_json(OWN / "claims.json", {}) or {}
    try:
        path = start_program(pid, seed_job, claims)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    write_json(OWN / "claims.json", claims)
    return path


def classify(info: dict[str, Any]) -> str | None:
    """Final outcome of a claim lineage, or None while a round is pending/running."""
    if info["passed"]:
        return "passed"
    if not info["names"] or info["pending"]:
        return None
    last = info["entries"][-1][1] if info["entries"] else {}
    if last.get("phase") == "failed":
        return "failed"
    return "exhausted"


def make_assignment(target: dict[str, Any], base: str, brief_rel: str | None,
                    defaults: dict[str, Any]) -> dict[str, Any]:
    pid = target["problem_id"]
    if not SAFE_ID.fullmatch(pid) or not SAFE_ID.fullmatch(base):
        raise ValueError("unsafe problem or attempt id")
    kind = target.get("kind", "fresh")
    a: dict[str, Any] = {
        "attempt_id": base, "problem_id": pid, "kind": kind,
        "verify_policy": target.get("verify_policy", defaults["verify_policy"]),
        "wall_seconds": int(target.get("wall_seconds", defaults["wall_seconds"])),
        "token_limit": int(target.get("token_limit", defaults["token_limit"])),
        "verification_seconds": int(target.get("verification_seconds",
                                               defaults["verification_seconds"])),
        "worker_timeout_seconds": int(target.get("worker_timeout_seconds",
                                                 defaults["worker_timeout_seconds"])),
        "max_nudges": int(target.get("max_nudges", defaults["max_nudges"])),
        "auto_continue": {"base": base, "round": 1,
                          "max_rounds": int(target.get("max_rounds", defaults["max_rounds"]))},
        "origin": "fleet feeder",
        # the lineage's role, for `leh evolve` (the controller ignores it); restart_assignment sets others
        "lineage_role": "FRESH",
    }
    if brief_rel:
        a["brief_file"] = brief_rel
    if kind == "bundle":
        if not str(target.get("bundle_dir", "")).startswith("/"):
            raise ValueError("bundle target needs an absolute bundle_dir")
        a["bundle_dir"] = target["bundle_dir"]
    elif kind == "continuation":
        a["source"] = target["source"]  # root-relative dir containing snapshot/
    elif kind != "fresh":
        raise ValueError(f"unknown kind {kind}")
    if target.get("overlay_dir"):
        a["overlay_dir"] = target["overlay_dir"]
    if target.get("research_frontier"):
        a["research_frontier"] = target["research_frontier"]
    return a


def restart_assignment(root: Path, pid: str, target: dict[str, Any], source: Path, base: str,
                       defaults: dict[str, Any], role: str | None = None,
                       fields: dict[str, str] | None = None) -> dict[str, Any]:
    """A fresh lineage of `target` seeded with the proof files and NOTES of job `source`."""
    spec = dict(target, kind="fresh", overlay_dir=seed_overlay(root, pid, source))
    spec.pop("disabled", None)
    spec.pop("seed_dir", None)
    assignment = make_assignment(spec, base, sync_brief(root, base, pid, role, fields), defaults)
    assignment["lineage_role"] = role or "FRESH"
    return assignment


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        return ""  # a brief rendered without its fields (e.g. an old claim) must not crash a tick


# Roles whose brief stays in every round of the lineage, not only the first.
PERSISTENT_ROLES = ("CI_FIT",)

# Roles that choose a route get the shared tactic library (briefs/TACTICS.md).
ROUTE_ROLES = ("ROUTE_REVIEW", "ARCHITECT", "PARALLEL")


def brief_text(pid: str, role: str | None = None, **fields: str) -> str | None:
    """COMMON.md, the problem brief, each role of `role` ("ARCHITECT+REARCHITECT"), and the
    tactic library for route-choosing roles."""
    parts = []
    common = OWN / "briefs" / "COMMON.md"
    specific = OWN / "briefs" / f"{pid}.md"
    for path in (common, specific):
        if path.is_file():
            parts.append(path.read_text().strip())
    names = [name for name in (role or "").split("+") if name]
    for name in names:
        parts.append((OWN / "briefs" / "roles" / f"{name}.md").read_text().strip().format_map(_Fields(fields)).strip())
    library = OWN / "briefs" / "TACTICS.md"
    if library.is_file() and any(name in ROUTE_ROLES for name in names):
        parts.append(library.read_text().strip())
    return "\n\n".join(parts) + "\n" if parts else None


# Roles whose brief names what a parallel lineage of the problem already follows.
PARALLEL_ROLES = ("ROUTE_REVIEW", "RESUME", "PARALLEL")


def target_of(pid: str) -> dict[str, Any]:
    targets = (read_json(OWN / "targets.json", {}) or {}).get("targets", [])
    return next((t for t in targets if t.get("problem_id") == pid), {})


def parallel_text(target: dict[str, Any]) -> str:
    """The `{parallel}` block: a target's `parallel_of` (what the main lineage follows) and
    `avoid_tactics`, so a second lineage of the problem diverges instead of repeating it."""
    main = str(target.get("parallel_of") or "").strip()
    avoid = [str(t).strip() for t in target.get("avoid_tactics") or [] if str(t).strip()]
    if not main and not avoid:
        return ""
    parts = ["Parallel lineage:"]
    if main:
        parts.append(f"a sibling lineage is already following: {main};")
    if avoid:
        parts.append("do not use these tactics: " + "; ".join(avoid) + ";")
    parts.append("choose a different tactic.")
    return " ".join(parts)


def sync_brief(root: Path, base: str, pid: str, role: str | None = None,
               fields: dict[str, str] | None = None) -> str | None:
    fields = dict(fields or {})
    if "parallel" not in fields and any(name in PARALLEL_ROLES for name in (role or "").split("+")):
        fields["parallel"] = parallel_text(target_of(pid))
    text = brief_text(pid, role, **fields)
    if text is None:
        return None
    rel = f"briefs/{base}.md"
    path = root / rel
    path.parent.mkdir(exist_ok=True)
    if not path.exists() or path.read_text() != text:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    return rel


@contextlib.contextmanager
def manifest_lock(root: Path) -> Iterator[None]:
    """Same lock file as the controller uses for manifest read-modify-write."""
    fd = os.open(root / "manifest.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def append_assignment(root: Path, assignment: dict[str, Any]) -> None:
    with manifest_lock(root):
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("requested_config") != POLICY or manifest.get("host") != HOST:
            raise ValueError("queue manifest policy/host mismatch")
        if any(a["attempt_id"] == assignment["attempt_id"] for a in manifest["assignments"]):
            raise ValueError("attempt id already present")
        manifest["assignments"].append(assignment)
        tmp = root / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.replace(root / "manifest.json")


RUNTIME_LINE = re.compile(r'^RUNTIME = Path\("([^"]+)"\)', re.MULTILINE)


def ensure_runtime(root: Path, wanted: Path, alerts: list[str], busy: bool = False) -> bool:
    """Point the queue controller at the wanted runtime; True if it was changed.

    Applied only at a lineage boundary: while the runner has a pending assignment or a running
    claim/program role (`busy`), the switch is deferred (a restart mid-lineage changed the
    runtime between rounds of one lineage). The idle runner's controller is restarted before it
    gets new work. This also re-applies the pin after fleet upgrade tooling rewrites controller.py.
    """
    path = root / "controller.py"
    text = path.read_text()
    match = RUNTIME_LINE.search(text)
    if not match:
        alerts.append(f"{root.name}: no RUNTIME line in controller.py")
        return False
    if Path(match.group(1)) == wanted:
        return False
    if not (wanted / "runtime-manifest.json").is_file() or not (wanted / ".venv").exists():
        alerts.append(f"{root.name}: wanted runtime {wanted} is not deployed")
        return False
    if busy:
        alerts.append(f"{root.name}: runtime switch deferred to lineage boundary "
                      f"({match.group(1)} -> {wanted})")
        return False
    tmp = path.with_suffix(".py.tmp")
    tmp.write_text(text[:match.start(1)] + str(wanted) + text[match.end(1):])
    os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)
    log(f"{root.name}: runtime {match.group(1)} -> {wanted}")
    if controller_pids(root):
        wave = root.name.rsplit("-", 1)[-1]
        subprocess.run(["sudo", "-n", "systemctl", "restart", f"leh-runner@{wave}.service"],
                       check=False, timeout=60)
    return True


def credential_halts(t: "Tick") -> None:
    """An account whose runner halted for credential access may be exposed: block it for new work and give
    every other runner of this host on that account a controlled STOP (after its current job)."""
    exposed = {account_of(root, t.cfg) for root in t.runners
               if (t.views[root]["state"] or {}).get("status") == "halted_credential_access"}
    for account in sorted(exposed):
        t.blocks[account] = {"window": "credential_access", "until": t.now + 10 * 365 * 86400}
        for root in t.runners:
            if account_of(root, t.cfg) == account and not (root / "STOP").exists():
                (root / "STOP").write_text(f"credential_access on account {account}: rotate it, then remove "
                                           "this file and restart the runner\n")
                t.alerts.append(f"{root.name}: STOP, account {account} may be exposed (credential tripwire)")
                log(f"stop {root.name}: account {account} may be exposed")


def ensure_running(root: Path, state: dict[str, Any], alerts: list[str]) -> str:
    if (root / "STOP").exists():
        return "stopped(STOP file)"
    if controller_pids(root):
        return "running"
    status = state.get("status")
    restarts_path = OWN / "restarts.json"
    restarts = read_json(restarts_path, {}) or {}
    mine = [t for t in restarts.get(root.name, []) if t > time.time() - 86400]
    if status == "halted_credential_access":
        alerts.append(f"{root.name}: the agent reached for the account credentials (job SECURITY-EVENT.json); "
                      f"rotate the account, then restart leh-runner@{root.name.rsplit('-', 1)[-1]} by hand")
        return f"halted({status}); waiting for operator"
    if status in HALTED:
        cooldown = 7200 if status == "halted_authentication_failure" else 1800
        if mine and time.time() - mine[-1] < cooldown:
            alerts.append(f"{root.name}: {status}; restart cooldown")
            return f"halted({status}); cooldown"
        if len(mine) >= 8:
            alerts.append(f"{root.name}: {status}; 8 restarts in 24h, waiting for operator")
            return f"halted({status}); restart budget used"
    result = subprocess.run(["bash", str(root / "start.sh")], capture_output=True, text=True,
                            timeout=60)
    mine.append(time.time())
    restarts[root.name] = mine
    write_json(restarts_path, restarts)
    log(f"start {root.name} (previous status {status}): rc={result.returncode} "
        f"{(result.stdout + result.stderr).strip()[:200]}")
    return f"restarted(rc={result.returncode})"


# --------------------------------------------------------------------------- leaderboard

def our_solved(alerts: list[str]) -> set[str] | None:
    """Problems the public leaderboard credits to config.json "leaderboard_model" (our model id
    there); None (no filter) when no model is configured or the board is unavailable."""
    model = (read_json(OWN / "config.json", {}) or {}).get("leaderboard_model")
    if not model:
        return None
    cache = OWN / "leaderboard-cache.json"
    data = read_json(cache)
    if not data or data.get("model") != model or time.time() - data.get("fetched_at", 0) > 3600:
        try:
            with urllib.request.urlopen(LEADERBOARD, timeout=30) as response:
                board = json.loads(response.read())
            solved = sorted({c["problem_id"] for c in board["credits"]
                             if c["canonical_model_id"] == model})
            data = {"fetched_at": time.time(), "generated_at": board.get("generated_at"),
                    "model": model, "our_solved": solved}
            write_json(cache, data)
        except Exception as exc:  # noqa: BLE001 - board is advisory
            alerts.append(f"leaderboard fetch failed: {type(exc).__name__}")
            if not data:
                return None
    return set(data["our_solved"])


# --------------------------------------------------------------------------- packaging

def git_show(commit: str, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(BENCH_REPO), "show", f"{commit}:{path}"])


def official_log_stats(text: str) -> dict[str, Any]:
    built = re.findall(r"Built (Submission(?:\.[^\s]+)?) \(([\d.]+)(m?s)\)", text)
    seconds = []
    for name, value, unit in built:
        v = float(value)
        seconds.append((name, v / 1000 if unit == "ms" else v))
    total = sum(s for _, s in seconds)
    slowest = sorted(seconds, key=lambda x: -x[1])[:5]
    return {"submission_modules_built": len(seconds), "submission_build_seconds_sum": round(total, 1),
            "slowest_modules": [[n, round(s, 1)] for n, s in slowest],
            "markers": [m for m in MARKERS if m in text]}


# The LeanEval CI since 2026-10-22 (lean-eval-submissions #2161/#2162, lean-eval #665): one 360-minute job
# on a Namespace 16 CPU / 32 GB runner, about 6 min of setup, then evaluate_submission.py runs `lake test`
# (build, the toolchain's `lake comparator`: export, NanoDa, Lean checker). Calibration, the two accepted
# Poincaré packages: Submission module build times summed to 78,624 s and 86,071 s on a 16-CPU host, and
# the CI evaluate step took 227 and 226 min, i.e. 2.6-2.8 x (sum / 16 CPUs): the parallel build is ~76 %
# efficient and the comparator phase costs about as much again. CI_FACTOR 2.8 predicts 235 and 257 min.
# The old 4-vCPU / 15 GiB model (factor 1.6 per module, 62 + 30 min of preamble) held both packages at
# 466-673 min although they fit (2026-10-23).
CI = {"runner_cpus": 16, "job_minutes": 360, "fit_minutes": 330, "preamble_minutes": 6, "factor": 2.8,
      # the submission archive (Contents API): 46.8 MB failed with 422 (#2056), 29.5 MB went through (#1994)
      "archive_bytes": 29_500_000}
DIAGNOSTIC_OPTION = re.compile(rb"^[ \t]*set_option[ \t]+(?:profiler|trace\.)", re.M)


def ci_gate(stats: dict[str, Any], archive_bytes: int, files: dict[str, bytes] | None = None) -> dict[str, Any]:
    """LeanEval CI fit from local per-module timings (CI above). `notes` never block the package."""
    build_min = stats["submission_build_seconds_sum"] / CI["runner_cpus"] / 60
    worst = max((s for _, s in stats["slowest_modules"]), default=0.0)
    predicted = CI["preamble_minutes"] + CI["factor"] * max(build_min, worst / 60)
    reasons, notes = [], []
    if archive_bytes > CI["archive_bytes"]:
        reasons.append(f"archive {archive_bytes / 1e6:.1f} MB > {CI['archive_bytes'] / 1e6:.1f} MB")
    if predicted > CI["fit_minutes"]:
        reasons.append(f"predicted CI {predicted:.0f} min > {CI['fit_minutes']} (job limit {CI['job_minutes']})")
    diagnostic = sorted(n for n, data in (files or {}).items() if DIAGNOSTIC_OPTION.search(data))
    if diagnostic:
        notes.append(f"{len(diagnostic)} proof files set profiler/trace options (e.g. {diagnostic[0]}); "
                     f"they only add log volume and time to the official run")
    return {"predicted_ci_minutes": round(predicted), "ci_host_factor": CI["factor"],
            "ci_runner": f"{CI['runner_cpus']} CPU, {CI['job_minutes']} min", "archive_bytes": archive_bytes,
            "fit": not reasons, "reasons": reasons, "notes": notes}


def pass_runtime(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The runtime that produced a pass: `runtime_id` of the runtime-manifest.json of the runtime
    dir the job ran with (the job's own copy if it has one, else <job>/worker.json
    `remote_agent`); the job is the history entry with the PASS file's receipt and digest. Falls
    back to the queue's controller pin at packaging time, and says which source it used."""
    history = (read_json(root / "state.json", {}) or {}).get("history", {})
    digest = (entry.get("verification") or {}).get("artifact_digest")
    attempt = next((name for name, e in history.items() if isinstance(e, dict)
                    and e.get("receipt_run_id") == entry.get("receipt_run_id")
                    and (e.get("verification") or {}).get("artifact_digest") == digest), None)
    manifest, runtime_dir, source = None, None, None
    job = root / "jobs" / attempt if attempt and SAFE_ID.fullmatch(attempt) else None
    if job and (job / "runtime-manifest.json").is_file():
        manifest, source = read_json(job / "runtime-manifest.json"), "job runtime-manifest.json"
    elif job:
        agent = (read_json(job / "worker.json", {}) or {}).get("remote_agent")
        if agent:
            runtime_dir, source = Path(agent), "runtime of the job (worker.json remote_agent)"
    if manifest is None and runtime_dir is None:
        try:
            match = RUNTIME_LINE.search((root / "controller.py").read_text())
        except OSError:
            match = None
        if match:
            runtime_dir, source = Path(match.group(1)), "queue controller pin at packaging time"
    if manifest is None and runtime_dir is not None:
        manifest = read_json(runtime_dir / "runtime-manifest.json")
    runtime_id = (manifest or {}).get("runtime_id") if isinstance(manifest, dict) else None
    return {"runtime_id": runtime_id, "runtime_dir": str(runtime_dir) if runtime_dir else None,
            "source": source if runtime_id else None, "attempt": attempt}


def package_pass(root: Path, pass_path: Path) -> dict[str, Any]:
    entry = json.loads(pass_path.read_text())
    pid = entry["problem_id"]
    verification = entry["verification"]
    if verification.get("status") != "official_local_pass" or not verification.get("verified"):
        raise ValueError("PASS file without verified official_local_pass")
    digest = verification["artifact_digest"]
    out = OWN / "outbox" / pid / digest[:16]
    if (out / "package.json").exists():
        return json.loads((out / "package.json").read_text())
    receipt_dir = RUNS / entry["receipt_run_id"]
    receipt = json.loads((receipt_dir / "receipt.json").read_text())
    if (receipt["status"] != "official_local_pass" or receipt["artifact_digest"] != digest
            or receipt.get("service_acceptance") is not False):
        raise ValueError("receipt does not match the PASS entry")
    checks = receipt.get("checks") or {}
    if not (checks.get("official_lake_test") == 0 and checks.get("both_kernels_reported")
            and checks.get("inputs_unchanged")):
        raise ValueError(f"receipt checks not all passing: {checks}")
    official = (receipt_dir / "official.log").read_bytes()
    if sha256_bytes(official) != receipt["logs"]["official.log"]:
        raise ValueError("official.log hash mismatch")
    stats = official_log_stats(official.decode(errors="replace"))
    if len(stats["markers"]) != len(MARKERS):
        raise ValueError("official.log lacks a kernel acceptance marker")
    bundle = Path(verification["bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest["files"] != receipt["artifact"]["files"] or manifest["problem_id"] != pid:
        raise ValueError("bundle manifest differs from the receipt artifact")
    files: dict[str, bytes] = {}
    for name, expected in manifest["files"].items():
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not (
                name == "Submission.lean" or (p.parts[0] == "Submission" and p.suffix == ".lean")):
            raise ValueError(f"unexpected proof path {name}")
        data = (bundle / "proof" / name).read_bytes()
        if sha256_bytes(data) != expected:
            raise ValueError(f"proof file hash mismatch {name}")
        files[name] = data
    bench = receipt["artifact"]["benchmark_commit"]
    files["lakefile.toml"] = git_show(bench, f"generated/{pid}/lakefile.toml")
    files["lean-toolchain"] = git_show(bench, f"generated/{pid}/lean-toolchain")
    apache = git_show(bench, "LICENSE")
    files["LICENSE"] = apache
    files["provenance/LEAN-EVAL-LICENSE"] = apache

    archive = io.BytesIO()
    with gzip.GzipFile(fileobj=archive, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in sorted(n for n in files if n.startswith("Submission")):
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                tar.addfile(info, io.BytesIO(files[name]))
    gate = ci_gate(stats, len(archive.getvalue()), files)

    model = entry.get("model") or {}
    root_manifest = read_json(root / "manifest.json", {}) or {}
    lineage_names = [a["attempt_id"] for a in root_manifest.get("assignments", [])
                     if a.get("problem_id") == pid]
    sources_digest = sha256_bytes(json.dumps(manifest["files"], sort_keys=True).encode())
    notices = {}
    for name, data in files.items():
        if not name.startswith("Submission"):
            continue
        for line in data.decode(errors="replace").splitlines():
            if re.search(r"SPDX-License-Identifier|Apache License|MIT License|Copyright \(c\)|"
                         r"Copyright \d{4}|Released under", line):
                notices[name] = line.strip()[:200]
                break
    runtime = pass_runtime(root, entry)
    contributors = [str(c) for c in (read_json(OWN / "config.json", {}) or {}).get("contributors") or []]
    attempt = runtime.get("attempt")
    reuse = reuse_notes(root / "jobs" / attempt / "workspace") if attempt and SAFE_ID.fullmatch(attempt) else ""
    model_name = str(entry.get("effective_model") or POLICY["model"])
    provenance = {
        "problem_id": pid, "statement_revision": manifest["statement_revision"],
        "benchmark_commit": bench,
        "system": f"{model_name} via Claude Code CLI (fleet runner, campaign runtime "
                  f"{runtime['runtime_id'] or 'not recorded'})",
        "runtime": runtime,
        "backend_model": f"{entry.get('effective_model')} / {entry.get('effective_effort')}",
        "contributors": contributors,
        "kind": "campaign_proof",
        "campaign_root": str(root), "attempts": lineage_names,
        "final_attempt_tokens": model.get("observed_tokens"),
        "final_attempt_elapsed_seconds": model.get("elapsed_seconds"),
        "source_commit": manifest["source_commit"],
        "our_role": f"proof written by {model_name} agents in unattended, sandboxed campaign jobs; "
                    "adapted third-party sources are declared below (license notices in the files and the "
                    "REUSE section of the agents' notes)",
        "publication": "private, withheld; no current publication plan",
        "third_party_license_notices": notices,
        "declared_reuse": ("files above carry third-party license notices; adapted under those "
                           "licenses with attribution kept in the files" if notices else "none"),
        "reuse_notes": reuse or None,
    }
    verification_json = {
        "problem_id": pid, "statement_revision": manifest["statement_revision"],
        "benchmark_commit": bench, "status": "official_local_pass", "accepted": True,
        "nanoda_accepted": True, "lean_kernel_accepted": True,
        "official_lake_test_exit_code": 0, "markers": list(MARKERS),
        "receipt_run_id": receipt["run_id"], "artifact_digest": digest,
        "verification_wall_seconds": verification.get("verification_wall_seconds"),
        "official_log_sha256": receipt["logs"]["official.log"],
        "verifier_pins": (receipt.get("verifier") or {}).get("pins"),
        "verifier_binaries_sha256": (receipt.get("verifier") or {}).get("binaries"),
        "submission_sources_sha256": sources_digest,
        "submission_files_sha256": manifest["files"],
        "official_log_stats": stats, "ci_fit_estimate": gate,
        "note": "Fresh source-only local verification (lake test: comparator with nanoda and the "
                "Lean kernel) of exactly these sources in a pristine benchmark workspace by the "
                "unmodified campaign verifier. Local verification is distinct from organizer "
                "acceptance.",
    }
    readme = (
        f"# {pid} — private LeanEval submission\n\n"
        f"Problem: `{pid}`, statement revision {manifest['statement_revision']}.\n"
        f"Model/system: Claude Opus 5.5 via Claude Code CLI (campaign runtime claude-opus-5-5 / "
        f"xhigh, unattended fleet runner).\n"
        + (f"Contributor tags: {' '.join(contributors)}.\n" if contributors else "") + "\n" +
        f"The proof was written by Claude Opus 5.5 agents in a sandboxed campaign job "
        f"({', '.join(lineage_names) or 'see PROVENANCE.json'}). "
        + (f"{len(notices)} file(s) carry third-party license notices (adapted licensed sources, "
           f"attribution kept in the files; listed in PROVENANCE.json).\n\n" if notices else
           "No third-party license notices were found in the submitted sources.\n\n") +
        f"Fresh source-only official local verification passed both Nanoda and the Lean kernel with "
        f"the unmodified LeanEval comparator against benchmark commit `{bench}`, Lean "
        f"`v4.35.0-rc3` and pinned Mathlib (receipt `{receipt['run_id']}`, artifact `{digest}`). "
        f"`VERIFICATION.json` lists all {len(manifest['files'])} accepted source hashes and a "
        f"conservative CI-fit estimate. Local verification is distinct from organizer acceptance "
        f"and leaderboard credit.\n\n"
        f"The root `lakefile.toml` and `lean-toolchain` are copied unchanged from the pinned "
        f"benchmark workspace for server workspace discovery; the server overlays only "
        f"`Submission.lean` and `Submission/**/*.lean`. The benchmark license is retained in "
        f"`provenance/LEAN-EVAL-LICENSE`.\n\n"
        f"Publication status: Private, with no current publication plan.\n")
    files["README.md"] = readme.encode()
    files["PROVENANCE.json"] = (json.dumps(provenance, indent=1, ensure_ascii=False) + "\n").encode()
    files["VERIFICATION.json"] = (json.dumps(verification_json, indent=1) + "\n").encode()

    staging = out.with_name(out.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    for name, data in files.items():
        target = staging / "tree" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    package = {
        "problem_id": pid, "artifact_digest": digest, "receipt_run_id": receipt["run_id"],
        "branch": f"claude/{pid}-private", "created_at": iso(), "queue_root": str(root),
        "pass_file": str(pass_path), "files": {n: sha256_bytes(d) for n, d in sorted(files.items())},
        "ci_gate": gate, "status": "ready" if gate["fit"] else "hold_ci_review",
        "commit_message": (
            f"Package {pid} for private LeanEval submission\n\n"
            f"Fresh official local verification (comparator, Lean kernel, Nanoda) of the exact\n"
            f"sources produced by the fleet's Claude Opus 5.5 runner; see VERIFICATION.json.\n\n"
            f"Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"),
    }
    write_json(staging / "package.json", package)
    os.replace(staging, out)
    log(f"packaged {pid} {digest[:16]} status={package['status']} gate={gate}")
    return package


DIGEST = re.compile(r"[0-9a-f]{16,128}")


def keep_passes(root: Path, alerts: list[str]) -> list[Path]:
    """Copy each <root>/PASS-<pid>.json to <root>/passes/<pid>-<digest16>.json on first sight (the
    controller overwrites PASS-<pid>.json with a later pass of the problem); the kept copies are
    what gets packaged."""
    folder = root / "passes"
    for path in sorted(root.glob("PASS-*.json")):
        entry = read_json(path)
        pid = entry.get("problem_id") if isinstance(entry, dict) else None
        digest = str(((entry or {}).get("verification") or {}).get("artifact_digest") or "") if pid else ""
        if not pid or not SAFE_ID.fullmatch(str(pid)) or not DIGEST.fullmatch(digest):
            alerts.append(f"{root.name}/{path.name}: unreadable PASS file (no problem id or artifact digest)")
            continue
        kept = folder / f"{pid}-{digest[:16]}.json"
        if kept.exists():
            continue
        folder.mkdir(exist_ok=True)
        tmp = folder / f".{kept.name}.{os.getpid()}.tmp"
        shutil.copyfile(path, tmp)
        os.replace(tmp, kept)
        log(f"{root.name}: kept {path.name} as passes/{kept.name}")
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


# --------------------------------------------------------------------------- quota and host

# Phases of <job>/checkpoint.json while the runtime waits inside the session for a quota reset.
PAUSE_PHASES = ("usage_limit_pause", "coordinator_reserve_pause")
PROC = Path("/proc")


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# run_controller.sh uses this account when config.json "tokens" has no entry for the runner.
DEFAULT_TOKEN = "file:~/.config/leh/accounts/default.token"


def account_label(ref: str) -> str:
    """The account label of a secret reference, as the runtime computes it (leaneval.secret_refs):
    the file name without a ``.token`` suffix, or the variable name of an env: reference."""
    if ref.startswith("env:"):
        return ref[4:]
    name = Path(ref[5:] if ref.startswith("file:") else ref).name
    return name[:-len(".token")] if name.endswith(".token") and len(name) > len(".token") else name


def account_of(root: Path, cfg: dict[str, Any]) -> str:
    """The subscription a runner uses: the label of its secret reference in config.json "tokens",
    as run_controller.sh picks it."""
    return account_label(str((cfg.get("tokens") or {}).get(root.name.rsplit("-", 1)[-1]) or DEFAULT_TOKEN))


def pause_window(rate_limit: dict[str, Any], pause: dict[str, Any]) -> str:
    """five_hour / seven_day (or another rateLimitType) / reserve / unknown, from the CLI's
    rate_limit_info (status, rateLimitType, resetsAt, unifiedWindows) and the pause itself."""
    if pause.get("reason") == "coordinator_reserve_pause":
        return "reserve"
    if rate_limit.get("status") == "rejected" and rate_limit.get("rateLimitType"):
        return str(rate_limit["rateLimitType"])
    windows = rate_limit.get("unifiedWindows")
    full = [name for name, w in (windows.items() if isinstance(windows, dict) else [])
            if isinstance(w, dict) and (number(w.get("utilization")) or 0) >= 1]
    if full:
        return next((name for name in full if str(name).startswith("seven_day")), str(full[0]))
    started, until = number(pause.get("started_at")), number(pause.get("until"))
    if started and until:
        return "seven_day" if until - started > 5.5 * 3600 else "five_hour"
    return "unknown"


def runner_quota(root: Path, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Whether the runner's current job waits in a usage-limit pause. Only <job>/checkpoint.json
    shows it (phase usage_limit_pause, an open last pause with `until`); state.json keeps saying
    model_running. `active_since`: start of the running model call, when one runs. `account`: the
    account of the job's last model call (the runtime records it per invocation), which is the one
    that hit the limit even when config.json already names another (2026-10-19: runners switched to
    a fresh account mid-pause blocked the fresh account for 2.5 h)."""
    quota: dict[str, Any] = {"paused": False, "window": None, "until": 0.0, "since": None,
                             "active_since": None, "account": None}
    job = state.get("current_job")
    if not job or state.get("status") != "model_running":
        return quota
    checkpoint = read_json(Path(job) / "checkpoint.json", {})
    if not isinstance(checkpoint, dict):
        return quota
    invocations = [i for i in checkpoint.get("invocations") or [] if isinstance(i, dict)]
    if invocations and isinstance(invocations[-1].get("account"), str):
        quota["account"] = invocations[-1]["account"]
    if checkpoint.get("phase") == "model_running":
        starts = [number(i.get("started_at")) for i in invocations]
        starts = [s for s in starts if s]
        quota["active_since"] = max(starts) if starts else None
    pauses = [p for p in checkpoint.get("pauses") or [] if isinstance(p, dict)]
    pause = pauses[-1] if pauses else {}
    if checkpoint.get("phase") not in PAUSE_PHASES or not pause or pause.get("ended_at"):
        return quota
    rate_limit = checkpoint.get("rate_limit") if isinstance(checkpoint.get("rate_limit"), dict) else {}
    until = number(pause.get("until")) or number(rate_limit.get("resetsAt")) or now + 900
    if until > now:
        quota.update(paused=True, window=pause_window(rate_limit, pause), until=until,
                     since=number(pause.get("started_at")))
    return quota


def account_blocks(views: dict[Path, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    """Per account, the latest `until` of a usage-limit pause on any of its runners. Kept in
    account-blocks.json until it passes (a handed-off runner no longer shows the pause); dropped
    early once a runner of the account runs a model call started after the block was seen."""
    saved = read_json(OWN / "account-blocks.json", {}) or {}
    blocks = {a: b for a, b in saved.items() if isinstance(b, dict) and (number(b.get("until")) or 0) > now}
    for view in views.values():
        quota = view["quota"]
        account = (quota.get("account") if quota["paused"] else None) or view["account"]
        block = blocks.get(account)
        if quota["paused"]:
            if not block or quota["until"] > block["until"]:
                blocks[account] = {"until": quota["until"], "window": quota["window"],
                                   "runner": view["root"].name, "observed_at": now}
        elif block and quota["active_since"] and quota["active_since"] > (number(block.get("observed_at")) or now):
            blocks.pop(account)
    if blocks != saved:
        write_json(OWN / "account-blocks.json", blocks)
    return blocks


def host_metrics() -> dict[str, Any]:
    """MemAvailable, load average and memory pressure (PSI) of this host; None where unreadable."""
    metrics: dict[str, Any] = {"mem_available_gb": None, "mem_total_gb": None, "loadavg": None,
                               "memory_some_avg60": None, "memory_full_avg60": None}
    try:
        info = {}
        for line in (PROC / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if rest.split():
                info[key] = float(rest.split()[0]) / 2**20  # kB -> GB
        metrics.update(mem_available_gb=round(info["MemAvailable"], 1), mem_total_gb=round(info["MemTotal"], 1))
    except (OSError, ValueError, KeyError):
        pass
    with contextlib.suppress(OSError, AttributeError):
        metrics["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    try:
        for line in (PROC / "pressure" / "memory").read_text().splitlines():
            kind, *fields = line.split()
            values = dict(field.split("=", 1) for field in fields)
            metrics[f"memory_{kind}_avg60"] = float(values["avg60"])
    except (OSError, ValueError, KeyError):
        pass
    return metrics


def admission_reasons(cfg: dict[str, Any], metrics: dict[str, Any]) -> list[str]:
    """Why this host takes no new work now (min_mem_available_gb, max_memory_pressure_avg60)."""
    reasons = []
    floor = float(cfg.get("min_mem_available_gb") or 0)
    if floor and metrics["mem_available_gb"] is not None and metrics["mem_available_gb"] < floor:
        reasons.append(f"MemAvailable {metrics['mem_available_gb']} GB below min_mem_available_gb {floor:g}; "
                       "no new assignments")
    cap = cfg.get("max_memory_pressure_avg60")
    if cap is not None and metrics["memory_full_avg60"] is not None and metrics["memory_full_avg60"] > float(cap):
        reasons.append(f"memory pressure full avg60 {metrics['memory_full_avg60']} above "
                       f"max_memory_pressure_avg60 {cap}; no new assignments")
    return reasons


def tier_of(target: dict[str, Any]) -> str | None:
    """Prior difficulty from the optional targets.json hints (strategy report 2026-10-14):
    `ref_min_modules` (modules of the smallest known accepted solution) and `n_solvers`
    (systems credited with the problem): easy (<= 45 modules and >= 4 solvers), medium (<= 250),
    hard (251-600), extreme (> 600 or <= 1 solver); None without a size hint."""
    modules, solvers = number(target.get("ref_min_modules")), number(target.get("n_solvers"))
    if modules is None:
        return None
    if modules > 600 or (solvers is not None and solvers <= 1):
        return "extreme"
    if modules <= 45 and solvers is not None and solvers >= 4:
        return "easy"
    return "medium" if modules <= 250 else "hard"


def decide(claim: dict[str, Any], decision: str, reason: str = "") -> None:
    record = {"at": iso(), "decision": decision, "reason": str(reason)[:300]}
    claim["last_decision"] = record
    decisions = claim.setdefault("decisions", [])
    decisions.append(record)
    del decisions[:-20]


# --------------------------------------------------------------------------- tick

# Keys renamed in the 2026-10-27 release; files written before it are read under the new names.
LEGACY_KEYS = {"rival_min_modules": "ref_min_modules", "retry_max_rival_modules": "retry_max_ref_modules",
               "program_min_rival_modules": "program_min_ref_modules"}


def upgrade_keys(value: Any) -> Any:
    """A config or target dict with legacy key names renamed (a new name present wins)."""
    if isinstance(value, dict):
        for old, new in LEGACY_KEYS.items():
            if old in value:
                value.setdefault(new, value[old])
                del value[old]
    return value


class Tick:
    """What one feeder tick reads once and shares between its steps."""

    def __init__(self) -> None:
        cfg = self.cfg = upgrade_keys(read_json(OWN / "config.json") or {})
        self.defaults = dict(DEFAULTS, **(cfg.get("defaults") or {}))
        self.policy: dict[str, Any] = {
            "defaults": self.defaults,
            "verify": bool(cfg.get("verify_lineages", True)),
            "verify_backfill": bool(cfg.get("verify_backfill", False)),
            "verify_round_seconds": int(cfg.get("verify_round_seconds", 900)),
            "max_verifies": int(cfg.get("max_verifies", 2)),
            "churn_rounds": int(cfg.get("churn_rounds", 3) or 0),
            "estimate_ratio": float(cfg.get("estimate_stall_ratio", 0.75) or 0),
            "estimate_min": float(cfg.get("estimate_stall_min_lines", 1000)),
            "handoff": bool(cfg.get("quota_handoff", True)),
            "handoff_after": float(cfg.get("quota_handoff_after_s", 3600)),
            "adopt_paused": bool(cfg.get("adopt_paused_claims", False)),
            "stop_siblings": bool(cfg.get("stop_siblings_on_pass", True)),
            "inbox": bool(cfg.get("inbox", True)),
        }
        self.runners = [Path(r) for r in cfg.get("runners", [])]
        self.targets: list[dict[str, Any]] = [upgrade_keys(t) for t in
                                              (read_json(OWN / "targets.json", {}) or {}).get("targets", [])]
        self.by_id = {t["problem_id"]: t for t in self.targets}
        self.claims: dict[str, Any] = read_json(OWN / "claims.json", {}) or {}
        self.alerts: list[str] = []
        self.now = time.time()
        self.solved: set[str] | None = None
        self.programs: dict[Path, dict[str, Any]] = {}
        self.views: dict[Path, dict[str, Any]] = {}
        self.blocks: dict[str, dict[str, Any]] = {}
        self.disk_ok = True
        self.admission: list[str] = []
        self.free_quota = 0  # idle runners with quota, for five_hour handoffs
        self.held = 0  # idle runners kept free this tick for handoffs whose round is closing
        self.cap_alerted = False
        # runner holds of the coordinator/operator (holds.json: wave -> {reason, at, by})
        self.holds: dict[str, Any] = read_json(OWN / "holds.json", {}) or {}
        self.inbox_starts: list[dict[str, Any]] = []  # validated start_lineage directives
        self.inbox_pending: list[dict[str, Any]] = []  # directives that wait, for status.json
        self.inbox_done: dict[str, Any] = read_json(OWN / "inbox-processed.json", {}) or {}


def runner_view(root: Path, cfg: dict[str, Any], now: float) -> dict[str, Any]:
    state = read_json(root / "state.json", {}) or {}
    manifest = read_json(root / "manifest.json", {}) or {}
    manifest.setdefault("assignments", [])
    return {"root": root, "state": state, "manifest": manifest, "assignments": manifest["assignments"],
            "history": state.setdefault("history", {}), "account": account_of(root, cfg),
            "quota": runner_quota(root, state, now)}


def history_of(t: Tick, root: Any) -> dict[str, Any]:
    view = t.views.get(Path(root or "/nonexistent"))
    if view:
        return view["history"]
    return (read_json(Path(root or "/nonexistent") / "state.json", {}) or {}).get("history", {})


def running_on(t: Tick, root: Path) -> list[str]:
    mine = [p for p, c in t.claims.items() if c.get("runner") == str(root) and c.get("status") == "running"]
    mine += [p.get("problem_id") for p in t.programs.values() for sub in program_subs(p)
             if sub.get("runner") == str(root) and sub.get("status") == "running"]
    return mine


def runner_busy(t: Tick, root: Path) -> bool:
    """A pending assignment (incl. the running job) or a running claim/program role."""
    view = t.views[root]
    return (any(a["attempt_id"] not in view["history"] for a in view["assignments"])
            or bool(running_on(t, root)))


def wave_of(root: Path) -> str:
    return root.name.rsplit("-", 1)[-1]


def held(t: Tick, root: Path) -> dict[str, Any] | None:
    """The coordinator/operator hold of this runner (no new work; unlike STOP the controller
    keeps running and is restarted when it dies)."""
    hold = t.holds.get(wave_of(root))
    return hold if isinstance(hold, dict) else ({"reason": str(hold)} if hold else None)


def has_quota(t: Tick, root: Path) -> bool:
    """The runner can take work: no root STOP, no hold, a queue manifest, its account not
    quota-blocked."""
    return (not (root / "STOP").exists() and (root / "manifest.json").is_file()
            and not held(t, root) and t.views[root]["account"] not in t.blocks)


def start_lineage(t: Tick, root: Path, pid: str, claim: dict[str, Any], role: str, source: Path,
                  fields: dict[str, str] | None = None, overrides: dict[str, Any] | None = None,
                  reason: str = "") -> dict[str, Any]:
    """A new lineage of `pid` on `root` seeded with the files of finished job `source`; `claim`
    (the problem's one claim on this host) follows it."""
    view = t.views[root]
    target = dict(t.by_id.get(pid) or {"problem_id": pid}, **(overrides or {}))
    base = next_base(root, view["assignments"], pid)
    assignment = restart_assignment(root, pid, target, source, base, t.defaults, role=role, fields=fields)
    append_assignment(root, assignment)
    view["assignments"].append(assignment)
    lines = attempt_history(claim)
    for key in ("stopped_no_progress", "stop_kind", "last_finished", "finished_at", "passed_attempt", "error"):
        claim.pop(key, None)
    claim.update(status="running", runner=str(root), base=base, rounds=1, history=lines, progress={},
                 role=role, brief_fields=fields or {}, seeded_from=str(source))
    decide(claim, role, reason)
    log(f"{role.lower()} {pid} on {root.name} as {base} from {source.name}: {reason}"[:600])
    return assignment


def start_verify(t: Tick, root: Path, pid: str, claim: dict[str, Any], source: Path, reason: str,
                 count: bool = True) -> dict[str, Any]:
    """A 1-round VERIFY lineage: the controller runs the official verifier whatever the model
    replies (verify_policy always); a VERIFY does not count as a retry."""
    seconds = int(t.defaults["verification_seconds"])
    overrides = {"verify_policy": "always", "max_rounds": 1, "max_nudges": 0,
                 "wall_seconds": t.policy["verify_round_seconds"], "verification_seconds": seconds}
    digest = sha256_bytes(json.dumps(proof_inventory(source / "workspace"), sort_keys=True).encode())
    assignment = start_lineage(t, root, pid, claim, "VERIFY", source,
                               {"digest": digest[:16], "seconds": str(seconds)}, overrides, reason)
    if count:
        claim["verifies"] = int(claim.get("verifies", 0)) + 1
    claim["verify"] = {"from": str(source), "at": iso(), "reason": reason[:300]}
    return assignment


def official_log(entry: dict[str, Any] | None) -> str:
    run = str((entry or {}).get("receipt_run_id") or "")
    if not run or "/" in run or run in (".", ".."):
        return ""
    try:
        return (RUNS / run / "official.log").read_text(errors="replace")
    except OSError:
        return ""


def ci_measured(entry: dict[str, Any] | None) -> str:
    """The `{measured}` block of the CI_FIT brief: the verifier's time and slowest modules."""
    spent = number(((entry or {}).get("verification") or {}).get("verification_wall_seconds"))
    stats = official_log_stats(official_log(entry))
    parts = [f"the official verification (lake test: comparator, Lean kernel and Nanoda on a clean "
             f"workspace) timed out after {spent:.0f} s" if spent else "the official verification timed out"]
    if stats["slowest_modules"]:
        parts.append("slowest Submission modules in its log: "
                     + ", ".join(f"{name} {seconds:.0f} s" for name, seconds in stats["slowest_modules"]))
        parts.append(f"{stats['submission_modules_built']} modules built, "
                     f"{stats['submission_build_seconds_sum']:.0f} s summed")
    else:
        parts.append("its log recorded no per-module build times")
    return "; ".join(parts) + "."


def official_tail(entry: dict[str, Any] | None) -> str:
    text = official_log(entry)
    if text:
        tail = "\n".join(text.splitlines()[-60:])[-4000:]
    else:
        tail = str(((entry or {}).get("verification") or {}).get("error") or (entry or {}).get("error")
                   or "no official log was recorded")
    return textwrap.indent(tail, "    ")


def after_verify(t: Tick, root: Path, pid: str, claim: dict[str, Any], job: Path,
                 entry: dict[str, Any] | None) -> None:
    """After a VERIFY round without a pass: CI_FIT on a timeout, REPAIR on a rejection, another
    VERIFY (at most max_verifies per problem) when it ended without a verdict."""
    status = ((entry or {}).get("verification") or {}).get("status")
    source = Path((claim.get("verify") or {}).get("from") or job)
    if status == "timeout" and (job / "workspace").is_dir():
        start_lineage(t, root, pid, claim, "CI_FIT", job, {"measured": ci_measured(entry)}, {"max_rounds": 2},
                      f"VERIFY {job.name} timed out")
    elif status == "rejected" and (job / "workspace").is_dir():
        start_lineage(t, root, pid, claim, "REPAIR", job, {"status": status, "log_tail": official_tail(entry)},
                      {"max_rounds": 2}, f"VERIFY {job.name} rejected")
    elif int(claim.get("verifies", 0)) < t.policy["max_verifies"] and (source / "workspace").is_dir():
        start_verify(t, root, pid, claim, source,
                     f"VERIFY {job.name} ended without a verdict ({status or (entry or {}).get('phase')})")
    else:
        claim["verify_exhausted"] = True
        decide(claim, "verify_exhausted", f"{job.name}: {status or (entry or {}).get('phase')}")
        t.alerts.append(f"claim {pid}: complete proof not verified after {claim.get('verifies', 0)} "
                        "VERIFY lineages; operator")


def after_lineage(t: Tick, root: Path, pid: str, claim: dict[str, Any]) -> None:
    """A claim's lineage ended without a pass. The verification phase comes first (a complete,
    unverified last round gets a VERIFY lineage; a claim in it never becomes a program); then a
    large problem that stalled again after its route review becomes a lane program."""
    history = t.views[root]["history"]
    if claim.get("role") == "VERIFY":
        after_verify(t, root, pid, claim, root / "jobs" / claim["base"], history.get(claim["base"]))
        return
    last = finished_round_job(claim, history)
    verifies = int(claim.get("verifies", 0))
    # an operator CI_FIT lineage (no earlier VERIFY) is verified only on its own READY; the CI_FIT round
    # that the verify loop started after a VERIFY timeout still ends in VERIFY #2
    operator_fit = claim.get("role") in PERSISTENT_ROLES and not verifies
    if (t.policy["verify"] and last and verifies < t.policy["max_verifies"] and not operator_fit
            and verify_candidate(last, history.get(last.name), t.defaults)):
        start_verify(t, root, pid, claim, last, f"lineage ended; {last.name} is complete and unverified")
        return
    if verifies:
        if last and round_complete(last / "workspace"):
            claim["verify_exhausted"] = True
            t.alerts.append(f"claim {pid}: complete proof not verified after {verifies} VERIFY lineages; operator")
        return
    if second_lineage(pid, claim):
        return  # a second lineage on this host (claims v2) never becomes a program
    threshold = int(t.cfg.get("program_min_ref_modules") or 0)
    seed = last or last_round_job(claim)
    if (claim.get("status") == "exhausted" and claim.get("route_review") and threshold and seed
            and ((t.by_id.get(pid) or {}).get("ref_min_modules") or 0) >= threshold
            and not (OWN / "programs" / f"{pid}.json").exists() and not round_complete(seed / "workspace")):
        start_program(pid, seed, t.claims)


def after_stop(t: Tick, root: Path, pid: str, claim: dict[str, Any], reason: str) -> None:
    """The gate stopped the continuation: VERIFY for a complete round; otherwise one route review,
    seeded with the best finished round of the problem on this host."""
    source = root / "jobs" / claim["last_finished"]
    if claim.get("stop_kind") == "complete":
        start_verify(t, root, pid, claim, source, reason)
        return
    if claim.get("route_review") or second_lineage(pid, claim):
        # its route review already ran (a second lineage chose its route when it started): the
        # lineage ends (exhausted)
        decide(claim, "stop", reason)
        return
    seed = best_round_job(pid, t.runners, default=source)
    if not seed or not (seed / "workspace").is_dir():
        return
    if (t.policy["verify"] and seed != source and int(claim.get("verifies", 0)) < t.policy["max_verifies"]
            and verify_candidate(seed, job_entry(seed), t.defaults)):
        start_verify(t, root, pid, claim, seed, f"{reason[:200]}; best round {seed.name} is complete")
        return
    # One route-review lineage instead of giving up; it sees what each round moved and which
    # tactic it used.
    fields = {"history": history_text(attempt_history(claim), tactics(seed / "workspace"))}
    start_lineage(t, root, pid, claim, "ROUTE_REVIEW", seed, fields, reason=reason)
    claim["route_review"] = {"from": str(seed), "at": iso(), "reason": reason[:300]}


def backfill_running_review(t: Tick, root: Path, pid: str, claim: dict[str, Any], info: dict[str, Any]) -> bool:
    """verify_backfill: a running route review whose source round was complete and unverified
    (it should have been verified) is stopped for a VERIFY of that source."""
    if (not (t.policy["verify"] and t.policy["verify_backfill"]) or claim.get("role") != "ROUTE_REVIEW"
            or int(claim.get("verifies", 0)) >= t.policy["max_verifies"]):
        return False
    source = Path((claim.get("route_review") or {}).get("from") or "/nonexistent")
    if str(source) == claim.get("verify_checked") or not (source / "workspace").is_dir():
        return False
    claim["verify_checked"] = str(source)
    if not verify_candidate(source, history_of(t, source.parent.parent).get(source.name), t.defaults):
        return False
    stop_lineage(root, info["pending"], t.views[root]["state"])
    start_verify(t, root, pid, claim, source, f"verify backfill: route review {claim['base']} started "
                                              f"from the complete, unverified {source.name}")
    return True


def maybe_handoff(t: Tick, root: Path, pid: str, claim: dict[str, Any], info: dict[str, Any]) -> bool:
    """Quota handoff of a running lineage whose runner waits in a usage-limit pause: always for
    a seven_day window; for a shorter one only when the reset is more than quota_handoff_after_s
    away and a runner with quota is free on this host. The round gets a controlled STOP; the
    claim waits as `handoff` until that round has closed, then resumes elsewhere."""
    view = t.views[root]
    quota = view["quota"]
    if not (t.policy["handoff"] and quota["paused"]):
        return False
    if Path(view["state"].get("current_job") or "").name not in info["pending"]:
        return False
    weekly = str(quota["window"]).startswith("seven_day")
    if not weekly and not (quota["until"] - t.now > t.policy["handoff_after"] and t.free_quota > 0):
        return False
    stop_lineage(root, info["pending"], view["state"])
    if not weekly:
        t.free_quota -= 1
    claim.update(status="handoff", handoff={"from_runner": root.name, "account": view["account"],
                                            "window": quota["window"], "until": iso(quota["until"]),
                                            "at": iso(), "at_ts": t.now})
    decide(claim, "handoff", f"{quota['window']} pause until {iso(quota['until'])}")
    log(f"handoff {pid}: {root.name} ({view['account']}) waits for its {quota['window']} reset "
        f"at {iso(quota['until'])}; lineage {claim['base']} stopped")
    return True


def step_claim(t: Tick, root: Path, pid: str, claim: dict[str, Any]) -> None:
    """Close, gate, verify or hand off one running claim of this runner; refresh its brief."""
    view = t.views[root]
    info = lineage(view["assignments"], view["history"], claim["base"])
    outcome = classify(info)
    claim["rounds"] = len(info["names"])
    if outcome:
        claim.update(status=outcome, finished_at=iso(), passed_attempt=(info["passed"] or [None])[0])
        if outcome == "failed":
            claim["error"] = (info["entries"][-1][1].get("error") or "")[:500]
        log(f"claim {pid} on {root.name}: {outcome}")
        decide(claim, outcome, claim["base"])
        if outcome != "passed":
            after_lineage(t, root, pid, claim)
        return
    if backfill_running_review(t, root, pid, claim, info):
        return
    try:
        stopped = gate_lineage(root, claim, info, view["state"], t.policy)
    except Exception as exc:  # noqa: BLE001
        stopped = None
        t.alerts.append(f"progress gate {pid}: {type(exc).__name__}: {exc}")
    if stopped:
        log(f"claim {pid} on {root.name}: stopping continuation, {stopped}"[:600])
        after_stop(t, root, pid, claim, stopped)
    elif maybe_handoff(t, root, pid, claim, info):
        return
    # A role brief applies to the first round of its lineage only, except a CI-fit task: its later
    # rounds kept only the problem brief, which tells an agent to prove the open lanes (2026-10-16).
    role = claim.get("role")
    first = claim["base"] not in view["history"]
    sync_brief(root, claim["base"], pid, role if first or role in PERSISTENT_ROLES else None,
               claim.get("brief_fields"))


HOLD = "hold"  # an idle runner kept free for a handed-off lineage whose round is still closing


def handoff_candidates(t: Tick) -> tuple[list[tuple[float, str, dict[str, Any], Path | None, dict]], int]:
    """Handed-off claims (and, with adopt_paused_claims, operator-parked `paused` ones) whose old
    round has closed, closest to done first; and how many still wait for that round to close."""
    ready, waiting = [], 0
    for key, claim in t.claims.items():
        status = claim.get("status") if isinstance(claim, dict) else None
        if status != "handoff" and not (status == "paused" and t.policy["adopt_paused"]):
            continue
        pid = claim_pid(key, claim)
        target = t.by_id.get(pid) or {}
        if target.get("disabled") or (t.solved is not None and pid in t.solved) or not claim.get("base"):
            continue
        old = Path(claim.get("runner") or "/nonexistent")
        view = t.views.get(old) or (runner_view(old, t.cfg, t.now) if (old / "manifest.json").is_file() else None)
        history = view["history"] if view else {}
        if view:
            info = lineage(view["assignments"], history, claim["base"])
            if info["passed"]:
                claim.update(status="passed", passed_attempt=info["passed"][0], finished_at=iso())
                decide(claim, "passed", "the handed-off round passed")
                continue
            if info["pending"]:
                # an idle runner is held for it while its stopped round closes (seconds), not longer
                if t.now - (number((claim.get("handoff") or {}).get("at_ts")) or 0) < 3600:
                    waiting += 1
                else:
                    t.alerts.append(f"handoff {pid}: round of {claim['base']} on {old.name} still open")
                continue
        source = finished_round_job(claim, history)
        seeded = Path(claim.get("seeded_from") or "/nonexistent")
        if source is None and (seeded / "workspace").is_dir():
            source = seeded  # paused before its first round finished: resume from its own seed
        entry = (claim.get("progress") or {}).get(source.name) if source else None
        if source and entry is None:
            entry = first_round(source / "workspace")
        ready.append((closeness(entry, claim.get("role")), pid, claim, source, history, key))
    ready.sort(key=lambda item: -item[0])
    return ready, waiting


def assign_handoff(t: Tick, root: Path) -> Any:
    """Resume a handed-off lineage on this runner (not a retry): VERIFY when its last round is
    complete and unverified (or it was a VERIFY), else RESUME from its last finished round."""
    ready, waiting = handoff_candidates(t)
    if not ready:
        if waiting > t.held:
            t.held += 1
            return HOLD
        return None
    _, pid, claim, source, history, key = ready[0]
    target = t.by_id.get(pid) or {"problem_id": pid}
    old_root, old_role = Path(claim["runner"]), claim.get("role")
    reason = (f"handoff from {old_root.name} "
              f"({(claim.get('handoff') or {}).get('window') or claim.get('paused_reason') or claim.get('status')})")
    claim["handoffs"] = int(claim.get("handoffs", 0)) + 1
    claim["handoff"] = dict(claim.get("handoff") or {}, to_runner=root.name, resumed_at=iso())
    verify_from = Path((claim.get("verify") or {}).get("from") or "/nonexistent")
    entry = history.get(claim["base"]) or {}
    if old_role == "VERIFY" and (entry.get("verification") or {}).get("status") in ("timeout", "rejected"):
        after_verify(t, root, pid, claim, old_root / "jobs" / claim["base"], entry)
    elif old_role == "VERIFY" and (verify_from / "workspace").is_dir():
        start_verify(t, root, pid, claim, verify_from, reason, count=False)
    elif (source and t.policy["verify"] and int(claim.get("verifies", 0)) < t.policy["max_verifies"]
          and verify_candidate(source, history.get(source.name), t.defaults)):
        start_verify(t, root, pid, claim, source, reason)
    elif source:
        total = int(target.get("max_rounds", t.defaults["max_rounds"]))
        done = sum(1 for n, e in history.items()
                   if round_index(claim["base"], n) is not None and (e or {}).get("phase") == "done")
        start_lineage(t, root, pid, claim, "RESUME", source, {"reason": reason},
                      {"max_rounds": max(1, total - done)}, reason)
    else:
        assign_fresh(t, root, pid, target, f"{reason}; no finished round to resume from", key=key)
    return True


def assign_verify_backfill(t: Tick, root: Path) -> Any:
    """verify_backfill: an exhausted claim whose last finished round is complete and unverified."""
    if not (t.policy["verify"] and t.policy["verify_backfill"]):
        return None
    for target in sorted(t.targets, key=lambda x: x.get("ref_min_modules") or 10**9):
        pid = target["problem_id"]
        claim = t.claims.get(pid) or {}
        if (claim.get("status") not in ("exhausted", "failed") or claim.get("program_status")
                or claim.get("verify_exhausted") or target.get("disabled")
                or int(claim.get("verifies", 0)) >= t.policy["max_verifies"]
                or (t.solved is not None and pid in t.solved)):
            continue
        source = finished_round_job(claim, history_of(t, claim.get("runner")))
        if not source or claim.get("verify_checked") == str(source):
            continue
        claim["verify_checked"] = str(source)
        if verify_candidate(source, history_of(t, source.parent.parent).get(source.name), t.defaults):
            return start_verify(t, root, pid, claim, source,
                                f"verify backfill: {source.name} is complete and unverified")
    return None


def assign_program(t: Tick, root: Path) -> Any:
    """The next role of an active lane program (at most max_program_runners at once)."""
    busy = sum(1 for p in t.programs.values() for sub in program_subs(p) if sub.get("status") == "running")
    for program in sorted(t.programs.values(), key=lambda p: p.get("created_at", "")):
        if busy >= int(t.cfg.get("max_program_runners", 2)):
            break
        if program.get("status") not in ("architect", "lanes", "integrating"):
            continue
        assignment = program_assignment(root, program, t.by_id, t.views[root]["assignments"], t.defaults)
        if assignment:
            append_assignment(root, assignment)
            t.views[root]["assignments"].append(assignment)
            log(f"program {program['problem_id']}: assigned {assignment['attempt_id']} to {root.name}")
            return assignment
    return None


def assign_retry(t: Tick, root: Path) -> Any:
    """Retry tried-but-unsolved targets, smallest size hint first. 17 of our first 32 passes came from
    lineages restarted after they had been stopped, so there is no size cap unless
    "retry_max_ref_modules" is set. Seeded with the best finished round on this host."""
    retry_max = int(t.cfg.get("retry_max_ref_modules") or 0)
    max_retries = int(t.cfg.get("max_retries", 2))
    for target in sorted(t.targets, key=lambda x: x.get("ref_min_modules") or 10**9):
        pid = target["problem_id"]
        claim = t.claims.get(pid) or {}
        retries = int(claim.get("retries", 1 if claim.get("retried") else 0))
        if (claim.get("status") not in ("exhausted", "failed") or retries >= max_retries
                or target.get("disabled") or claim.get("program_status") or claim.get("verify_exhausted")
                or (retry_max and (target.get("ref_min_modules") or 10**9) > retry_max)
                or (t.solved is not None and pid in t.solved) or program_active(pid)
                or any(k != pid and c.get("status") in LIVE_CLAIM for k, c in claims_of(t.claims, pid))):
            continue
        last = last_round_job(claim) or finished_round_job(claim, history_of(t, claim.get("runner")))
        source = best_round_job(pid, t.runners, default=last)
        if source is None:
            continue
        if (t.policy["verify"] and t.policy["verify_backfill"]
                and int(claim.get("verifies", 0)) < t.policy["max_verifies"]
                and verify_candidate(source, history_of(t, source.parent.parent).get(source.name), t.defaults)):
            return start_verify(t, root, pid, claim, source, f"retry seed {source.name} is complete and unverified")
        view = t.views[root]
        base = next_base(root, view["assignments"], pid)
        # A retry follows a stalled lineage, so it always re-decides the route, with the
        # measured history of every earlier round: the tactic changes (AdaEvolve rotates
        # tactics on stagnation; repeating the stalled one is what it was stopped for).
        lines = attempt_history(claim)
        fields = {"history": history_text(lines, tactics(source / "workspace"))}
        assignment = restart_assignment(root, pid, target, source, base, t.defaults,
                                        role="ROUTE_REVIEW", fields=fields)
        append_assignment(root, assignment)
        view["assignments"].append(assignment)
        new = {"status": "running", "runner": str(root), "base": base,
               "claimed_at": iso(), "kind": "retry", "retried": True,
               "retries": retries + 1, "seeded_from": str(source),
               "role": "ROUTE_REVIEW", "history": lines, "brief_fields": fields,
               "route_review": {"from": str(source), "at": iso(), "reason": "retry"},
               "previous_claim": {k: claim.get(k) for k in ("base", "runner", "rounds", "finished_at")}}
        new.update({k: claim[k] for k in ("verifies", "handoffs", "trajectory", "decisions") if k in claim})
        t.claims[pid] = new
        decide(new, "retry", f"from {source.name}")
        log(f"retry {pid} on {root.name} as {base} from {source.name} (route review)")
        return assignment
    return None


def assign_fresh(t: Tick, root: Path, pid: str, target: dict[str, Any], reason: str = "new target",
                 key: str | None = None) -> dict[str, Any]:
    """A target's first lineage on this host; latest proof files exported from another host
    (seed_dir: an earlier attempt stalled there) start it as a route review. `key`: the claim
    it replaces (claims v2), default the problem id."""
    key = key or pid
    view = t.views[root]
    base = next_base(root, view["assignments"], pid)
    role = fields = None
    spec = target
    if target.get("seed_dir"):
        role = "ROUTE_REVIEW"
        fields = {"history": history_text([], tactics(Path(target["seed_dir"])))}
        spec = dict(target, kind="fresh", overlay_dir=seed_overlay_dir(root, pid, Path(target["seed_dir"])))
    assignment = make_assignment(spec, base, sync_brief(root, base, pid, role, fields), t.defaults)
    append_assignment(root, assignment)
    view["assignments"].append(assignment)
    claim = {"status": "running", "runner": str(root), "base": base,
             "claimed_at": iso(), "kind": assignment["kind"]}
    claim.update({k: v for k, v in (t.claims.get(key) or {}).items()
                  if k in ("handoffs", "handoff", "history", "retries", "verifies", "trajectory", "decisions",
                           "problem_id", "key")})
    if role:
        claim.update(role=role, brief_fields=fields,
                     route_review={"from": target["seed_dir"], "at": iso(), "reason": "seeded transfer"})
    t.claims[key] = claim
    decide(claim, "assign", reason)
    log(f"assigned {pid} to {root.name} as {base}" + (" (route review)" if role else ""))
    return assignment


def assign_target(t: Tick, root: Path) -> Any:
    for target in t.targets:
        pid = target["problem_id"]
        if target.get("disabled") or pid in t.claims:
            continue
        if t.solved is not None and pid in t.solved:
            t.claims[pid] = {"status": "skipped_already_ours", "at": iso()}
            continue
        return assign_fresh(t, root, pid, target)
    return None


def assign_idle(t: Tick, root: Path) -> str:
    """New work for an idle runner, in this order: handoff, coordinator start (inbox), VERIFY
    backfill, program role, retry, new target. Returns what happened, for status.json."""
    view = t.views[root]
    if (root / "STOP").exists():
        return "stopped (STOP file)"
    hold = held(t, root)
    if hold:
        return f"held ({str(hold.get('reason') or 'no reason given')[:160]})"
    if not (root / "manifest.json").is_file():
        return "no manifest"
    if not t.disk_ok or t.admission:
        return "host admission closed"
    block = t.blocks.get(view["account"])
    if block:
        return f"quota-blocked ({block.get('window')}) until {iso(block['until'])}"
    cap = int(t.cfg.get("max_active_runners") or 0)
    if cap and sum(runner_busy(t, r) for r in t.runners) >= cap:
        if not t.cap_alerted:
            t.alerts.append(f"max_active_runners {cap} reached; idle runners get no new work")
            t.cap_alerted = True
        return "max_active_runners reached"
    for step in (assign_handoff, assign_inbox, assign_verify_backfill, assign_program, assign_retry, assign_target):
        result = step(t, root)
        if result == HOLD:
            return "held for a handoff"
        if result:
            return f"assigned {view['assignments'][-1]['attempt_id']}" if view["assignments"] else "assigned"
    return "idle"


def end_program(t: Tick, program: dict[str, Any], reason: str, stop_roles: bool = False) -> None:
    """Mark an active program `stopped`; with stop_roles its running role lineages also get
    stopped (their running round a controlled STOP, unstarted rounds dropped)."""
    for sub in program_subs(program):
        if sub.get("status") == "running":
            if stop_roles:
                stop_claim_lineage(t, sub)
            sub["status"] = "stopped"
    program.update(status="stopped", reason=reason, stopped_at=iso())
    log(f"program {program.get('problem_id')}: stopped ({reason})"[:600])


def stop_claim_lineage(t: Tick, claim: dict[str, Any]) -> list[str]:
    """Stop the pending rounds of one claim's (or program role's) lineage on this host."""
    root = Path(claim.get("runner") or "/nonexistent")
    view = t.views.get(root)
    if not view or not claim.get("base"):
        return []
    info = lineage(view["assignments"], view["history"], claim["base"])
    if info["pending"]:
        stop_lineage(root, info["pending"], view["state"])
        view["assignments"][:] = (read_json(root / "manifest.json", {}) or {}).get("assignments", [])
    return info["pending"]


def stop_siblings(t: Tick, pid: str, spare_roles: tuple[str, ...] = ()) -> None:
    """A pass ends every other lineage of the problem on this host: its pending assignments
    (the running one gets a controlled STOP), its other live claims (claims v2) and an active
    program. Live claims whose role is in `spare_roles` keep running, with their runners: a
    CI_FIT pass re-verifies a proof that is already complete, so a parallel CI_FIT lineage of
    the same problem is deliberate (2026-10-18: two CI-fit routes of poincare_3d_smooth)."""
    spared = {str(c.get("runner")) for _, c in claims_of(t.claims, pid)
              if c.get("status") in ("running", "handoff", "paused") and c.get("role") in spare_roles}
    for root in t.runners:
        if str(root) in spared:
            continue
        state = read_json(root / "state.json", {}) or {}
        history = state.get("history", {})
        manifest = read_json(root / "manifest.json", {}) or {}
        pending = [a["attempt_id"] for a in manifest.get("assignments", [])
                   if a.get("problem_id") == pid and a["attempt_id"] not in history]
        if pending:
            stop_lineage(root, pending, state)
            log(f"{pid} passed: stopped its other lineages {pending} on {root.name}")
            if root in t.views:
                t.views[root]["assignments"][:] = (read_json(root / "manifest.json", {}) or {}).get("assignments", [])
    for key, claim in claims_of(t.claims, pid):
        if claim.get("status") in ("running", "handoff", "paused") and claim.get("role") not in spare_roles:
            claim.update(status="stopped", stopped_at=iso(), stopped_reason="the problem passed on this host")
            decide(claim, "stop", "a sibling lineage passed")
    for program in t.programs.values():
        if program.get("problem_id") == pid and program.get("status") not in PROGRAM_FINAL:
            end_program(t, program, "problem passed by another lineage on this host")


def claim_signals(t: Tick) -> dict[str, Any]:
    """Per-claim inputs for an adaptive policy: tier, sorry/estimate trajectory, closeness, last decision."""
    signals = {}
    for key, claim in t.claims.items():
        if not isinstance(claim, dict) or not claim.get("base"):
            continue
        pid = claim_pid(key, claim)
        trajectory = claim.get("trajectory") or []
        verifying = claim.get("role") == "VERIFY" and claim.get("status") == "running"
        signals[key] = {"problem_id": pid, "tier": tier_of(t.by_id.get(pid) or {}), "status": claim.get("status"),
                        "role": claim.get("role"), "trajectory": trajectory[-12:],
                        "estimates": [row[2] for row in trajectory][-12:],
                        "closeness": 1.0 if verifying else claim.get("closeness"),
                        "last_decision": claim.get("last_decision"),
                        "verifies": claim.get("verifies", 0), "handoffs": claim.get("handoffs", 0)}
    return signals


# --------------------------------------------------------------------------- coordinator inbox
# <OWN>/inbox/<id>.json, written by local/coordinator.py (docs/COORDINATOR-POLICY.md). Each id is
# applied once (inbox-processed.json, file moved to inbox/processed/); a start that has no idle
# runner with quota yet waits until its `expires_at`.

INBOX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.~-]{0,159}")
INBOX_ROLES = ("ROUTE_REVIEW", "RESUME", "PARALLEL", "VERIFY", "CI_FIT")
INBOX_MARKS = ("stopped", "passed_elsewhere", "delegated")
INBOX_KEEP = 2000  # processed ids kept in inbox-processed.json (inbox/processed/ keeps the files)
FIELD_CHARS = 4000


def record_ts(record: Any) -> float:
    return (number(record.get("at_ts")) if isinstance(record, dict) else None) or 0.0


def inbox_order(path: Path) -> tuple[float, str]:
    try:
        return path.stat().st_mtime, path.name
    except OSError:
        return 0.0, path.name


def inbox_finish(t: Tick, path: Path, d: Any, status: str, detail: Any) -> None:
    """Record the outcome of directive `path` (applied / rejected / expired) and move it to
    inbox/processed/."""
    d = d if isinstance(d, dict) else {}
    t.inbox_done[path.stem] = {"type": d.get("type"), "problem_id": d.get("problem_id"), "status": status,
                               "detail": detail, "at": iso(t.now), "at_ts": t.now}
    if len(t.inbox_done) > INBOX_KEEP:
        order = sorted(t.inbox_done, key=lambda k: record_ts(t.inbox_done[k]))
        for k in order[:len(t.inbox_done) - INBOX_KEEP]:
            t.inbox_done.pop(k)
    write_json(OWN / "inbox-processed.json", t.inbox_done)
    done = path.parent / "processed"
    done.mkdir(exist_ok=True)
    with contextlib.suppress(OSError):
        os.replace(path, done / path.name)
    log(f"inbox {path.stem}: {status} {json.dumps(detail, default=str)}"[:600])


def inbox_seed(value: Any) -> Path:
    """A start's seed: an existing directory under <OWN>/seeds with a Submission.lean."""
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise ValueError("seed_dir must be an absolute path")
    seeds = (OWN / "seeds").resolve()
    path = Path(value).resolve()
    if seeds not in path.parents:
        raise ValueError(f"seed_dir {value} is not under {OWN / 'seeds'}")
    main = path / "Submission.lean"
    if not main.is_file() or main.is_symlink():
        raise ValueError(f"seed_dir {value} has no Submission.lean")
    return path


def inbox_fields(raw: Any) -> dict[str, str]:
    """Brief fields of a start: short string values; `avoid_tactics` (a list) and `parallel_of`
    become the {parallel} block of the role brief."""
    raw = raw if isinstance(raw, dict) else {}
    fields = {}
    for key, value in raw.items():
        if key in ("avoid_tactics", "parallel_of") or not re.fullmatch(r"[a-z][a-z_]{0,39}", str(key)):
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            fields[str(key)] = str(value)[:FIELD_CHARS]
    avoid = raw.get("avoid_tactics")
    avoid = [str(x)[:300] for x in avoid if isinstance(x, (str, int, float))][:12] if isinstance(avoid, list) else []
    main = str(raw.get("parallel_of") or "")[:FIELD_CHARS]
    if avoid or main:
        fields["parallel"] = parallel_text({"avoid_tactics": avoid, "parallel_of": main})
    return fields


def ci_held(pid: str) -> bool:
    """Every branch package of `pid` in the outbox waits on the CI-fit gate (none is ready): its pass
    does not fit the organizers' CI yet, so a CI_FIT lineage of the passed problem is still useful."""
    states = [(read_json(path, {}) or {}).get("status") for path in (OWN / "outbox" / pid).glob("*/package.json")]
    return bool(states) and all(s == "hold_ci_review" for s in states)


def inbox_check_start(t: Tick, d: dict[str, Any]) -> dict[str, Any]:
    """Validate a start_lineage directive against this host; the normalized start."""
    pid = d.get("problem_id")
    if not isinstance(pid, str) or not SAFE_ID.fullmatch(pid):
        raise ValueError("bad problem_id")
    role = d.get("role")
    if role not in INBOX_ROLES:
        raise ValueError(f"role must be one of {list(INBOX_ROLES)}")
    seed = inbox_seed(d.get("seed_dir"))
    if (t.by_id.get(pid) or {}).get("disabled"):
        raise ValueError("target disabled on this host")
    if t.solved is not None and pid in t.solved:
        raise ValueError("already credited to our model")
    done = [k for k, c in claims_of(t.claims, pid) if c.get("status") in ("passed", "passed_elsewhere")]
    if done and not (role == "CI_FIT" and ci_held(pid)):
        raise ValueError(f"the problem passed ({done[0]} is {t.claims[done[0]].get('status')})")
    if program_active(pid):
        raise ValueError("a lane program of this problem is active on this host")
    rounds = d.get("max_rounds")
    if rounds is not None and (not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 50):
        raise ValueError("max_rounds must be an integer in 1..50")
    prefer = d.get("prefer_runner")
    if prefer is not None and not (isinstance(prefer, str) and SAFE_ID.fullmatch(prefer)):
        raise ValueError("bad prefer_runner")
    return {"id": d["id"], "d": d, "pid": pid, "role": role, "seed": seed, "fields": inbox_fields(d.get("fields")),
            "prefer": prefer, "max_rounds": rounds, "reason": str(d.get("reason") or "fleet coordinator")[:300]}


def inbox_stop(t: Tick, d: dict[str, Any]) -> dict[str, Any]:
    """stop_lineage: end the lineages of a problem on this host (or the one claim with `base`):
    pending rounds stopped (the running one with a controlled STOP), claims marked `mark`
    (stopped / passed_elsewhere / delegated); `what` all / claims / program. passed_elsewhere also
    blocks a problem that has no claim here, so no feeder path picks it up."""
    pid, base = d.get("problem_id"), d.get("base")
    if pid is not None and not (isinstance(pid, str) and SAFE_ID.fullmatch(pid)):
        raise ValueError("bad problem_id")
    if base is not None and not (isinstance(base, str) and SAFE_ID.fullmatch(base)):
        raise ValueError("bad base")
    if not pid and not base:
        raise ValueError("stop_lineage needs problem_id or base")
    mark = d.get("mark") or "stopped"
    if mark not in INBOX_MARKS:
        raise ValueError(f"mark must be one of {list(INBOX_MARKS)}")
    what = d.get("what") or "all"
    if what not in ("all", "claims", "program"):
        raise ValueError("what must be all, claims or program")
    expect = d.get("expect_status")
    expect = [expect] if isinstance(expect, str) else [str(x) for x in (expect or [])]
    reason = str(d.get("reason") or "fleet coordinator")[:300]
    if base:
        chosen = [(k, c) for k, c in t.claims.items() if isinstance(c, dict) and c.get("base") == base]
        if not chosen:
            raise ValueError(f"no claim with base {base} on this host")
        if pid and any(claim_pid(k, c) != pid for k, c in chosen):
            raise ValueError(f"base {base} belongs to another problem")
        pid = pid or claim_pid(*chosen[0])
    else:
        chosen = claims_of(t.claims, pid) if what != "program" else []
    stopped: dict[str, list[str]] = {}
    marked = []
    for key, claim in chosen:
        status = claim.get("status")
        if expect and status not in expect:
            if base:
                raise ValueError(f"claim {key} is {status}, expected {expect}")
            continue
        if status in ("passed", "program", mark):
            continue  # a pass stays; an active program is handled below
        live = status in ("running", "handoff", "paused")
        if not live and mark != "passed_elsewhere":
            continue  # a finished claim keeps its outcome
        if live:
            stopped[key] = stop_claim_lineage(t, claim)
        claim.update(status=mark, previous_status=status, stopped_at=iso(), stopped_reason=reason, stopped_by=d["id"])
        if mark == "delegated":
            claim["host"] = str(d.get("to_host") or "")[:40] or None
        decide(claim, f"coordinator {mark}", reason)
        marked.append(key)
    programs = []
    if not base and what in ("all", "program"):
        for program in t.programs.values():
            if program.get("problem_id") == pid and program.get("status") not in PROGRAM_FINAL:
                end_program(t, program, f"fleet coordinator: {reason}", stop_roles=True)
                programs.append(pid)
        main = t.claims.get(pid)
        if programs and isinstance(main, dict) and main.get("status") == "program":
            main["program_status"] = "stopped"
            if mark == "passed_elsewhere":
                main.update(status=mark, previous_status="program", stopped_at=iso(), stopped_reason=reason,
                            stopped_by=d["id"])
                marked.append(pid)
    if mark == "passed_elsewhere" and not base and not claims_of(t.claims, pid):
        t.claims[pid] = {"status": mark, "problem_id": pid, "at": iso(), "stopped_reason": reason,
                         "stopped_by": d["id"]}
        marked.append(pid)
    log(f"coordinator stop {pid} ({mark}): stopped {stopped}, marked {marked}, programs {programs}: {reason}"[:600])
    return {"problem_id": pid, "stopped": stopped, "marked": marked, "programs": programs}


def inbox_runner(t: Tick, name: Any) -> Path:
    if isinstance(name, str) and name:
        for root in t.runners:
            if root.name == name or wave_of(root) == name:
                return root
    raise ValueError(f"no runner {name!r} on this host")


def inbox_hold(t: Tick, d: dict[str, Any]) -> dict[str, Any]:
    """hold_runner: no new work for this runner until release_runner (the controller keeps
    running; a root STOP would also stop its restarts)."""
    root = inbox_runner(t, d.get("runner"))
    t.holds[wave_of(root)] = {"reason": str(d.get("reason") or "")[:300], "at": iso(), "by": d["id"]}
    write_json(OWN / "holds.json", t.holds)
    return {"runner": root.name, "held": True}


def inbox_release(t: Tick, d: dict[str, Any]) -> dict[str, Any]:
    root = inbox_runner(t, d.get("runner"))
    was = t.holds.pop(wave_of(root), None)
    write_json(OWN / "holds.json", t.holds)
    return {"runner": root.name, "released": was is not None}


INBOX_HANDLERS = {"stop_lineage": inbox_stop, "hold_runner": inbox_hold, "release_runner": inbox_release,
                  "start_lineage": None}


def process_inbox(t: Tick) -> None:
    """Apply stop_lineage / hold_runner / release_runner directives now, oldest first; validate
    start_lineage directives into t.inbox_starts (placed by assign_inbox). A directive whose id
    was already processed is dropped; an invalid one is rejected with the reason."""
    folder = OWN / "inbox"
    if not t.policy["inbox"] or not folder.is_dir():
        return
    for path in sorted(folder.glob("*.json"), key=inbox_order):
        if path.stem in t.inbox_done or (folder / "processed" / path.name).exists():
            with contextlib.suppress(OSError):
                path.unlink()  # a re-sent directive is applied once
            log(f"inbox {path.stem}: already processed; duplicate dropped")
            continue
        d = read_json(path)
        if not isinstance(d, dict) or not INBOX_ID.fullmatch(path.stem) or d.get("id") != path.stem:
            inbox_finish(t, path, d, "rejected", "unreadable directive, or its id differs from the file name")
            continue
        kind = d.get("type")
        if kind not in INBOX_HANDLERS:
            inbox_finish(t, path, d, "rejected", f"unknown type {kind!r}")
            continue
        expires = number(d.get("expires_at"))
        if expires is not None and t.now > expires:
            inbox_finish(t, path, d, "expired", f"not applied before {iso(expires)}")
            continue
        try:
            if kind == "start_lineage":
                t.inbox_starts.append(dict(inbox_check_start(t, d), path=str(path)))
                continue
            detail = INBOX_HANDLERS[kind](t, d)
        except (ValueError, KeyError, TypeError) as exc:
            inbox_finish(t, path, d, "rejected", f"{type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - never replayed; the coordinator sees the reason
            t.alerts.append(f"inbox {path.stem}: {type(exc).__name__}: {exc}")
            log(traceback.format_exc()[-1500:])
            inbox_finish(t, path, d, "rejected", f"{type(exc).__name__}: {exc}")
            continue
        inbox_finish(t, path, d, "applied", detail)


TARGET_FIELDS = {"ref_min_modules": (int, float), "n_solvers": (int, float), "title": (str,), "max_rounds": (int,)}


def inbox_target(t: Tick, pid: str, raw: Any) -> None:
    """A moved lineage (not PARALLEL) brings its target entry to a host that lacks it, so the
    problem's tier, budget and later retries work here as on its old host."""
    if pid in t.by_id or not isinstance(raw, dict):
        return
    entry: dict[str, Any] = {"problem_id": pid}
    for key, types in TARGET_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, types) and not isinstance(value, bool):
            entry[key] = value[:200] if isinstance(value, str) else value
    if not 1 <= int(entry.get("max_rounds", 1)) <= 50:
        entry.pop("max_rounds")
    data = read_json(OWN / "targets.json", {}) or {}
    data.setdefault("targets", [])
    if any(x.get("problem_id") == pid for x in data["targets"] if isinstance(x, dict)):
        return
    data["targets"].append(entry)
    write_json(OWN / "targets.json", data)
    t.targets.append(entry)
    t.by_id[pid] = entry
    log(f"target {pid} added from a coordinator directive: {entry}")


def inbox_start(t: Tick, root: Path, start: dict[str, Any]) -> dict[str, Any]:
    """A lineage of start["pid"] on `root` seeded with the files of start["seed"], in the
    directive's role; its claim is the problem's key or `<pid>~N` (claims v2)."""
    d, pid, role, seed = start["d"], start["pid"], start["role"], start["seed"]
    if role != "PARALLEL":
        inbox_target(t, pid, d.get("target"))
    fields = dict(start["fields"])
    overrides: dict[str, Any] = {"max_rounds": start["max_rounds"]} if start["max_rounds"] else {}
    if role == "VERIFY":
        seconds = int(t.defaults["verification_seconds"])
        overrides.update(verify_policy="always", max_rounds=1, max_nudges=0,
                         wall_seconds=t.policy["verify_round_seconds"], verification_seconds=seconds)
        digest = sha256_bytes(json.dumps(proof_inventory(seed), sort_keys=True).encode())
        fields.update(digest=digest[:16], seconds=str(seconds))
    elif role == "ROUTE_REVIEW":
        fields.setdefault("history", history_text([], tactics(seed)))
    elif role == "RESUME":
        fields.setdefault("reason", start["reason"])
    view = t.views[root]
    base = next_base(root, view["assignments"], pid)
    spec = dict(t.by_id.get(pid) or {}, **overrides)
    spec.update(kind="fresh", problem_id=pid, overlay_dir=seed_overlay_dir(root, pid, seed))
    for name in ("disabled", "seed_dir", "reason"):
        spec.pop(name, None)
    assignment = make_assignment(spec, base, sync_brief(root, base, pid, role, fields), t.defaults)
    append_assignment(root, assignment)
    view["assignments"].append(assignment)
    key = claim_key(t.claims, pid, ("passed",) if role == "CI_FIT" else ())  # a held pass stays recorded
    old = t.claims.get(key) if isinstance(t.claims.get(key), dict) else {}
    claim: dict[str, Any] = {
        "status": "running", "runner": str(root), "base": base, "claimed_at": iso(), "kind": "fresh",
        "problem_id": pid, "rounds": 1, "role": role, "brief_fields": fields, "seeded_from": str(seed),
        "directive": d["id"],
        "coordinator": {k: str(d[k])[:600] for k in ("from_host", "from_job", "from_key", "reason") if d.get(k)}}
    if key != pid:
        claim["key"] = key
    claim.update({k: old[k] for k in ("handoffs", "history", "retries", "verifies", "trajectory", "decisions")
                  if k in old})
    if old:
        claim["previous_claim"] = {k: old.get(k) for k in ("status", "base", "runner", "rounds", "finished_at", "role")}
    if role in ("ROUTE_REVIEW", "PARALLEL") or d.get("route_review_done"):
        claim["route_review"] = {"from": str(seed), "at": iso(), "reason": f"coordinator {role.lower()}"}
    if role == "RESUME":
        claim["handoffs"] = int(claim.get("handoffs", 0)) + 1
        claim["handoff"] = {"from_host": d.get("from_host"), "to_runner": root.name, "resumed_at": iso()}
    if role == "VERIFY":
        claim["verifies"] = int(claim.get("verifies", 0)) + 1
        claim["verify"] = {"from": str(root / "jobs" / base), "at": iso(), "reason": start["reason"]}
    t.claims[key] = claim
    decide(claim, role, f"coordinator {d['id']}: {start['reason']}")
    log(f"coordinator {role.lower()} {pid} on {root.name} as {base} (claim {key}) from seed {seed.name}")
    return {"assignment": assignment, "runner": root.name, "base": base, "key": key}


def assign_inbox(t: Tick, root: Path) -> Any:
    """A validated coordinator start for this idle runner: one that prefers it, else one whose
    preferred runner is not idle now. Re-checked first (a pass may have come this tick)."""
    others = [r for r in t.runners if r != root and not runner_busy(t, r) and has_quota(t, r)]

    def prefers(start: dict[str, Any], r: Path) -> bool:
        return bool(start["prefer"]) and start["prefer"] in (r.name, wave_of(r))

    while t.inbox_starts:
        start = (next((s for s in t.inbox_starts if prefers(s, root)), None)
                 or next((s for s in t.inbox_starts if not any(prefers(s, r) for r in others)), None))
        if start is None:
            return None
        t.inbox_starts.remove(start)
        path = Path(start["path"])
        try:
            start = dict(inbox_check_start(t, start["d"]), path=start["path"])
            result = inbox_start(t, root, start)
        except (ValueError, KeyError, TypeError) as exc:
            inbox_finish(t, path, start["d"], "rejected", f"{type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            t.alerts.append(f"inbox {path.stem}: {type(exc).__name__}: {exc}")
            log(traceback.format_exc()[-1500:])
            inbox_finish(t, path, start["d"], "rejected", f"{type(exc).__name__}: {exc}")
            continue
        inbox_finish(t, path, start["d"], "applied", {k: result[k] for k in ("runner", "base", "key")})
        return result["assignment"]
    return None


def inbox_waiting(t: Tick) -> None:
    """Starts not placed this tick stay in the inbox; status.json says why."""
    idle = [r for r in t.runners if not runner_busy(t, r) and has_quota(t, r)]
    for start in t.inbox_starts:
        if not t.disk_ok or t.admission:
            why = "host admission closed"
        elif not idle:
            why = "no idle runner with quota"
        else:
            why = f"waiting for {start['prefer']}" if start["prefer"] else "no runner took it this tick"
        t.inbox_pending.append({"id": start["id"], "type": "start_lineage", "problem_id": start["pid"],
                                "role": start["role"], "prefer_runner": start["prefer"], "waiting": why,
                                "expires_at": start["d"].get("expires_at")})


def tick() -> dict[str, Any]:
    t = Tick()
    cfg = t.cfg
    t.solved = our_solved(t.alerts)
    before = {pid: (c or {}).get("status") for pid, c in t.claims.items()}
    free_gb = shutil.disk_usage("/").free / 2**30
    t.disk_ok = free_gb >= float(cfg.get("min_free_gb", 25))
    if not t.disk_ok:
        t.alerts.append(f"disk free {free_gb:.0f} GB below minimum; no new assignments")
    metrics = host_metrics()
    t.admission = admission_reasons(cfg, metrics)
    t.alerts.extend(t.admission)
    # 0. programs (architect -> lanes -> integrator), updated before any assignment
    t.programs = {path: read_json(path, {}) for path in sorted((OWN / "programs").glob("*.json"))}
    for program in t.programs.values():
        if program.get("status") not in PROGRAM_FINAL:
            try:
                update_program(program, t.claims, t.alerts)
            except Exception as exc:  # noqa: BLE001
                t.alerts.append(f"program {program.get('problem_id')}: {type(exc).__name__}: {exc}")
                log(traceback.format_exc()[-1500:])
    t.views = {root: runner_view(root, cfg, t.now) for root in t.runners}
    t.blocks = account_blocks(t.views, t.now)
    credential_halts(t)
    for account, block in sorted(t.blocks.items()):
        t.alerts.append(f"account {account} quota-blocked ({block.get('window')}) until "
                        f"{iso(block['until'])}: no new work for its runners")
    # coordinator directives: stops and holds now, starts are validated here and placed in step 3
    try:
        process_inbox(t)
    except Exception as exc:  # noqa: BLE001 - the inbox never blocks a tick
        t.alerts.append(f"inbox: {type(exc).__name__}: {exc}")
        log(traceback.format_exc()[-1500:])
    if t.disk_ok and not t.admission:
        t.free_quota = sum(1 for root in t.runners if has_quota(t, root) and not runner_busy(t, root))
    # 1. close finished claims, gate/verify/hand off running ones, refresh briefs (all runners
    # first, so a handoff decided here can take an idle runner in step 3)
    for root in t.runners:
        for key, claim in list(t.claims.items()):
            if isinstance(claim, dict) and claim.get("runner") == str(root) and claim.get("status") == "running":
                try:
                    step_claim(t, root, claim_pid(key, claim), claim)
                except Exception as exc:  # noqa: BLE001
                    t.alerts.append(f"claim {key} on {root.name}: {type(exc).__name__}: {exc}")
                    log(traceback.format_exc()[-1500:])
    runner_status = {}
    for root in t.runners:
        view = t.views[root]
        busy = runner_busy(t, root)
        # 2. runtime pin (config "runtimes": {"wave101": "/abs/runtime/dir"}) at a lineage boundary
        wanted = (cfg.get("runtimes") or {}).get(root.name.rsplit("-", 1)[-1])
        if wanted:
            ensure_runtime(root, Path(wanted), t.alerts, busy=busy)
        # 3. new work when idle, then liveness
        note = None
        if not busy:
            try:
                note = assign_idle(t, root)
            except Exception as exc:  # noqa: BLE001
                t.alerts.append(f"assignment on {root.name}: {type(exc).__name__}: {exc}")
                log(traceback.format_exc()[-1500:])
        state = view["state"]
        liveness = ensure_running(root, state, t.alerts)
        quota = view["quota"]
        runner_status[root.name] = {
            "controller": liveness, "queue_status": state.get("status"),
            "current_job": state.get("current_job"),
            "updated_at": iso(state["updated_at"]) if state.get("updated_at") else None,
            "pending": [a["attempt_id"] for a in view["assignments"] if a["attempt_id"] not in view["history"]],
            "finished": len(view["history"]), "account": view["account"],
            "quota_pause": ({"window": quota["window"], "until": iso(quota["until"])} if quota["paused"] else None),
            "held": bool(held(t, root)), "assignment": note}
    inbox_waiting(t)
    # 4. a problem that passed on this host stops its other lineages here
    passed = sorted({claim_pid(k, c) for k, c in t.claims.items()
                     if isinstance(c, dict) and c.get("status") == "passed" and before.get(k) != "passed"})
    if t.policy["stop_siblings"]:
        for pid in passed:
            roles = {c.get("role") for k, c in claims_of(t.claims, pid)
                     if c.get("status") == "passed" and before.get(k) != "passed"}
            try:
                stop_siblings(t, pid, PERSISTENT_ROLES if roles <= set(PERSISTENT_ROLES) else ())
            except Exception as exc:  # noqa: BLE001
                t.alerts.append(f"stopping siblings of {pid}: {type(exc).__name__}: {exc}")
    write_json(OWN / "claims.json", t.claims)
    for path, program in t.programs.items():
        write_json(path, program)
    # 5. keep every pass (passes/<pid>-<digest16>.json) and package it
    packages = []
    for root in t.runners:
        try:
            kept = keep_passes(root, t.alerts)
        except OSError as exc:
            t.alerts.append(f"{root.name}: keeping PASS files failed: {type(exc).__name__}: {exc}")
            kept = []
        for pass_path in kept:
            try:
                packages.append({k: v for k, v in package_pass(root, pass_path).items()
                                 if k in ("problem_id", "artifact_digest", "status", "ci_gate")})
            except Exception as exc:  # noqa: BLE001
                t.alerts.append(f"packaging {root.name}/passes/{pass_path.name} failed: {type(exc).__name__}: {exc}")
                log(traceback.format_exc()[-1500:])
    published = sorted(str(p.parent.relative_to(OWN / "outbox"))
                       for p in (OWN / "outbox").glob("*/*/published.json"))
    status = {"checked_at": iso(), "disk_free_gb": round(free_gb, 1), "runners": runner_status,
              "host": dict(metrics, active_runners=sum(runner_busy(t, r) for r in t.runners),
                           max_active_runners=int(cfg.get("max_active_runners") or 0) or None),
              "account_blocks": {a: dict(b, until=iso(b["until"])) for a, b in t.blocks.items()},
              "claims": t.claims, "signals": claim_signals(t), "packages": packages, "published": published,
              "leaderboard_our_solved": len(t.solved) if t.solved is not None else None,
              "programs": {p.get("problem_id"): {"status": p.get("status"), "reason": p.get("reason"),
                           "created_at": p.get("created_at"),
                           "lanes": {k: v.get("done") for k, v in (p.get("lanes") or {}).items()}}
                           for p in t.programs.values()},
              "inbox": {"pending": t.inbox_pending,
                        "recent": dict(sorted(t.inbox_done.items(), key=lambda kv: -record_ts(kv[1]))[:30])},
              "holds": t.holds,
              "alerts": t.alerts}
    write_json(OWN / "status.json", status)
    return status


def migrate_verification_seconds(seconds: int) -> dict[str, int]:
    """Operator: set verification_seconds of every unfinished assignment (the running job and the
    pending ones; finished history is never touched) under the controller's manifest lock. The
    running round keeps the value it started with; its continuations inherit the new one."""
    if seconds < 600:
        raise SystemExit("verification seconds below 600 look like a mistake")
    cfg = read_json(OWN / "config.json") or {}
    changed = {}
    for root in map(Path, cfg.get("runners", [])):
        if not (root / "manifest.json").is_file():
            continue
        with manifest_lock(root):
            history = (read_json(root / "state.json", {}) or {}).get("history", {})
            manifest = json.loads((root / "manifest.json").read_text())
            count = 0
            for a in manifest["assignments"]:
                if a["attempt_id"] not in history and a.get("verification_seconds") != seconds:
                    a["verification_seconds"] = seconds
                    count += 1
            if count:
                tmp = root / "manifest.json.tmp"
                tmp.write_text(json.dumps(manifest, indent=2) + "\n")
                tmp.replace(root / "manifest.json")
        changed[root.name] = count
    log(f"verification_seconds -> {seconds} in unfinished assignments: {changed}")
    return changed


def enqueue(pid: str, runner: str) -> str:
    """Operator: queue a target on a runner now, even if it has a running claim.

    The controller runs pending assignments in manifest order, so the new lineage starts
    after the current round and before later auto_continue rounds of other lineages.
    """
    cfg = read_json(OWN / "config.json") or {}
    defaults = dict(DEFAULTS, **(cfg.get("defaults") or {}))
    roots = [Path(r) for r in cfg.get("runners", []) if Path(r).name.endswith(runner)]
    if len(roots) != 1:
        raise SystemExit(f"runner {runner!r} not found")
    root = roots[0]
    targets = {t["problem_id"]: t for t in
               (read_json(OWN / "targets.json", {}) or {}).get("targets", [])}
    claims = read_json(OWN / "claims.json", {}) or {}
    if pid not in targets or targets[pid].get("disabled"):
        raise SystemExit(f"{pid} is not an enabled target")
    if pid in claims:
        raise SystemExit(f"{pid} already claimed: {claims[pid]}")
    assignments = (read_json(root / "manifest.json", {}) or {}).get("assignments", [])
    base = next_base(root, assignments, pid)
    assignment = make_assignment(targets[pid], base, sync_brief(root, base, pid), defaults)
    append_assignment(root, assignment)
    claims[pid] = {"status": "running", "runner": str(root), "base": base, "claimed_at": iso(),
                   "kind": assignment["kind"], "enqueued_by": "operator"}
    write_json(OWN / "claims.json", claims)
    log(f"enqueued {pid} on {root.name} as {base}")
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one tick (default)")
    parser.add_argument("--enqueue", metavar="PROBLEM", help="operator: queue PROBLEM now")
    parser.add_argument("--runner", default="", help="with --enqueue: wave101 or wave102")
    parser.add_argument("--program", metavar="PROBLEM", help="operator: start a lane program")
    parser.add_argument("--seed", help="with --program: seed job (abs path or <root>/<job>)")
    parser.add_argument("--migrate-verification-seconds", type=int, metavar="N",
                        help="operator: set verification_seconds of unfinished assignments to N")
    args = parser.parse_args()
    with flock(OWN / "feeder.lock", blocking=True) as acquired:
        if not acquired:
            print("another feeder tick is running")
            return
        if args.enqueue:
            print(enqueue(args.enqueue, args.runner))
            return
        if args.program:
            if not args.seed:
                parser.error("--program needs --seed")
            print(create_program(args.program, args.seed))
            return
        if args.migrate_verification_seconds:
            print(json.dumps(migrate_verification_seconds(args.migrate_verification_seconds)))
            return
        status = tick()
    print(json.dumps({k: status[k] for k in ("checked_at", "runners", "alerts")}, indent=1))


if __name__ == "__main__":
    main()
