"""End-to-end test against a running server: python tests/e2e.py http://127.0.0.1:8000"""
import json, os, subprocess, sys, tempfile, time
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
T = tempfile.mkdtemp(prefix="cf_media_")
ok_n = fail_n = 0
ST = os.environ.get("STAGES", "ABCD")
jid = None

def check(name, cond, extra=""):
    global ok_n, fail_n
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    ok_n += bool(cond); fail_n += (not cond)

def ff(*a):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *a], check=True)

def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", p],
                       capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    return v, a, float(j["format"]["duration"])

def make(name, dur, loud, vcodec="libx264", acodec="aac", size="1280x720"):
    p = os.path.join(T, name)
    ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={dur}",
       "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}",
       "-af", f"volume='if(between(t,{loud[0]},{loud[1]}),1.0,0.04)':eval=frame",
       "-c:v", vcodec, *(["-preset","veryfast","-crf","34"] if vcodec=="libx264" else ["-b:v","400k"]), "-pix_fmt", "yuv420p", "-c:a", acodec, "-shortest", p)
    return p

def upload(path):
    with open(path, "rb") as f:
        return requests.post(f"{BASE}/api/upload", params={"filename": os.path.basename(path)}, data=f,
                             headers={"Content-Type": "application/octet-stream"})

def wait(path, what, timeout=300):
    t0 = time.time(); last = None
    while time.time() - t0 < timeout:
        j = requests.get(BASE + path).json()
        if j.get("stage") != last:
            last = j.get("stage"); print(f"   {what}: {j.get('stage')} {j.get('progress')}%")
        if j["status"] in ("ready", "error"):
            return j
        time.sleep(1)
    raise TimeoutError(what)

def render(jid, **kw):
    body = dict(start=60, end=90, format="9:16", fit="fill", focus=50, hook="")
    body.update(kw)
    r = requests.post(f"{BASE}/api/jobs/{jid}/render", json=body)
    if r.status_code != 200:
        return r, None
    return r, wait("/api/renders/" + r.json()["render_id"], "render")


def prep():
    global jid
    if jid: return
    r = upload(make("test.mp4", 150, (60, 90))); jid = r.json()["job_id"]
    assert wait("/api/jobs/" + jid, "job")["status"] == "ready"
if "A" in ST:
    print("== health")
    h = requests.get(BASE + "/healthz").json(); print("  ", h)
    check("healthz ok and ffmpeg found", h["ok"] and h["ffmpeg"])
    check("frontend served at /", "ClipForge" in requests.get(BASE + "/").text)


if "A" in ST:
    print("== upload MP4 (150 s, loud section 60-90 s)")
    mp4 = make("test.mp4", 150, (60, 90))
    r = upload(mp4); check("upload accepted", r.status_code == 200, r.text)
    jid = r.json()["job_id"]
    j = wait("/api/jobs/" + jid, "job")
    check("job ready", j["status"] == "ready", j.get("error"))
    check("duration detected", abs(j["duration"] - 150) < 1.5, j.get("duration"))
    c = requests.get(f"{BASE}/api/jobs/{jid}/clips", params={"length": 30, "count": 5}).json()
    print("   clips:", c["clips"])
    top = c["clips"][0]
    check("at least 3 non-overlapping clips returned", len(c["clips"]) >= 3, len(c["clips"]))
    check("activity array length matches duration", abs(len(c["activity"]) - 150) <= 1)
    check("top clip lies in the loud section", top["s"] >= 50 and top["e"] <= 100, top)
    check("no overlapping clips", all(a["e"] <= b["s"] or b["e"] <= a["s"] for i, a in enumerate(c["clips"]) for b in c["clips"][i + 1:]))
    pv = requests.get(f"{BASE}/api/jobs/{jid}/preview", headers={"Range": "bytes=0-99"})
    check("preview supports range requests", pv.status_code == 206 and pv.headers["content-type"].startswith("video/mp4"), pv.status_code)


if "A" in ST:
    print("== render 9:16 fill with user hook")
    r, s = render(jid, start=top["s"], end=top["e"], hook="Wait until you see this")
    check("render ready", s and s["status"] == "ready", s)
    dl = requests.get(BASE + s["download_url"])
    check("download is an attachment", "attachment" in dl.headers.get("content-disposition", "") and dl.status_code == 200)
    out = os.path.join(T, "out.mp4"); open(out, "wb").write(dl.content)
    v, a, d = probe(out)
    check("output is 1080x1920", (v["width"], v["height"]) == (1080, 1920), (v["width"], v["height"]))
    check("output is H.264 + AAC MP4", v["codec_name"] == "h264" and a and a["codec_name"] == "aac")
    check("output is about 30 s", abs(d - 30) < 0.7, d)
    check("hook recorded", s["hook"] == "Wait until you see this", s.get("hook"))
    check("caption note present when captions unavailable", s["captions"] or bool(s["caption_note"]))
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "2", "-i", out, "-frames:v", "1", "/tmp/frame_fill.png"], check=True)


