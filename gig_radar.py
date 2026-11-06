#!/usr/bin/env python3
"""
gig-radar: finds fresh freelance gigs that match your skills and pings your phone.

Sources (all official feeds or public APIs, no HTML scraping, no logins):
  - Reddit subreddit RSS feeds (r/forhire, r/slavelabour, r/hiring, ...)
  - Hacker News monthly "Freelancer? Seeking freelancer?" and "Who is hiring?"
    threads, via the official HN Algolia API
  - We Work Remotely RSS feeds (attribution: links point back to WWR)
  - Remote OK public JSON API (attribution: links point back to Remote OK)

Upwork killed its RSS feeds in Aug 2024 and Contra has no public jobs API, so
those two are covered by their own in-app alerts (see README).

Standard library only. Python 3.9+.

Usage:
  python gig_radar.py                 run once, print new matches, notify
  python gig_radar.py --loop 15       keep running, check every 15 minutes
  python gig_radar.py --all           show every match in the time window, not just new ones
  python gig_radar.py --no-notify     print only
  python gig_radar.py --dry-run       don't save what was seen (good for tuning keywords)
  python gig_radar.py --test-notify   send one test push and exit
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("GIG_RADAR_CONFIG", HERE / "config.json"))
STATE_PATH = Path(os.environ.get("GIG_RADAR_STATE", HERE / "seen.json"))
DIGEST_PATH = Path(os.environ.get("GIG_RADAR_DIGEST", HERE / "matches.md"))

USER_AGENT = "gig-radar/1.0 (personal freelance job alert script; polls a few times an hour)"
ATOM = "{http://www.w3.org/2005/Atom}"

# --------------------------------------------------------------------------- #
# Default config. Anything in config.json overrides these keys.
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict = {
    "max_age_hours": 72,
    # A keyword in the title counts 2, in the body counts 1.
    # 3 means: a title hit plus at least one more hit, so "AI" alone in a title isn't enough.
    "min_score": 3,
    "max_push_per_run": 8,
    "include": [
        # AI work
        "ai", "llm", "gpt", "chatgpt", "openai", "claude", "anthropic", "gemini",
        "chatbot", "chat bot", "ai agent", "agents", "rag", "langchain", "vector",
        "embeddings", "prompt", "voice agent", "ai integration", "fine-tune",
        # automation
        "automation", "automate", "n8n", "zapier", "make.com", "workflow",
        "api integration", "webhook", "google sheets", "airtable",
        # scraping / data
        "scraper", "scraping", "scrape", "crawler", "data extraction", "puppeteer",
        "playwright",
        # apps / stack
        "web app", "mobile app", "app developer", "mvp", "saas", "next.js", "nextjs",
        "react", "react native", "typescript", "node", "firebase", "supabase",
        "postgres", "postgresql", "full stack", "full-stack", "discord bot",
        "telegram bot", "chrome extension", "stripe",
    ],
    "exclude": [
        "unpaid", "equity only", "equity-only", "rev share", "revenue share only",
        "no budget", "for exposure", "[for hire]", "for hire]", "[offer]",
        "wordpress theme", "seo backlinks", "data entry only", "video editor",
        "onlyfans", "crypto pump", "trading bot guaranteed",
        "commission-only", "commission only", "paid after launch",
    ],
    # Each source can be switched off with "enabled": false.
    "sources": {
        "reddit": {
            "enabled": True,
            # subreddit -> title must contain one of these tags (lowercase). [] = no tag needed.
            "subreddits": {
                "forhire": ["[hiring]"],
                "slavelabour": ["[task]"],
                "hiring": [],
                "freelance_forhire": ["[hiring]"],
                "jobbit": ["hiring"],
                "HireaProgrammer": [],
            },
        },
        "hn": {
            "enabled": True,
            # "Who is hiring" is mostly full-time; only keep posts that mention one of these.
            "who_is_hiring_require_any": ["contract", "freelance", "part-time", "part time", "contractor"],
        },
        "wwr": {
            "enabled": True,
            "feeds": [
                "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
                "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
            ],
            "require_any": ["contract", "freelance", "part-time", "part time", "contractor"],
        },
        "remoteok": {
            "enabled": True,
            "require_any": ["contract", "freelance", "part-time", "part time", "contractor"],
        },
    },
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in user.items():
            if key == "sources" and isinstance(value, dict):
                for src, src_cfg in value.items():
                    cfg["sources"].setdefault(src, {}).update(src_cfg)
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------- #
# Data model + helpers
# --------------------------------------------------------------------------- #
@dataclass
class Gig:
    id: str
    source: str
    title: str
    url: str
    posted: datetime
    body: str = ""
    budget: str | None = None
    score: int = 0
    matched: list[str] = field(default_factory=list)

    @property
    def age(self) -> str:
        mins = int((datetime.now(timezone.utc) - self.posted).total_seconds() // 60)
        if mins < 60:
            return f"{max(mins, 0)}m ago"
        if mins < 60 * 48:
            return f"{mins // 60}h ago"
        return f"{mins // 1440}d ago"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(raw) if "&lt;" in raw else raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def http_get(url: str, timeout: int = 20) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(5)
                continue
            log(f"  ! {url} -> HTTP {e.code}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"  ! {url} -> {e}")
            return None
    return None


def http_post_json(url: str, payload: dict, headers: dict | None = None) -> bool:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as e:
        log(f"  ! notify failed ({url.split('?')[0]}): {e}")
        return False


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def first_line(text: str, limit: int = 110) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "..."


def parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


BUDGET_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*[kK]?"
    r"(?:\s*(?:-|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?\s*[kK]?)?"
    r"(?:\s*(?:/|per)\s*(?:hr|hour|h|project|month|mo|week|wk))?"
    r"|\b\d{2,3}\s*(?:usd|USD)\s*(?:/|per)\s*(?:hr|hour)\b"
)


def find_budget(text: str) -> str | None:
    m = BUDGET_RE.search(text)
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def term_regex(term: str) -> re.Pattern:
    # Lookarounds instead of \b so terms like "c#" and "next.js" work.
    return re.compile(r"(?<![\w])" + re.escape(term.lower()) + r"(?![\w])")


def contains_any(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(term_regex(t).search(low) for t in terms)


def score_gig(gig: Gig, include: list[str], exclude: list[str]) -> Gig | None:
    title = gig.title.lower()
    body = gig.body.lower()
    if any(term_regex(t).search(title) or term_regex(t).search(body) for t in exclude):
        return None
    matched, score = [], 0
    for term in include:
        rx = term_regex(term)
        in_title, in_body = bool(rx.search(title)), bool(rx.search(body))
        if in_title or in_body:
            matched.append(term)
            score += 2 if in_title else 1
    gig.score, gig.matched = score, matched
    if gig.budget is None:
        gig.budget = find_budget(gig.title + "\n" + gig.body)
    return gig


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
SUBREDDIT_RE = re.compile(r"/r/([A-Za-z0-9_]+)/", re.I)


def parse_reddit_atom(xml_bytes: bytes, tags_by_sub: dict[str, list[str]]) -> list[Gig]:
    """Parse a (possibly multi-subreddit) feed. tags_by_sub maps subreddit -> required title tags."""
    tags_by_sub = {k.lower(): [t.lower() for t in v] for k, v in tags_by_sub.items()}
    gigs: list[Gig] = []
    root = ET.fromstring(xml_bytes)
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        link_el = entry.find(f"{ATOM}link")
        url = link_el.get("href") if link_el is not None else ""
        cat_el = entry.find(f"{ATOM}category")
        m = SUBREDDIT_RE.search(url)
        subreddit = (cat_el.get("term") if cat_el is not None else None) or (m.group(1) if m else "")
        required_tags = tags_by_sub.get(subreddit.lower(), [])
        if required_tags and not any(tag in title.lower() for tag in required_tags):
            continue
        author = (entry.findtext(f"{ATOM}author/{ATOM}name") or "").lower()
        if "automoderator" in author:
            continue
        posted = parse_iso(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated"))
        if not url or not posted:
            continue
        body = strip_html(entry.findtext(f"{ATOM}content") or "")
        body = re.sub(r"submitted by .*$", "", body, flags=re.S).strip()
        gid = entry.findtext(f"{ATOM}id") or url
        gigs.append(Gig(f"reddit:{gid}", f"r/{subreddit}", title, url, posted, body))
    return gigs


def fetch_reddit(cfg: dict) -> list[Gig]:
    # One combined feed (r/a+b+c) instead of one request per subreddit:
    # Reddit rate-limits (HTTP 429) back-to-back requests from cloud IPs.
    subs = cfg["subreddits"]
    raw = http_get(f"https://www.reddit.com/r/{'+'.join(subs)}/new/.rss?limit=100")
    if not raw:
        return []
    try:
        return parse_reddit_atom(raw, subs)
    except ET.ParseError as e:
        log(f"  ! reddit: bad feed ({e})")
        return []


HN_API = "https://hn.algolia.com/api/v1"


def latest_hn_threads(search_json: dict) -> dict[str, int]:
    """Return {"freelancer": id, "hiring": id} for the newest monthly threads."""
    found: dict[str, int] = {}
    for hit in sorted(search_json.get("hits", []), key=lambda h: h.get("created_at_i", 0), reverse=True):
        title = (hit.get("title") or "").lower()
        if "freelancer?" in title and "freelancer" not in found:
            found["freelancer"] = int(hit["objectID"])
        elif "who is hiring" in title and "hiring" not in found:
            found["hiring"] = int(hit["objectID"])
    return found


def parse_hn_thread(item_json: dict, kind: str, require_any: list[str]) -> list[Gig]:
    gigs: list[Gig] = []
    for child in item_json.get("children", []) or []:
        text = strip_html(child.get("text") or "")
        if not text:
            continue
        if kind == "freelancer":
            # Only clients looking for help, not freelancers advertising themselves.
            if not text.upper().lstrip().startswith("SEEKING FREELANCER"):
                continue
            src = "HN Freelancer thread"
        else:
            if require_any and not contains_any(text, require_any):
                continue
            src = "HN Who is hiring"
        ts = child.get("created_at_i")
        posted = datetime.fromtimestamp(ts, timezone.utc) if ts else parse_iso(child.get("created_at"))
        if not posted:
            continue
        cid = child.get("id")
        gigs.append(
            Gig(f"hn:{cid}", src, first_line(text), f"https://news.ycombinator.com/item?id={cid}", posted, text)
        )
    return gigs


def fetch_hn(cfg: dict) -> list[Gig]:
    raw = http_get(f"{HN_API}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    if not raw:
        return []
    threads = latest_hn_threads(json.loads(raw))
    gigs: list[Gig] = []
    for kind, tid in threads.items():
        item = http_get(f"{HN_API}/items/{tid}", timeout=60)
        if item:
            gigs += parse_hn_thread(json.loads(item), kind, cfg.get("who_is_hiring_require_any", []))
    return gigs


def parse_rss2(xml_bytes: bytes, source: str, require_any: list[str]) -> list[Gig]:
    gigs: list[Gig] = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        posted = parse_rfc822(item.findtext("pubDate"))
        body = strip_html(item.findtext("description") or "")
        if not url or not posted:
            continue
        if require_any and not contains_any(title + "\n" + body, require_any):
            continue
        gigs.append(Gig(f"wwr:{item.findtext('guid') or url}", source, title, url, posted, body))
    return gigs


def fetch_wwr(cfg: dict) -> list[Gig]:
    gigs: list[Gig] = []
    for feed in cfg["feeds"]:
        raw = http_get(feed)
        if raw:
            try:
                gigs += parse_rss2(raw, "We Work Remotely", cfg.get("require_any", []))
            except ET.ParseError as e:
                log(f"  ! WWR feed: bad XML ({e})")
    return gigs


def parse_remoteok(data: list, require_any: list[str]) -> list[Gig]:
    gigs: list[Gig] = []
    for job in data:
        if not isinstance(job, dict) or "id" not in job or "position" not in job:
            continue  # first element is the legal notice
        body = strip_html(job.get("description") or "")
        tags = " ".join(job.get("tags") or [])
        title = f"{job.get('company', '').strip()}: {job['position'].strip()}".strip(": ")
        if require_any and not contains_any(f"{title}\n{tags}\n{body}", require_any):
            continue
        posted = (
            datetime.fromtimestamp(int(job["epoch"]), timezone.utc) if job.get("epoch") else parse_iso(job.get("date"))
        )
        if not posted:
            continue
        budget = None
        if job.get("salary_min") and job.get("salary_max"):
            budget = f"${int(job['salary_min']):,} to ${int(job['salary_max']):,}/yr"
        url = job.get("url") or f"https://remoteok.com/remote-jobs/{job['id']}"
        gigs.append(Gig(f"remoteok:{job['id']}", "Remote OK", title, url, posted, f"{tags}\n{body}", budget))
    return gigs


def fetch_remoteok(cfg: dict) -> list[Gig]:
    raw = http_get("https://remoteok.com/api")
    if not raw:
        return []
    try:
        return parse_remoteok(json.loads(raw), cfg.get("require_any", []))
    except json.JSONDecodeError:
        log("  ! Remote OK: bad JSON")
        return []


FETCHERS = {"reddit": fetch_reddit, "hn": fetch_hn, "wwr": fetch_wwr, "remoteok": fetch_remoteok}


# --------------------------------------------------------------------------- #
# State, output, notifications
# --------------------------------------------------------------------------- #
def load_seen() -> dict[str, float]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict[str, float]) -> None:
    cutoff = time.time() - 45 * 86400  # forget after 45 days so the file stays small
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_PATH.write_text(json.dumps(pruned, indent=0, sort_keys=True), encoding="utf-8")


def print_table(gigs: list[Gig], heading: str) -> None:
    print(f"\n{heading} ({len(gigs)})")
    print("-" * 72)
    for g in gigs:
        budget = f"  [{g.budget}]" if g.budget else ""
        print(f"{g.age:>8}  {g.source:<22} score {g.score:<2}{budget}")
        print(f"          {g.title}")
        print(f"          {g.url}")
        print(f"          matched: {', '.join(g.matched[:8])}\n")


def excerpt_of(text: str, limit: int = 600) -> str:
    """One-line excerpt of the post body, so matches.md can be read without opening each post."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def write_digest(gigs: list[Gig]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Gig radar\n\nUpdated {now}. Newest first.\n"]
    for g in gigs:
        budget = f" | **{g.budget}**" if g.budget else ""
        title = g.title.replace("[", "(").replace("]", ")")
        posted = g.posted.strftime("%b %d %H:%M UTC")
        lines.append(f"- [{title}]({g.url})  \n  {g.source} | {posted} | score {g.score}{budget} | {', '.join(g.matched[:6])}")
        excerpt = excerpt_of(g.body)
        if excerpt:
            lines.append(f"  > {excerpt}")
    DIGEST_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def notify(gigs: list[Gig], max_push: int) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not topic and not discord:
        log("  (no NTFY_TOPIC or DISCORD_WEBHOOK_URL set, skipping push)")
        return

    for g in gigs[:max_push]:
        budget = f" | {g.budget}" if g.budget else ""
        msg = f"{g.source}{budget}\n{first_line(g.body, 220) or g.title}"
        if topic:
            http_post_json(server, {
                "topic": topic,
                "title": g.title[:120],
                "message": msg,
                "click": g.url,
                "tags": ["moneybag"] if g.budget else ["briefcase"],
                "priority": 4 if g.score >= 4 else 3,
            })
        if discord:
            http_post_json(discord, {"content": f"**{g.title[:200]}**\n{msg[:1500]}\n{g.url}"})
        time.sleep(0.5)

    extra = len(gigs) - max_push
    if extra > 0:
        summary = f"{extra} more new matches. Open matches.md or run the script to see them all."
        if topic:
            http_post_json(server, {"topic": topic, "title": "Gig radar", "message": summary})
        if discord:
            http_post_json(discord, {"content": summary})


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def collect(cfg: dict) -> list[Gig]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["max_age_hours"])
    all_gigs: dict[str, Gig] = {}
    for name, fetch in FETCHERS.items():
        src_cfg = cfg["sources"].get(name, {})
        if not src_cfg.get("enabled", True):
            continue
        log(f"Checking {name}...")
        for gig in fetch(src_cfg):
            if gig.posted < cutoff:
                continue
            scored = score_gig(gig, cfg["include"], cfg["exclude"])
            if scored and scored.score >= cfg["min_score"]:
                all_gigs.setdefault(scored.id, scored)
    return sorted(all_gigs.values(), key=lambda g: g.posted, reverse=True)


