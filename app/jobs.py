import logging, os, re, secrets, shutil, threading, time
from concurrent.futures import ThreadPoolExecutor
from . import config as C

log = logging.getLogger("clipforge")

class UserError(Exception):
    """An error whose message is safe and useful to show to the visitor."""

_lock = threading.Lock()
_items = {}
pool = ThreadPoolExecutor(max_workers=C.MAX_WORKERS, thread_name_prefix="cf")
ID_RE = re.compile(r"^[0-9a-f]{20}$")
PUBLIC = ("id", "kind", "status", "stage", "message", "progress", "error", "duration",
          "title", "has_audio", "hook", "hook_source", "captions", "caption_note", "size", "job_id")

def job_dir(jid):
    return os.path.join(C.DATA_DIR, "jobs", jid)

def new(kind, **kw):
    i = secrets.token_hex(10)
    item = dict(id=i, kind=kind, status="queued", stage="queued", message="Waiting for a free worker",
                progress=0, error=None, created=time.time(), **kw)
    with _lock:
        _items[i] = item
    if kind == "job":
        os.makedirs(job_dir(i), exist_ok=True)
    return i

def get(i):
    with _lock:
        it = _items.get(i)
        return dict(it) if it else None

def upd(i, **kw):
    with _lock:
        if i in _items:
            _items[i].update(kw)

def public(i):
    it = get(i)
    return {k: it[k] for k in PUBLIC if k in it} if it else None

def submit(fn, *a):
    def wrap():
        try:
            fn(*a)
        except Exception:
            log.exception("worker crashed")
    pool.submit(wrap)

def _sweep():
    cutoff = time.time() - C.FILE_TTL_MIN * 60
    root = os.path.join(C.DATA_DIR, "jobs")
    os.makedirs(root, exist_ok=True)
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    with _lock:
        for k in [k for k, v in _items.items() if v["created"] < cutoff]:
            del _items[k]

def start_cleanup():
    def loop():
        while True:
            try:
                _sweep()
            except Exception:
                log.exception("cleanup failed")
            time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()
