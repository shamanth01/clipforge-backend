import glob, logging, os
from urllib.parse import urlparse
from . import config as C
from .jobs import UserError

log = logging.getLogger("clipforge")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

def validate_url(u):
    p = urlparse(u.strip())
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in HOSTS:
        raise UserError("Only YouTube links are supported. You can upload the video file instead.")
    return u.strip()

def download(url, out_dir, cb):
    if C.YT_POLICY == "off":
        raise UserError("YouTube links are turned off on this server. Please upload the video file instead.")
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 2}
    if C.YTDLP_COOKIES and os.path.exists(C.YTDLP_COOKIES):
        base["cookiefile"] = C.YTDLP_COOKIES
    if C.YTDLP_PROXY:
        base["proxy"] = C.YTDLP_PROXY
    try:
        with yt_dlp.YoutubeDL(base) as y:
            info = y.extract_info(url, download=False)
        if info.get("is_live"):
            raise UserError("Live streams are not supported.")
        if (info.get("duration") or 0) > C.MAX_MINUTES * 60:
            raise UserError(f"This video is longer than {C.MAX_MINUTES} minutes.")
        if C.YT_POLICY == "cc_only" and "creative commons" not in (info.get("license") or "").lower():
            raise UserError("This video is not marked Creative Commons, so it can't be downloaded here. "
                            "If you own it or have permission, upload the video file instead.")
        best = [0.0]
        def hook(d):
            if d.get("status") == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if tot:
                    best[0] = max(best[0], min(d.get("downloaded_bytes", 0) / tot, 1))
                    cb(best[0])
        opts = dict(base, outtmpl=os.path.join(out_dir, "source.%(ext)s"), merge_output_format="mp4",
                    format="bv*[height<=720]+ba/b[height<=720]/b", max_filesize=C.MAX_UPLOAD_MB * 1024 * 1024,
                    progress_hooks=[hook])
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    except UserError:
        raise
    except Exception as e:
        msg = str(e).lower()
        log.warning("yt-dlp failed: %s", e)
        if "sign in" in msg or "bot" in msg or "403" in msg:
            raise UserError("YouTube blocked this server's request. Please upload the video file instead.")
        raise UserError("Could not download this video. It may be private, age-restricted or unavailable. "
                        "Please upload the video file instead.")
    files = [f for f in glob.glob(os.path.join(out_dir, "source.*")) if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise UserError("The download finished but no video file was produced. Please upload the file instead.")
    return files[0], (info.get("title") or "video")
