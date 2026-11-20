import logging, os, re, shutil, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import ai, config as C, jobs, media, pipeline, ytdl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipforge")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

@asynccontextmanager
async def lifespan(_):
    os.makedirs(os.path.join(C.DATA_DIR, "jobs"), exist_ok=True)
    jobs.start_cleanup()
    yield

app = FastAPI(title="ClipForge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
if C.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=C.CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])

@app.exception_handler(StarletteHTTPException)
async def http_err(_, e):
    return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

@app.exception_handler(Exception)
async def any_err(_, e):
    log.exception("unhandled")
    return JSONResponse({"error": "Unexpected server error. Please try again."}, status_code=500)

class Limiter:
    def __init__(self, n, window=3600):
        self.n, self.w, self.h = n, window, defaultdict(deque)
    def ok(self, key):
        now, q = time.time(), self.h[key]
        while q and q[0] < now - self.w:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True

up_lim, url_lim, rend_lim = Limiter(C.UPLOADS_PER_HOUR), Limiter(C.UPLOADS_PER_HOUR), Limiter(C.RENDERS_PER_HOUR)

def ip(request):
    return request.client.host if request.client else "unknown"

def need(i, kind):
    it = jobs.get(i) if jobs.ID_RE.match(i or "") else None
    if not it or it["kind"] != kind:
        raise HTTPException(404, "This item was not found. It may have expired. Please start again.")
    return it

@app.get("/healthz")
def healthz():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "captions": ai.captions_available(),
            "ai_hooks": ai.ai_hooks_available(), "youtube": C.YT_POLICY,
            "limits": {"max_upload_mb": C.MAX_UPLOAD_MB, "max_minutes": C.MAX_MINUTES}}

@app.post("/api/upload")
async def upload(request: Request, filename: str = "video"):
    if not up_lim.ok(ip(request)):
        raise HTTPException(429, "Too many uploads from your network. Please try again in a while.")
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext not in C.ALLOWED_EXT:
        raise HTTPException(400, "Only MP4, MOV and WebM files are supported.")
    limit = C.MAX_UPLOAD_MB * 1024 * 1024
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, f"This file is larger than {C.MAX_UPLOAD_MB} MB.")
    jid = jobs.new("job", title=os.path.splitext(os.path.basename(filename))[0][:80] or "video")
    path = os.path.join(jobs.job_dir(jid), f"source.{ext}")
    size = 0
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"This file is larger than {C.MAX_UPLOAD_MB} MB.")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(jobs.job_dir(jid), ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(jobs.job_dir(jid), ignore_errors=True)
        raise HTTPException(400, "The upload was interrupted. Please try again.")
    if size == 0:
        shutil.rmtree(jobs.job_dir(jid), ignore_errors=True)
        raise HTTPException(400, "The uploaded file is empty.")
    jobs.submit(pipeline.process_job, jid, "file", path)
    return {"job_id": jid}

class UrlReq(BaseModel):
    url: str

@app.post("/api/url")
def from_url(req: UrlReq, request: Request):
    if not url_lim.ok(ip(request)):
        raise HTTPException(429, "Too many requests from your network. Please try again in a while.")
    try:
        u = ytdl.validate_url(req.url)
    except jobs.UserError as e:
        raise HTTPException(400, str(e))
    jid = jobs.new("job")
    jobs.submit(pipeline.process_job, jid, "url", u)
    return {"job_id": jid}

@app.post("/api/youtube-details")
def youtube_details(req: UrlReq):
    try:
        u = ytdl.validate_url(req.url)
        return ytdl.get_youtube_details(u)
    except jobs.UserError as e:
        raise HTTPException(400, str(e))

@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    need(jid, "job")
    return jobs.public(jid)

def ready_job(jid):
    j = need(jid, "job")
    if j["status"] == "error":
        raise HTTPException(409, j.get("error") or "Processing failed.")
    if j["status"] != "ready":
        raise HTTPException(409, "This video is still being processed.")
    return j

@app.get("/api/jobs/{jid}/clips")
def clips(jid: str, length: int = 30, count: int = 5):
    j = ready_job(jid)
    try:
        picks = media.pick_clips(j["activity"], max(10, min(120, length)), max(1, min(10, count)))
    except jobs.UserError as e:
        raise HTTPException(400, str(e))
    return {"duration": j["duration"], "activity": j["activity"], "clips": picks}

@app.get("/api/jobs/{jid}/preview")
def preview(jid: str):
    j = ready_job(jid)
    return FileResponse(j["preview"], media_type=j["preview_mime"])

class RenderReq(BaseModel):
    start: float
    end: float
    format: str = "9:16"
    fit: str = "fill"
    focus: float = 50
    hook: str = ""

@app.post("/api/jobs/{jid}/render")
def render(jid: str, req: RenderReq, request: Request):
    j = ready_job(jid)
    if not rend_lim.ok(ip(request)):
        raise HTTPException(429, "Too many exports from your network. Please try again in a while.")
    if req.format not in media.SIZES or req.fit not in ("fill", "blur"):
        raise HTTPException(400, "Unsupported format or framing option.")
    if not (0 <= req.start < req.end <= j["duration"] + 0.5) or not (3 <= req.end - req.start <= 120):
        raise HTTPException(400, "The clip range is not valid for this video.")
    hook = re.sub(r"[\x00-\x1f]", " ", req.hook).strip()[:100]
    rid = jobs.new("render", job_id=jid)
    jobs.submit(pipeline.render_job, rid, jid, dict(start=req.start, end=min(req.end, j["duration"]), format=req.format,
                fit=req.fit, focus=max(0, min(100, req.focus)), hook=hook))
    return {"render_id": rid}

@app.get("/api/renders/{rid}")
def render_status(rid: str):
    need(rid, "render")
    s = jobs.public(rid)
    if s["status"] == "ready":
        s["file_url"] = f"/api/renders/{rid}/file"
        s["download_url"] = f"/api/renders/{rid}/download"
    return s

def _file(rid):
    r = need(rid, "render")
    if r["status"] != "ready" or not os.path.exists(r.get("file", "")):
        raise HTTPException(409, "This clip is not ready yet.")
    return r

@app.get("/api/renders/{rid}/file")
def render_file(rid: str):
    return FileResponse(_file(rid)["file"], media_type="video/mp4")

@app.get("/api/renders/{rid}/download")
def render_download(rid: str):
    r = _file(rid)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", r.get("title") or "clip").strip("-")[:40] or "clip"
    return FileResponse(r["file"], media_type="video/mp4", filename=f"clipforge-{slug}-{rid[:6]}.mp4")

if os.path.isdir(STATIC):
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
