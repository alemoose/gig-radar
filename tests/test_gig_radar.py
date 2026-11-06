"""Offline tests using sample payloads shaped like the real feeds. Run: python -m unittest discover tests"""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gig_radar as gr  # noqa: E402

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


REDDIT_ATOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/smallbiz_owner</name></author>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;Need a GPT chatbot on our Shopify site that answers FAQ from our docs. Budget $400-600. Must use Next.js or similar.&lt;/p&gt;&lt;/div&gt; submitted by /u/smallbiz_owner</content>
    <id>t3_aaa111</id>
    <link href="https://www.reddit.com/r/forhire/comments/aaa111/hiring_ai_chatbot/" />
    <updated>{iso(NOW - timedelta(minutes=20))}</updated>
    <published>{iso(NOW - timedelta(minutes=20))}</published>
    <title>[Hiring] AI chatbot for our store website</title>
  </entry>
  <entry>
    <author><name>/u/freelancer_bob</name></author>
    <content type="html">&lt;p&gt;I build React apps and AI agents.&lt;/p&gt;</content>
    <id>t3_bbb222</id>
    <link href="https://www.reddit.com/r/forhire/comments/bbb222/for_hire_dev/" />
    <published>{iso(NOW - timedelta(minutes=5))}</published>
    <title>[For Hire] React + AI developer</title>
  </entry>
  <entry>
    <author><name>/u/founder</name></author>
    <content type="html">&lt;p&gt;Looking for a cofounder to build an AI SaaS. Equity only.&lt;/p&gt;</content>
    <id>t3_ccc333</id>
    <link href="https://www.reddit.com/r/forhire/comments/ccc333/x/" />
    <published>{iso(NOW - timedelta(minutes=40))}</published>
    <title>[Hiring] AI SaaS developer</title>
  </entry>
  <entry>
    <author><name>/u/old_post</name></author>
    <content type="html">&lt;p&gt;Python scraper needed, $50.&lt;/p&gt;</content>
    <id>t3_ddd444</id>
    <link href="https://www.reddit.com/r/forhire/comments/ddd444/x/" />
    <published>{iso(NOW - timedelta(days=6))}</published>
    <title>[Hiring] Web scraper for real estate listings</title>
  </entry>
</feed>""".encode()

HN_SEARCH = {
    "hits": [
        {"objectID": "50000001", "title": "Ask HN: Who is hiring? (November 2026)", "created_at_i": 1793000000},
        {"objectID": "50000002", "title": "Ask HN: Freelancer? Seeking freelancer? (November 2026)", "created_at_i": 1793000001},
        {"objectID": "49000002", "title": "Ask HN: Freelancer? Seeking freelancer? (October 2026)", "created_at_i": 1790000000},
    ]
}

HN_FREELANCER_ITEM = {
    "id": 50000002,
    "children": [
        {"id": 1, "created_at_i": int((NOW - timedelta(hours=3)).timestamp()),
         "text": "SEEKING FREELANCER | Remote | n8n + OpenAI automation<p>We want to automate lead intake from Typeform into Airtable with GPT summaries. ~20 hours, $75&#x2F;hr."},
        {"id": 2, "created_at_i": int((NOW - timedelta(hours=1)).timestamp()),
         "text": "SEEKING WORK | Remote | Full-stack TypeScript, React, AI agents"},
    ],
}

HN_HIRING_ITEM = {
    "id": 50000001,
    "children": [
        {"id": 3, "created_at_i": int((NOW - timedelta(hours=2)).timestamp()),
         "text": "Acme | Senior Backend Engineer | Full-time | NYC<p>Postgres, Node, LLM infra."},
        {"id": 4, "created_at_i": int((NOW - timedelta(hours=2)).timestamp()),
         "text": "Beta Labs | Contract React Native developer | Remote<p>3 month contract building an AI journaling app."},
    ],
}

WWR_RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Gamma: Contract Full-Stack Developer (Next.js + OpenAI)</title>
<link>https://weworkremotely.com/remote-jobs/gamma-contract</link>
<guid>https://weworkremotely.com/remote-jobs/gamma-contract</guid>
<pubDate>{(NOW - timedelta(hours=10)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
<description>&lt;p&gt;6 week contract to ship an AI feature.&lt;/p&gt;</description></item>
<item><title>Delta: Staff Engineer</title>
<link>https://weworkremotely.com/remote-jobs/delta-staff</link>
<pubDate>{(NOW - timedelta(hours=2)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
<description>&lt;p&gt;Full-time role, React and AI.&lt;/p&gt;</description></item>
</channel></rss>""".encode()

REMOTEOK = [
    {"legal": "API terms: link back to Remote OK"},
    {"id": "777", "epoch": int((NOW - timedelta(hours=5)).timestamp()), "company": "Epsilon",
     "position": "Freelance AI Automation Engineer", "tags": ["ai", "python", "contract"],
     "description": "<p>Build Zapier and LLM workflows.</p>", "url": "https://remoteok.com/remote-jobs/777",
     "salary_min": 60000, "salary_max": 90000},
    {"id": "778", "epoch": int((NOW - timedelta(hours=5)).timestamp()), "company": "Zeta",
     "position": "Senior AI Engineer", "tags": ["ai"], "description": "Full-time.", "url": "https://remoteok.com/x"},
]


