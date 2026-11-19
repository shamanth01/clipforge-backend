import logging, os
from . import ai, config as C, jobs, media, ytdl
from .jobs import UserError

log = logging.getLogger("clipforge")

def _seg(i, a, b, stage, msg):
    def cb(p):
        jobs.upd(i, stage=stage, message=msg, progress=round(a + (b - a) * max(0.0, min(1.0, p)), 1))
    return cb

def process_job(jid, kind, arg):
    d = jobs.job_dir(jid)
    try:
        jobs.upd(jid, status="running")
        if kind == "url":
            src, title = ytdl.download(arg, d, _seg(jid, 0, 30, "downloading", "Downloading video"))
            lo = 30
        else:
            src, title, lo = arg, jobs.get(jid).get("title"), 0
        span = 100 - lo
        jobs.upd(jid, stage="checking", message="Checking video", progress=lo)
        info = media.probe(src)
        if info["duration"] > C.MAX_MINUTES * 60:
            raise UserError(f"This video is longer than {C.MAX_MINUTES} minutes. Please choose a shorter one.")
        if info["duration"] < media.MIN_SEC:
            raise UserError("This video is too short to clip (minimum 5 seconds).")
        jobs.upd(jid, duration=round(info["duration"], 2), has_audio=info["has_audio"], title=title, src=src)
        prev, mime = media.make_preview(src, d, info, _seg(jid, lo + .03 * span, lo + .4 * span, "preparing", "Preparing preview"))
        jobs.upd(jid, preview=prev, preview_mime=mime)
        audio, motion = media.signals(src, info["duration"], _seg(jid, lo + .4 * span, 100, "analyzing", "Analyzing audio and video"))
        act = media.activity(audio, motion, info["has_audio"])
        jobs.upd(jid, activity=[round(float(x), 3) for x in act], status="ready", stage="ready", message="Ready", progress=100)
    except UserError as e:
        jobs.upd(jid, status="error", error=str(e))
    except Exception:
        log.exception("job %s failed", jid)
        jobs.upd(jid, status="error", error="Processing failed. Please try again, or use a different video file.")

def render_job(rid, jid, r):
    j = jobs.get(jid)
    wd = os.path.join(jobs.job_dir(jid), "r_" + rid)
    try:
        os.makedirs(wd, exist_ok=True)
        jobs.upd(rid, status="running", stage="captions", message="Writing captions", progress=3)
        dur = r["end"] - r["start"]
        words, note = ai.transcribe(j["src"], r["start"], r["end"], wd)
        hook, hsrc = (r["hook"], "user") if r["hook"] else ("", "none")
        if not hook and words:
            jobs.upd(rid, message="Writing hook headline", progress=20)
            hook, hsrc = ai.make_hook(" ".join(w["w"] for w in words))
        ass = ai.build_ass(words, hook, r["format"], dur)
        if ass:
            with open(os.path.join(wd, "cap.ass"), "w", encoding="utf-8") as f:
                f.write(ass)
        out = os.path.join(wd, "clip.mp4")
        media.render_clip(j["src"], r["start"], dur, r["format"], r["fit"], r["focus"], "cap.ass" if ass else None,
                          out, j.get("has_audio", True), _seg(rid, 25, 100, "rendering", "Rendering video"), wd)
        jobs.upd(rid, status="ready", stage="ready", message="Ready", progress=100, file=out, size=os.path.getsize(out),
                 hook=hook, hook_source=hsrc, captions=bool(words), caption_note=note, title=j.get("title"))
    except UserError as e:
        jobs.upd(rid, status="error", error=str(e))
    except Exception:
        log.exception("render %s failed", rid)
        jobs.upd(rid, status="error", error="Rendering failed. Please try again.")
