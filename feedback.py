#!/usr/bin/env python3
"""Record lead outcomes and compare sources locally. Never sends applications."""
import argparse
import json
from collections import Counter
from pathlib import Path

from gig_radar import atomic_write

OUTCOMES = ("worth_pitching", "wrong_fit", "pitched", "won", "lost")


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record(path, archive, gig_id, outcome):
    if outcome not in OUTCOMES:
        raise ValueError("Unknown outcome")
    gigs = json.loads(archive.read_text(encoding="utf-8"))["gigs"]
    gig = next((g for g in gigs if gig_id in [g["id"]] + g.get("aliases", [])), None)
    if gig is None:
        raise ValueError("ID not found in the archive")
    data = load(path)
    # Keep event history so a later win does not erase the earlier pitch count.
    events = data.setdefault(gig["id"], {"source": gig["source"], "events": []})["events"]
    if outcome not in events:
        events.append(outcome)
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def summary(data):
    sources = {}
    for entry in data.values():
        count = sources.setdefault(entry["source"], Counter())
        count.update(entry["events"])
        count["reviewed"] += 1
    return {source: dict(count) for source, count in sorted(sources.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="private durable feedback JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    mark = commands.add_parser("mark")
    mark.add_argument("--archive", type=Path, default=Path("matches.json"))
    mark.add_argument("--id", required=True)
    mark.add_argument("--outcome", choices=OUTCOMES, required=True)
    commands.add_parser("report")
    args = parser.parse_args()
    if args.command == "mark":
        record(args.file, args.archive, args.id, args.outcome)
    else:
        print(json.dumps(summary(load(args.file)), indent=2))


if __name__ == "__main__":
    main()
