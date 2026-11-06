#!/usr/bin/env python3
"""
gig-radar: finds fresh freelance gigs that match your skills and pings your phone.

Sources use RSS and public APIs. Optional marketplace access uses an API token:
  - Reddit subreddit RSS feeds (r/forhire, r/slavelabour, r/hiring, ...)
  - Hacker News monthly "Freelancer? Seeking freelancer?" and "Who is hiring?"
    threads, via the official HN Algolia API
  - We Work Remotely RSS feeds (attribution: links point back to WWR)
  - Remote OK public JSON API (attribution: links point back to Remote OK)
  - Himalayas contract search API, cached daily
  - Optional Freelancer project API and private exported email alerts

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
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import discovery

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
    "min_hourly_usd": 25,
    "min_project_usd": 75,
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
        "telegram bot", "chrome extension", "stripe", "python", "node.js", "javascript",
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
            "rotate_subreddit": True,
            "max_requests_per_run": 1,
            "search_queries": [
                '"[hiring]" (automation OR spreadsheet OR sheets OR zapier OR n8n)',
                '"[task]" (website OR scraper OR python OR bot)',
                '"[hiring]" (chatbot OR API OR integration)',
                '"[hiring]" ("fix website" OR "finish app" OR MVP OR booking)',
            ],
            "request_delay_seconds": 3,
            "subreddits": {
                "forhire": ["[hiring]"],
                "slavelabour": ["[task]"],
                "hiring": ["[hiring]"],
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
        "himalayas": {
            "enabled": True,
            "min_interval_hours": 24,
            "country": "US",
            "employment_type": "Contractor,Part Time,Temporary",
            "max_pages": 3,
        },
        "freelancer": {
            "enabled": False,
            "queries": ["automation", "chatbot", "API integration", "web scraping", "Next.js"],
            "max_pages": 2,
            "min_interval_hours": 1,
        },
        "alerts": {"enabled": False},
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
    queue: str = "project"
    priority: int = 0
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    location: str = ""
    employment_type: str = ""
    currency: str = ""
    apply_url: str = ""
    expires: datetime | None = None
    imported: bool = False
    date_basis: str = "posted"

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


class FetchedGigs(list):
    """Source results with incomplete-request warnings for the health report."""
    def __init__(self, gigs=(), warnings=()):
        super().__init__(gigs)
        self.warnings = list(warnings)


class SourceError(RuntimeError):
    """A source could not be read; an empty result would hide the outage."""


def http_get(url: str, timeout: int = 20, headers: dict | None = None) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # A quick 429 retry aggravated cloud-IP limiting in the live smoke test.
        raise SourceError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceError(type(e).__name__) from e


def http_post_json(url: str, payload: dict, headers: dict | None = None) -> bool:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as e:
        log(f"  ! notification failed: {type(e).__name__}")
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
    occupied = {"title": [], "body": []}
    # Prefer specific phrases; a nested keyword is not independent evidence.
    for term in sorted(set(include), key=lambda term: (-len(term), term.casefold())):
        rx = term_regex(term)
        hits = {}
        for name, text in (("title", title), ("body", body)):
            spans = [m.span() for m in rx.finditer(text)
                     if not any(m.start() < end and m.end() > start
                                for start, end in occupied[name])]
            hits[name] = bool(spans)
            occupied[name].extend(spans)
        in_title, in_body = hits["title"], hits["body"]
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
        if subreddit.lower() not in tags_by_sub:
            continue
        required_tags = tags_by_sub[subreddit.lower()]
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
    subs = cfg["subreddits"]
    if not subs:
        return []
    base = f"https://www.reddit.com/r/{'+'.join(subs)}"
    urls = [f"{base}/new/.rss?limit=100"]
    # Rotate feed types across runs rather than issuing back-to-back requests.
    # Advance community/query indexes once per full cycle to cover every entry.
    tick = int(time.time() // (20 * 60))
    queries = cfg.get("search_queries", [])
    kinds = 1 + bool(cfg.get("rotate_subreddit", True)) + bool(queries)
    cycle = tick // kinds
    if cfg.get("rotate_subreddit", True):
        sub = list(subs)[cycle % len(subs)]
        urls.append(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100")
    if queries:
        query = urllib.parse.urlencode({"q": queries[cycle % len(queries)],
                                        "restrict_sr": "on", "sort": "new", "t": "week", "limit": 100})
        urls.append(f"{base}/search.rss?{query}")
    start = tick % len(urls)
    urls = (urls[start:] + urls[:start])[:max(1, min(3, int(cfg.get("max_requests_per_run", 1))))]
    gigs, succeeded, warnings = [], 0, []
    for i, url in enumerate(urls):
        if i:
            time.sleep(max(3, cfg.get("request_delay_seconds", 3)))
        try:
            raw = http_get(url)
            if raw is None:
                raise SourceError("No Reddit response")
            gigs.extend(parse_reddit_atom(raw, subs))
            succeeded += 1
        except (SourceError, ET.ParseError) as e:
            reason = str(e) if isinstance(e, SourceError) else "invalid XML"
            warnings.append(f"request {i + 1}/{len(urls)}: {reason}")
            log(f"  ! Reddit {warnings[-1]}")
            # Do not try more requests when Reddit has blocked or limited us.
            if isinstance(e, SourceError):
                break
    if not succeeded:
        raise SourceError("All Reddit feeds unavailable: " + "; ".join(warnings))
    return FetchedGigs({gig.id: gig for gig in gigs}.values(), warnings)


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
                raise SourceError("WWR returned invalid XML") from e
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
    except json.JSONDecodeError as e:
        raise SourceError("Remote OK returned invalid JSON") from e


def parse_himalayas(data: dict) -> list[Gig]:
    if not isinstance(data.get("jobs"), list):
        raise SourceError("Himalayas response is missing jobs")
    gigs = []
    for row in data["jobs"]:
        posted = datetime.fromtimestamp(float(row["pubDate"]), timezone.utc)
        expires = datetime.fromtimestamp(float(row["expiryDate"]), timezone.utc) if row.get("expiryDate") else None
        guid = row["guid"]
        if not discovery.canonical_url(guid):
            guid = "https://himalayas.app" + guid
        if urllib.parse.urlsplit(guid).hostname != "himalayas.app":
            continue
        amounts = [str(row[key]) for key in ("minSalary", "maxSalary") if row.get(key) is not None]
        currency = row.get("currency") or ""
        budget = f"{currency} {' to '.join(amounts)}/{row.get('salaryPeriod') or 'annual'}" if amounts else None
        gigs.append(Gig(
            "himalayas:" + str(row["guid"]), "Himalayas",
            f"{row.get('companyName', '')}: {row['title']}".strip(": "), guid,
            posted, strip_html(row.get("description") or row.get("excerpt") or ""), budget,
            location=", ".join(row.get("locationRestrictions") or []) or "Worldwide (check timezone)",
            employment_type=row.get("employmentType") or "", currency=currency,
            apply_url=row.get("applicationLink") or "", expires=expires,
        ))
    return gigs


def fetch_himalayas(cfg: dict) -> list[Gig]:
    gigs = []
    for page in range(1, min(10, max(1, int(cfg.get("max_pages", 3)))) + 1):
        query = urllib.parse.urlencode({
            "country": cfg.get("country", "US"),
            "employment_type": cfg.get("employment_type", "Contractor,Part Time,Temporary"),
            "sort": "recent", "page": page,
        })
        if page > 1:
            time.sleep(1)
        raw = http_get("https://himalayas.app/jobs/api/search?" + query)
        if raw is None:
            raise SourceError("No Himalayas response")
        data = json.loads(raw)
        gigs.extend(parse_himalayas(data))
        if not data["jobs"] or len(data["jobs"]) < 20:
            break
    return gigs


def parse_freelancer(data: dict) -> list[Gig]:
    if data.get("status") != "success" or not isinstance(data.get("result", {}).get("projects"), list):
        raise SourceError("Freelancer returned an unsuccessful response")
    gigs = []
    for row in data["result"]["projects"]:
        if row.get("status") not in (None, "active") or row.get("local"):
            continue
        currency = (row.get("currency") or {}).get("code") or ""
        bounds = row.get("budget") or {}
        amounts = [str(bounds[k]) for k in ("minimum", "maximum") if bounds.get(k) is not None]
        budget = f"{currency} {' to '.join(amounts)}" if amounts else None
        if budget and row.get("type") == "hourly":
            budget += "/hour"
        seo = row.get("seo_url") or ""
        url = "https://www.freelancer.com/projects/" + (seo if seo else str(row["id"]))
        gigs.append(Gig(
            f"freelancer:{row['id']}", "Freelancer", row["title"], url,
            datetime.fromtimestamp(float(row.get("time_submitted") or row["submitdate"]), timezone.utc),
            strip_html(row.get("description") or row.get("preview_description") or ""), budget,
            currency=currency, employment_type="project",
        ))
    return gigs


def fetch_freelancer(cfg: dict) -> list[Gig]:
    # The official SDK uses an OAuth token. Never assume anonymous API access.
    token = os.environ.get("FREELANCER_OAUTH_TOKEN", "").strip()
    if not token:
        raise SourceError("Set FREELANCER_OAUTH_TOKEN and enable the source after API setup")
    gigs = []
    for query in cfg.get("queries", [])[:8]:
        for page in range(min(3, max(1, int(cfg.get("max_pages", 2))))):
            params = urllib.parse.urlencode({"query": query, "limit": 50, "offset": page * 50,
                                             "full_description": "true", "sort_field": "time_submitted"})
            raw = http_get("https://www.freelancer.com/api/projects/0.1/projects/active/?" + params,
                           headers={"Freelancer-OAuth-V1": token})
            if raw is None:
                raise SourceError("No Freelancer response")
            data = json.loads(raw)
            gigs.extend(parse_freelancer(data))
            time.sleep(1)
            if len(data["result"]["projects"]) < 50:
                break
    return list({g.id: g for g in gigs}.values())


def fetch_alerts(cfg: dict) -> list[Gig]:
    # Private local exports are intentionally kept out of the public repo.
    path = os.environ.get("GIG_RADAR_ALERTS")
    if not path:
        raise SourceError("Set GIG_RADAR_ALERTS to a private imported-alerts JSON file")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [gig_from_row(row) for row in data["gigs"]]


def gig_to_row(gig: Gig) -> dict:
    row = asdict(gig)
    row["posted"] = gig.posted.isoformat()
    row["expires"] = gig.expires.isoformat() if gig.expires else None
    return row


def gig_from_row(row: dict) -> Gig:
    row = dict(row)
    row["posted"] = parse_iso(row["posted"])
    row["expires"] = parse_iso(row.get("expires"))
    if row["posted"] is None:
        raise ValueError("Invalid date in gig data")
    return Gig(**row)


def source_cache_path(name: str) -> Path:
    return STATE_PATH.parent / ".cache" / "gig-radar" / f"{name}.json"


def fetch_with_cache(name: str, fetch, cfg: dict, write_cache: bool = True) -> tuple[list[Gig], bool]:
    interval = float(cfg.get("min_interval_hours", 0)) * 3600
    path = source_cache_path(name)
    signature = json.dumps(cfg, sort_keys=True)
    if interval and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - data["fetched_at"]
            if data.get("config") == signature and 0 <= age < interval:
                return [gig_from_row(row) for row in data["gigs"]], True
        except (ValueError, KeyError, TypeError):
            log(f"  ! {name}: discarding invalid source cache")
    gigs = fetch(cfg)
    if interval and write_cache:
        atomic_write(path, json.dumps({"fetched_at": time.time(), "config": signature,
                                      "gigs": [gig_to_row(gig) for gig in gigs]}, sort_keys=True))
    return gigs, False


FETCHERS = {"reddit": fetch_reddit, "hn": fetch_hn, "wwr": fetch_wwr,
            "remoteok": fetch_remoteok, "himalayas": fetch_himalayas,
            "freelancer": fetch_freelancer, "alerts": fetch_alerts}


# --------------------------------------------------------------------------- #
# State, output, notifications
# --------------------------------------------------------------------------- #
def atomic_write(path: Path, text: str) -> None:
    """Replace a file only after the entire new value is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, delete=False) as out:
            tmp = Path(out.name)
            out.write(text)
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def archive_path() -> Path:
    return DIGEST_PATH.with_suffix(".json")


