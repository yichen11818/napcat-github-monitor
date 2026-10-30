import io
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
from PIL import Image
from services.github_monitor import GitHubMonitorService, normalize, repository

from handlers.github_commands import handle_github


BASE_TIME = datetime(2026, 10, 29, 6, tzinfo=timezone.utc).timestamp()


def event(eid, kind="IssuesEvent", created_at=None):
    return {"id": str(eid), "type": kind, "actor": {"login": "tester"}, "created_at": created_at or datetime.fromtimestamp(BASE_TIME + int(eid), timezone.utc).isoformat(), "payload": {"action": "opened", "issue": {"number": 12, "title": "修复中文标题与换行", "body": "回归验证\n多行摘要", "html_url": "https://github.com/a/b/issues/12"}}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        clock_patch = patch("services.github_monitor.time.time", return_value=BASE_TIME)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qq = Mock()
        self.qq.send_image.return_value = True
        self.events = [event(1)]
        self.fetch = Mock(side_effect=lambda repo: self.events)
        self.svc = GitHubMonitorService(self.qq, Path(self.tmp.name)/"state.db", fetch=self.fetch, render=Mock())

    def test_baseline_dedupe_retry_and_unsubscribe(self):
        self.svc.subscribe("g:1", "a/b")
        self.svc.tick()
        self.qq.send_image.assert_not_called()
        self.events = [event(2), event(1)]
        self.svc.next_poll = 0
        self.qq.send_image.return_value = False
        self.svc.tick()
        self.assertEqual(self.qq.send_image.call_count, 1)
        with self.svc.db() as db:
            self.assertEqual(db.execute("SELECT attempts FROM pending").fetchone()[0], 1)
            db.execute("UPDATE pending SET due=0")
        self.qq.send_image.return_value = True
        self.svc.tick()
        self.svc.next_poll = 0
        self.svc.tick()
        self.assertEqual(self.qq.send_image.call_count, 2)
        self.svc.unsubscribe("g:1", "a/b")
        self.assertIn("暂无", self.svc.list_text("g:1"))

    def test_new_subscriber_does_not_steal_existing_subscribers_events(self):
        self.svc.subscribe("g:1", "a/b")
        self.events = [event(2), event(1)]
        self.svc.subscribe("g:2", "a/b")
        self.svc.tick()
        self.assertEqual(self.qq.send_image.call_count, 1)
        self.assertEqual(self.qq.send_image.call_args.args[0], "1")
        self.events = [event(3), *self.events]
        self.svc.next_poll = 0
        self.svc.tick()
        self.assertEqual(self.qq.send_image.call_count, 3)

    def test_fetch_failure_preserves_state_and_unsubscribe_cancels_pending(self):
        self.svc.subscribe("u:1", "a/b")
        self.events = [event(2), event(1)]
        self.qq.send_image.return_value = False
        self.svc.tick()
        self.svc.fetch = Mock(side_effect=RuntimeError("offline"))
        self.svc.next_poll = 0
        self.svc.tick()
        self.svc.unsubscribe("u:1", "a/b")
        with self.svc.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)

    def test_restart_clears_pending_and_ignores_offline_and_delayed_history(self):
        self.svc.subscribe("g:1", "a/b")
        self.events = [event(2), event(1)]
        self.qq.send_image.return_value = False
        self.svc.tick()
        with self.svc.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pending").fetchone()[0], 1)
        self.clock.return_value = BASE_TIME + 10
        restarted = GitHubMonitorService(self.qq, self.svc.path, fetch=self.fetch, render=Mock())
        self.assertIn("a/b", restarted.list_text("g:1"))
        with restarted.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
        self.qq.reset_mock()
        self.qq.send_image.return_value = True
        # Even a failed first poll must not move the startup cutoff forward.
        restarted.fetch = Mock(side_effect=RuntimeError("offline"))
        restarted.tick()
        self.clock.return_value = BASE_TIME + 30
        self.events = [event(20), event(10), event(9), event(2)]
        restarted.fetch = self.fetch
        restarted.next_poll = 0
        restarted.tick()
        self.qq.send_image.assert_called_once()
        self.assertEqual(restarted.render.call_args.args[0]["id"], "20")
        self.events.append(event(8))
        restarted.next_poll = 0
        restarted.tick()
        self.qq.send_image.assert_called_once()

    def test_late_old_event_is_filtered_per_subscription(self):
        self.svc.subscribe("g:1", "a/b")
        self.clock.return_value = BASE_TIME + 10
        self.svc.subscribe("g:2", "a/b")
        self.events = [event(20), event(5), event(1)]
        self.svc.tick()
        self.assertEqual([c.args[0] for c in self.qq.send_image.call_args_list], ["1", "1", "2"])

    def test_unknown_event_time_is_not_delivered(self):
        self.svc.subscribe("g:1", "a/b")
        self.events = []
        for eid, stamp in enumerate([None, "invalid", "2026-10-29T08:00:00"], 2):
            raw = event(eid)
            raw["created_at"] = stamp
            self.events.append(raw)
        missing = event(5)
        del missing["created_at"]
        self.events.append(missing)
        self.svc.tick()
        self.qq.send_image.assert_not_called()

    def test_legacy_database_migration_keeps_subscription_and_drops_backlog(self):
        path = Path(self.tmp.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE subscriptions(target TEXT, repo TEXT, PRIMARY KEY(target,repo));
                INSERT INTO subscriptions VALUES ('g:1','a/b');
                CREATE TABLE pending(target TEXT, repo TEXT, id TEXT, event TEXT, attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, PRIMARY KEY(target,repo,id));
                INSERT INTO pending(target,repo,id,event) VALUES ('g:1','a/b','old','{}');
            """)
        migrated = GitHubMonitorService(self.qq, path, fetch=self.fetch, render=Mock())
        self.assertIn("a/b", migrated.list_text("g:1"))
        with migrated.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
        self.events = [event(0), event(2)]
        migrated.tick()
        self.qq.send_image.assert_called_once()
        self.assertEqual(migrated.render.call_args.args[0]["id"], "2")

    def test_all_four_kinds_and_repository_validation(self):
        for raw, kind in [("PushEvent", "push"), ("ReleaseEvent", "release"), ("IssuesEvent", "issue"), ("PullRequestEvent", "pr")]:
            self.assertEqual(normalize("a/b", event(1, raw))["kind"], kind)
        self.assertIsNone(normalize("a/b", event(1, "WatchEvent")))
        self.assertEqual(repository("https://github.com/A/B.git"), "a/b")
        for value in ("https://evil/a/b", "a/..", "a/b?x=y", "a/b/c"):
            with self.assertRaises(ValueError):
                repository(value)

    def test_permissions_and_queue_are_enforced(self):
        h = Mock()
        h.admin_qq = "99"
        h.github_monitor_service = self.svc
        h.github_job_queue.submit.side_effect = lambda job: job() or True
        h.qq_api.get_group_member_info.return_value = {"role": "member"}
        handle_github(h, "GitHub订阅 a/b", 1, 2)
        self.fetch.assert_not_called()
        h.qq_api.get_group_member_info.return_value = {"role": "admin"}
        handle_github(h, "GitHub订阅 a/b", 1, 2)
        self.assertIn("a/b", self.svc.list_text("g:1"))
        self.assertIsNone(handle_github(h, "今天github怎么样", 1, 2))



if __name__ == "__main__":
    unittest.main()
