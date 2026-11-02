"""Local verification commands. Never publish benchmark submissions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from leaneval.artifacts import export_bundle, load_bundle
from leaneval.verifier import accept_receipt, verify


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="leaneval",
        description="Export and verify local proof bundles. No submissions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle", help="Export proof files from an exact Git commit")
    bundle.add_argument("problem_id")
    bundle.add_argument("--repo", type=Path, required=True)
    bundle.add_argument("--commit", required=True)
    bundle.add_argument("--workspace", required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--controls", action="store_true")
    for name in ("verify", "receipt"):
        command = commands.add_parser(name, help="Local official verification / receipt validation")
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--runs", type=Path, required=True)
        command.add_argument("--controls", action="store_true")
        if name == "verify":
            command.add_argument("--workbench", type=Path, required=True)
            command.add_argument("--seconds", type=int, required=True)
        else:
            command.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "bundle":
            bundle = export_bundle(args.repo, args.commit, args.workspace, args.problem_id,
                                   args.output, controls=args.controls)
            print(json.dumps({"artifact_digest": bundle.identity, "bundle": str(args.output)}))
        elif args.command == "verify":
            receipt = verify(args.bundle, args.workbench, args.runs, seconds=args.seconds,
                             controls=args.controls)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0 if receipt["status"] == "official_local_pass" else 1
        else:
            receipt = json.loads(args.receipt.read_text())
            bundle = load_bundle(args.bundle, controls=args.controls)
            accepted = accept_receipt(args.runs, receipt, bundle)
            print(json.dumps({"current_artifact_verified": accepted, "service_acceptance": False}))
            return 0 if accepted else 1
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"leaneval: {exc}", file=sys.stderr)
        return 2
    return 0
