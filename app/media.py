import json, logging, os, subprocess, tempfile, threading
import numpy as np
from . import config as C
from .jobs import UserError

log = logging.getLogger("clipforge")
SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
MIN_SEC = 5

def run_ffmpeg(args, total, cb=None, cwd=None):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"] + args
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd)
        timer = threading.Timer(C.FFMPEG_TIMEOUT, p.kill)
        timer.start()
        try:
            for line in p.stdout:
                if cb and line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        cb(int(line.split("=")[1]) / 1e6 / max(total, 0.1))
                    except ValueError:
                        pass
            rc = p.wait()
        finally:
            timer.cancel()
            if p.poll() is None:
                p.kill()
        err.seek(0)
        tail = err.read().decode(errors="replace")[-600:]
    if rc != 0:
        log.error("ffmpeg failed (%s): %s", rc, tail)
        raise RuntimeError("ffmpeg failed")

def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UserError("This file is not a valid video. Please upload an MP4, MOV or WebM file.")
    j = json.loads(r.stdout or "{}")
    streams = j.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise UserError("No video track was found in this file.")
    try:
        dur = float(j.get("format", {}).get("duration") or v.get("duration"))
    except (TypeError, ValueError):
        raise UserError("Could not read the length of this video.")
    return dict(duration=dur, has_audio=a is not None, vcodec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"),
                acodec=a.get("codec_name") if a else None, width=v.get("width"), height=v.get("height"))

def make_preview(src, d, info, cb):
    """Browser-playable preview. WebM is served as is, compatible H.264 is remuxed, anything else is transcoded."""
    if src.lower().endswith(".webm"):
        return src, "video/webm"
    dst = os.path.join(d, "preview.mp4")
    if info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p" and info["acodec"] in (None, "aac", "mp3"):
        run_ffmpeg(["-i", src, "-c", "copy", "-movflags", "+faststart", dst], info["duration"], cb)
    else:
        run_ffmpeg(["-i", src, "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", C.X264_PRESET,
                    "-crf", "27", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", dst], info["duration"], cb)
    return dst, "video/mp4"

def _audio_levels(src, n):
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True)
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    m = x.size // 8000
    out = np.full(n, -100.0)
    if m > 0:
        rms = np.sqrt((x[:m * 8000].reshape(m, 8000) ** 2).mean(1))
        out[:min(n, m)] = (20 * np.log10(rms + 1e-5))[:n]
    return out

def _motion_levels(src, n, cb, fps=2, W=96, H=54):
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-an", "-vf",
                          f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fs, prev, diffs = W * H, None, []
    while True:
        b = p.stdout.read(fs)
        if len(b) < fs:
            break
        f = np.frombuffer(b, np.uint8).astype(np.float32)
        diffs.append(0.0 if prev is None else float(np.abs(f - prev).mean()))
        prev = f
        if cb and len(diffs) % 60 == 0:
            cb(len(diffs) / max(n * fps, 1))
    p.wait()
    d = np.array(diffs) if diffs else np.zeros(1)
    m = len(d) // fps
    sec = d[:m * fps].reshape(m, fps).mean(1) if m else np.zeros(1)
    out = np.zeros(n)
    out[:min(n, len(sec))] = sec[:n]
    return out

def signals(src, duration, cb):
    n = int(np.ceil(duration))
    box = {}
    t = threading.Thread(target=lambda: box.__setitem__("a", _audio_levels(src, n)))
    t.start()
    mot = _motion_levels(src, n, cb)
    t.join()
    return box["a"], mot

def _norm(x):
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    return np.clip((x - lo) / (hi - lo), 0, 1) if hi > lo else np.zeros_like(x)

def _smooth(x, k=3):
    return np.convolve(x, np.ones(k) / k, mode="same")

def activity(audio_db, motion, has_audio):
    m = _smooth(_norm(motion))
    if not has_audio:
        return m
    return 0.65 * _smooth(_norm(audio_db)) + 0.35 * m

def pick_clips(act, length, count):
    n = len(act)
    if n < MIN_SEC:
        raise UserError("This video is too short to clip (minimum 5 seconds).")
    length = min(length, n)
    x = np.asarray(act, dtype=np.float64)
    c, c2 = np.concatenate([[0], np.cumsum(x)]), np.concatenate([[0], np.cumsum(x * x)])
    s = np.arange(0, n - length + 1)
    mean = (c[s + length] - c[s]) / length
    var = np.maximum((c2[s + length] - c2[s]) / length - mean * mean, 0)
    v = mean + 0.6 * np.sqrt(var)
    picks = []
    for i in np.argsort(-v):
        if len(picks) >= count:
            break
        if all(abs(int(i) - p) >= length for p, _ in picks):
            picks.append((int(i), float(v[i])))
    hi, lo = float(v.max()), float(v.min())
    out = [{"s": p, "e": p + length, "score": int(round(55 + 44 * (t - lo) / max(hi - lo, 1e-9)))} for p, t in picks]
    return sorted(out, key=lambda c: -c["score"])

def render_clip(src, start, dur, fmt, fit, focus, ass, out, has_audio, cb, cwd):
    W, H = SIZES[fmt]
    R = W / H
    if fit == "blur":
        fc = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=30:5,eq=brightness=-0.12[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v0]")
    else:
        fc = (f"[0:v]crop=w='min(iw,ih*{R:.5f})':h='min(ih,iw/{R:.5f})':x='(iw-ow)*{focus / 100:.3f}':y='(ih-oh)/2',"
              f"scale={W}:{H}:flags=lanczos,setsar=1,fps=30[v0]")
    fc += f";[v0]ass={ass}[v]" if ass else ";[v0]null[v]"
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src, "-filter_complex", fc, "-map", "[v]"]
    if has_audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    args += ["-c:v", "libx264", "-preset", C.X264_PRESET, "-crf", "21", "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{dur:.3f}", out]
    run_ffmpeg(args, dur, cb, cwd)
