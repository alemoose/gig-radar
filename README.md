# Gig radar

Find paid software projects and contract roles, with small projects ranked first.
Python 3.9+, standard library only. Drafting and sending the digest remains a
separate scheduled task. This tool never applies to jobs or contacts posters.

## Discovery

| Source | Coverage | Default |
| --- | --- | --- |
| Reddit | Rotates combined feed, individual communities and targeted searches | On, every scan |
| Hacker News | Latest freelancer and hiring threads; client posts and contract roles | On |
| We Work Remotely | Programming feeds with contract signals | On |
| Remote OK | Public API, contract signals | On |
| Himalayas | Paginated US-eligible Contractor, Part Time and Temporary search | On, cached 24h |
| Freelancer | Active project searches, full descriptions and currency-aware budgets | Off; API token required |
| Marketplace alerts | Direct job links from your exported Upwork, Freelancer or Contra emails | Off; private file required |

Reddit rotates through r/forhire, r/slavelabour, r/hiring,
r/freelance_forhire, r/jobbit and r/HireaProgrammer. The extra community request
reduces crowding in the combined 100-post feed. Four rotating searches cover
spreadsheet/workflow automation, scrapers/bots, APIs/chatbots and app/site repairs.
The default makes one request per scan, rotating between the three feed types.
On a 20-minute schedule, the combined feed is checked hourly, all six individual
communities over six hours, and all four searches over four hours. Delayed runs
can skip slots. This is bounded coverage, not an exhaustive Reddit crawl. The
live test rate-limited a second back-to-back request, so that is not the default.
`max_requests_per_run` can be raised to 3 on a host that tolerates it; extra requests
are spaced at least three seconds apart and stop on a request error. HTTP 429s
are not immediately retried. Blocks are reported, not bypassed.

Himalayas is a secondary contract source, not a source of instant freelance gigs.
Its API refreshes daily. API details and source requirements are documented in
[DISCOVERY.md](DISCOVERY.md).

## Try it

```bash
python -m unittest discover -s tests
python gig_radar.py --all --no-notify --dry-run
```

Dry runs make source requests but write no files or caches and send no alerts.
They can repeat API calls because they do not save the source cache. Use an
existing cache when testing frequently.

Copy `config.example.json` to `config.json` to tune sources, queries and budget
preferences. Configuration lists replace default lists; source objects merge one
level into their defaults. The country filter defaults to US and is configurable.
No budget floor or skill score establishes that a job is legitimate or suitable.

Optional phone pushes use `NTFY_TOPIC` and/or `DISCORD_WEBHOOK_URL` environment
variables. Use a long random ntfy topic: anyone knowing the topic can read it.

```bash
python gig_radar.py --test-notify
python gig_radar.py
python gig_radar.py --loop 20
```

## How matching works

1. Match stack keywords with word boundaries. A title term scores 2, a body term
   scores 1; nested terms such as React and React Native count once per field.
2. Also match deliverables: connect a calendar to a CRM, automate a spreadsheet,
   fix a booking website, collect product data, or build a document assistant.
   With hiring intent, each problem signal adds 3, capped at 6. A post can qualify
   without naming a programming language.
3. Reject obvious self-promotion, closed titles, structured full-time roles,
   full-time-only text, expired API listings and configured exclusions. Reddit
   additionally requires a hiring signal. These are language heuristics and can
   still miss ambiguous wording or languages other than English.
4. Rank **small projects** separately from **contract roles**. Within each queue,
   reward freshness, a stated budget and a small scope; flag unknown budgets,
   eligibility, seniority and sparse email excerpts. Explicit low USD budgets
   receive a penalty, not automatic rejection. Other currencies are not converted.
5. Deduplicate tracking variants of a URL, Reddit slug changes, and exact long
   crosspost text. Preserve ID aliases so notifications and digest acknowledgements
   can recognize the same lead. Identical short job titles alone never merge.

Scores are heuristics with visible reasons, not estimated probabilities of winning.
Check source posts, scope, client history, currency and location before pitching.

## Output and scheduling

- `matches.md`: project and contract queues, fit reasons, cautions and excerpts.
- `matches.json`: full bodies, stable IDs, ID aliases and structured metadata.
- `seen.json`: previous push attempts, pruned after 45 days. Failed pushes are not
  retried automatically.
- `scan-report.json`: source status, fetched/matched/filtered/expired counts,
  partial-request warnings and cache usage. Counts are per source before cross-source
  deduplication, not unique opportunities across the whole scan.
