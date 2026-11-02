#!/usr/bin/env python3
"""fleet coordinator (runs on the operator machine, e.g. every 5 minutes).

Replaces the operator scripts move3/move5 (seeded cross-host transfers), dup (parallel lineages)
and park (weekly-exhausted accounts) with one policy over all hosts in fleet.json.

One tick:
  gather   read-only ssh to every host: coordinator_gather.py `snapshot` prints the host's
           runners (idle / STOP / held, account), account blocks, live lineages (claims with their
           last finished round: sorry, build, completeness, the NOTES `Active tactic`), passes,
           programs, targets and the outcome of our earlier directives; `best` gives, only for
           the parallel candidates, each host's best finished round of the problem
  plan     the pure function plan(state, now, policy) -> actions (tests/test_coordinator.py):
           (a) a pass anywhere stops the problem's lineages and programs on every other host;
               its claims there become passed_elsewhere, which also blocks unclaimed targets and
               retries; a host with a live CI_FIT lineage is spared while every pass of the problem
               is held at the CI-fit gate (feeder.ci_held: the passing proof does not fit CI yet);
           (b) a lineage handed off for quota that its own host cannot place within
               handoff_wait_s moves to an idle runner with quota on another host: seed exported
               from its last finished round, the source claim marked delegated, RESUME (VERIFY
               when that round is complete) on the destination; a start that the destination
               rejects or lets expire is re-placed, at most max_transfer_attempts times;
           (c) at most one active lane program per problem and route fleet-wide: a duplicate with
               fewer lanes done (then the newer one) is stopped; an operator `route` tag in the
               program file (e.g. a qualitative route beside a quantitative one) makes it a separate
               program, not a duplicate;
           (d) idle runners with quota take a PARALLEL lineage of a problem whose closeness is
               at least parallel_min_closeness and that has fewer than max_live_lineages live
               lineages, seeded from the best files fleet-wide (fewest sorry with a passing
               build), with a brief that forbids the live lineages' active tactics; at most one
               per problem per tick and per parallel_cooldown_s;
           (e) no work is ever placed on a runner whose account has a quota block on any host,
               or on a host whose feeder status is stale or has no inbox yet.
  apply    with --apply only: export seed files (fleet.py EXPORT; source host -> this machine ->
           destination <OWN>/seeds/<name>, extracted into .partial and renamed), then write each
           directive to <OWN>/inbox/<id>.json on its host; the host feeder applies it under its
           lock on its next tick (server/feeder.py, process_inbox).

The ledger (~/.local/state/leh-fleet/coordinator-ledger.json) records every directive before
it is written and the outcome its host reports. Directive ids are deterministic per decision, so
a decision is sent once and a host applies an id at most once. Failures are not replayed
blindly: the plan shows them, and only a failed transfer start is re-placed (bounded).
No tokens, no credentials, never kills anything; ssh options as fleet.py. Policy: fleet.json
"coordinator"; docs/COORDINATOR-POLICY.md. Each host reports its own state directory (`own_root`).

  coordinator.py                 dry run (default): gather, plan, print; writes nothing
  coordinator.py --apply --once  one tick that executes the plan (the LaunchAgent)
  coordinator.py --status        the ledger, no network
  coordinator.py --hold host-2:wave111 --reason "account-a weekly" [--apply]
  coordinator.py --release host-2:wave111 [--apply]
  coordinator.py --stop host-3:green_tao --reason "..." [--apply]
                                 operator directives through the same inbox and ledger (a hold
                                 replaces park.py's runner STOP: no new work, controller alive)
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = _load("leh_fleet", HERE / "fleet.py")  # EXPORT snippet, ssh options

FLEET_JSON = fleet.FLEET_PATH
GATHER = HERE / "coordinator_gather.py"
STATE = Path(os.environ.get("LEH_STATE_DIR") or Path.home() / ".local/state/leh-fleet")
LEDGER = STATE / "coordinator-ledger.json"
OWN = fleet.OWN  # the hosts' state directory, relative to the remote home (inbox writes)
SSH = [*fleet.SSH, "-o", "ServerAliveInterval=30"]
PROGRAM_FINAL = ("passed", "failed", "stopped")
RETRYABLE = ("exhausted", "failed")
# Absolute host paths reported by the hosts; no component may start with "." (no "..").
_PATH = r"(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+"
SEED_RE = re.compile(_PATH + r"/seeds/[A-Za-z0-9][A-Za-z0-9_.-]{0,200}")
JOB_RE = re.compile(_PATH + r"/[A-Za-z0-9][A-Za-z0-9_.-]*/jobs/[A-Za-z0-9][A-Za-z0-9_.-]{0,200}")
ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.~-]")

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,                 # false: --apply only prints
    "stop_on_pass": True,            # (a)
    "cross_host_handoff": True,      # (b)
    "program_dedup": True,           # (c)
    "parallel": True,                # (d)
    "parallel_min_closeness": 0.34,  # θ: closeness 1/(1+sorry+0.5·frontier) (+0.1), feeder.closeness
    "max_live_lineages": 2,          # k: live lineages per problem fleet-wide before (d) adds one
    "parallel_cooldown_s": 14400,    # one PARALLEL start per problem per this many seconds
    "parallel_max_rounds": 6,
    "blocked_live_s": 3600,          # a lineage whose account is blocked for less than this still counts as live
    "handoff_wait_s": 900,           # the lineage's own host gets this long to place a handoff itself
    "adopt_paused": False,           # also move operator-parked (paused) claims
    "max_transfer_attempts": 3,
    "max_verifies": 2,
    "default_max_rounds": 10,
    "directive_ttl_s": 1800,         # a start not applied by then expires on the host
    "result_grace_s": 600,           # a directive not seen on its host this long after sending is lost
    "max_status_age_s": 900,         # a host whose status.json is older takes no new work
    "max_starts_per_tick": 4,
    "heavy_tiers": ["hard", "extreme"],
    "default_speed": 1.0,
    "hosts": {},                     # host -> {"speed": relative throughput, "heavy_single": bool}
    "exclude_problems": [],
}


# ------------------------------------------------------------------------------- small helpers

def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def directive_id(*parts: Any) -> str:
    """A deterministic, inbox-safe id from the decision it records."""
    text = ".".join(ID_UNSAFE.sub("-", str(p)) for p in parts if p not in (None, ""))
    if len(text) > 150:
        text = text[:120].rstrip(".") + "." + hashlib.sha256(text.encode()).hexdigest()[:12]
    return text


def merged_policy(fleet_cfg: dict[str, Any]) -> dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["default_max_rounds"] = int((fleet_cfg.get("defaults") or {}).get("max_rounds", policy["default_max_rounds"]))
    extra = dict(fleet_cfg.get("coordinator") or {})
    extra.pop("note", None)
    unknown = sorted(set(extra) - set(DEFAULT_POLICY))
    if unknown:
        raise ValueError(f"unknown fleet.json coordinator keys: {unknown}")
    for key, value in extra.items():
        policy[key] = dict(policy["hosts"], **value) if key == "hosts" else value
    return policy


def host_speed(policy: dict[str, Any], host: str) -> float:
    return float((policy["hosts"].get(host) or {}).get("speed", policy["default_speed"]))


def takes_heavy(policy: dict[str, Any], host: str) -> bool:
    return bool((policy["hosts"].get(host) or {}).get("heavy_single", True))


def start_state(record: dict[str, Any], now: float, grace: float) -> str:
    """inflight / applied / failed for a start_lineage record of the ledger."""
    result = (record.get("result") or {}).get("status")
    if result in ("applied", "processed"):
        return "applied"  # `processed`: applied long ago, record pruned on the host
    if result in ("rejected", "expired", "missing") or record.get("state") == "send_failed":
        return "failed"
    if now > (number(record.get("expires_at")) or 0) + grace:
        return "failed"
    return "inflight"


# ------------------------------------------------------------------------------- fleet view (pure)

class View:
    """Facts plan() and parallel_candidates() share, from the gathered state."""

    def __init__(self, state: dict[str, Any], now: float, policy: dict[str, Any]) -> None:
        self.now, self.policy = now, policy
        self.hosts = {h: s for h, s in sorted((state.get("hosts") or {}).items()) if s.get("ok")}
        self.records: dict[str, Any] = (state.get("ledger") or {}).get("directives") or {}
        self.grace = float(policy["result_grace_s"])
        self.blocks: dict[str, float] = {}
        for snapshot in self.hosts.values():
            for account, block in (snapshot.get("account_blocks") or {}).items():
                until = number((block or {}).get("until"))
                if until and until > now:
                    self.blocks[account] = max(self.blocks.get(account, 0.0), until)
        self.passes: dict[str, dict[str, Any]] = {}
        for host, snapshot in self.hosts.items():
            for item in snapshot.get("passed") or []:
                pid = item["problem_id"]
                self.passes.setdefault(pid, {"host": host, "attempt": item.get("attempt"), "ci_held": True})
                # a pass that every reporting host holds at the CI-fit gate does not fit the organizers' CI yet
                self.passes[pid]["ci_held"] = self.passes[pid]["ci_held"] and bool(item.get("ci_held"))
            for pid, program in (snapshot.get("programs") or {}).items():
                if program.get("status") == "passed":
                    self.passes.setdefault(pid, {"host": host, "attempt": "lane program", "ci_held": False})
        self.excluded = set(policy.get("exclude_problems") or [])
        for snapshot in self.hosts.values():
            self.excluded |= set(snapshot.get("our_solved") or [])
            self.excluded |= {pid for pid, t in (snapshot.get("targets") or {}).items() if t.get("disabled")}
            self.excluded |= {c.get("problem_id") for c in (snapshot.get("claims") or {}).values()
                              if c.get("status") == "skipped_already_ours"}
        self.program_pids = {pid for s in self.hosts.values() for pid, p in (s.get("programs") or {}).items()
                             if p.get("status") not in PROGRAM_FINAL}
        self.inflight: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.records.values():
            if record.get("type") == "start_lineage" and start_state(record, now, self.grace) == "inflight":
                self.inflight[record.get("problem_id")].append(record)

    def usable(self, host: str) -> bool:
        """A destination for directives: its feeder has the inbox, wrote status recently and the
        host reported its state directory (seeds go there)."""
        snapshot = self.hosts.get(host) or {}
        checked = number(snapshot.get("checked_ts"))
        return (bool(snapshot.get("inbox_supported")) and checked is not None
                and str(snapshot.get("own_root") or "").startswith("/")
                and self.now - checked <= float(self.policy["max_status_age_s"]))

    def seeds(self, host: str) -> str:
        return f"{self.hosts[host]['own_root']}/seeds"

    def live(self, lineage: dict[str, Any]) -> bool:
        """Running on an account without a quota block (or one that lifts within blocked_live_s),
        or handed off and about to resume."""
        if lineage.get("status") == "handoff":
            return True
        if lineage.get("status") != "running":
            return False
        until = self.blocks.get(lineage.get("account"))
        return until is None or until - self.now <= float(self.policy["blocked_live_s"])

    def target(self, pid: str) -> dict[str, Any]:
        for snapshot in self.hosts.values():
            target = (snapshot.get("targets") or {}).get(pid)
            if target:
                return {k: v for k, v in target.items() if k != "disabled"}
        return {"problem_id": pid}


def parallel_candidates(state: dict[str, Any], now: float, policy: dict[str, Any],
                        view: View | None = None) -> list[dict[str, Any]]:
    """Problems that may get a PARALLEL lineage (d), closest first."""
    v = view or View(state, now, policy)
    by_pid: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for host, snapshot in v.hosts.items():
        for lineage in snapshot.get("lineages") or []:
            by_pid[lineage["problem_id"]].append((host, lineage))
    out = []
    for pid, lineages in sorted(by_pid.items()):
        if pid in v.passes or pid in v.excluded or pid in v.program_pids:
            continue
        running = [(h, l) for h, l in lineages if l.get("status") == "running"]
        if not running or any(l.get("role") == "VERIFY" or l.get("source_complete") for _, l in lineages):
            continue  # nothing runs, or a complete proof is being verified: no new route
        closeness = max(number(l.get("closeness")) or 0.0 for _, l in running)
        if closeness < float(policy["parallel_min_closeness"]):
            continue
        live = [(h, l) for h, l in lineages if v.live(l)]
        count = len(live) + len(v.inflight.get(pid, []))
        if count >= int(policy["max_live_lineages"]):
            continue
        recent = [r for r in v.records.values() if r.get("purpose") == "parallel_start" and r.get("problem_id") == pid
                  and now - (number(r.get("sent_at")) or 0) < float(policy["parallel_cooldown_s"])]
        if recent:
            continue
        out.append({"problem_id": pid, "closeness": round(closeness, 3), "live": count, "running": running,
                    "accounts": sorted({l.get("account") for _, l in lineages if l.get("account")}),
                    "heavy": any(l.get("tier") in policy["heavy_tiers"] for _, l in lineages)})
    return sorted(out, key=lambda c: (-c["closeness"], c["problem_id"]))


def best_seed(v: View, pid: str) -> tuple[str, dict[str, Any]] | None:
    """The best finished round of `pid` fleet-wide: fewest sorry with a passing build, then latest."""
    found = [(host, b) for host, s in v.hosts.items() for p, b in (s.get("best") or {}).items()
             if p == pid and b.get("build_ok") and b.get("job") and number(b.get("sorry")) is not None]
    if not found:
        return None
    return min(found, key=lambda hb: (hb[1]["sorry"], -(number(hb[1].get("finished_at")) or 0), hb[0]))


def tactic_of(lineage: dict[str, Any]) -> str:
    return lineage.get("active_tactic") or ", ".join(lineage.get("trying") or []) or "active tactic not recorded"


def history_of(host: str, lineage: dict[str, Any]) -> str:
    rows = []
    for row in lineage.get("trajectory") or []:
        if isinstance(row, list) and len(row) >= 4:
            estimate = f", estimate {row[2]} lines" if row[2] is not None else ""
            rows.append(f"- {row[0]}: sorry {row[1]}{estimate}, {row[3] or 'no progress'}")
    head = f"Sibling lineage {lineage.get('base')} on {host}/{lineage.get('runner')} (round, sorry, estimate, kind):"
    return "\n".join([head] + (rows or ["- no round measured yet"]))


# ------------------------------------------------------------------------------- plan (pure)

def plan(state: dict[str, Any], now: float, policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions for one tick, in order: export_seed (before the directives of its group),
    directive (one inbox file), alert (printed only). Deterministic for a given state."""
    p = policy
    v = View(state, now, p)
    actions: list[dict[str, Any]] = []
    planned: set[str] = set()
    ttl = float(p["directive_ttl_s"])

    def emit(group: str, host: str, directive: dict[str, Any], purpose: str, **meta: Any) -> bool:
        did = directive["id"]
        sent = v.records.get(did)
        if (sent and sent.get("state") != "send_failed") or did in planned:
            return False
        if not v.usable(host):
            actions.append({"kind": "alert", "message": f"{host}: feeder without inbox or stale status; "
                                                        f"{purpose} {did} not sent"})
            return False
        planned.add(did)
        body = {k: val for k, val in dict(directive, created_at=iso(now)).items() if val is not None}
        actions.append({"kind": "directive", "group": group, "host": host, "purpose": purpose,
                        "directive": body, "meta": meta})
        return True

    # (a) a pass anywhere stops the problem's lineages and programs on every other host
    if p["stop_on_pass"]:
        for pid, done in sorted(v.passes.items()):
            for host, snapshot in v.hosts.items():
                if host == done["host"]:
                    continue  # the passing host's feeder stops its own siblings
                lineages = [l for l in snapshot.get("lineages") or [] if l["problem_id"] == pid]
                program = (snapshot.get("programs") or {}).get(pid) or {}
                claims = [c for c in (snapshot.get("claims") or {}).values() if c.get("problem_id") == pid]
                target = (snapshot.get("targets") or {}).get(pid)
                open_target = bool(target) and not target.get("disabled") and not claims
                retryable = any(c.get("status") in RETRYABLE for c in claims)
                if done.get("ci_held") and any(l.get("role") == "CI_FIT" for l in lineages):
                    continue  # CI_FIT lineages of a pass held at the CI-fit gate are deliberate: keep them
                if not (lineages or open_target or retryable
                        or (program and program.get("status") not in PROGRAM_FINAL)):
                    continue
                emit(f"pass:{pid}:{host}", host,
                     {"id": directive_id("pass-stop", pid, host), "type": "stop_lineage", "problem_id": pid,
                      "what": "all", "mark": "passed_elsewhere",
                      "reason": f"{pid} passed on {done['host']} ({done.get('attempt')})"},
                     "pass_stop", problem_id=pid)

    # (c) one active lane program per problem and route fleet-wide
    if p["program_dedup"]:
        active: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for host, snapshot in v.hosts.items():
            for pid, program in (snapshot.get("programs") or {}).items():
                if program.get("status") not in PROGRAM_FINAL:
                    active[(pid, str(program.get("route") or ""))].append((host, program))
        for (pid, _route), programs in sorted(active.items()):
            if pid in v.passes or len(programs) < 2:
                continue
            # more lanes done first; on a tie the faster host (hosts.<h>.speed), then the older
            ranked = sorted(programs, key=lambda hp: (-(hp[1].get("lanes_done") or 0), -host_speed(p, hp[0]),
                                                      number(hp[1].get("created_ts")) or float("inf"), hp[0]))
            keep_host, keep = ranked[0]
            for host, program in ranked[1:]:
                emit(f"program:{pid}:{host}", host,
                     {"id": directive_id("program-dedup", pid, host, int(number(program.get("created_ts")) or 0)),
                      "type": "stop_lineage", "problem_id": pid, "what": "program", "mark": "stopped",
                      "reason": (f"duplicate lane program; {keep_host} keeps the program of {pid} "
                                 f"({keep.get('lanes_done')} of {keep.get('lanes')} lanes done, "
                                 f"created {keep.get('created_at')})")},
                     "program_dedup", problem_id=pid)

    # idle runners with quota on usable hosts, minus those promised to starts in flight
    pool = []
    for host, snapshot in v.hosts.items():
        if not v.usable(host):
            continue
        for wave, runner in sorted((snapshot.get("runners") or {}).items()):
            if (runner.get("idle") and not runner.get("stop") and not runner.get("held")
                    and runner.get("account") not in v.blocks):
                pool.append({"host": host, "runner": wave, "account": runner.get("account")})
    for records in v.inflight.values():
        for record in records:
            match = (next((r for r in pool if r["host"] == record.get("host")
                           and r["runner"] == record.get("prefer_runner")), None)
                     or next((r for r in pool if r["host"] == record.get("host")), None))
            if match:
                pool.remove(match)

    def pick(heavy: bool, accounts: set[str]) -> dict[str, Any] | None:
        fits = [r for r in pool if not (heavy and not takes_heavy(p, r["host"]))]
        if not fits:
            return None
        fits.sort(key=lambda r: (-host_speed(p, r["host"]), r["account"] in accounts, r["host"], r["runner"]))
        pool.remove(fits[0])
        return fits[0]

    starts = 0
    started: set[str] = set()

    # (b) handoffs their own host cannot place move to another host's idle runner with quota
    if p["cross_host_handoff"]:
        transfers: dict[str, dict[str, Any]] = defaultdict(lambda: {"release": None, "starts": []})
        for record in v.records.values():
            tid = (record.get("meta") or {}).get("transfer")
            if tid and record.get("purpose") == "handoff_release":
                transfers[tid]["release"] = record
            elif tid and record.get("purpose") == "handoff_start":
                transfers[tid]["starts"].append(record)
        items = []
        for host, snapshot in v.hosts.items():
            if not v.usable(host):
                continue  # the source must be able to mark its claim delegated
            for lineage in snapshot.get("lineages") or []:
                pid = lineage["problem_id"]
                if lineage.get("status") != "handoff" and not (lineage.get("status") == "paused" and p["adopt_paused"]):
                    continue
                if pid in v.passes or pid in v.excluded or lineage.get("round_open") or not lineage.get("source_job"):
                    continue
                if (lineage.get("status") == "handoff"
                        and now - (number(lineage.get("handoff_at")) or 0) < float(p["handoff_wait_s"])):
                    continue
                if any(r["host"] == host for r in pool):
                    continue  # the host's own feeder places it on its next tick
                tid = directive_id(pid, host, lineage["base"])
                if tid in transfers:
                    continue  # a transfer of this lineage is already under way
                items.append({"tid": tid, "pid": pid, "src": host, "lineage": lineage, "attempt": 1, "release": True})
        for tid, transfer in sorted(transfers.items()):
            release = transfer["release"]
            if not release or (release.get("result") or {}).get("status") != "applied":
                if (release and (release.get("result") or {}).get("status") in ("rejected", "expired")
                        and now - (number(release.get("sent_at")) or 0) < 21600):
                    applied = [r for r in transfer["starts"] if start_state(r, now, v.grace) == "applied"]
                    if applied:
                        actions.append({"kind": "alert", "message": (
                            f"transfer {tid}: the source kept its lineage ({(release.get('result') or {}).get('detail')}); "
                            f"the moved lineage on {applied[0].get('host')} runs beside it")})
                continue
            states = [start_state(r, now, v.grace) for r in transfer["starts"]]
            if "applied" in states or "inflight" in states:
                continue
            meta = release.get("meta") or {}
            pid = release.get("problem_id")
            if pid in v.passes or pid in v.excluded or not meta.get("lineage"):
                continue
            if len(states) >= int(p["max_transfer_attempts"]):
                actions.append({"kind": "alert", "message": (
                    f"transfer {tid}: {len(states)} starts failed; the lineage is delegated on "
                    f"{meta.get('src_host')} and runs nowhere ({meta['lineage'].get('source_job')}); operator")})
                continue
            items.append({"tid": tid, "pid": pid, "src": meta.get("src_host"), "lineage": meta["lineage"],
                          "attempt": len(states) + 1, "release": False})
        for item in sorted(items, key=lambda i: (-(number(i["lineage"].get("closeness")) or 0), i["tid"])):
            if starts >= int(p["max_starts_per_tick"]):
                break
            lineage, pid, src = item["lineage"], item["pid"], item["src"]
            if not JOB_RE.fullmatch(str(lineage.get("source_job"))):
                actions.append({"kind": "alert", "message": f"transfer {item['tid']}: unusable source {lineage.get('source_job')}"})
                continue
            dest = pick(lineage.get("tier") in p["heavy_tiers"], {lineage.get("account")})
            if dest is None:
                actions.append({"kind": "alert", "message": (
                    f"handoff {pid} from {src}/{lineage.get('runner')} ({lineage.get('account')}) waits: "
                    "no idle runner with quota on any host")})
                continue
            job = lineage["source_job"]
            seed = f"{v.seeds(dest['host'])}/{pid}-xfer-{Path(job).name}"
            group = f"xfer:{item['tid']}:a{item['attempt']}"
            verify = (lineage.get("source_complete") and not lineage.get("verify_exhausted")
                      and int(lineage.get("verifies") or 0) < int(p["max_verifies"]))
            role = "VERIFY" if verify else "RESUME"
            total = int(v.target(pid).get("max_rounds") or p["default_max_rounds"])
            reason = (f"{lineage.get('handoff_window') or 'quota'} pause of {lineage.get('account')} on "
                      f"{src}/{lineage.get('runner')}; moved by the fleet coordinator")
            actions.append({"kind": "export_seed", "group": group, "src_host": src, "job": job,
                            "dest_host": dest["host"], "seed_dir": seed})
            if item["release"]:
                compact = {k: lineage.get(k) for k in ("key", "base", "runner", "account", "status", "role", "tier",
                                                         "closeness", "rounds_done", "source_job", "source_complete",
                                                         "verifies", "verify_exhausted", "route_review",
                                                         "handoff_window")}
                emit(group, src,
                     {"id": directive_id("xfer", item["tid"], "release"), "type": "stop_lineage",
                      "problem_id": pid, "base": lineage["base"], "mark": "delegated", "to_host": dest["host"],
                      "expect_status": [lineage.get("status")], "reason": f"moved to {dest['host']}/{dest['runner']}"},
                     "handoff_release", problem_id=pid, transfer=item["tid"], src_host=src, lineage=compact)
            emit(group, dest["host"],
                 {"id": directive_id("xfer", item["tid"], f"a{item['attempt']}"), "type": "start_lineage",
                  "problem_id": pid, "role": role, "seed_dir": seed, "prefer_runner": dest["runner"],
                  "max_rounds": max(1, total - int(lineage.get("rounds_done") or 0)) if role == "RESUME" else None,
                  "fields": {"reason": reason}, "reason": reason, "from_host": src, "from_job": job,
                  "from_key": lineage.get("key"), "route_review_done": bool(lineage.get("route_review")),
                  "target": v.target(pid), "expires_at": now + ttl},
                 "handoff_start", problem_id=pid, transfer=item["tid"], prefer_runner=dest["runner"])
            starts += 1
            started.add(pid)

    # (d) idle runners with quota: a PARALLEL lineage for close problems with < k live lineages
    if p["parallel"]:
        stamp = time.strftime("%Y%m%dT%H%MZ", time.gmtime(now))
        for cand in parallel_candidates(state, now, p, v):
            if starts >= int(p["max_starts_per_tick"]) or not pool:
                break
            pid = cand["problem_id"]
            if pid in started:
                continue
            seed_src = best_seed(v, pid)
            if seed_src is None:
                actions.append({"kind": "alert", "message": f"parallel {pid}: no finished round with a passing build"})
                continue
            src_host, best = seed_src
            if not JOB_RE.fullmatch(str(best["job"])):
                continue
            dest = pick(cand["heavy"], set(cand["accounts"]))
            if dest is None:
                continue
            avoid: list[str] = []
            for _, lineage in cand["running"]:
                for tactic in [lineage.get("active_tactic"), *(lineage.get("trying") or [])]:
                    if tactic and tactic not in avoid:
                        avoid.append(tactic)
            main_host, main = max(cand["running"], key=lambda hl: number(hl[1].get("closeness")) or 0)
            seed = f"{v.seeds(dest['host'])}/{pid}-par-{Path(best['job']).name}"
            group = f"par:{pid}:{stamp}"
            reason = (f"parallel lineage: closeness {cand['closeness']}, {cand['live']} live lineage(s); "
                      f"seed {Path(best['job']).name} on {src_host} (sorry {best.get('sorry')})")
            actions.append({"kind": "export_seed", "group": group, "src_host": src_host, "job": best["job"],
                            "dest_host": dest["host"], "seed_dir": seed})
            emit(group, dest["host"],
                 {"id": directive_id("par", pid, stamp), "type": "start_lineage", "problem_id": pid,
                  "role": "PARALLEL", "seed_dir": seed, "prefer_runner": dest["runner"],
                  "max_rounds": int(p["parallel_max_rounds"]),
                  "fields": {"avoid_tactics": avoid,
                             "parallel_of": "; ".join(f"{h}/{l.get('runner')} {l.get('base')}: {tactic_of(l)}"
                                                      for h, l in cand["running"]),
                             "history": history_of(main_host, main)},
                  "reason": reason, "from_host": src_host, "from_job": best["job"], "expires_at": now + ttl},
                 "parallel_start", problem_id=pid, prefer_runner=dest["runner"])
            starts += 1
            started.add(pid)
    return actions


