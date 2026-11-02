"""Summarize a declaration map (map.tsv) per part of the proof tree (see harness.map)."""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

# Compiler-generated declarations: not counted as work, not reported as duplicates.
AUX = re.compile(r"(\._proof_\d+|\.match_\d+|\.eq_\d+|_def$|\.proof_\d+|\._simp_\d+|\._unfold$|\.induct$|"
                 r"\.mk\.injEq$|\.ext_iff$|\._sizeOf|\.rec$|\.recOn$|\.casesOn$|\.noConfusion|\.below$|\.brecOn$|"
                 r"\._auxLemma|\._abel_|\._flat_ctor|\.congr_simp$)")


def load(path: Path) -> list[tuple[str, str, bool, bool, bool, str]]:
    """Rows of map.tsv; the sixth column (the statement a declaration refutes, `-` if none) is optional."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = line.split("\t")
        if len(cells) in (5, 6):
            rows.append((cells[0], cells[1], cells[2] == "1", cells[3] == "1", cells[4] == "1",
                         cells[5] if len(cells) == 6 and cells[5] != "-" else ""))
    return rows


def part_of(module: str) -> str:
    """The lane of a module `Submission.Lanes.<Lane>...`; other modules by their second component."""
    parts = module.split(".")
    if len(parts) > 2 and parts[1] == "Lanes" and parts[2][:1].isupper():
        return parts[2]
    return parts[1] if len(parts) > 1 else module


def summarize(rows: list[tuple[str, str, bool, bool, bool, str]], final: str,
              lanes: dict[str, Any] | None = None) -> dict[str, Any]:
    lanes = lanes or {}
    final_row = next((r for r in rows if r[0] == final), None)
    work = [r for r in rows if not AUX.search(r[0])]
    left = sorted(({"name": n, "module": m} for n, m, own, _, reached, _ in work if own and reached),
                  key=lambda r: (r["module"], r["name"]))
    parts: dict[str, dict[str, int]] = collections.defaultdict(lambda: dict(decls=0, reached=0, sorry_dep=0, own_sorry=0))
    for _n, m, own, dep, reached, _ in work:
        p = parts[part_of(m)]
        p["decls"] += 1
        p["reached"] += reached
        p["sorry_dep"] += dep and reached
        p["own_sorry"] += own
    # Refutations: declarations whose type is `¬ …Statement`. One reached by the final theorem means a step of the
    # proof closes by contradiction with an interface statement: vacuous rather than proved.
    refuted: dict[str, list[str]] = collections.defaultdict(list)
    for n, _, _, _, _, statement in work:
        if statement:
            refuted[statement].append(n)
    refutations = sorted(n for n, _, _, _, reached, statement in work if reached and statement)
    short: dict[str, set[str]] = collections.defaultdict(set)
    for n, m, *_ in work:
        if m.startswith("Submission.Lanes."):
            short[n.rsplit(".", 1)[-1]].add(part_of(m))
    duplicates = sorted(([k, sorted(v)] for k, v in short.items() if len(v) > 1 and len(k) > 6),
                        key=lambda kv: (-len(kv[1]), kv[0]))
    return {
        "declarations": len(rows), "reached": sum(r[4] for r in rows),
        "final": final, "final_found": final_row is not None, "final_depends_on_sorry": bool(final_row and final_row[3]),
        "whats_left": left, "refutations_in_final_proof": refutations,
        "refuted_statements": {k: sorted(v) for k, v in sorted(refuted.items())},
        "parts": {k: dict(v, dead=v["decls"] - v["reached"], status=("done" if (lanes.get(k) or {}).get("done")
                                                                     else (lanes.get(k) or {}).get("status")))
                  for k, v in parts.items()},
        "duplicates": duplicates,
    }


def text(summary: dict[str, Any], limit: int = 40) -> str:
    out = [f"declarations {summary['declarations']}; reached by {summary['final']}: {summary['reached']}; "
           f"final theorem depends on sorry: {summary['final_depends_on_sorry']}"
           + ("" if summary["final_found"] else " (final theorem not found)"),
           f"what's left: {len(summary['whats_left'])} declarations with their own sorry, reached by the final theorem"]
    out += [f"  {r['name']} ({r['module']})" for r in summary["whats_left"][:limit]]
    for statement, where in summary["refuted_statements"].items():
        out.append(f"REFUTED {statement}: {', '.join(where[:6])}")
    if summary["refutations_in_final_proof"]:
        out.append("refutations the final proof relies on (vacuous steps): "
                   + ", ".join(summary["refutations_in_final_proof"][:limit]))
    out.append(f"{'part':24} {'decls':>7} {'reached':>8} {'dead':>6} {'sorry-dep':>9} {'own sorry':>9}  status")
    for name, p in sorted(summary["parts"].items(), key=lambda kv: -kv[1]["decls"]):
        out.append(f"{name:24} {p['decls']:7} {p['reached']:8} {p['dead']:6} {p['sorry_dep']:9} {p['own_sorry']:9}"
                   f"  {p['status'] or ''}")
    out.append(f"names defined in more than one lane: {len(summary['duplicates'])}")
    out += [f"  {name}: {', '.join(where)}" for name, where in summary["duplicates"][:limit]]
    return "\n".join(out) + "\n"


def from_files(map_tsv: Path, final: str, lanes_json: Path | None = None) -> dict[str, Any]:
    lanes = json.loads(lanes_json.read_text()).get("lanes", {}) if lanes_json and lanes_json.is_file() else {}
    return summarize(load(map_tsv), final, lanes)
