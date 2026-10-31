"""
Game reviews - one Markdown file per game in reviews/<appid>.md

The editor in the web app writes these files, but they're plain text, so you
can also write or tweak them by hand in VS Code. Format:

    ---
    title: Braid Review
    subtitle: A time-bending puzzler that still holds up
    score: 9
    platform: PC
    reviewed: 2026-10-31
    hours: 7.3
    completion: 100
    pros: Inventive puzzles | Gorgeous painted art | Haunting soundtrack
    cons: Story is deliberately opaque | Late difficulty spike
    verdict: One short paragraph summing it up.
    ---

    The review itself, in Markdown. ## Headings, **bold**, *italics*,
    > quotes and ![screenshots](/review-images/whatever.jpg) all work.

Reviews are meant to be committed to Git, so they sync between devices.
Screenshots you upload go in reviews/images/.
"""

import re
import time
from pathlib import Path

import markdown

REVIEWS_DIR = Path(__file__).parent / "reviews"
IMAGES_DIR = REVIEWS_DIR / "images"
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_MB = 15

# IGN's verdict labels, by whole score.
VERDICTS = {10: "Masterpiece", 9: "Amazing", 8: "Great", 7: "Good", 6: "Okay",
            5: "Mediocre", 4: "Bad", 3: "Awful", 2: "Painful", 1: "Unbearable"}

FIELDS = ["title", "subtitle", "score", "platform", "reviewed",
          "hours", "completion", "pros", "cons", "verdict"]
LIST_FIELDS = {"pros", "cons"}


def path_for(appid):
    return REVIEWS_DIR / f"{int(appid)}.md"


def verdict_label(score):
    return VERDICTS.get(int(score)) if score else None


def format_score(score):
    """9.0 -> '9', 8.5 -> '8.5'"""
    return f"{score:g}" if score is not None else ""


def parse(text):
    """Split a review file into its header fields and Markdown body."""
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
        body = m.group(2)

    for key in LIST_FIELDS:
        meta[key] = [x.strip() for x in meta.get(key, "").split("|") if x.strip()]
    for key in ("score", "hours", "completion"):
        try:
            meta[key] = float(meta[key])
        except (KeyError, ValueError):
            meta[key] = None
    meta["body"] = body.strip()
    return meta


def render(body):
    """Markdown -> HTML (tables, footnotes etc. included via 'extra')."""
    return markdown.markdown(body, extensions=["extra", "sane_lists"])


def load(appid):
    """Return the review for a game (with rendered HTML), or None."""
    path = path_for(appid)
    if not path.exists():
        return None
    review = parse(path.read_text(encoding="utf-8"))
    review["html"] = render(review["body"])
    review["label"] = verdict_label(review["score"])
    review["score_text"] = format_score(review["score"])
    review["updated_at"] = path.stat().st_mtime
    return review


def save(appid, data):
    """Write a review file from the editor's fields."""
    lines = ["---"]
    for key in FIELDS:
        value = data.get(key)
        if key in LIST_FIELDS:
            value = " | ".join(str(x).replace("|", "/").strip()
                               for x in (value or []) if str(x).strip())
        elif isinstance(value, float):
            value = format_score(value)
        # The header is one line per field, so flatten any line breaks.
        lines.append(f"{key}: {' '.join(str(value or '').split())}")
    lines += ["---", "", (data.get("body") or "").strip(), ""]

    REVIEWS_DIR.mkdir(exist_ok=True)
    path_for(appid).write_text("\n".join(lines), encoding="utf-8")


def delete(appid):
    path_for(appid).unlink(missing_ok=True)


def all_scores():
    """{appid: score} for every reviewed game, for badges on the home page."""
    if not REVIEWS_DIR.exists():
        return {}
    scores = {}
    for path in REVIEWS_DIR.glob("*.md"):
        if path.stem.isdigit():
            score = parse(path.read_text(encoding="utf-8"))["score"]
            scores[path.stem] = format_score(score) if score is not None else "✓"
    return scores


def save_image(appid, filename, data):
    """Store an uploaded screenshot; returns the URL to use in Markdown."""
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_TYPES:
        raise ValueError(f"Images must be one of: {', '.join(sorted(IMAGE_TYPES))}")
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise ValueError(f"Images must be under {MAX_IMAGE_MB} MB")
    stem = re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")[:40] or "image"
    name = f"{int(appid)}-{int(time.time())}-{stem}{ext}"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / name).write_bytes(data)
    return f"/review-images/{name}"
