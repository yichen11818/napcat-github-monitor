"""Standalone NapCat/OneBot HTTP GitHub monitor."""
import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

import requests
from flask import Flask, abort, request
from waitress import serve

from handlers.github_commands import handle_github
from services.github_monitor import GitHubMonitorService


class OneBot:
    def __init__(self, url, token=""):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token} if token else {}

    def call(self, action, payload, timeout=20):
        response = requests.post(self.url + "/" + action, json=payload,
                                 headers=self.headers, timeout=(5, timeout))
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok" or data.get("retcode") != 0:
            raise RuntimeError("OneBot action failed: " + action)
        return data.get("data")

    def send_message(self, gid, message, uid=None, at=False, timeout=20):
        scope, target = ("group", gid) if gid is not None else ("private", uid)
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        self.call("send_" + scope + "_msg", {
            "group_id" if gid is not None else "user_id": int(target),
            "message": message}, timeout)
        return True

    def send_image(self, gid, path, uid=None, text="", at=False, timeout=20):
        content = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return self.send_message(gid, [
            {"type": "image", "data": {"file": "base64://" + content}},
            {"type": "text", "data": {"text": text}}], uid, timeout=timeout)

    def get_group_member_info(self, gid, uid):
        return self.call("get_group_member_info", {"group_id": int(gid), "user_id": int(uid)})


class JobQueue:
    def __init__(self):
        self.jobs = queue.Queue(maxsize=30)
        threading.Thread(target=self.run, name="github-commands", daemon=True).start()

    def submit(self, job):
        try:
            self.jobs.put_nowait(job)
            return True
        except queue.Full:
            return False

    def run(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                logging.exception("Command failed")
            finally:
                self.jobs.task_done()


def create_app(handler, secret):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/onebot/events")
    def events():
        signature = "sha1=" + hmac.new(secret.encode(), request.get_data(), hashlib.sha1).hexdigest()
        if not hmac.compare_digest(signature, request.headers.get("X-Signature", "")):
            abort(403)
        event = request.get_json()
        if not isinstance(event, dict):
            abort(400)
        if event.get("post_type") != "message" or event.get("user_id") == event.get("self_id"):
            return {}
        if event.get("message_type") not in ("group", "private") or not event.get("user_id"):
            return {}
        message = event.get("message", "")
        if isinstance(message, list):
            message = "".join(str(s.get("data", {}).get("text", ""))
                              for s in message if s.get("type") == "text")
        gid = event.get("group_id") if event["message_type"] == "group" else None
        if event["message_type"] == "group" and gid is None:
            abort(400)
        handle_github(handler, str(message), gid, event["user_id"])
        return {}

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secret = os.environ.get("ONEBOT_SECRET", "")
    if not secret:
        raise SystemExit("Set ONEBOT_SECRET to match the NapCat HTTP report secret")
    qq = OneBot(os.environ.get("ONEBOT_HTTP_URL", "http://127.0.0.1:3000"),
                os.environ.get("ONEBOT_ACCESS_TOKEN", ""))
    monitor = GitHubMonitorService(qq, os.environ.get("STATE_PATH", "output/github-monitor.sqlite3"))
    handler = SimpleNamespace(qq_api=qq, admin_qq=os.environ.get("ADMIN_QQ", ""),
                              github_monitor_service=monitor, github_job_queue=JobQueue())
    threading.Thread(target=monitor.run_loop, name="github-monitor", daemon=True).start()
    serve(create_app(handler, secret), host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8088")), threads=4)


if __name__ == "__main__":
    main()
