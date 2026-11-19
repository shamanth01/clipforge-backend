import os

def _i(k, d):
    return int(os.environ.get(k, d))

MAX_UPLOAD_MB = _i("MAX_UPLOAD_MB", 500)
MAX_MINUTES = _i("MAX_MINUTES", 60)
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/clipforge")
FILE_TTL_MIN = _i("FILE_TTL_MIN", 120)
MAX_WORKERS = _i("MAX_WORKERS", 2)
UPLOADS_PER_HOUR = _i("UPLOADS_PER_HOUR", 20)
RENDERS_PER_HOUR = _i("RENDERS_PER_HOUR", 60)
ALLOWED_EXT = {"mp4", "mov", "webm"}
# YouTube policy: cc_only (only Creative Commons videos), open (any video, you accept responsibility), off
YT_POLICY = os.environ.get("YT_POLICY", "cc_only").lower()
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES", "")
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
ENABLE_CAPTIONS = os.environ.get("ENABLE_CAPTIONS", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
HOOK_MODEL = os.environ.get("HOOK_MODEL", "claude-haiku-5-5")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
X264_PRESET = os.environ.get("X264_PRESET", "veryfast")
FFMPEG_TIMEOUT = _i("FFMPEG_TIMEOUT", 1800)
