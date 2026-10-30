"""Public GitHub repository events, durable subscriptions and delivery retries."""
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from services.github_event_image import render_event_card

log = logging.getLogger(__name__)
HELP = "订阅 https://github.com/owner/repo（群内发送即订阅到当前群）\nGitHub订阅 owner/repo\nGitHub退订 owner/repo\nGitHub订阅列表\n默认推送提交、Release、Issue、PR 图片。每次启动只推送启动后的动态，新订阅不推送历史；约每5分钟检查，GitHub事件源可能延迟。群订阅由群主、群管理员或机器人管理员管理。"


def repository(value):
    value = re.sub(r"^https://github\.com/", "", str(value).strip(), flags=re.I).rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value) or value.split("/")[1] in (".", ".."):
        raise ValueError("仓库格式：owner/repo 或 https://github.com/owner/repo")
    return value.lower()


def normalize(repo, raw):
    p = raw.get("payload") or {}
    kind = {"PushEvent": "push", "ReleaseEvent": "release", "IssuesEvent": "issue", "PullRequestEvent": "pr"}.get(raw.get("type"))
    if not kind:
        return None
    actor = raw.get("actor") or {}
    event = dict(id=str(raw["id"]), repo=repo, kind=kind, actor=actor.get("login", "unknown"), avatar_id=actor.get("id"), created_at=raw.get("created_at"), action=p.get("action", "更新"))
    if kind == "push":
        commits = p.get("commits") or []
        head = p.get("head") or (commits[-1].get("sha") if commits else "")
        branch = str(p.get("ref", "")).removeprefix("refs/heads/")
        event.update(title=f"{branch or '仓库'} · 新提交", body="\n".join(str(c.get("message", "")) for c in commits) or "分支有新的代码提交，点击链接查看详情。", action="push", url=f"https://github.com/{repo}/commit/{head}" if re.fullmatch(r"[a-fA-F0-9]{40}", head) else f"https://github.com/{repo}/commits")
        event.update(sha=head, branch=branch)
        if commits and commits[-1].get("message"):
            message = str(commits[-1]["message"])
            event.update(title=message.splitlines()[0], body="\n".join(message.splitlines()[1:]).strip())
    else:
        obj = p.get({"release": "release", "issue": "issue", "pr": "pull_request"}[kind]) or {}
        number = obj.get("number") or p.get("number")
        title = obj.get("title") or obj.get("name") or obj.get("tag_name") or "仓库更新"
        event.update(title=(f"#{number} " if number else "") + str(title), body=str(obj.get("body") or "暂无详细说明")[:1600], url=obj.get("html_url") or f"https://github.com/{repo}")
        event.update(number=number, comments=obj.get("comments"), additions=obj.get("additions"),
                     deletions=obj.get("deletions"), merged=bool(obj.get("merged")),
                     tag=obj.get("tag_name"), prerelease=bool(obj.get("prerelease")))
    if not str(event["url"]).startswith("https://github.com/"):
        event["url"] = f"https://github.com/{repo}"
    return event


