import importlib.util, logging, os, re, subprocess, textwrap, threading
from . import config as C
from .media import SIZES

log = logging.getLogger("clipforge")
_model, _mlock = None, threading.Lock()

def captions_available():
    return C.ENABLE_CAPTIONS and importlib.util.find_spec("faster_whisper") is not None

def ai_hooks_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def transcribe(src, start, end, workdir):
    """Extract audio and generate word-level timestamps using Faster-Whisper."""

    if not captions_available():
        return None, "Captions are not available on this server."

    global _model

    try:
        # Validate input
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Source video not found: {src}")

        duration = float(end) - float(start)

        if duration <= 0:
            raise ValueError("Invalid clip duration")

        os.makedirs(workdir, exist_ok=True)

        # Unique WAV filename to avoid concurrent request conflicts
        wav = os.path.join(
            workdir,
            f"seg_{threading.get_ident()}.wav"
        )

        # Extract audio using FFmpeg
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            "-i", src,
            "-ss", f"{start:.3f}",
            "-t", f"{duration:.3f}",
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            "-f", "wav",
            wav
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg audio extraction failed: {result.stderr}"
            )

        # Validate extracted audio
        if not os.path.isfile(wav):
            raise RuntimeError("FFmpeg did not create WAV file")

        if os.path.getsize(wav) <= 44:
            raise RuntimeError("Extracted WAV file is empty")

        log.info(
            "Audio extracted successfully: %s bytes",
            os.path.getsize(wav)
        )

        # Load Whisper model
        from faster_whisper import WhisperModel

        with _mlock:
            if _model is None:
                _model = WhisperModel(
                    C.WHISPER_MODEL,
                    device="cpu",
                    compute_type="int8"
                )

        # Transcribe audio
        segs, info = _model.transcribe(
            wav,
            word_timestamps=True,
            vad_filter=True,
            beam_size=1
        )

        words = []

        for segment in segs:
            for word in segment.words or []:
                if word.word.strip():
                    words.append({
                        "w": word.word.strip(),
                        "s": float(word.start),
                        "e": float(word.end)
                    })

        log.info(
            "Transcription completed. Language: %s, Words: %d",
            info.language,
            len(words)
        )

        return words, (
            "" if words
            else "No speech was detected in this clip."
        )

    except Exception as e:
        log.exception("Transcription failed: %s", str(e))

        return None, "Captions could not be generated for this clip."


def make_hook(text):
    """Returns (hook, source) where source is ai, fallback or none."""
    text = " ".join(text.split())
    if len(text) < 20:
        return "", "none"
    if ai_hooks_available():
        try:
            import anthropic
            c = anthropic.Anthropic(timeout=25)
            prompt = ("You write hook headlines for short vertical videos. Based on this transcript of one clip, write ONE "
                      "hook headline of at most 8 words that makes people keep watching. Use the same language as the "
                      "transcript. No quotes, hashtags or emojis. Reply with the headline only.\n\nTranscript:\n" + text[:1500])
            m = c.messages.create(model=C.HOOK_MODEL, max_tokens=60, messages=[{"role": "user", "content": prompt}])
            h = m.content[0].text.strip().strip("\"'\u201c\u201d").replace("\n", " ")
            if h:
                return h[:80], "ai"
        except Exception:
            log.exception("AI hook failed")
    first = re.split(r"(?<=[.!?])\s+", text)[0].split()
    h = " ".join(first[:8]).rstrip(",;:")
    return (h + ("\u2026" if len(first) > 8 else "")), "fallback"

def _t(s):
    cs = int(round(max(s, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"

def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")

def build_ass(words, hook, fmt, dur):
    """ASS subtitle file with a hook headline (first 4 s) and karaoke-style word captions."""
    if not words and not hook:
        return None
    W, H = SIZES[fmt]
    base = min(W, H)
    fc, fh, pad, mx = round(base * .075), round(base * .062), round(base * .018), round(W * .07)
    mv_c = round(H * (.2 if H > W else .08))
    mv_h = round(H * (.09 if H > W else .07))
    out = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
           "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
           "MarginR, MarginV, Encoding",
           f"Style: Cap,Noto Sans,{fc},&H0047E1FD,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{round(fc*.09)},2,2,{mx},{mx},{mv_c},1",
           f"Style: Hook,Noto Sans,{fh},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,{pad},0,8,{mx},{mx},{mv_h},1",
           "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if hook:
        lines = textwrap.wrap(hook, width=max(12, int(W * .8 / (fh * .58)))) or [hook]
        out.append(f"Dialogue: 1,{_t(0)},{_t(min(4, dur))},Hook,,0,0,0,,{{\\fad(200,250)}}" + "\\N".join(_esc(l) for l in lines))
    chunks, cur = [], []
    for w in words or []:
        if cur and (len(cur) >= 4 or w["s"] - cur[-1]["e"] > .8 or sum(len(x["w"]) for x in cur) > 24):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        st = ch[0]["s"]
        nxt = chunks[i + 1][0]["s"] if i + 1 < len(chunks) else dur
        en = min(max(ch[-1]["e"] + .15, st + .3), nxt, dur)
        prev, txt = st, ""
        for w in ch:
            k = max(int(round((w["e"] - prev) * 100)), 1)
            prev = max(prev, w["e"])
            txt += f"{{\\k{k}}}{_esc(w['w'].upper())} "
        out.append(f"Dialogue: 0,{_t(st)},{_t(en)},Cap,,0,0,0,,{txt.strip()}")
    return "\n".join(out) + "\n"
