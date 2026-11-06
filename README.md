# gig-radar

Finds fresh freelance gigs for AI chatbots, automations, scrapers and small apps, newest first, and pushes new ones to your phone.

No dependencies. Python 3.9+.

## What it watches

| Source | How | Notes |
|---|---|---|
| Reddit: r/forhire, r/slavelabour, r/hiring, r/freelance_forhire, r/jobbit, r/HireaProgrammer | Public subreddit RSS | Keeps only `[Hiring]` / `[TASK]` posts, drops `[For Hire]` |
| Hacker News "Freelancer? Seeking freelancer?" | Official HN Algolia API | Keeps only `SEEKING FREELANCER` posts (clients, not other devs) |
| Hacker News "Who is hiring?" | Official HN Algolia API | Keeps only posts that mention contract / freelance / part-time |
| We Work Remotely | Official RSS | Contract roles only. Links point back to WWR, as they ask |
| Remote OK | Official JSON API | Contract roles only. Links point back to Remote OK, as they ask |

**Not covered by the script, on purpose:**

- **Upwork** shut off RSS in Aug 2024 and its API needs an approved key that isn't meant for job alerts. Use its own alerts instead: save 3 searches (`AI chatbot`, `automation n8n OR zapier`, `scraper`), filter to Payment verified + posted in the last 24h, and turn on push for saved searches in the Upwork app.
- **Contra** has no public jobs API. Set your skills in your Contra profile and turn on job email/push alerts there.
- **Reddit's OAuth API** now needs Reddit's approval for new apps (Nov 2025 policy), so this uses the plain RSS feeds every feed reader uses, at a gentle pace.

## Setup (5 minutes)

1. **Phone alerts:** install the free **ntfy** app (iOS/Android). Tap +, subscribe to a topic with a long random name like `agustin-gigs-7f3k9q2x`. Anyone who knows the name can read it, so don't make it guessable.
2. **Try it locally:**
   ```bash
   export NTFY_TOPIC=agustin-gigs-7f3k9q2x
   python gig_radar.py --test-notify      # you should get a push
   python gig_radar.py --all --no-notify --dry-run   # see what matches right now
   ```
3. **Tune keywords (optional):** copy `config.example.json` to `config.json` and edit. Any key you set overrides the defaults at the top of `gig_radar.py` (`include`, `exclude`, `max_age_hours`, `min_score`, sources).

## Run it on a schedule

**Option A: GitHub Actions (runs even when your laptop is off)**

1. Push this folder to a new GitHub repo. A public repo gets unlimited Actions minutes, and the repo itself doubles as a portfolio piece.
2. Repo Settings > Secrets and variables > Actions > New secret: `NTFY_TOPIC` = your topic. (Optional: `DISCORD_WEBHOOK_URL`.)
3. Actions tab > gig-radar > Run workflow, to test. After that it runs every 20 minutes and commits `matches.md`, which you can open on your phone in the GitHub app.

Heads up: Reddit sometimes blocks cloud IPs like GitHub's. If the Actions log shows `r/forhire -> HTTP 403`, use Option B for Reddit, or both (set Reddit `"enabled": false` in the repo's `config.json`).

**Option B: your own computer**

```bash
python gig_radar.py --loop 15
```

Or cron (Mac/Linux): `*/15 * * * * cd ~/gig-radar && NTFY_TOPIC=... /usr/bin/python3 gig_radar.py >> radar.log 2>&1`

## How matching works

- A keyword in the title scores 2, in the body scores 1. Default threshold is 2.
- Any `exclude` term kills the post (equity only, unpaid, `[For Hire]`, etc).
- Posts older than `max_age_hours` (default 72) are ignored. Speed wins small gigs: reply within the first hour when you can.
- Budgets like `$400-600` or `$75/hr` are pulled out and shown in the alert.
- `seen.json` remembers what you've been alerted about for 45 days, so you never get the same gig twice.

## Tests

```bash
python -m unittest discover tests
```