def merge_archive(current: list[Gig], cfg: dict) -> list[Gig]:
    """Retain discovered matches for the full window, even if a feed loses them."""
    archived = []
    if archive_path().exists():
        data = json.loads(archive_path().read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported match archive version")
        for row in data["gigs"]:
            row = dict(row)
            row["posted"] = parse_iso(row["posted"])
            if row["posted"] is None:
                raise ValueError("Invalid date in match archive")
            row["expires"] = parse_iso(row.get("expires"))
            archived.append(Gig(**row))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["max_age_hours"])
    merged = {g.id: g for g in archived}
    for gig in current:
        if gig.id in merged:
            gig.aliases = sorted(set(gig.aliases + merged[gig.id].aliases))
        merged[gig.id] = gig
    kept = []
    for g in merged.values():
        if cutoff <= g.posted <= datetime.now(timezone.utc) + timedelta(minutes=5):
            scored = score_gig(g, cfg["include"], cfg["exclude"])
            if scored and discovery.classify(scored, cfg) and scored.score >= cfg["min_score"]:
                kept.append(scored)
    result = discovery.dedupe(kept)
    feedback_path = os.environ.get("GIG_RADAR_FEEDBACK")
    if feedback_path:
        import feedback
        decisions = feedback.load(Path(feedback_path))
        result = [g for g in result if not any(
            "wrong_fit" in decisions.get(key, {}).get("events", [])
            for key in [g.id] + g.aliases)]
    return result


