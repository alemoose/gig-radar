# Changes prepared November 6, 2026

Based on alemoose/gig-radar commit ed539d53c920b0f9f683a99af4be52653765b883.
The attachment contained a summary and prompt; source was retrieved through GitHub.

## Better discovery

Rotating Reddit community and targeted-search feeds, deliverable-based matching,
project and contract queues, a daily cached Himalayas search, optional authenticated
Freelancer search, a private email-export importer, URL/crosspost deduplication and
visible fit reasons. Local outcomes show which sources produce pitchable work.
See DISCOVERY.md for source references, validation and coverage limitations.

## Reliability

Retained structured archive, isolated source failures, honest partial-source health,
preview-only dry runs, stable keyword ordering, atomic individual file replacement,
redacted notification error logs, durable digest acknowledgements and alias-aware
delivery deduplication. See README for setup and interrupted-send recovery.

## Verification

49 offline tests pass. Python compilation and Git whitespace checks pass. Added
GitHub CI for Python 3.9 and 3.12; both versions pass on Ubuntu 22.04 runners.
A read-only source-smoke workflow checks public feed/API access without notifications.
A read-only GitHub smoke run parsed Himalayas, HN, WWR, Remote OK and the first
Reddit feed; Reddit then rate-limited an extra request. Final defaults use one
rotating Reddit request per scan and no quick 429 retries. Freelancer and Gmail
delivery remain unverified. No live email task has been reconfigured.

## Deployment notes

Keep existing seen.json and matches.md. The scanner generates matches.json on its
first successful real run. Default discovery changes require no new credentials.
Freelancer is off until configured with your own API token; email imports and
feedback stay private and local. Persistent source caching is included in Actions.
The optional revised email prompt requires durable private storage and explicit
RUN_MODE and DIGEST_SLOT inputs before use.