def run_once(args, cfg: dict) -> None:
    matches = collect(cfg)
    seen = load_seen()
    new = [g for g in matches if g.id not in seen]

    if args.all:
        print_table(matches, "All matches in window, newest first")
    else:
        print_table(new, "New matches, newest first")

    write_digest(matches)
    if new and not args.no_notify:
        notify(new, cfg["max_push_per_run"])
    if not args.dry_run:
        now = time.time()
        for g in new:
            seen[g.id] = now
        save_seen(seen)


def main() -> None:
    ap = argparse.ArgumentParser(description="Find fresh freelance gigs that match your skills.")
    ap.add_argument("--loop", type=int, metavar="MIN", help="re-check every MIN minutes")
    ap.add_argument("--all", action="store_true", help="show all matches in the window, not just new")
    ap.add_argument("--no-notify", action="store_true", help="don't send push notifications")
    ap.add_argument("--dry-run", action="store_true", help="don't record seen gigs")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()
    cfg = load_config()

    if args.test_notify:
        demo = Gig("test", "gig-radar", "Test: notifications are working", "https://news.ycombinator.com",
                   datetime.now(timezone.utc), "If you see this on your phone, you're set.", "$500")
        notify([demo], 1)
        return

    if args.loop:
        interval = max(args.loop, 10)  # never hammer the feeds faster than every 10 min
        while True:
            run_once(args, cfg)
            log(f"Sleeping {interval} min...")
            time.sleep(interval * 60)
    else:
        run_once(args, cfg)


if __name__ == "__main__":
    main()
