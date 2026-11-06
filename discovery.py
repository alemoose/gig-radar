"""Discovery signals and ranking. All scores are heuristics, never verified job quality."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Each pattern describes a deliverable, not just a fashionable technology.
PROBLEMS = {
    "spreadsheet automation": r"\b(?:automate|automation|sync|connect|populate|integrate)\b.{0,90}\b(?:spreadsheet|sheets|excel|airtable)\b|\b(?:spreadsheet|google sheets|excel|airtable)\b.{0,60}\b(?:automat\w*|sync|integration)\b",
    "API connection": r"\b(?:connect|integrate|integration|sync)\b.{0,70}\b(?:api|crm|stripe|calendar|shopify|webhook)\b",
    "lead intake": r"\b(?:lead|form)\b.{0,60}\b(?:intake|routing|automation|spreadsheet|crm|notification)\b",
    "document assistant": r"\b(?:chatbot|assistant|chat bot)\b.{0,90}\b(?:documents|docs|pdf|knowledge base|faq)\b",
    "website repair": r"\b(?:fix|repair|debug|finish)\b.{0,60}\b(?:website|web site|web app|app|mvp|chatbot|bot|booking|payment|integration)\b",
    "website build": r"\b(?:build|create|develop|redesign)\b.{0,55}\b(?:website|landing page|booking system|dashboard|web app)\b",
    "data collection": r"\b(?:collect|extract|scrape|scraping)\b.{0,60}\b(?:data|prices|listings|website|directory|product)\b",
    "workflow repair": r"\b(?:fix|build|set up|setup|automate)\b.{0,60}\b(?:workflow|zapier|n8n|make\.com|email automation)\b",
}
BUYER = re.compile(r"\[\s*(?:hiring|task)\s*\]|\b(?:seeking freelancer|looking (?:for|to hire)|need (?:someone|a developer|help|a freelancer)|hiring|budget|paid project)\b", re.I)
SELLER = re.compile(r"^\s*(?:\[\s*(?:for\s*hire|offer)\s*\]|seeking work\b)|\b(?:i offer|my services|available for hire|hire me)\b", re.I)
CONTRACT = re.compile(r"\b(?:contract(?:or)?|freelance|part[- ]time|temporary)\b", re.I)
FULL_TIME = re.compile(r"\bfull[-_ ]time\b", re.I)
NEGATED_CONTRACT = re.compile(r"\b(?:no|not (?:a|an|accepting|offering)|without)\s+(?:freelanc\w*|contract(?:or)?s?|part[- ]time)\b|\b(?:contractors?|freelancers?)\s+(?:are\s+)?not\s+(?:accepted|allowed|eligible)\b", re.I)
CLOSED = re.compile(r"\[\s*(?:closed|filled|hired)\s*\]|\b(?:position (?:has been |is )?filled|no longer (?:hiring|accepting applications))\b", re.I)


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return ""
    host = re.sub(r"^www\.", "", parts.hostname.lower())
    if host in ("old.reddit.com", "new.reddit.com"):
        host = "reddit.com"
    path = parts.path.rstrip("/")
    if host == "reddit.com":
        post = re.search(r"/comments/([a-z0-9]+)", path, re.I)
        if post:
            return f"https://reddit.com/comments/{post.group(1).lower()}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in
             ("ref", "source", "fbclid", "gclid", "referrer", "trackingid")]
    # Keep job identifiers, especially HN's ?id=.
    return urlunsplit(("https", host + (f":{port}" if port else ""), path, urlencode(sorted(query)), ""))


def problem_matches(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.lower())
    return [label for label, pattern in PROBLEMS.items() if re.search(pattern, text)]


def classify(gig, cfg: dict, now: datetime | None = None) -> bool:
    """Enrich a gig and return whether it is an actionable candidate."""
    now = now or datetime.now(timezone.utc)
    text = gig.title + "\n" + gig.body
    gig.reasons, gig.flags = [], []
    if not canonical_url(gig.url) or SELLER.search(gig.title) or CLOSED.search(gig.title):
        return False
    if gig.expires and gig.expires <= now:
        return False
    structured = gig.employment_type.lower().replace("_", " ")
    if (re.search(r"\bfull[- ]time only\b", text, re.I) or structured in ("full time", "full-time", "intern", "volunteer")
            or (FULL_TIME.search(text) and not CONTRACT.search(NEGATED_CONTRACT.sub("", text)))):
        return False
    buyer = bool(BUYER.search(text)) or gig.imported
    if gig.imported:
        gig.score += 1
    # Discussion and freelancers' self-promotion are not job leads.
    if gig.source.startswith("r/") and (not buyer or SELLER.search(gig.title + " " + gig.body[:150])):
        return False
    problems = problem_matches(text)
    if problems and (buyer or gig.source in ("Freelancer", "Upwork alert")):
        gig.score += min(len(problems), 2) * 3
        gig.matched.extend(f"problem: {p}" for p in problems)
        gig.reasons.extend(problems[:2])
    is_contract = (structured in ("contract", "contractor", "part time", "part-time", "temporary")
                   or gig.source in ("HN Who is hiring", "We Work Remotely", "Remote OK", "Himalayas", "Remotive")
                   or bool(CONTRACT.search(gig.title)))
    gig.queue = "contract" if is_contract else "project"
    gig.priority = min(gig.score, 12) * 2
    age_hours = max(0, (now - gig.posted).total_seconds() / 3600)
    if age_hours <= 6:
        gig.priority += 12
        gig.reasons.append(f"{gig.date_basis} within 6 hours")
    elif age_hours <= 24:
        gig.priority += 6
    if gig.queue == "project":
        gig.priority += 8
    if buyer:
        gig.priority += 5
        gig.reasons.append("explicit hiring intent")
    if gig.budget:
        gig.priority += 4
        gig.reasons.append("budget stated, verify scope and currency")
    else:
        gig.flags.append("budget unknown")
    # Only compare explicit USD amounts; do not guess currency or annual equivalence.
    if gig.budget and ("$" in gig.budget or "USD" in gig.budget.upper()):
        match = re.search(r"(?:\$|USD\s*)(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", gig.budget, re.I)
        if match:
            amount = float(match[1].replace(",", "")) * (1000 if match[2] else 1)
            hourly = bool(re.search(r"/(?:hr|hour(?:ly)?|h)\b|per\s+hour", gig.budget, re.I))
            ambiguous = bool(re.search(r"/yr|annual|salary|/mo|/week|/month", gig.budget, re.I))
            # Bare dollars can mean USD, CAD, AUD or another currency.
            known_usd = "USD" in gig.budget.upper() or gig.currency == "USD"
            if known_usd and not ambiguous and hourly and amount < cfg.get("min_hourly_usd", 25):
                gig.flags.append("hourly budget below preferred floor")
                gig.priority -= 15
            elif known_usd and not ambiguous and not hourly and amount < cfg.get("min_project_usd", 75):
                gig.flags.append("project budget below preferred floor")
                gig.priority -= 12
    if gig.budget and "$" in gig.budget and not gig.currency and "USD" not in gig.budget.upper():
        gig.flags.append("dollar currency unverified")
    if gig.location:
        gig.flags.append(f"check eligibility: {gig.location}")
    if re.search(r"\b(?:senior|staff|principal|lead engineer|[5-9]\+? years|1\d\+? years)\b", text, re.I):
        gig.priority -= 10
        gig.flags.append("seniority requirement needs review")
    if re.search(r"\b(?:small (?:task|project)|one[- ]off|bug fix|first milestone|few hours)\b", text, re.I):
        gig.priority += 5
        gig.reasons.append("small scope signal")
    if gig.imported:
        gig.flags.append("email excerpt only; open the original listing")
    return True


def rank_key(gig):
    return (gig.queue != "project", -gig.priority, -gig.posted.timestamp(), gig.id)


def dedupe(gigs: list) -> list:
    """Collapse URL duplicates and exact long-text crossposts, retaining ID aliases."""
    kept, by_key = [], {}
    for gig in sorted(gigs, key=lambda g: (g.posted, g.id)):
        url = canonical_url(gig.url)
        if not url:
            continue
        text = re.sub(r"\s+", " ", gig.body).strip().lower()
        keys = ["url:" + url, "id:" + gig.id]
        # Deliberately avoid fuzzy titles: different clients use identical job titles.
        if len(text) >= 180 and not gig.imported:
            keys.append("body:" + hashlib.sha256(text.encode()).hexdigest())
        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing is None:
            kept.append(gig)
            existing = gig
        else:
            existing.aliases = sorted(set(existing.aliases + [gig.id] + gig.aliases) - {existing.id})
            if len(gig.body) > len(existing.body):
                existing.body = gig.body
        for key in keys:
            by_key[key] = existing
    return sorted(kept, key=rank_key)
