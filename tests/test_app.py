import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import OneBot, create_app


class AppTests(unittest.TestCase):
    def test_signed_event_and_reject_unsigned(self):
        client = create_app(Mock(), "secret").test_client()
        payload = json.dumps(dict(post_type="message", message_type="group", group_id=1,
                                  user_id=2, self_id=3, message=[
                                      {"type": "text", "data": {"text": "GitHub列表"}}])).encode()
        self.assertEqual(client.post("/onebot/events", data=payload).status_code, 403)
        signature = "sha1=" + hmac.new(b"secret", payload, hashlib.sha1).hexdigest()
        with patch("app.handle_github") as handle:
            response = client.post("/onebot/events", data=payload, content_type="application/json",
                                   headers={"X-Signature": signature})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(handle.call_args.args[1:], ("GitHub列表", 1, 2))

    def test_image_transport_and_failed_delivery(self):
        api = OneBot("http://localhost:3000", "token")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "card.png"
            path.write_bytes(b"image")
            with patch("app.requests.post") as post:
                post.return_value.json.return_value = {"status": "ok", "retcode": 0}
                self.assertTrue(api.send_image("123", path, text="https://github.com/a/b"))
                payload = post.call_args.kwargs["json"]
                self.assertEqual(payload["group_id"], 123)
                self.assertEqual(payload["message"][0]["data"]["file"], "base64://aW1hZ2U=")
                post.return_value.json.return_value = {"status": "failed", "retcode": 1}
                with self.assertRaises(RuntimeError):
                    api.send_message(None, "hello", "456")
