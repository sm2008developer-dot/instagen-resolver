"""
InstaGen resolver (self-hosted reference implementation).

Takes a PUBLIC Instagram link and returns the direct media URL in the JSON format that
HttpMediaResolver.java expects. Uses the open-source yt-dlp library.

Notes
- Works only for publicly accessible posts/Reels. Private or login-only content returns error "private".
- It does not use or store any Instagram login or cookies.
- Instagram changes often: keep yt-dlp updated.
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


def fail(error, detail=""):
    return jsonify(ok=False, error=error, detail=(detail or "")[:200])


@app.get("/health")
def health():
    return jsonify(ok=True, ytdlp=yt_dlp.version.__version__)


@app.get("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not host_ok(url):
        return fail("unsupported", "not an instagram url")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignore_no_formats_error": True,   # photo posts have no video formats
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        low = msg.lower()
        if "login" in low or "log in" in low or "private" in low or "restricted" in low:
            return fail("private", msg)
        if "not found" in low or "unavailable" in low or "404" in low or "removed" in low:
            return fail("not_found", msg)
        return fail("unsupported", msg)
    except Exception as e:  # noqa
        return fail("unsupported", str(e))

    if not info:
        return fail("unsupported", "empty result")

    # carousel posts: use the first item
    entries = info.get("entries")
    if entries:
        info = next((e for e in entries if e), None) or {}

    media_url = info.get("url")
    ext = info.get("ext")

    if not media_url:
        fmts = [f for f in (info.get("formats") or []) if f.get("url") and f.get("vcodec") != "none"]
        if fmts:
            media_url = fmts[-1]["url"]
            ext = fmts[-1].get("ext") or ext

    # photo post: no video at all -> take the largest image
    if not media_url and not info.get("duration"):
        thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
        if thumbs:
            best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            media_url = best["url"]
            ext = "jpg"

    if not media_url or not media_url.startswith("https://"):
        return fail("unsupported", "no downloadable media found")

    return jsonify(
        ok=True,
        mediaUrl=media_url,
        mimeType=mime_for(ext),
        thumbnailUrl=info.get("thumbnail") or "",
        title=(info.get("title") or info.get("description") or "")[:200],
        author=info.get("uploader") or info.get("channel") or "",
        durationMs=int((info.get("duration") or 0) * 1000),
        sizeBytes=int(info.get("filesize") or info.get("filesize_approx") or 0),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
