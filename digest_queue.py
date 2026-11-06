#!/usr/bin/env python3
"""Prepare and acknowledge digest batches in a durable local state directory.

A single digest worker must own this directory. Never acknowledge uncertain sends.
"""
import argparse
import json
from pathlib import Path

from gig_radar import atomic_write


def prepare(archive: Path, state: Path, preview: bool = False) -> dict:
    data = json.loads(archive.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported archive version")
    ledger_path = state / "processed.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    pending_path = state / "pending.json"
    if pending_path.exists() and not preview:
        # A previous send may have succeeded. Require review instead of resending.
        raise ValueError("Unresolved pending batch. Check Sent mail, then ack or discard it.")
    batch = {"schema_version": 1, "gigs": [g for g in data["gigs"] if not any(k in ledger for k in [g["id"]] + g.get("aliases", []))]}
    if not preview:
        atomic_write(pending_path, json.dumps(batch, indent=2) + "\n")
    return batch


def acknowledge(state: Path) -> None:
    pending_path = state / "pending.json"
    pending = json.loads(pending_path.read_text())
    ledger_path = state / "processed.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    for gig in pending["gigs"]:
        for key in [gig["id"]] + gig.get("aliases", []):
            ledger[key] = True
    atomic_write(ledger_path, json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    pending_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="durable private directory, outside the temporary clone")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--archive", type=Path, required=True)
    prep.add_argument("--preview", action="store_true")
    sub.add_parser("ack", help="confirm successful delivery or an intentional no-email decision")
    sub.add_parser("discard", help="release a batch ONLY after confirming it was not sent")
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args.archive, args.state_dir, args.preview), indent=2))
    elif args.command == "ack":
        acknowledge(args.state_dir)
    else:
        (args.state_dir / "pending.json").unlink()


if __name__ == "__main__":
    main()