if "B" in ST:
    prep()
    print("== render 9:16 blurred fit, 1:1 and 16:9 (8 s each)")
    for fmt, fit, size in [("9:16", "blur", (1080, 1920)), ("1:1", "fill", (1080, 1080)), ("16:9", "fill", (1920, 1080))]:
        r, s = render(jid, start=70, end=78, format=fmt, fit=fit, focus=20)
        ok = s and s["status"] == "ready"
        if ok:
            p = os.path.join(T, f"{fmt.replace(':','x')}_{fit}.mp4"); open(p, "wb").write(requests.get(BASE + s["file_url"]).content)
            v, a, d = probe(p); ok = (v["width"], v["height"]) == size and abs(d - 8) < 0.7
        check(f"{fmt} {fit} renders at {size[0]}x{size[1]}", ok, s)
        if fmt == "9:16" and fit == "blur":
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "2", "-i", p, "-frames:v", "1", "/tmp/frame_blur.png"], check=True)


if "C" in ST:
    print("== MOV and WebM uploads")
    mov = make("test.mov", 40, (10, 25), size="1920x1080")
    j2 = wait("/api/jobs/" + upload(mov).json()["job_id"], "mov job")
    check("MOV processed", j2["status"] == "ready", j2.get("error"))
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libvpx " in enc:
        wb = make("test.webm", 30, (8, 20), vcodec="libvpx", acodec="libvorbis", size="640x360")
        j3 = wait("/api/jobs/" + upload(wb).json()["job_id"], "webm job")
        check("WebM processed", j3["status"] == "ready", j3.get("error"))
        if j3["status"] == "ready":
            cl = requests.get(f"{BASE}/api/jobs/{j3['id']}/clips", params={"length": 10, "count": 2}).json()["clips"]
            r, s = render(j3["id"], start=cl[0]["s"], end=cl[0]["e"])
            check("WebM clip rendered to MP4", s and s["status"] == "ready", s)
    else:
        print("SKIP WebM (no libvpx encoder in this ffmpeg)")


if "D" in ST:
    prep()
    print("== error handling")
    r = requests.post(BASE + "/api/upload", params={"filename": "x.avi"}, data=b"abc")
    check("rejects .avi with a clear message", r.status_code == 400 and "MP4, MOV and WebM" in r.json()["error"], r.text)
    bad = os.path.join(T, "bad.mp4"); open(bad, "wb").write(os.urandom(200000))
    jb = wait("/api/jobs/" + upload(bad).json()["job_id"], "bad file")
    check("corrupt file gives a friendly error", jb["status"] == "error" and "not a valid video" in jb["error"], jb)
    long_ = os.path.join(T, "long.mp4")
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:r=1", "-t", "3700", "-c:v", "libx264", "-g", "100", "-pix_fmt", "yuv420p", long_)
    jl = wait("/api/jobs/" + upload(long_).json()["job_id"], "long file")
    check("video over 60 minutes is rejected", jl["status"] == "error" and "60 minutes" in jl["error"], jl)
    big = os.path.join(T, "big.mp4"); open(big, "wb").write(os.urandom(int(os.environ.get("BIG_MB", "35")) * 1024 * 1024))
    rb = upload(big)
    check("oversize upload is rejected (413)", rb.status_code == 413, rb.status_code)
    r = requests.post(BASE + "/api/url", json={"url": "https://example.com/video.mp4"})
    check("non-YouTube link rejected", r.status_code == 400 and "YouTube" in r.json()["error"], r.text)
    r = requests.post(BASE + "/api/url", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    check("YouTube link accepted into the queue", r.status_code == 200, r.text)
    jy = wait("/api/jobs/" + r.json()["job_id"], "youtube", 120)
    check("YouTube failure gives a friendly message", jy["status"] == "error" and "upload the video file" in jy["error"].lower(), jy)
    print("   youtube message:", jy.get("error"))
    check("unknown job id is a 404 with a message", requests.get(BASE + "/api/jobs/" + "0" * 20).status_code == 404)
    check("path-traversal id is rejected", requests.get(BASE + "/api/jobs/..%2f..%2fetc").status_code == 404)
    r = requests.post(f"{BASE}/api/jobs/{jid}/render", json=dict(start=10, end=500, format="9:16", fit="fill", focus=50, hook=""))
    check("invalid clip range rejected", r.status_code == 400, r.text)
    r = requests.post(f"{BASE}/api/jobs/{jid}/render", json=dict(start=10, end=20, format="4:3", fit="fill", focus=50, hook=""))
    check("invalid format rejected", r.status_code == 400, r.text)


print(f"\n{ok_n} passed, {fail_n} failed")
sys.exit(1 if fail_n else 0)
