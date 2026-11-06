import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import digest_queue as dq
import gig_radar as gr


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, path in (("DIGEST_PATH", "matches.md"), ("STATE_PATH", "seen.json")):
            patcher = mock.patch.object(gr, name, self.root / path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = json.loads(json.dumps(gr.DEFAULT_CONFIG))
        self.gig = gr.Gig("test:1", "test", "AI chatbot", "https://example.com/job",
                          datetime.now(timezone.utc), "Need automation", score=5)

    def run_scan(self, dry=False):
        with contextlib.redirect_stdout(io.StringIO()):
            gr.run_once(argparse.Namespace(all=False, no_notify=False, dry_run=dry), self.cfg)

    def test_archive_retains_gig_missing_from_next_feed(self):
        with mock.patch.object(gr, "collect", side_effect=[[self.gig], []]), mock.patch.object(gr, "notify") as notify:
            self.run_scan()
            self.run_scan()
        self.assertEqual(notify.call_count, 1)
        self.assertIn("AI chatbot", gr.DIGEST_PATH.read_text())
        self.assertEqual(json.loads(gr.archive_path().read_text())["gigs"][0]["id"], "test:1")

    def test_archive_expires_old_and_rechecks_exclusions(self):
        gr.write_archive([self.gig])
        self.cfg["exclude"] = ["chatbot"]
        self.assertEqual(gr.merge_archive([], self.cfg), [])
        self.gig.posted -= timedelta(days=4)
        gr.write_archive([self.gig])
        self.assertEqual(gr.merge_archive([], gr.DEFAULT_CONFIG), [])

    def test_dry_run_has_no_writes_or_notifications(self):
        with mock.patch.object(gr, "collect", return_value=[self.gig]), mock.patch.object(gr, "notify") as notify:
            self.run_scan(dry=True)
        notify.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_source_does_not_stop_healthy_source(self):
        bad = mock.Mock(side_effect=json.JSONDecodeError("bad", "", 0))
        good = mock.Mock(return_value=[self.gig])
        with mock.patch.object(gr, "FETCHERS", {"bad": bad, "good": good}):
            self.assertEqual(gr.collect(self.cfg), [self.gig])

    def test_total_outage_preserves_existing_files(self):
        gr.DIGEST_PATH.write_text("previous digest")
        with mock.patch.object(gr, "FETCHERS", {"bad": mock.Mock(side_effect=gr.SourceError("offline"))}):
            with self.assertRaises(gr.SourceError):
                self.run_scan()
        self.assertEqual(gr.DIGEST_PATH.read_text(), "previous digest")
        self.assertFalse(gr.STATE_PATH.exists())

    def test_nested_keywords_do_not_inflate_score(self):
        self.gig.title, self.gig.body = "React Native developer", ""
        self.assertEqual(gr.score_gig(self.gig, ["react", "react native"], []).score, 2)

    def test_corrupt_seen_fails_without_overwriting(self):
        gr.STATE_PATH.write_text("broken")
        with mock.patch.object(gr, "collect", return_value=[self.gig]):
            with self.assertRaises(ValueError):
                self.run_scan()
        self.assertEqual(gr.STATE_PATH.read_text(), "broken")
        self.assertFalse(gr.DIGEST_PATH.exists())

    def test_digest_retry_requires_resolution_and_ack_prevents_repeat(self):
        gr.write_archive([self.gig])
        state = self.root / "private"
        self.assertEqual(len(dq.prepare(gr.archive_path(), state)["gigs"]), 1)
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            dq.prepare(gr.archive_path(), state)
        dq.acknowledge(state)
        self.assertEqual(dq.prepare(gr.archive_path(), state)["gigs"], [])

    def test_digest_aliases_prevent_repeat_after_source_identity_changes(self):
        self.gig.aliases = ["old-id"]
        gr.write_archive([self.gig])
        state = self.root / "private"
        state.mkdir()
        (state / "processed.json").write_text(json.dumps({"old-id": True}))
        self.assertEqual(dq.prepare(gr.archive_path(), state)["gigs"], [])

    def test_manual_preview_does_not_change_queue(self):
        gr.write_archive([self.gig])
        state = self.root / "private"
        self.assertEqual(len(dq.prepare(gr.archive_path(), state, preview=True)["gigs"]), 1)
        self.assertFalse(state.exists())

    def test_notification_errors_do_not_expose_webhook(self):
        secret = "https://discord.com/api/webhooks/private-token"
        with mock.patch("urllib.request.urlopen", side_effect=OSError(secret)), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertFalse(gr.http_post_json(secret, {}))
        self.assertNotIn("private-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
