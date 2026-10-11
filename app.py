"""
InstaGen resolver (self-hosted reference implementation).

Takes a PUBLIC Instagram link and returns direct media URL(s) in the JSON format that
HttpMediaResolver.java expects. Uses the open-source yt-dlp library.

- Public posts/Reels only. Private or login-only content returns error "private".
- No Instagram login or cookies are used or stored.
- If a video's picture and sound are separate streams, both URLs are returned
  (mediaUrl + audioUrl) and the Android app merges them into one MP4 with sound.
- Results are cached in memory for 15 minutes so repeated searches are instant.
"""
import os
import threading
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request
import yt_dlp

app = Flask(__name__)

ALLOWED_HOSTS = ("instagram.com", "instagr.am")
CACHE_TTL = 15 * 60
_cache = {}
_lock = threading.Lock()


def host_ok(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS)


def mime_for(ext):
    return {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    }.get((ext or "").lower(), "")


def has(f, key):
    return f.get(key) not in (None, "none")


def fail(error, detail=""):
    return jsonify(ok=False, error=error, detail=(detail or "")[:200])


def cache_get(key):
    with _lock:
        item = _cache.get(key)
        if item and time.time() - item[0] < CACHE_TTL:
            return item[1]
        _cache.pop(key, None)
    return None


def cache_put(key, value):
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (time.time(), value)


def pick_streams(info):
    """Returns (video_url, audio_url, ext). audio_url is '' when the video already has sound."""
    rf = info.get("requested_formats")
    if rf:
        vid = next((f for f in rf if has(f, "vcodec")), None)
        aud = next((f for f in rf if has(f, "acodec") and not has(f, "vcodec")), None)
        if vid and aud and vid.get("url") and aud.get("url"):
            return vid["url"], aud["url"], "mp4"
        if vid and vid.get("url"):
            return vid["url"], "", vid.get("ext")
    if info.get("url"):
        return info["url"], "", info.get("ext")
    fmts = [f for f in (info.get("formats") or []) if f.get("url") and has(f, "vcodec")]
    if fmts:
        return fmts[-1]["url"], "", fmts[-1].get("ext")
    return "", "", None


@app.get("/health")
def health():
    return jsonify(ok=True, ytdlp=yt_dlp.version.__version__)


@app.get("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not host_ok(url):
        return fail("unsupported", "not an instagram url")

    cached = cache_get(url)
    if cached:
        return jsonify(cached)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignore_no_formats_error": True,   # photo posts have no video formats
        "socket_timeout": 20,
        # 1) one file with picture+sound  2) H.264 video + AAC audio (merged by the app)  3) anything
        "format": "b[ext=mp4][acodec!=none][vcodec!=none]/bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b",
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

    entries = info.get("entries")          # carousel posts: first item
    if entries:
        info = next((e for e in entries if e), None) or {}

    media_url, audio_url, ext = pick_streams(info)

    if not media_url and not info.get("duration"):   # photo post: largest image
        thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
        if thumbs:
            best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            media_url, audio_url, ext = best["url"], "", "jpg"

    if not media_url or not media_url.startswith("https://"):
        return fail("unsupported", "no downloadable media found")
    if audio_url and not audio_url.startswith("https://"):
        audio_url = ""

    result = dict(
        ok=True,
        mediaUrl=media_url,
        audioUrl=audio_url,
        mimeType=mime_for(ext),
        thumbnailUrl=info.get("thumbnail") or "",
        title=(info.get("title") or info.get("description") or "")[:200],
        author=info.get("uploader") or info.get("channel") or "",
        durationMs=int((info.get("duration") or 0) * 1000),
        sizeBytes=int(info.get("filesize") or info.get("filesize_approx") or 0),
    )
    cache_put(url, result)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
