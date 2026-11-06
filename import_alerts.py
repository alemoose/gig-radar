#!/usr/bin/env python3
"""Import job links from .eml exports into a private local source file.

No mailbox access, login, redirects, or link fetching. Email snippets are not job
verification. Link allowlists intentionally ignore tracking and unsubscribe URLs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from gig_radar import Gig, atomic_write, gig_from_row, gig_to_row, parse_rfc822
import discovery


class JobLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href, self.label = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href", "")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join(" ".join(self.label).split())))
            self.href, self.label = None, []


def provider_for(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("upwork.com", "www.upwork.com") and (
        re.match(r"/jobs/~[a-zA-Z0-9]+", parts.path)
        or parts.path.startswith("/freelance-jobs/apply/")
        or re.match(r"/nx/search/jobs/details/~[a-zA-Z0-9]+", parts.path)
    ):
        return "Upwork alert"
    if host in ("freelancer.com", "www.freelancer.com") and parts.path.startswith("/projects/"):
        return "Freelancer alert"
    if host in ("contra.com", "www.contra.com") and parts.path.startswith("/opportunity/"):
        return "Contra alert"
    return None


def parse_eml(raw: bytes) -> list[Gig]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    received = parse_rfc822(str(message.get("Date", "")))
    if received is None:
        raise ValueError("Alert has no valid Date header; refusing to invent freshness")
    links = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/html":
            parser = JobLinks()
            parser.feed(part.get_content())
            links.extend(parser.links)
        elif part.get_content_type() == "text/plain":
            lines = part.get_content().splitlines()
            for index, line in enumerate(lines):
                for url in re.findall(r"https?://[^\s<>]+", line):
                    title = line.replace(url, "").strip(" -*:|")
                    if not title and index:
                        title = lines[index - 1].strip()
                    links.append((url.rstrip(".,)>"), title))
    gigs = {}
    for url, title in links:
        canonical = discovery.canonical_url(url)
        provider = provider_for(canonical) if canonical else None
        if not provider or len(title) < 8 or title.lower() in ("view job", "apply now", "view details", "unsubscribe"):
            continue
        identity = "alert:" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
        # Do not copy the full email into every job: this would leak unrelated
        # information and attribute other cards' budgets to the wrong lead.
        gig = Gig(identity, provider, title[:400], canonical, received,
                  body=title[:400], imported=True, date_basis="email received")
        if identity not in gigs or len(gig.title) > len(gigs[identity].title):
            gigs[identity] = gig
    return list(gigs.values())


def import_files(files: list[Path], output: Path) -> int:
    existing = {}
    if output.exists():
        data = json.loads(output.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported alert file version")
        existing = {row["id"]: gig_from_row(row) for row in data["gigs"]}
    added = 0
    for path in files:
        for gig in parse_eml(path.read_bytes()):
            if gig.id not in existing:
                existing[gig.id] = gig
                added += 1
            # Repeated email alerts do not make an old job fresh again.
    atomic_write(output, json.dumps({"schema_version": 1,
                                    "gigs": [gig_to_row(g) for g in existing.values()]}, indent=2) + "\n")
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="private path outside the public repository")
    args = parser.parse_args()
    print(f"Imported {import_files(args.files, args.output)} new leads. Open original listings before pitching.")


if __name__ == "__main__":
    main()
