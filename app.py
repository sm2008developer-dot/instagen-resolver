"""
InstaGen resolver (self-hosted reference implementation).

Takes a PUBLIC Instagram link and returns the direct media URL in the JSON format that
HttpMediaResolver.java expects. Uses the open-source yt-dlp library.

Notes
- Works only for publicly accessible posts/Reels. Private or login-only content returns error "private".
- It does not use or store any Instagram login or cookies.
- Instagram changes often: keep yt-dlp updated (pip install -U yt-dlp).
- You are responsible for complying with Instagram's terms and the law where you run this.
"""
import os
from urllib.parse import urlparse

from flask import Flask, jsonify, request
import yt_dlp

app = Flask(__name__)

ALLOWED_HOSTS = ("instagram.com", "instagr.am")


def host_ok(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS)


def mime_for(ext):
    ext = (ext or "").lower()
    return {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    }.get(ext, "")


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not host_ok(url):
        return jsonify(ok=False, error="unsupported")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "login" in msg or "private" in msg or "log in" in msg or "restricted" in msg:
            return jsonify(ok=False, error="private")
        if "not found" in msg or "unavailable" in msg or "404" in msg or "removed" in msg:
            return jsonify(ok=False, error="not_found")
        return jsonify(ok=False, error="unsupported")
    except Exception:
        return jsonify(ok=False, error="unsupported")

    # carousel posts: use the first item
    entries = info.get("entries")
    if entries:
        info = next((e for e in entries if e), None) or {}

    media_url = info.get("url")
    if not media_url:
        fmts = [f for f in (info.get("formats") or []) if f.get("url")]
        if fmts:
            media_url = fmts[-1]["url"]
            info.setdefault("ext", fmts[-1].get("ext"))
    if not media_url or not media_url.startswith("https://"):
        return jsonify(ok=False, error="unsupported")

    return jsonify(
        ok=True,
        mediaUrl=media_url,
        mimeType=mime_for(info.get("ext")),
        thumbnailUrl=info.get("thumbnail") or "",
        title=(info.get("title") or info.get("description") or "")[:200],
        author=info.get("uploader") or info.get("channel") or "",
        durationMs=int((info.get("duration") or 0) * 1000),
        sizeBytes=int(info.get("filesize") or info.get("filesize_approx") or 0),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
