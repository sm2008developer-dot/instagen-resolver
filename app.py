"""
InstaGen resolver (self-hosted reference implementation).

Takes a PUBLIC Instagram link and returns direct media URL(s) in the JSON format that
HttpMediaResolver.java expects. Uses the open-source yt-dlp library.

- Public posts/Reels only. Private or login-only content returns error "private".
- No Instagram login or cookies are used or stored.
- Instagram often delivers picture and sound as separate streams. This server merges them
  with ffmpeg (stream copy, no re-encoding) at /merge and returns ONE normal MP4 URL with sound.
  If ffmpeg is unavailable it falls back to returning mediaUrl + audioUrl (the app can merge on-device).
- Results are cached in memory for 15 minutes so repeated searches are instant.
"""
import hashlib
import os
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode, urlparse

from flask import Flask, jsonify, request, send_file
import yt_dlp

try:                                    # bundled ffmpeg binary (pip package imageio-ffmpeg)
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:                       # noqa
    FFMPEG = None

app = Flask(__name__)

ALLOWED_HOSTS = ("instagram.com", "instagr.am")
CDN_SUFFIXES = (".fbcdn.net", ".cdninstagram.com")      # /merge only fetches from Instagram CDNs
MERGE_DIR = os.path.join(tempfile.gettempdir(), "instagen_merge")
os.makedirs(MERGE_DIR, exist_ok=True)
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


def cdn_ok(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme == "https" and any(h.endswith(s) for s in CDN_SUFFIXES)


def cleanup_merge_dir():
    now = time.time()
    for name in os.listdir(MERGE_DIR):
        p = os.path.join(MERGE_DIR, name)
        try:
            if now - os.path.getmtime(p) > 30 * 60:
                os.remove(p)
        except OSError:
            pass


@app.get("/health")
def health():
    return jsonify(ok=True, ytdlp=yt_dlp.version.__version__, ffmpeg=bool(FFMPEG))


@app.get("/merge")
def merge():
    """Merges a video-only and an audio-only stream into one MP4 (stream copy) and serves it."""
    v = request.args.get("v", "")
    a = request.args.get("a", "")
    if not FFMPEG or not cdn_ok(v) or not cdn_ok(a):
        return fail("unsupported", "merge not available"), 400
    cleanup_merge_dir()
    name = hashlib.sha1((v + "|" + a).encode()).hexdigest() + ".mp4"
    out = os.path.join(MERGE_DIR, name)
    if not os.path.exists(out):
        tmp = out + ".part"
        ua = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
        cmd = [FFMPEG, "-y", "-loglevel", "error",
               "-user_agent", ua, "-i", v,
               "-user_agent", ua, "-i", a,
               "-map", "0:v:0", "-map", "1:a:0",
               "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=100)
        except subprocess.TimeoutExpired:
            return fail("unsupported", "merge timeout"), 504
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            return fail("unsupported", (r.stderr or b"").decode("utf8", "ignore")[-180:]), 502
        os.replace(tmp, out)
    return send_file(out, mimetype="video/mp4", conditional=True)


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

    # picture + sound separate -> hand the app ONE merged MP4 URL (ffmpeg stream copy on this server)
    if audio_url and FFMPEG and cdn_ok(media_url) and cdn_ok(audio_url):
        media_url = "https://%s/merge?%s" % (request.host, urlencode({"v": media_url, "a": audio_url}))
        audio_url = ""
        ext = "mp4"

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