# ------------------------------------------------------------------------------- ledger

class Ledger:
    """directives: id -> {host, type, purpose, problem_id, state sending/sent/send_failed, sent_at,
    expires_at, prefer_runner, meta, result {status, detail, at}}."""

    def __init__(self, path: Path, persist: bool = True) -> None:
        self.path, self.persist = Path(path), persist
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {"version": 1, "directives": {}}
        self.data.setdefault("directives", {})

    @property
    def directives(self) -> dict[str, Any]:
        return self.data["directives"]

    def save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def open_ids(self, host: str) -> list[str]:
        return sorted(i for i, r in self.directives.items()
                      if r.get("host") == host and not r.get("result") and r.get("state") in ("sending", "sent"))

    def merge(self, snapshots: dict[str, Any], now: float, grace: float) -> list[str]:
        """Outcomes reported by the hosts: applied / rejected / expired; `missing` once a sent
        directive has not shown up on its host for `grace` seconds."""
        notes = []
        for host, snapshot in snapshots.items():
            if not snapshot.get("ok"):
                continue
            for did, result in ((snapshot.get("inbox") or {}).get("results") or {}).items():
                record = self.directives.get(did)
                if not record or record.get("result"):
                    continue
                status = (result or {}).get("status")
                if status in ("applied", "rejected", "expired", "processed"):
                    record["result"] = {"status": status, "detail": (result or {}).get("detail"),
                                        "at": (result or {}).get("at") or iso(now)}
                    notes.append(f"{host} {did}: {status} {json.dumps((result or {}).get('detail'), default=str)[:200]}")
                elif status == "missing" and now - (number(record.get("sent_at")) or now) > grace:
                    record["result"] = {"status": "missing", "detail": "not found on the host", "at": iso(now)}
                    notes.append(f"{host} {did}: missing")
        self.save()
        return notes

    def sending(self, action: dict[str, Any], now: float) -> None:
        directive = action["directive"]
        self.directives[directive["id"]] = {
            "id": directive["id"], "host": action["host"], "type": directive["type"], "purpose": action["purpose"],
            "problem_id": directive.get("problem_id"), "prefer_runner": directive.get("prefer_runner"),
            "expires_at": directive.get("expires_at"), "state": "sending", "sent_at": now,
            "meta": action.get("meta") or {}, "result": None}
        self.save()

    def mark(self, did: str, state: str, error: str | None = None) -> None:
        record = self.directives[did]
        record["state"] = state
        if error:
            record["error"] = error[:500]
        self.save()