class TestParsers(unittest.TestCase):
    def setUp(self):
        self.cfg = gr.load_config()

    def scored(self, gigs):
        out = []
        for g in gigs:
            s = gr.score_gig(g, self.cfg["include"], self.cfg["exclude"])
            if s and s.score >= self.cfg["min_score"]:
                out.append(s)
        return out

    def test_reddit_keeps_hiring_drops_for_hire_and_equity(self):
        gigs = gr.parse_reddit_atom(REDDIT_ATOM, "forhire", ["[hiring]"])
        self.assertEqual(len(gigs), 3)  # [For Hire] dropped by tag filter
        kept = self.scored(gigs)
        titles = [g.title for g in kept]
        self.assertIn("[Hiring] AI chatbot for our store website", titles)
        self.assertNotIn("[Hiring] AI SaaS developer", titles)  # equity only
        chatbot = next(g for g in kept if "chatbot" in g.title)
        self.assertEqual(chatbot.budget, "$400-600")
        self.assertNotIn("submitted by", chatbot.body)
        self.assertIn("chatbot", chatbot.matched)

    def test_hn_threads_pick_latest(self):
        threads = gr.latest_hn_threads(HN_SEARCH)
        self.assertEqual(threads, {"freelancer": 50000002, "hiring": 50000001})

    def test_hn_freelancer_only_seeking_freelancer(self):
        gigs = gr.parse_hn_thread(HN_FREELANCER_ITEM, "freelancer", [])
        self.assertEqual(len(gigs), 1)
        g = self.scored(gigs)[0]
        self.assertTrue(g.title.startswith("SEEKING FREELANCER"))
        self.assertEqual(g.budget, "$75/hr")
        self.assertEqual(g.url, "https://news.ycombinator.com/item?id=1")

    def test_hn_who_is_hiring_requires_contract(self):
        gigs = gr.parse_hn_thread(HN_HIRING_ITEM, "hiring", ["contract", "freelance"])
        self.assertEqual([g.id for g in gigs], ["hn:4"])

    def test_wwr_requires_contract(self):
        gigs = gr.parse_rss2(WWR_RSS, "We Work Remotely", ["contract"])
        self.assertEqual(len(gigs), 1)
        self.assertIn("Gamma", gigs[0].title)

    def test_remoteok_skips_legal_and_fulltime(self):
        gigs = gr.parse_remoteok(REMOTEOK, ["freelance", "contract"])
        self.assertEqual(len(gigs), 1)
        self.assertEqual(gigs[0].budget, "$60,000 to $90,000/yr")

    def test_term_matching_word_boundaries(self):
        self.assertFalse(gr.contains_any("He said the paint was dry", ["ai"]))
        self.assertTrue(gr.contains_any("Build an AI chatbot", ["ai"]))
        self.assertTrue(gr.contains_any("Next.js app", ["next.js"]))


class TestEndToEnd(unittest.TestCase):
    def test_run_once_sorts_dedupes_and_notifies_only_new(self):
        responses = {
            "reddit.com/r/forhire": REDDIT_ATOM,
            "search_by_date": json.dumps(HN_SEARCH).encode(),
            "items/50000002": json.dumps(HN_FREELANCER_ITEM).encode(),
            "items/50000001": json.dumps(HN_HIRING_ITEM).encode(),
            "weworkremotely": WWR_RSS,
            "remoteok.com/api": json.dumps(REMOTEOK).encode(),
        }

        def fake_get(url, timeout=20):
            for key, val in responses.items():
                if key in url:
                    return val
            return None

        sent = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gr, "STATE_PATH", Path(tmp) / "seen.json"), \
                mock.patch.object(gr, "DIGEST_PATH", Path(tmp) / "matches.md"), \
                mock.patch.object(gr, "CONFIG_PATH", Path(tmp) / "none.json"), \
                mock.patch.object(gr, "http_get", side_effect=fake_get), \
                mock.patch.object(gr, "notify", side_effect=lambda gigs, n: sent.append([g.id for g in gigs])), \
                mock.patch.object(gr.time, "sleep"):
            cfg = gr.load_config()
            matches = gr.collect(cfg)
            # newest first
            self.assertEqual(matches, sorted(matches, key=lambda g: g.posted, reverse=True))
            ids = {g.id for g in matches}
            self.assertIn("reddit:t3_aaa111", ids)
            self.assertIn("hn:1", ids)
            self.assertIn("hn:4", ids)
            self.assertIn("remoteok:777", ids)
            self.assertNotIn("reddit:t3_ddd444", ids)  # too old

            args = mock.Mock(all=False, no_notify=False, dry_run=False)
            gr.run_once(args, cfg)
            gr.run_once(args, cfg)  # second run: nothing new
            self.assertEqual(len(sent), 1)
            self.assertEqual(set(sent[0]), ids)
            digest = (Path(tmp) / "matches.md").read_text()
            self.assertIn("AI chatbot", digest)


class TestNotifyPayload(unittest.TestCase):
    def test_ntfy_payload(self):
        posted = []
        g = gr.Gig("x", "r/forhire", "[Hiring] AI chatbot", "https://reddit.com/x",
                   NOW, "Need a bot. $500", "$500", 4, ["chatbot"])
        with mock.patch.dict("os.environ", {"NTFY_TOPIC": "abc", "DISCORD_WEBHOOK_URL": ""}), \
                mock.patch.object(gr, "http_post_json", side_effect=lambda u, p, h=None: posted.append((u, p)) or True), \
                mock.patch.object(gr.time, "sleep"):
            gr.notify([g], 5)
        url, payload = posted[0]
        self.assertEqual(url, "https://ntfy.sh")
        self.assertEqual(payload["topic"], "abc")
        self.assertEqual(payload["click"], "https://reddit.com/x")
        self.assertEqual(payload["priority"], 4)


if __name__ == "__main__":
    unittest.main()
