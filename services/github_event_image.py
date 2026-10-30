"""Local GitHub-style previews; optional cached avatars, no remote card service.

Reference: https://github.blog/open-source/git/framework-building-open-graph-images/
"""
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

KINDS = {
    "push": ("代码提交", "#0969da"),
    "release": ("版本发布", "#8250df"),
    "issue": ("议题动态", "#1a7f37"),
    "pr": ("合并请求", "#8250df"),
}
INK, MUTED, FAINT = "#24292f", "#57606a", "#8c959f"
_AVATAR_RETRY = {}


def plain_text(value, limit=1600):
    text = str(value or "")[:12000]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?m)^\s*[#>*]+\s*", "", text)
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text.strip()[:limit]


def font_file(bold=False):
    weight = "Bold" if bold else "Regular"
    for name in [
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",
        f"/usr/share/fonts/truetype/noto/NotoSansCJK-{weight}.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
    ]:
        if Path(name).is_file():
            return name
    raise RuntimeError("GitHub 卡片缺少中文字体")


def event_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "时间未知"
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") + " UTC+8"
    except (ValueError, TypeError, OverflowError):
        return "时间未知"


def _ellipsize(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text.rstrip() + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, font, width, limit):
    """Keep English words intact, wrap CJK, and bound long identifiers."""
    rows, line = [], ""
    for token in re.findall(r"\n|[^\S\n]+|[A-Za-z0-9_./:@+-]+|.", text):
        if token == "\n":
            if line:
                rows.append(line.rstrip())
                line = ""
            continue
        if line and draw.textlength(line + token, font=font) > width:
            rows.append(line.rstrip())
            line = ""
        for char in token.lstrip() if not line else token:
            if line and draw.textlength(line + char, font=font) > width:
                rows.append(line.rstrip())
                line = ""
            line += char
    if line.strip():
        rows.append(line.rstrip())
    if len(rows) > limit:
        rows = rows[:limit]
        rows[-1] = _ellipsize(draw, rows[-1] + "…", font, width)
    return rows


def _placeholder(actor):
    digest = hashlib.sha256(str(actor).encode()).digest()
    image = Image.new("RGB", (160, 160), "#f0f3f6")
    draw = ImageDraw.Draw(image)
    color = tuple(60 + c % 120 for c in digest[:3])
    for row in range(5):
        for col in range(3):
            if digest[3 + row * 3 + col] & 1:
                for mirror in {col, 4 - col}:
                    x, y = 20 + mirror * 24, 20 + row * 24
                    draw.rectangle((x, y, x + 23, y + 23), fill=color)
    return image


def _avatar(event, directory):
    """Accept only numeric GitHub IDs; bound downloads and tolerate offline use."""
    avatar_id = str(event.get("avatar_id", ""))
    fallback = lambda: _placeholder(event.get("actor", "unknown"))
    if not re.fullmatch(r"[0-9]{1,20}", avatar_id):
        return fallback()
    path = directory / (avatar_id + ".png")
    cached = None
    try:
        with Image.open(path) as image:
            if image.width <= 1024 and image.height <= 1024:
                cached = image.convert("RGB")
        if cached is not None and time.time() - path.stat().st_mtime < 86400:
            return cached
    except (OSError, ValueError):
        pass
    if time.time() < _AVATAR_RETRY.get(avatar_id, 0):
        return cached if cached is not None else fallback()
    try:
        with requests.get(f"https://avatars.githubusercontent.com/u/{avatar_id}?s=160",
                          timeout=(3, 5), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("Avatar unavailable")
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ValueError("Avatar too large")
        with Image.open(io.BytesIO(data)) as source:
            if source.width > 1024 or source.height > 1024:
                raise ValueError("Avatar dimensions too large")
            image = ImageOps.fit(source.convert("RGB"), (160, 160), method=Image.Resampling.LANCZOS)
        directory.mkdir(parents=True, exist_ok=True)
        _save(image, path)
        if cached is not None:
            cached.close()
        return image
    except (requests.RequestException, OSError, ValueError):
        if len(_AVATAR_RETRY) >= 256:
            _AVATAR_RETRY.clear()
        _AVATAR_RETRY[avatar_id] = time.time() + 300
        return cached if cached is not None else fallback()


def _paste_avatar(image, avatar, xy, size):
    thumb = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (size * 3, size * 3))
    ImageDraw.Draw(mask).ellipse((0, 0, size * 3 - 1, size * 3 - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    image.paste(thumb, xy, mask)


def _icon(draw, kind, x, y, color=MUTED):
    if kind == "comments":
        draw.rounded_rectangle((x, y + 2, x + 24, y + 20), 3, outline=color, width=2)
        draw.line((x + 5, y + 20, x + 5, y + 27, x + 12, y + 20), fill=color, width=2)
    elif kind == "push":
        draw.line((x, y + 14, x + 28, y + 14), fill=color, width=3)
        draw.ellipse((x + 7, y + 7, x + 21, y + 21), fill="white", outline=color, width=3)
    elif kind == "issue":
        draw.ellipse((x + 2, y + 2, x + 26, y + 26), outline=color, width=2)
        draw.ellipse((x + 12, y + 12, x + 16, y + 16), fill=color)
    elif kind == "release":
        draw.polygon([(x + 2, y + 2), (x + 14, y + 2), (x + 28, y + 16), (x + 16, y + 28), (x + 2, y + 14)], outline=color, width=2)
        draw.ellipse((x + 7, y + 7, x + 10, y + 10), fill=color)
    else:
        for cx, cy in ((x + 5, y + 4), (x + 5, y + 25), (x + 24, y + 25)):
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), outline=color, width=2)
        draw.line((x + 5, y + 7, x + 5, y + 22), fill=color, width=2)
        draw.line((x + 24, y + 22, x + 24, y + 11, x + 16, y + 5), fill=color, width=2)
        draw.line((x + 16, y + 12, x + 16, y + 5, x + 23, y + 5), fill=color, width=2)


def _save(image, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".github-", suffix=".png", dir=output.parent)
    try:
        os.close(fd)
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_event_card(event, output_path, *, font_path=None):
    label, accent = KINDS[event["kind"]]
    regular = font_path or font_file()
    bold = font_path or font_file(bold=True)
    font = lambda size, strong=False: ImageFont.truetype(bold if strong else regular, size)
    image = Image.new("RGB", (1200, 600), "white")
    draw = ImageDraw.Draw(image)
    output = Path(output_path)
    avatar = _avatar(event, output.parent.parent / "github-avatars")
    _paste_avatar(image, avatar, (944, 80), 160)
    _paste_avatar(image, avatar, (80, 484), 44)
    avatar.close()

    repo = _ellipsize(draw, str(event.get("repo", "")), font(28), 800)
    draw.text((80, 80), repo, font=font(28), fill=MUTED, anchor="lt")
    title = plain_text(event.get("title")) or label
    number = event.get("number")
    if number:
        title = re.sub(r"^#?" + re.escape(str(number)) + r"\s+", "", title)
        title = f"#{number} {title}"
    title_font = font(62, True)
    rows = _wrap(draw, title, title_font, 800, 3)
    for index, line in enumerate(rows):
        y = 139 + index * 76
        if index == 0 and number and line.startswith(f"#{number} "):
            prefix, rest = f"#{number} ", line[len(str(number)) + 2:]
            draw.text((80, y), prefix, font=title_font, fill=FAINT, anchor="lt")
            draw.text((80 + draw.textlength(prefix, font=title_font), y), rest, font=title_font, fill=INK, anchor="lt")
        else:
            draw.text((80, y), line, font=title_font, fill=INK, anchor="lt")

    y = 139 + len(rows) * 76 + 17
    action = {"opened": "已创建", "closed": "已关闭", "reopened": "重新打开", "edited": "已编辑",
              "published": "已发布", "released": "已发布", "prereleased": "预发布", "push": "代码提交"}.get(event.get("action"), label)
    if event.get("merged"):
        action = "已合并"
    elif event.get("prerelease"):
        action = "预发布"
    _icon(draw, event["kind"], 80, y, accent)
    detail = action
    if event["kind"] == "push" and event.get("branch"):
        detail = str(event["branch"])
    elif event["kind"] == "release" and event.get("tag"):
        detail += " · " + str(event["tag"])
    detail = _ellipsize(draw, detail, font(24), 330)
    draw.text((120, y - 1), detail, font=font(24), fill=MUTED, anchor="lt")
    x = 120 + draw.textlength(detail, font=font(24)) + 34
    comments = event.get("comments")
    if isinstance(comments, int) and 0 <= comments <= 99999999:
        _icon(draw, "comments", x, y)
        text = f"{comments} 条评论"
        draw.text((x + 36, y - 1), text, font=font(24), fill=MUTED, anchor="lt")
        x += 36 + draw.textlength(text, font=font(24)) + 30
    additions, deletions = event.get("additions"), event.get("deletions")
    if all(isinstance(n, int) and 0 <= n <= 99999999 for n in (additions, deletions)):
        for text, color in ((f"+{additions}", "#1a7f37"), (f"−{deletions}", "#cf222e")):
            if x + draw.textlength(text, font=font(24, True)) > 1104:
                break
            draw.text((x, y - 1), text, font=font(24, True), fill=color, anchor="lt")
            x += draw.textlength(text, font=font(24, True)) + 12

    if len(rows) < 3:
        body = re.sub(r"\s+", " ", plain_text(event.get("body"), 600))
        if body and body != "暂无详细说明" and body != title:
            for index, line in enumerate(_wrap(draw, body, font(25), 1024, 3 if len(rows) == 1 else 1)):
                draw.text((80, y + 55 + index * 36), line, font=font(25), fill=MUTED, anchor="lt")

    actor = _ellipsize(draw, str(event.get("actor", "unknown")), font(24, True), 300)
    draw.text((144, 493), actor, font=font(24, True), fill=INK, anchor="lt")
    x = 144 + draw.textlength(actor, font=font(24, True)) + 16
    date = "· " + event_time(event.get("created_at"))
    draw.text((x, 494), date, font=font(22), fill=MUTED, anchor="lt")
    x += draw.textlength(date, font=font(22)) + 24
    sha = str(event.get("sha", ""))
    if re.fullmatch(r"[a-fA-F0-9]{40}", sha) and x < 880:
        _icon(draw, "push", x, 493, FAINT)
        draw.text((x + 40, 494), sha[:7], font=font(22), fill=MUTED, anchor="lt")

    mark = Path(__file__).resolve().parents[1] / "static/github/github-mark.png"
    if mark.is_file():
        with Image.open(mark) as source:
            mask = ImageOps.invert(source.convert("L")).resize((44, 44), Image.Resampling.LANCZOS)
            image.paste("#8c959f", (1060, 484, 1104, 528), mask)
    else:
        draw.text((1008, 496), "GitHub", font=font(24, True), fill=FAINT, anchor="lt")
    draw.rectangle((0, 576, 1199, 599), fill=accent)
    _save(image, output)
    image.close()
    return str(output)