- `.cache/gig-radar/`: successful source responses, used to respect daily API cadence.

Discovered matches remain in the archive for `max_age_hours` (72 by default) even
if the source feed rotates them out. They are rescored when configuration changes.
A removed or filled post can remain until it expires; reopen it before pitching.
Failures preserve archived leads. Total source failure exits unsuccessfully rather
than replacing the digest. Invalid state fails visibly. Individual files use atomic
replacement, but the whole run is not one multi-file transaction.

The included GitHub workflow runs tests, restores source caches, polls, uploads a
seven-day `discovery-health` artifact and commits changed matches and seen IDs.
The separate `source-smoke` workflow can test public source access without
notifications or saved-match changes; it also runs when its workflow file changes.
The scheduled scanner requires Actions contents write permission. Optional tokens and push targets
belong in repository secrets. No credentials belong in config files.
GitHub caches are best effort; eviction or a new branch can cause an extra API
fetch. Only one scanner should own a state directory.

## Enable Freelancer project searches

Set up API access through [Freelancer's developer portal](https://developers.freelancer.com/),
put your token in `FREELANCER_OAUTH_TOKEN` (a repository secret in Actions), and set
`sources.freelancer.enabled` to true. The adapter uses GET only, requests full
project descriptions, and paginates up to two 50-result pages per query by default.
It preserves project currency and hourly versus fixed budgets. Queries default
to automation, chatbot, API integration, web scraping and Next.js. Adjust them to
what you can deliver. Endpoint and token behavior follow the official SDK; live
access has not been verified in this environment.

## Import marketplace alerts privately

Save an alert as `.eml`, then:

```bash
python import_alerts.py --output /private/gig-radar/alerts.json /path/to/alert.eml
export GIG_RADAR_ALERTS=/private/gig-radar/alerts.json
export GIG_RADAR_STATE=/private/gig-radar/seen.json
export GIG_RADAR_DIGEST=/private/gig-radar/matches.md
```

Enable `sources.alerts.enabled` in config and scan normally. Only recognized direct
job URLs are imported. Tracking redirects are skipped and never followed. Imported
records contain link labels, not the entire email or unrelated job-card budgets.
Their timestamp means **email received**, not job posted, and repeated alerts keep
the first timestamp. Open the original listing for the full scope and real posting
date. Some email templates use generic buttons or wrapped links and yield no leads.
This is a local export importer, not an automatic Gmail/Outlook integration.

Do not commit private alerts or email exports to a public repository. Run this
source locally with private output paths; the public GitHub workflow intentionally
has no mailbox access and no alert import step.

## Measure which sources are useful

Use an ID from matches.json:

```bash
python feedback.py --file /private/gig-radar/feedback.json mark --id reddit:t3_example --outcome worth_pitching
python feedback.py --file /private/gig-radar/feedback.json mark --id reddit:t3_example --outcome pitched
python feedback.py --file /private/gig-radar/feedback.json report
export GIG_RADAR_FEEDBACK=/private/gig-radar/feedback.json
```

Outcomes: `worth_pitching`, `wrong_fit`, `pitched`, `won`, `lost`. Repeated labels
count once per lead. A pitch and a later win both remain in its history. With the
environment variable set, explicit `wrong_fit` decisions suppress that lead and
its aliases from future output. Source totals help you decide which searches to
tune or disable; the tool does not invent weights from a tiny feedback sample.
Feedback is private and is not automatically synchronized into Actions.

## Durable email handoff

`gig-digest-prompt.txt` requires an owner-configured `DIGEST_RECIPIENT`, explicit
`RUN_MODE`, `DIGEST_SLOT`, and a durable
private `DIGEST_STATE_DIR` outside a temporary clone. Fresh sessions do not
necessarily share storage. Set up persistence before enabling automatic delivery.
Run one digest worker at a time.

```bash
python digest_queue.py --state-dir /private/gig-digest prepare --archive matches.json
# After confirmed delivery, or a deliberate scheduled no-email decision:
python digest_queue.py --state-dir /private/gig-digest ack
```

For manual tests, append `--preview` to prepare; do not acknowledge. An unresolved
pending batch blocks scheduled retries. Check Sent mail: if sent, acknowledge;
if definitely not sent, use `discard` and retry. An uncertain send must be reviewed
because email and local state cannot be committed atomically. The archive window
limits how far back recovery can go.

This repository does not configure mailbox connectors or replace a live scheduled
task. For a public scanner, keep imported email leads and private feedback on your
own machine or in a private worker.
