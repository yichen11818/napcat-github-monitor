import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image, ImageDraw, ImageFont

from services.github_event_image import render_event_card, _wrap, _avatar, font_file
from services.github_monitor import GitHubMonitorService, normalize


class CardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_event_kinds_render_cjk_and_long_content_offline(self):
        with patch("services.github_event_image.requests.get") as get:
            for kind in ("push", "release", "issue", "pr"):
                event = dict(kind=kind, repo="owner/" + "long-repository-name-" * 6,
                             title="修复中文换行与超长标题：" + "verylongidentifier" * 20,
                             actor="very-long-author-name-" * 5, body="中文说明\n" * 100,
                             created_at="2026-10-29T06:00:00Z", number=123 if kind in ("issue", "pr") else None)
                path = self.root / "cards" / (kind + ".png")
                self.assertEqual(render_event_card(event, path), str(path))
                with Image.open(path) as image:
                    self.assertEqual(image.size, (1200, 600))
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                self.assertFalse(list(path.parent.glob(".github-*")))
            get.assert_not_called()

    def test_long_unbroken_title_and_cjk_stay_within_width(self):
        draw = ImageDraw.Draw(Image.new("RGB", (1200, 600)))
        font = ImageFont.truetype(font_file(bold=True), 62)
        for text in ("中文混合ABC" * 100, "longidentifier" * 100, "word " * 100):
            lines = _wrap(draw, text, font, 800, 3)
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[-1].endswith("…"))
            self.assertTrue(all(draw.textlength(line, font=font) <= 800 for line in lines))

    def test_avatar_timeout_still_renders_and_uses_backoff(self):
        event = dict(kind="release", repo="a/b", title="版本发布", actor="tester", avatar_id=987654321,
                     created_at="2026-10-29T06:00:00Z")
        with patch("services.github_event_image.requests.get", side_effect=requests.Timeout) as get:
            for i in range(2):
                render_event_card(event, self.root / "cards" / f"{i}.png")
            self.assertEqual(get.call_count, 1)
        with patch("services.github_event_image.requests.get") as get:
            _avatar(dict(actor="test", avatar_id="../../etc/passwd"), self.root).close()
            get.assert_not_called()

    def test_normalized_metadata_for_cards(self):
        push = normalize("a/b", dict(id="1", type="PushEvent", actor={"login": "author", "id": 7},
                                     payload={"ref": "refs/heads/main", "head": "a" * 40,
                                              "commits": [{"message": "fix: 修复崩溃\n详细说明"}]}))
        self.assertEqual(push["title"], "fix: 修复崩溃")
        self.assertEqual(push["body"], "详细说明")
        self.assertEqual(push["branch"], "main")
        self.assertEqual(push["avatar_id"], 7)
        pr = normalize("a/b", dict(id="2", type="PullRequestEvent", payload={"action": "closed", "number": 42,
                                   "pull_request": {"title": "修复", "merged": True, "comments": 3,
                                                    "additions": 20, "deletions": 2}}))
        self.assertEqual((pr["number"], pr["merged"], pr["comments"], pr["additions"]), (42, True, 3, 20))

    def test_commit_enrichment_cached_and_failure_does_not_block_cards(self):
        svc = GitHubMonitorService(Mock(), self.root / "state.db")
        event = dict(kind="push", repo="a/b", sha="a" * 40, title="main · 新提交")
        response = Mock(status_code=200)
        response.json.return_value = {"commit": {"message": "Real title\nDetails"}, "stats": {"additions": 32, "deletions": 30}}
        with patch("services.github_monitor.requests.get", return_value=response) as get:
            result = svc._prepare_card(event)
            self.assertEqual((result["title"], result["additions"]), ("Real title", 32))
            self.assertEqual(svc._prepare_card(event), result)
            get.assert_called_once()
        event["sha"] = "b" * 40
        with patch("services.github_monitor.requests.get", side_effect=requests.Timeout):
            self.assertEqual(svc._prepare_card(event), event)


if __name__ == "__main__":
    unittest.main()