def write_archive(gigs: list[Gig]) -> None:
    rows = []
    for gig in gigs:
        rows.append(gig_to_row(gig))
    atomic_write(archive_path(), json.dumps(
        {"schema_version": 1, "gigs": rows}, indent=2, ensure_ascii=False) + "\n")


def load_seen() -> dict[str, float]:
    if STATE_PATH.exists():
        try:
            seen = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(seen, dict) or any(
                not isinstance(v, (int, float)) for v in seen.values()
            ):
                raise ValueError("seen.json must map gig IDs to numeric timestamps")
            return seen
        except json.JSONDecodeError as e:
            raise ValueError("seen.json is corrupt; restore it before scanning") from e
    return {}


def save_seen(seen: dict[str, float]) -> None:
    cutoff = time.time() - 45 * 86400  # forget after 45 days so the file stays small
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    atomic_write(STATE_PATH, json.dumps(pruned, indent=0, sort_keys=True))


def print_table(gigs: list[Gig], heading: str) -> None:
    print(f"\n{heading} ({len(gigs)})")
    print("-" * 72)
    for g in gigs:
        budget = f"  [{g.budget}]" if g.budget else ""
        print(f"{g.age:>8}  {g.source:<22} score {g.score:<2}{budget}")
        print(f"          {g.title}")
        print(f"          {g.url}")
        print(f"          {g.queue} | fit {g.priority} | {'; '.join(g.reasons)}")
        print(f"          matched: {', '.join(g.matched[:8])}\n")