# ------------------------------------------------------------------------------- I/O

def log(message: str) -> None:
    line = f"{iso(time.time())} {message}"
    print(line, flush=True)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "coordinator.log").open("a") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


class RemoteIO:
    """ssh to the fleet hosts (fleet.py options: BatchMode, strict host keys)."""

    def ssh(self, host: str, command: str, stdin: bytes = b"", timeout: int = 600) -> bytes:
        out = subprocess.run([*SSH, host, command], input=stdin, capture_output=True, timeout=timeout)
        if out.returncode:
            raise RuntimeError(f"{host}: {command[:80]}: rc={out.returncode} "
                               f"{out.stderr.decode(errors='replace')[-400:]}")
        return out.stdout

    def gather(self, host: str, mode: str, values: list[str]) -> dict[str, Any]:
        out = self.ssh(host, f"LEH_REMOTE_OWN_ROOT={shlex.quote(OWN)} python3 -B - {mode} {shlex.quote(json.dumps(values))}",
                       GATHER.read_bytes(), timeout=600)
        lines = out.decode(errors="replace").strip().splitlines()
        return json.loads(lines[-1]) if lines else {}

    def export_seed(self, src: str, job: str, dest: str, seed: str) -> str:
        if not SEED_RE.fullmatch(seed) or not JOB_RE.fullmatch(job):
            raise ValueError(f"unsafe seed {seed!r} or job {job!r}")
        q = shlex.quote
        if self.ssh(dest, f"test -f {q(seed + '/Submission.lean')} && echo yes || true").strip() == b"yes":
            return "already there"
        blob = self.ssh(src, f"python3 - {q(job + '/workspace')}", fleet.EXPORT.encode(), timeout=900)
        if len(blob) < 1024:
            raise RuntimeError(f"export of {job} produced no archive")
        partial = seed + ".partial"
        self.ssh(dest, f"rm -rf {q(partial)} && mkdir -p {q(partial)} && tar -xf - -C {q(partial)} && "
                       f"test -f {q(partial + '/Submission.lean')} && mv -T {q(partial)} {q(seed)}", blob, timeout=900)
        return f"{len(blob)} bytes"

    def drop_directive(self, host: str, directive: dict[str, Any]) -> None:
        did = directive["id"]
        body = (json.dumps(directive, indent=1, sort_keys=True) + "\n").encode()
        q = shlex.quote
        tmp, final = f"{OWN}/inbox/.{did}.tmp", f"{OWN}/inbox/{did}.json"
        self.ssh(host, f"mkdir -p {q(OWN + '/inbox')} && cat > {q(tmp)} && mv {q(tmp)} {q(final)}", body)