class GitHubMonitorService:
    def __init__(self, qq_api, state_path=None, fetch=None, render=None, interval=300):
        self.started_at = time.time()
        self.qq = qq_api
        self.path = Path(state_path or Path(__file__).resolve().parents[1] / "output/github-monitor.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.fetch = fetch or self._fetch
        self.render = render or render_event_card
        self.interval = max(300, interval)
        self.next_poll = 0
        self.blocked_until = 0
        self.commit_cache = {}
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions(target TEXT, repo TEXT, PRIMARY KEY(target,repo));
            CREATE TABLE IF NOT EXISTS seen(repo TEXT, id TEXT, PRIMARY KEY(repo,id));
            CREATE TABLE IF NOT EXISTS pending(target TEXT, repo TEXT, id TEXT, event TEXT, attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, PRIMARY KEY(target,repo,id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}
            if "since" not in columns:
                db.execute("ALTER TABLE subscriptions ADD COLUMN since REAL NOT NULL DEFAULT 0")
            discarded = db.execute("DELETE FROM pending").rowcount
        log.warning("GitHub startup baseline=%s; discarded_pending=%s",
                    datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(), discarded)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=20)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _fetch(self, repo):
        if time.time() < self.blocked_until:
            raise RuntimeError("GitHub API 限流中，稍后重试")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "NapCat-GitHub-Monitor", "X-GitHub-Api-Version": "2022-11-28"}
        token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("YUMI_GITHUB_TOKEN"))
        if token:
            headers["Authorization"] = "Bearer " + token
        response = requests.get(f"https://api.github.com/repos/{repo}/events", params={"per_page": 100}, headers=headers, timeout=(5, 20))
        if response.status_code == 304:
            return []
        if response.status_code in (403, 429):
            self.blocked_until = max(time.time() + 300, float(response.headers.get("X-RateLimit-Reset", 0)))
            raise RuntimeError("GitHub API 限流，已延后重试")
        if response.status_code == 404:
            raise ValueError("仓库不存在或不是可访问的公开仓库")
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list):
            raise RuntimeError("GitHub 返回了无效事件列表")
        return events

    def _ingest(self, db, repo, events):
        targets = db.execute("SELECT target,since FROM subscriptions WHERE repo=?", (repo,)).fetchall()
        for raw in reversed(events):
            inserted = db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (repo, str(raw["id"]))).rowcount
            if not inserted:
                continue
            # API results may contain delayed historical events. IDs alone are not a time baseline.
            try:
                created = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    continue
                created_at = created.timestamp()
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                continue
            if created_at <= self.started_at:
                continue
            event = normalize(repo, raw)
            if event:
                for target, since in targets:
                    if created_at <= since:
                        continue
                    db.execute("INSERT OR IGNORE INTO pending(target,repo,id,event) VALUES (?,?,?,?)", (target, repo, event["id"], json.dumps(event, ensure_ascii=False)))

    def _prepare_card(self, event):
        """Events may omit commit messages; enrich a head commit without blocking delivery on errors."""
        sha = str(event.get("sha", ""))
        if event.get("kind") != "push" or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
            return event
        try:
            repo = repository(event["repo"])
        except (ValueError, KeyError):
            return event
        key = (repo, sha)
        cached = self.commit_cache.get(key)
        if cached and cached[0] > time.time():
            return dict(event, **cached[1])
        if time.time() < self.blocked_until:
            return event
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "NapCat-GitHub-Monitor"}
        token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("YUMI_GITHUB_TOKEN"))
        if token:
            headers["Authorization"] = "Bearer " + token
        fields = {}
        try:
            response = requests.get(f"https://api.github.com/repos/{repo}/commits/{sha}",
                                    headers=headers, timeout=(3, 8), allow_redirects=False)
            if response.status_code in (403, 429):
                self.blocked_until = max(time.time() + 300, float(response.headers.get("X-RateLimit-Reset", 0)))
            response.raise_for_status()
            data = response.json()
            message = str((data.get("commit") or {}).get("message") or "").strip()
            if message:
                fields.update(title=message.splitlines()[0], body="\n".join(message.splitlines()[1:]).strip()[:1600])
            stats = data.get("stats") or {}
            for name in ("additions", "deletions"):
                if isinstance(stats.get(name), int):
                    fields[name] = stats[name]
        except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
            log.warning("GitHub commit details unavailable for %s (%s); using event data", repo, type(error).__name__)
        if len(self.commit_cache) >= 128:
            self.commit_cache.clear()
        self.commit_cache[key] = (time.time() + (86400 if fields else 300), fields)
        return dict(event, **fields)

    def subscribe(self, target, value):
        repo = repository(value)
        with self.lock, self.db() as db:
            if db.execute("SELECT 1 FROM subscriptions WHERE target=? AND repo=?", (target, repo)).fetchone():
                return "已经订阅：" + repo
            repos = {r[0] for r in db.execute("SELECT DISTINCT repo FROM subscriptions")}
            if repo not in repos and len(repos) >= 4:
                raise ValueError("当前最多监控4个不同仓库，以控制 GitHub API 请求额度")
            since = time.time()
            events = self.fetch(repo)
            self._ingest(db, repo, events)  # existing subscribers retain newly observed events
            db.execute("INSERT INTO subscriptions(target,repo,since) VALUES (?,?,?)", (target, repo, since))
        return "已订阅：" + repo + "\n首次仅建立基线，不推送历史；后续动态以图片发送。"

    def unsubscribe(self, target, value):
        repo = repository(value)
        with self.lock, self.db() as db:
            count = db.execute("DELETE FROM subscriptions WHERE target=? AND repo=?", (target, repo)).rowcount
            db.execute("DELETE FROM pending WHERE target=? AND repo=?", (target, repo))
            if not db.execute("SELECT 1 FROM subscriptions WHERE repo=?", (repo,)).fetchone():
                db.execute("DELETE FROM seen WHERE repo=?", (repo,))
        return ("已退订：" if count else "尚未订阅：") + repo

    def list_text(self, target):
        with self.db() as db:
            rows = db.execute("SELECT repo FROM subscriptions WHERE target=? ORDER BY repo", (target,)).fetchall()
        return "GitHub订阅列表：\n" + ("\n".join(r[0] for r in rows) or "暂无订阅")

    def tick(self):
        with self.lock:
            if time.time() >= self.next_poll:
                self.next_poll = time.time() + self.interval
                with self.db() as db:
                    repos = [r[0] for r in db.execute("SELECT DISTINCT repo FROM subscriptions")]
                for repo in repos:
                    try:
                        events = self.fetch(repo)
                        with self.db() as db:
                            self._ingest(db, repo, events)
                    except Exception as error:
                        log.warning("GitHub poll %s failed (%s)", repo, type(error).__name__)
            with self.db() as db:
                rows = db.execute("SELECT target,repo,id,event,attempts FROM pending WHERE due<=? ORDER BY rowid LIMIT 8", (time.time(),)).fetchall()
            for target, repo, eid, data, attempts in rows:
                path = self.path.parent / "github-cards" / (str(abs(hash((target, repo, eid)))) + ".png")
                try:
                    event = self._prepare_card(json.loads(data))
                    self.render(event, path)
                    scope, number = target.split(":", 1)
                    if self.qq.send_image(number if scope == "g" else None, str(path), number if scope == "u" else None, event["url"], at=False, timeout=20) is not True:
                        raise RuntimeError("QQ delivery failed")
                    with self.db() as db:
                        db.execute("DELETE FROM pending WHERE target=? AND repo=? AND id=?", (target, repo, eid))
                except Exception as error:
                    log.warning("GitHub delivery failed (%s), retry %s", type(error).__name__, attempts + 1)
                    with self.db() as db:
                        db.execute("UPDATE pending SET attempts=?,due=? WHERE target=? AND repo=? AND id=?", (attempts + 1, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), target, repo, eid))
                finally:
                    path.unlink(missing_ok=True)

    def run_loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("GitHub monitor tick failed")
            time.sleep(15)
