# Discovery changes and verification

## What changes the leads you get

The old scanner depended on one combined Reddit listing and stack keywords. It
could miss a client asking to connect two business systems, crowd smaller
communities out of the combined feed, or rank a large contract ahead of a tiny fix.

The updated scanner adds:

- A rotating individual community feed and a rotating targeted search, alongside
  the combined feed. One request per scan rotates feed types: the combined feed hourly, six individual
  communities over six hours, and four searches over four hours on a 20-minute schedule.
- Eight categories of deliverables, with explicit hiring intent. For example,
  "[Hiring] Connect our calendar to our CRM" can qualify without a stack keyword.
- Separate project and contract queues, with visible ranking reasons and cautions.
- A daily cached Himalayas contract search, with US eligibility filtering and up
  to three result pages. This complements small gigs rather than replacing them.
- An optional, paginated Freelancer project search using your API token.
- A private local importer for exported marketplace alert emails.
- URL/crosspost deduplication, expired-job removal and ID aliases for delivery state.
- A source health artifact and a local outcome tracker so you can measure useful
  leads rather than counting raw keyword hits.

The new sources do not establish that a client is legitimate. Budgets, eligibility
and scope still require review. No source coverage or lead-volume increase is
promised without real runs and user feedback.

## Primary references checked November 6, 2026

- [Himalayas API documentation](https://himalayas.app/api): public search endpoint,
  employment and country filters, pagination, response fields and daily refresh.
  The adapter preserves source attribution and the original applicationLink.
- [Himalayas API terms](https://himalayas.app/terms): review current terms before
  redistributing data. This tool is for your own discovery, not resale of feeds.
- [Official Freelancer SDK search implementation](https://github.com/freelancer/freelancer-sdk-python/blob/master/freelancersdk/resources/projects/projects.py)
  and [projection helpers](https://github.com/freelancer/freelancer-sdk-python/blob/master/freelancersdk/resources/projects/helpers.py):
  active-project endpoint, query/pagination parameters and full-description flag.
  API access requires your own setup; anonymous access is not assumed.
- [r/forhire rules](https://www.reddit.com/r/forhire/about/rules.json): buyer/seller
  separation and no bots contacting members. The scanner only reads feeds; all
  contact remains manual. This does not verify every community's current rules.
- [Remotive API documentation](https://github.com/remotive-com/remote-jobs-api):
  verified as an option but not added because its public results are delayed 24
  hours and focus more on employment. Extra sources should earn their place.

## Validation and limits

Offline fixtures cover source parsing, search rotation, pagination, blocking,
cache persistence, problem matching, buyer/seller distinction, full-time exclusions,
expiry, duplicate identities, alert privacy and feedback. Python compilation and
Git whitespace checks are also run. Fixtures simulate API schemas; they are not
live integration tests.

A read-only GitHub Actions smoke run on November 6, 2026 successfully parsed
Himalayas (60 fetched listings), HN (18), WWR (26) and Remote OK (24). Reddit's first
feed returned 18 posts, then its second request hit HTTP 429. Based on that result,
the final default rotates one Reddit request per scan and does not retry 429s.
The targeted Reddit search endpoint itself was not reached in that smoke run.
Fetched counts are not counts of pitchable leads. The Freelancer adapter and Gmail
delivery remain unverified. See the [smoke run](https://github.com/alemoose/gig-radar/actions/runs/42164468498)
and its source-smoke-health artifact. Python 3.9 and 3.12 offline CI both pass on
Ubuntu 22.04.

Saved alert exports are an optional input, not automatic inbox ingestion. Tracking
redirects and templates without descriptive direct job links are skipped. Feedback
tracks per-source reviewed/pitched/won outcomes and suppresses explicitly rejected
leads; it does not automatically infer new skills or rewrite searches.

The default scanner still has a 72-hour archive window. It does not paginate every
Reddit feed or guarantee that every post is seen. It does not implement a general
web search scraper. Source status is available in the scan artifact rather than
being committed as a timestamp every 20 minutes.