def gather_all(io: Any, hosts: list[str], ledger: Ledger) -> dict[str, Any]:
    snapshots = {}
    for host in hosts:
        try:
            snapshot = io.gather(host, "snapshot", ledger.open_ids(host))
            snapshot["ok"] = True
        except Exception as exc:  # noqa: BLE001 - an unreachable host is skipped this tick
            snapshot = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[-300:]}"}
        snapshots[host] = snapshot
    return snapshots


def execute(actions: list[dict[str, Any]], ledger: Ledger, now: float, io: Any) -> int:
    """Run the plan: a failed step skips the rest of its group (a start never goes out without its
    seed, a release never without its start's seed)."""
    failed: set[str] = set()
    failures = 0
    for action in actions:
        group = action.get("group")
        if action["kind"] == "alert":
            log(f"ALERT {action['message']}")
            continue
        if group in failed:
            log(f"skipped {action['kind']} of {group}: an earlier step of it failed")
            continue
        try:
            if action["kind"] == "export_seed":
                note = io.export_seed(action["src_host"], action["job"], action["dest_host"], action["seed_dir"])
                log(f"seed {action['dest_host']}:{action['seed_dir']} from {action['src_host']}:{action['job']} ({note})")
            elif action["kind"] == "directive":
                did = action["directive"]["id"]
                ledger.sending(action, now)
                io.drop_directive(action["host"], action["directive"])
                ledger.mark(did, "sent")
                log(f"directive {action['host']} {did} ({action['purpose']})")
        except Exception as exc:  # noqa: BLE001 - recorded; the next tick decides again
            failures += 1
            failed.add(group)
            if action["kind"] == "directive":
                ledger.mark(action["directive"]["id"], "send_failed", f"{type(exc).__name__}: {exc}")
            log(f"FAILED {action['kind']} ({group}): {type(exc).__name__}: {str(exc)[-300:]}")
    return failures


