import argparse
import contextlib
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import discovery
import feedback
import gig_radar as gr
import import_alerts as alerts

NOW = datetime.now(timezone.utc)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(gr.DEFAULT_CONFIG))

    def gig(self, title="[Hiring] Connect our calendar to our CRM", body="Need someone for a small project", **kwargs):
        return gr.Gig("x", "r/forhire", title, "https://example.com/job", NOW, body, **kwargs)

    def qualify(self, gig):
        gig = gr.score_gig(gig, self.cfg["include"], self.cfg["exclude"])
        return gig is not None and discovery.classify(gig, self.cfg) and gig.score >= self.cfg["min_score"]

    def test_problem_description_without_stack_names_matches(self):
        gig = self.gig()
        self.assertTrue(self.qualify(gig))
        self.assertIn("API connection", gig.reasons)
        self.assertEqual(gig.queue, "project")

    def test_discussion_is_not_hiring(self):
        self.assertFalse(self.qualify(self.gig("How do I automate my spreadsheet?", "Any advice on automating Excel?")))

    def test_seller_is_not_hiring_even_with_budget(self):
        self.assertFalse(self.qualify(self.gig("[For Hire] AI chatbot developer", "My services start at $500")))

    def test_closed_job_is_not_a_candidate(self):
        self.assertFalse(self.qualify(self.gig("[Closed] AI chatbot developer")))

    def test_fulltime_with_negated_contract_is_filtered(self):
        self.assertFalse(self.qualify(self.gig("[Hiring] React developer", "Full-time only. No contractors. Build automation with Next.js.")))

    def test_contract_queue_separate_from_project_queue(self):
        project = self.gig("[Hiring] Fix our booking website")
        contract = self.gig("[Hiring] Contract React developer", "AI chatbot work", employment_type="contractor")
        self.assertTrue(self.qualify(project))
        self.assertTrue(self.qualify(contract))
        self.assertEqual(contract.queue, "contract")
        self.assertEqual(sorted([contract, project], key=discovery.rank_key)[0], project)

    def test_budget_floor_only_when_currency_known(self):
        usd = self.gig("[Hiring] AI chatbot automation", budget="USD 10/hour", currency="USD")
        aud = self.gig("[Hiring] AI chatbot automation", budget="$10/hour", currency="AUD")
        self.qualify(usd)
        self.qualify(aud)
        self.assertIn("hourly budget below preferred floor", usd.flags)
        self.assertNotIn("hourly budget below preferred floor", aud.flags)
        self.assertGreater(aud.priority, usd.priority)

    def test_tracking_duplicates_preserve_identity_query(self):
        self.assertEqual(discovery.canonical_url("https://news.ycombinator.com/item?id=4&utm_source=test"),
                         "https://news.ycombinator.com/item?id=4")
        self.assertNotEqual(discovery.canonical_url("https://news.ycombinator.com/item?id=4"),
                            discovery.canonical_url("https://news.ycombinator.com/item?id=5"))
        a, b = self.gig(), self.gig()
        b.id, b.url = "second", "https://example.com/job?utm_source=mail"
        result = discovery.dedupe([a, b])
        self.assertEqual(len(result), 1)
        self.assertEqual({result[0].id, *result[0].aliases}, {"x", "second"})

    def test_reddit_slug_changes_are_same_post(self):
        a = "https://old.reddit.com/r/forhire/comments/abc123/title/"
        b = "https://www.reddit.com/r/forhire/comments/abc123/edited/?utm_source=mail"
        self.assertEqual(discovery.canonical_url(a), discovery.canonical_url(b))

    def test_different_clients_same_title_not_deduplicated(self):
        a, b = self.gig(), self.gig()
        b.id, b.url = "second", "https://example.com/other"
        self.assertEqual(len(discovery.dedupe([a, b])), 2)

    def test_rotation_adds_community_and_problem_search_with_spacing(self):
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        cfg = {**self.cfg["sources"]["reddit"], "max_requests_per_run": 3}
        with mock.patch.object(gr, "http_get", return_value=empty) as get, mock.patch.object(gr.time, "sleep") as sleep, mock.patch.object(gr.time, "time", return_value=0):
            gr.fetch_reddit(cfg)
        urls = [call.args[0] for call in get.call_args_list]
        self.assertEqual(len(urls), 3)
        self.assertIn("/new/.rss", urls[1])
        self.assertIn("/search.rss?", urls[2])
        self.assertEqual(sleep.call_count, 2)

    def test_default_reddit_rotation_covers_all_communities_and_queries(self):
        cfg = self.cfg["sources"]["reddit"]
        urls = []
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        with mock.patch.object(gr, "http_get", side_effect=lambda url: urls.append(url) or empty):
            for tick in range(18):
                with mock.patch.object(gr.time, "time", return_value=tick * 1200):
                    gr.fetch_reddit(cfg)
        self.assertEqual(len(urls), 18)
        for sub in cfg["subreddits"]:
            self.assertIn(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100", urls)
        self.assertEqual(len({u for u in urls if "/search.rss?" in u}), 4)

    def test_http_rate_limit_is_not_retried(self):
        from urllib.error import HTTPError
        with mock.patch("urllib.request.urlopen", side_effect=HTTPError("https://reddit.com", 429, "limited", {}, None)) as get:
            with self.assertRaises(gr.SourceError):
                gr.http_get("https://reddit.com")
        self.assertEqual(get.call_count, 1)

    def test_reddit_block_stops_extra_requests(self):
        with mock.patch.object(gr, "http_get", side_effect=gr.SourceError("HTTP 429")) as get:
            with self.assertRaises(gr.SourceError):
                gr.fetch_reddit(self.cfg["sources"]["reddit"])
        self.assertEqual(get.call_count, 1)

    def test_partial_reddit_failure_visible_in_report(self):
        report = {}
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        self.cfg["sources"]["reddit"]["max_requests_per_run"] = 3
        with mock.patch.object(gr, "FETCHERS", {"reddit": gr.fetch_reddit}), \
             mock.patch.object(gr, "http_get", side_effect=[empty, gr.SourceError("HTTP 429")]), \
             mock.patch.object(gr.time, "sleep"):
            gr.collect(self.cfg, report)
        self.assertEqual(report["status"], "partial")
        self.assertIn("HTTP 429", report["sources"]["reddit"]["warnings"][0])

    def test_himalayas_preserves_application_location_currency_and_expiry(self):
        row = {"guid": "https://himalayas.app/companies/acme/jobs/developer", "title": "React developer",
               "companyName": "Acme", "pubDate": NOW.timestamp(), "expiryDate": (NOW + timedelta(days=10)).timestamp(),
               "employmentType": "Contractor", "locationRestrictions": ["United States"],
               "currency": "USD", "minSalary": 40, "maxSalary": 60, "salaryPeriod": "hourly",
               "applicationLink": "https://acme.example/apply?source=himalayas", "description": "<p>AI chatbot work</p>"}
        gig = gr.parse_himalayas({"jobs": [row]})[0]
        self.assertEqual(gig.apply_url, row["applicationLink"])
        self.assertEqual(gig.location, "United States")
        self.assertEqual(gig.currency, "USD")
        self.assertTrue(self.qualify(gig))
        self.assertEqual(gig.queue, "contract")
        self.assertNotIn("project budget below preferred floor", gig.flags)
        self.assertNotIn("hourly budget below preferred floor", gig.flags)
        gig.expires = NOW - timedelta(days=1)
        self.assertFalse(self.qualify(gig))

    def test_himalayas_search_paginates_and_filters_server_side(self):
        row = {"guid": "/companies/acme/jobs/dev", "title": "Contract React developer", "pubDate": NOW.timestamp()}
        pages = [json.dumps({"jobs": [row] * 20}).encode(), b'{"jobs": []}']
        with mock.patch.object(gr, "http_get", side_effect=pages) as get, mock.patch.object(gr.time, "sleep"):
            gr.fetch_himalayas({"max_pages": 3})
        self.assertEqual(get.call_count, 2)
        self.assertIn("country=US", get.call_args.args[0])
        self.assertIn("page=2", get.call_args.args[0])

    def test_freelancer_budget_and_pagination(self):
        row = {"id": 4, "title": "Fix API integration", "time_submitted": NOW.timestamp(),
               "seo_url": "python/fix-api", "type": "fixed", "currency": {"code": "EUR"},
               "budget": {"minimum": 200, "maximum": 500}, "description": "Need someone to connect an API"}
        payload = {"status": "success", "result": {"projects": [row]}}
        with mock.patch.dict("os.environ", {"FREELANCER_OAUTH_TOKEN": "test-only"}), \
             mock.patch.object(gr, "http_get", return_value=json.dumps(payload).encode()) as get, mock.patch.object(gr.time, "sleep"):
            gigs = gr.fetch_freelancer({"queries": ["API integration"], "max_pages": 2})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(gigs[0].budget, "EUR 200 to 500")
        self.assertEqual(get.call_args.kwargs["headers"]["Freelancer-OAuth-V1"], "test-only")
        self.assertIn("full_description=true", get.call_args.args[0])

    def test_freelancer_requires_setup_instead_of_anonymous_assumption(self):
        with mock.patch.dict("os.environ", {"FREELANCER_OAUTH_TOKEN": ""}):
            with self.assertRaises(gr.SourceError):
                gr.fetch_freelancer({})

    def test_daily_cache_survives_new_invocations_and_config_change(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gr, "STATE_PATH", Path(tmp) / "seen.json"):
            fetch = mock.Mock(return_value=[self.gig()])
            self.assertFalse(gr.fetch_with_cache("himalayas", fetch, {"min_interval_hours": 24})[1])
            self.assertTrue(gr.fetch_with_cache("himalayas", fetch, {"min_interval_hours": 24})[1])
            self.assertEqual(fetch.call_count, 1)
            gr.fetch_with_cache("himalayas", fetch, {"min_interval_hours": 24, "country": "CA"})
            self.assertEqual(fetch.call_count, 2)

    def test_dry_source_fetch_does_not_save_cache(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gr, "STATE_PATH", Path(tmp) / "seen.json"):
            gr.fetch_with_cache("himalayas", lambda _: [self.gig()], {"min_interval_hours": 24}, write_cache=False)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_wrong_fit_feedback_suppresses_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gig = self.gig()
            gig.aliases = ["old"]
            feedback_path = root / "feedback.json"
            feedback_path.write_text(json.dumps({"old": {"source": gig.source, "events": ["wrong_fit"]}}))
            with mock.patch.object(gr, "DIGEST_PATH", root / "matches.md"), \
                 mock.patch.dict("os.environ", {"GIG_RADAR_FEEDBACK": str(feedback_path)}):
                gr.write_archive([gig])
                self.assertEqual(gr.merge_archive([], self.cfg), [])

    def test_source_report_distinguishes_empty_and_failed(self):
        report = {}
        with mock.patch.object(gr, "FETCHERS", {"empty": lambda _: [], "bad": mock.Mock(side_effect=gr.SourceError("offline"))}):
            gr.collect(self.cfg, report)
        self.assertEqual(report["sources"]["empty"]["status"], "ok")
        self.assertEqual(report["sources"]["bad"]["status"], "failed")
        self.assertEqual(report["status"], "partial")


class AlertAndFeedbackTests(unittest.TestCase):
    def email(self, html):
        return ("Date: Fri, 06 Nov 2026 03:00:00 +0000\nContent-Type: text/html; charset=utf-8\n\n" + html).encode()

    def test_import_only_job_links_and_no_unrelated_email_content(self):
        gigs = alerts.parse_eml(self.email('<a href="https://www.upwork.com/jobs/~abc123?utm_source=mail">Fix our booking website</a>'
                                         '<p>Private email details and another budget $50,000</p>'
                                         '<a href="https://www.upwork.com/unsubscribe">Unsubscribe</a>'))
        self.assertEqual(len(gigs), 1)
        self.assertEqual(gigs[0].date_basis, "email received")
        self.assertNotIn("50,000", gigs[0].body)
        self.assertTrue(gigs[0].imported)
        self.assertNotIn("utm_source", gigs[0].url)

    def test_alert_dates_are_required(self):
        with self.assertRaises(ValueError):
            alerts.parse_eml(b'Content-Type: text/plain\n\nFix website https://www.upwork.com/jobs/~abc123')

    def test_tracking_redirects_are_not_followed(self):
        self.assertEqual(alerts.parse_eml(self.email('<a href="https://tracking.example/redirect?url=upwork">Fix website</a>')), [])

    def test_repeated_alert_keeps_original_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            message = root / "alert.eml"
            message.write_bytes(self.email('<a href="https://www.upwork.com/jobs/~abc123">Fix our booking website</a>'))
            self.assertEqual(alerts.import_files([message], root / "alerts.json"), 1)
            original = (root / "alerts.json").read_text()
            message.write_bytes(message.read_bytes().replace(b"03:00:00", b"05:00:00"))
            self.assertEqual(alerts.import_files([message], root / "alerts.json"), 0)
            self.assertEqual((root / "alerts.json").read_text(), original)

    def test_feedback_keeps_pitch_and_win_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "matches.json"
            archive.write_text(json.dumps({"gigs": [{"id": "x", "source": "r/forhire", "aliases": ["old"]}]}))
            path = root / "feedback.json"
            feedback.record(path, archive, "old", "pitched")
            feedback.record(path, archive, "x", "won")
            feedback.record(path, archive, "x", "won")
            summary = feedback.summary(feedback.load(path))["r/forhire"]
            self.assertEqual(summary["pitched"], 1)
            self.assertEqual(summary["won"], 1)
            self.assertEqual(summary["reviewed"], 1)


if __name__ == "__main__":
    unittest.main()
