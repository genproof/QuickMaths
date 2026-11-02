"""Durable solo-run accounting; process supervision and proof verification live elsewhere.

Reservations are conservative: a lost process never frees its allowance. A supervisor
must reconcile its usage with ``finish`` before retrying work. USD is unknown for
subscription-backed execution unless the caller has an actual accounting source.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class RuntimeBlocked(RuntimeError):
    """Cancellation, unresolved usage, or exhausted allowance prevents execution."""


@dataclass(frozen=True)
class Budget:
    tokens: int
    wall_seconds: float
    usd: float | None = None

    def __post_init__(self) -> None:
        if self.tokens is None or self.wall_seconds is None:
            raise ValueError("budget requires known token and wall allowances")
        _validate(self.tokens, self.wall_seconds, self.usd)


@dataclass(frozen=True)
class Usage:
    tokens: int | None
    wall_seconds: float | None
    usd: float | None = None

    def __post_init__(self) -> None:
        _validate(self.tokens, self.wall_seconds, self.usd)


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    generation: int
    checkpoint_hash: str | None
    tokens: int
    wall_seconds: float
    usd: float | None


@dataclass(frozen=True)
class Checkpoint:
    generation: int
    content_hash: str
    content: dict[str, Any]


def _validate(tokens: int | None, wall: float | None, usd: float | None) -> None:
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise ValueError("tokens must be a nonnegative integer")
    for value in (wall, usd):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("wall_seconds and usd must be finite and nonnegative")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RunLedger:
    """A SQLite ledger for one run, safely reopened by a replacement supervisor.

    ``wall_seconds`` bounds both cumulative accounted work and elapsed time since
    run creation (including downtime). Attempts are not a scheduler or proof verdict.
    ``identity`` should bind problem/revision, model and actual backend configuration.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        budget: Budget | None = None,
        identity: dict[str, Any] | None = None,
        cancellation_file: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cancellation_file = Path(cancellation_file) if cancellation_file else None
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, data TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, data TEXT)""")
            row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
            if row is None:
                if budget is None:
                    raise ValueError("new ledger requires a budget")
                state = {"budget": asdict(budget), "identity": identity or {},
                         "created_at": time.time(), "generation": 0, "checkpoint": None}
                db.execute("INSERT INTO state VALUES (1, ?)", (_json(state),))
                self._event(db, "created", state)
            else:
                state = json.loads(row[0])
                if budget is not None and state["budget"] != asdict(budget):
                    raise ValueError("resume budget differs from persisted budget")
                if identity is not None and state["identity"] != identity:
                    raise ValueError("resume identity differs from persisted identity")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db: sqlite3.Connection) -> dict[str, Any]:
        return json.loads(db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])

    @staticmethod
    def _event(
        db: sqlite3.Connection, kind: str, data: Any, event_id: str | None = None,
    ) -> None:
        payload = {"kind": kind, "at": time.time(), "data": data}
        db.execute("INSERT INTO events(event_id, data) VALUES (?, ?)", (event_id, _json(payload)))

    def cancelled(self) -> bool:
        return self.cancellation_file is not None and self.cancellation_file.exists()

    @staticmethod
    def _totals(db: sqlite3.Connection) -> dict[str, Any]:
        totals: dict[str, Any] = {"tokens": 0, "wall_seconds": 0.0, "usd": 0.0,
                                  "unresolved": False}
        for (raw,) in db.execute("SELECT data FROM attempts"):
            attempt = json.loads(raw)
            usage = attempt.get("usage")
            unresolved = attempt["status"] != "settled"
            totals["unresolved"] |= attempt["status"] == "ambiguous"
            for key in ("tokens", "wall_seconds", "usd"):
                value = usage.get(key) if usage else None
                if unresolved:
                    reserved = attempt["reservation"][key]
                    value = max(value or 0, reserved) if reserved is not None else value
                if key == "usd" and (value is None or totals[key] is None):
                    totals[key] = None
                else:
                    totals[key] += value or 0
        return totals

    def reserve(
        self, attempt_id: str, *, tokens: int, wall_seconds: float, usd: float | None = None,
    ) -> Reservation:
        Budget(tokens, wall_seconds, usd)
        if not attempt_id:
            raise ValueError("attempt_id must be nonempty")
        with self._transaction() as db:
            state = self._state(db)
            if self.cancelled():
                raise RuntimeBlocked("external cancellation requested")
            if db.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
                raise RuntimeBlocked("attempt already exists; reconcile instead of blind retry")
            totals = self._totals(db)
            if totals["unresolved"]:
                raise RuntimeBlocked("ambiguous usage requires reconciliation")
            budget = state["budget"]
            if time.time() - state["created_at"] >= budget["wall_seconds"]:
                raise RuntimeBlocked("run wall deadline exhausted")
            requests = (("tokens", tokens), ("wall_seconds", wall_seconds), ("usd", usd))
            for key, requested in requests:
                cap = budget[key]
                if cap is not None and (requested is None or totals[key] is None
                                        or totals[key] + requested > cap):
                    raise RuntimeBlocked(f"{key} allowance exhausted or unknown")
            checkpoint = state["checkpoint"]
            reservation = Reservation(attempt_id, state["generation"],
                                      checkpoint["content_hash"] if checkpoint else None,
                                      tokens, wall_seconds, usd)
            attempt = {"reservation": asdict(reservation), "status": "reserved", "usage": None}
            db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, _json(attempt)))
            self._event(db, "reserved", asdict(reservation))
            return reservation

    def append_event(self, kind: str, data: dict[str, Any]) -> None:
        """Append supervisor diagnostics without changing budget or proof state."""
        with self._transaction() as db:
            self._event(db, kind, data)

    def observe_usage(self, attempt_id: str, usage: Usage) -> bool:
        """Store cumulative observed usage; return whether supervisor should stop.

        CLI tool loops cannot prebound token consumption. Reservations are admission
        allowances; this is a stopping threshold, and incurred overruns stay charged.
        Count input + output tokens; cached input is already part of input tokens.
        """
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt["status"] == "settled":
                raise ValueError("attempt already settled")
            previous = attempt.get("usage")
            values = asdict(usage)
            if previous and any(previous[k] is not None and
                                (values[k] is None or values[k] < previous[k])
                                for k in previous):
                raise ValueError("observed cumulative usage cannot decrease")
            attempt["usage"] = values
            db.execute("UPDATE attempts SET data=? WHERE id=?", (_json(attempt), attempt_id))
            self._event(db, "usage", {"attempt_id": attempt_id, "usage": values})
            reserved = attempt["reservation"]
            state = self._state(db)
            exhausted = any(values[k] is not None and reserved[k] is not None
                            and values[k] >= reserved[k] for k in values)
            return (self.cancelled() or exhausted or
                    time.time() - state["created_at"] >= state["budget"]["wall_seconds"])

    def mark_ambiguous(self, attempt_id: str, reason: str) -> None:
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt["status"] == "settled":
                raise ValueError("cannot mark a settled attempt ambiguous")
            attempt["status"] = "ambiguous"
            db.execute("UPDATE attempts SET data=? WHERE id=?", (_json(attempt), attempt_id))
            self._event(db, "ambiguous", {"attempt_id": attempt_id, "reason": reason})

    @staticmethod
    def _attempt(db: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
        row = db.execute("SELECT data FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ValueError("unknown attempt")
        return json.loads(row[0])

    def finish(
        self, attempt_id: str, *, event_id: str, usage: Usage, result: dict[str, Any],
        generation: int, checkpoint_hash: str | None, ambiguous: bool = False,
    ) -> bool:
        """Account incurred work even for stale results; return whether result is current.

        Exact event replays are idempotent. Reconciliation uses a new event ID and
        cumulative final usage for the same attempt, never an incremental delta.
        """
        if not event_id:
            raise ValueError("event_id must be nonempty")
        payload = {"attempt_id": attempt_id, "usage": asdict(usage), "result": result,
                   "generation": generation, "checkpoint_hash": checkpoint_hash,
                   "ambiguous": ambiguous}
        with self._transaction() as db:
            old = db.execute("SELECT data FROM events WHERE event_id=?", (event_id,)).fetchone()
            if old:
                saved = json.loads(old[0])["data"]
                if saved["input"] != payload:
                    raise ValueError("event_id reused with different result")
                return saved["accepted"]
            attempt = self._attempt(db, attempt_id)
            if attempt["status"] == "settled":
                raise ValueError("attempt already settled")
            previous = attempt.get("usage")
            if previous and any(previous[k] is not None and
                                (asdict(usage)[k] is None or asdict(usage)[k] < previous[k])
                                for k in previous):
                raise ValueError("reconciled cumulative usage cannot decrease")
            state = self._state(db)
            checkpoint = state["checkpoint"]
            expected_hash = checkpoint["content_hash"] if checkpoint else None
            reserved = attempt["reservation"]
            current = (generation == state["generation"] == reserved["generation"]
                       and checkpoint_hash in (expected_hash, reserved["checkpoint_hash"]))
            unknown = usage.tokens is None or usage.wall_seconds is None
            if state["budget"]["usd"] is not None and usage.usd is None:
                unknown = True
            accepted = current and not ambiguous and not unknown and not self.cancelled()
            attempt.update(usage=asdict(usage), status="ambiguous" if ambiguous or unknown
                           else "settled")
            db.execute("UPDATE attempts SET data=? WHERE id=?", (_json(attempt), attempt_id))
            self._event(db, "result", {"input": payload, "accepted": accepted}, event_id)
            return accepted

    def checkpoint(
        self, content: dict[str, Any], *, advance_generation: bool = False,
    ) -> Checkpoint:
        serialized = _json(content)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self._transaction() as db:
            if self.cancelled():
                raise RuntimeBlocked("external cancellation requested")
            state = self._state(db)
            checkpoint = Checkpoint(
                state["generation"] + int(advance_generation), digest, json.loads(serialized),
            )
            state.update(generation=checkpoint.generation, checkpoint=asdict(checkpoint))
            db.execute("UPDATE state SET data=? WHERE id=1", (_json(state),))
            self._event(db, "checkpoint", asdict(checkpoint))
            return checkpoint

    def resume(self) -> Checkpoint | None:
        with self._transaction() as db:
            checkpoint = self._state(db)["checkpoint"]
            if checkpoint is None:
                return None
            if hashlib.sha256(_json(checkpoint["content"]).encode()).hexdigest() != checkpoint[
                "content_hash"
            ]:
                raise ValueError("checkpoint content hash mismatch")
            return Checkpoint(**checkpoint)

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as db:
            state = self._state(db)
            return {**state, "accounted": self._totals(db), "cancelled": self.cancelled(),
                    "attempts": {key: json.loads(raw) for key, raw in
                                 db.execute("SELECT id, data FROM attempts")}}

    def events(self) -> list[dict[str, Any]]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM events ORDER BY seq")
            return [json.loads(raw) for (raw,) in rows]