def describe(snapshots: dict[str, Any], actions: list[dict[str, Any]], now: float) -> str:
    lines = []
    for host, snapshot in sorted(snapshots.items()):
        if not snapshot.get("ok"):
            lines.append(f"== {host}: unavailable ({snapshot.get('error')})")
            continue
        idle = [w for w, r in (snapshot.get("runners") or {}).items() if r.get("idle")]
        age = now - (number(snapshot.get("checked_ts")) or 0)
        lines.append(f"== {host}: status {age / 60:.0f} min old, inbox {'yes' if snapshot.get('inbox_supported') else 'NO'}; "
                     f"idle {idle}; blocks {sorted(snapshot.get('account_blocks') or {})}; "
                     f"lineages {[(l['problem_id'], l['status'], l.get('closeness')) for l in snapshot.get('lineages') or []]}")
    lines.append(f"-- plan: {sum(a['kind'] == 'directive' for a in actions)} directive(s), "
                 f"{sum(a['kind'] == 'export_seed' for a in actions)} seed export(s)")
    for action in actions:
        if action["kind"] == "directive":
            d = action["directive"]
            what = {k: d.get(k) for k in ("problem_id", "role", "base", "mark", "what", "prefer_runner", "seed_dir") if d.get(k)}
            lines.append(f"  {action['host']} <- {d['type']} {d['id']} {json.dumps(what)}")
            if d.get("reason"):
                lines.append(f"      reason: {d['reason']}")
        elif action["kind"] == "export_seed":
            lines.append(f"  seed {action['src_host']}:{action['job']} -> {action['dest_host']}:{action['seed_dir']}")
        else:
            lines.append(f"  ALERT {action['message']}")
    return "\n".join(lines)