def excerpt_of(text: str, limit: int = 600) -> str:
    """One-line excerpt of the post body, so matches.md can be read without opening each post."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def write_digest(gigs: list[Gig]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Gig radar\n\nUpdated {now}. Projects first, ranked by fit and freshness.\n"]
    queue = None
    for g in sorted(gigs, key=discovery.rank_key):
        if g.queue != queue:
            queue = g.queue
            lines.append("\n## " + ("Small projects" if queue == "project" else "Contract roles") + "\n")
        budget = f" | **{g.budget}**" if g.budget else ""
        title = g.title.replace("[", "(").replace("]", ")")
        posted = g.posted.strftime("%b %d %H:%M UTC")
        lines.append(f"- [{title}]({g.url})  \n  {g.source} | {g.date_basis} {posted} | score {g.score}{budget} | {', '.join(g.matched[:6])}")
        lines.append(f"  Fit: {g.priority} | {'; '.join(g.reasons)}")
        if g.flags:
            lines.append("  Check: " + "; ".join(g.flags))
        excerpt = excerpt_of(g.body)
        if excerpt:
            lines.append(f"  > {excerpt}")
    atomic_write(DIGEST_PATH, "\n".join(lines) + "\n")


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
def collect(cfg: dict, report: dict | None = None, write_cache: bool = True) -> list[Gig]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg["max_age_hours"])
    all_gigs: dict[str, Gig] = {}
    report = report if report is not None else {}
    report.update(checked_at=now.isoformat(), sources={})
    enabled, succeeded = 0, 0
    for name, fetch in FETCHERS.items():
        src_cfg = cfg["sources"].get(name, {})
        if not src_cfg.get("enabled", True):
            continue
        enabled += 1
        log(f"Checking {name}...")
        stats = {"fetched": 0, "matched": 0, "expired": 0, "filtered": 0}
        report["sources"][name] = stats
        try:
            gigs, cached = fetch_with_cache(name, fetch, src_cfg, write_cache)
        except (SourceError, ValueError, TypeError, KeyError, OverflowError, OSError, ET.ParseError) as e:
            stats["status"] = "failed"
            stats["error"] = str(e) if isinstance(e, SourceError) else type(e).__name__
            log(f"  ! {name} unavailable ({stats['error']}); retaining archived matches")
            continue
        succeeded += 1
        warnings = getattr(gigs, "warnings", [])
        stats.update(status="partial" if warnings else "cached" if cached else "ok", fetched=len(gigs))
        if warnings:
            stats["warnings"] = warnings
        log(f"  {name}: {len(gigs)} posts ({stats['status']})")
        for gig in gigs:
            if gig.posted < cutoff or gig.posted > now + timedelta(minutes=5):
                stats["expired"] += 1
                continue
            scored = score_gig(gig, cfg["include"], cfg["exclude"])
            if scored and discovery.classify(scored, cfg, now) and scored.score >= cfg["min_score"]:
                all_gigs.setdefault(scored.id, scored)
                stats["matched"] += 1
            else:
                stats["filtered"] += 1
    report["status"] = "failed" if enabled and not succeeded else "partial" if (succeeded < enabled or any(s["status"] == "partial" for s in report["sources"].values())) else "ok"
    if enabled and not succeeded:
        raise SourceError("All enabled sources failed; existing output was preserved")
    # Preserve the existing collect API's chronological order; output is ranked after merging.
    return sorted(all_gigs.values(), key=lambda g: g.posted, reverse=True)


def run_once(args, cfg: dict) -> None:
    report = {}
    try:
        matches = merge_archive(collect(cfg, report=report, write_cache=not args.dry_run), cfg)
    finally:
        if not args.dry_run and report:
            atomic_write(STATE_PATH.parent / "scan-report.json", json.dumps(report, indent=2) + "\n")
    seen = load_seen()
    new = [g for g in matches if not any(key in seen for key in [g.id] + g.aliases)]

    if args.all:
        print_table(matches, "All matches in window, newest first")
    else:
        print_table(new, "New matches, newest first")

    if args.dry_run:
        return
    write_archive(matches)
    write_digest(matches)
    if new and not args.no_notify:
        notify(new, cfg["max_push_per_run"])
    if not args.dry_run:
        now = time.time()
        for g in new:
            for key in [g.id] + g.aliases:
                seen[key] = now
        save_seen(seen)


def main() -> None:
    ap = argparse.ArgumentParser(description="Find fresh freelance gigs that match your skills.")
    ap.add_argument("--loop", type=int, metavar="MIN", help="re-check every MIN minutes")
    ap.add_argument("--all", action="store_true", help="show all matches in the window, not just new")
    ap.add_argument("--no-notify", action="store_true", help="don't send push notifications")
    ap.add_argument("--dry-run", action="store_true", help="preview only: no files or notifications")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()
    cfg = load_config()

    if args.test_notify and args.dry_run:
        ap.error("--test-notify cannot be combined with --dry-run")

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