def print_status(ledger: Ledger) -> None:
    counts: dict[str, int] = defaultdict(int)
    for record in ledger.directives.values():
        counts[(record.get("result") or {}).get("status") or record.get("state")] += 1
    print(f"ledger {ledger.path}: {dict(counts)}")
    for did, record in sorted(ledger.directives.items(), key=lambda kv: -(number(kv[1].get("sent_at")) or 0))[:40]:
        result = record.get("result") or {}
        print(f"  {iso(number(record.get('sent_at')) or 0)} {record.get('host')} {did}: "
              f"{result.get('status') or record.get('state')} {json.dumps(result.get('detail'), default=str)[:160]}")


def operator_actions(args: argparse.Namespace, hosts: list[str], now: float) -> list[dict[str, Any]]:
    """--hold/--release HOST:RUNNER and --stop HOST:PROBLEM as inbox directives (the park.py runner
    STOP becomes a hold: no new work, the controller keeps running)."""
    actions = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    reason = f"operator: {args.reason}" if args.reason else "operator"
    for kind, values in (("hold_runner", args.hold), ("release_runner", args.release), ("stop_lineage", args.stop)):
        for value in values or []:
            host, _, name = value.partition(":")
            if host not in hosts or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise SystemExit(f"{value!r}: expected HOST:{'PROBLEM' if kind == 'stop_lineage' else 'RUNNER'} "
                                 f"with HOST in {hosts}")
            directive = {"id": directive_id("op", kind.split("_")[0], host, name, stamp), "type": kind,
                         "reason": reason, "created_at": iso(now)}
            directive.update({"problem_id": name, "what": "all", "mark": "stopped"} if kind == "stop_lineage"
                             else {"runner": name})
            actions.append({"kind": "directive", "group": directive["id"], "host": host, "purpose": "operator",
                            "directive": directive, "meta": {}})
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="gather and print the plan (the default)")
    mode.add_argument("--apply", action="store_true", help="execute the plan: seeds and inbox directives")
    parser.add_argument("--once", action="store_true", help="one tick, then exit (LaunchAgent; a tick is always one)")
    parser.add_argument("--status", action="store_true", help="print the ledger and exit (no network)")
    parser.add_argument("--ledger", default=str(LEDGER))
    parser.add_argument("--json", action="store_true", help="print the actions as JSON")
    parser.add_argument("--hold", action="append", metavar="HOST:RUNNER", help="operator: no new work for a runner")
    parser.add_argument("--release", action="append", metavar="HOST:RUNNER", help="operator: end a hold")
    parser.add_argument("--stop", action="append", metavar="HOST:PROBLEM",
                        help="operator: stop every lineage and program of a problem on a host (claims `stopped`)")
    parser.add_argument("--reason", default="", help="with --hold/--release/--stop")
    args = parser.parse_args(argv)
    fleet_cfg = json.loads(FLEET_JSON.read_text())
    policy = merged_policy(fleet_cfg)
    if args.status:
        print_status(Ledger(Path(args.ledger), persist=False))
        return 0
    if args.hold or args.release or args.stop:
        now = time.time()
        actions = operator_actions(args, list(fleet_cfg["hosts"]), now)
        print(describe({}, actions, now))
        if not args.apply:
            print("DRY RUN: nothing written; --apply sends these directives")
            return 0
        lock_path = Path(args.ledger).parent / "coordinator.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # waits for a running tick; the ledger has one writer
            return 1 if execute(actions, Ledger(Path(args.ledger)), now, RemoteIO()) else 0
    io = RemoteIO()
    lock_path = Path(args.ledger).parent / "coordinator.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        if args.apply:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("another coordinator tick holds the lock")
                return 0
        ledger = Ledger(Path(args.ledger), persist=args.apply)
        now = time.time()
        snapshots = gather_all(io, list(fleet_cfg["hosts"]), ledger)
        for note in ledger.merge(snapshots, now, float(policy["result_grace_s"])):
            log(f"outcome {note}")
        state = {"hosts": snapshots, "ledger": ledger.data}
        pids = [c["problem_id"] for c in parallel_candidates(state, now, policy)] if policy["parallel"] else []
        if pids:
            for host, snapshot in snapshots.items():
                if snapshot.get("ok"):
                    try:
                        snapshot["best"] = io.gather(host, "best", pids)
                    except Exception as exc:  # noqa: BLE001
                        log(f"{host}: best rounds unavailable: {type(exc).__name__}: {str(exc)[-200:]}")
        actions = plan(state, now, policy)
        print(json.dumps(actions, indent=1, default=str) if args.json else describe(snapshots, actions, now))
        if not args.apply:
            print("DRY RUN: nothing written; --apply executes")
            return 0
        if not policy["enabled"]:
            log("coordinator disabled (fleet.json coordinator.enabled); nothing applied")
            return 0
        failures = execute(actions, ledger, now, io)
        ledger.save()
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
