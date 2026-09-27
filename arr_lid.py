#!/usr/bin/env python3
# arr-media-guard, a Sonarr and Radarr import hook that sets default tracks and catches broken files.
# Copyright (C) 2026 samwiseg0
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Spoken-language identification for arr-media-guard. It hears the language of one audio stream.

arr_decide.py trusts track language tags, and tags lie. A Spanish or Japanese film may carry an eng tag, and some
tracks carry no tag. The hook asks this module only when a tag is missing or conflicts with the app's metadata.

identify() cuts 30-second samples from the stream with ffmpeg, mono at 16 kHz, first at 25, 50 and 75 percent of the
duration. That skips intros and credits. Silero VAD keeps the speech, and faster-whisper names its language. A
sample without speech costs only the cut, so identify() moves on through SAMPLE_AT until three samples hold speech.
The answer counts only when every clear sample agrees and the mean probability clears MIN_PROB.
The per-sample results are cached in sqlite by path, size, mtime, stream index and model. A cache hit re-runs
combine() with the current thresholds, so a threshold change needs no new analysis. --fresh hears the file again and
replaces the cached row. The hook's second check before a re-grab uses it.

This half needs only the stdlib. numpy and faster-whisper load only on a cache miss, from the venv. The hook
imports this module inside try/except ImportError, for carry() only. It runs identify() as a subprocess of the
venv Python in its own process group, with at most 120 seconds, so a slow NAS never costs the flag edit:

    /opt/arr-media-guard-lid/venv/bin/python arr_lid.py PATH INDEX DURATION --cache FILE --model-dir DIR [--fresh] --expect TAG ORIGINAL...

INDEX is the ffmpeg audio index (0:a:INDEX), the same index the hook's sample() uses. The CLI prints one JSON line.
At install, "arr_lid.py --fetch" downloads the pinned model once and checks its sha256. docs/design.md, section
"Audio language detection", has the rules and the install.
"""
import argparse, fcntl, hashlib, json, os, sqlite3, subprocess, sys, time
from contextlib import closing

ENGINE = "faster-whisper"
MODEL = "small"                          # small names a wrong language less often than tiny and base (docs/design.md)
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"   # Systran/faster-whisper-small on Hugging Face
MODEL_SHA256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"   # its model.bin
MODEL_FILES = ("config.json", "tokenizer.json", "vocabulary.txt")   # the rest of the model. A killed download can leave one out.
MODEL_DIR = "/opt/arr-media-guard-lid/models"     # one directory per model, downloaded at deploy time, never at run time
CACHE = "/var/lib/arr-media-guard/lid.sqlite"
THREADS = 4                              # a few cores, so Sonarr and Radarr keep running
# Where samples start, as a share of the duration, in the order they are taken. 0.20 and 0.80 stay clear of an
# anime opening and ending.
SAMPLE_AT = (0.25, 0.50, 0.75, 0.40, 0.60, 0.33, 0.67, 0.20, 0.80)
SAMPLE_SECS = 30                         # Whisper reads 30 seconds, so a longer window adds no evidence
SPEECH_SAMPLES = 3                       # stop once this many samples hold speech
# The thresholds. Clean read speech got wrong answers between close relatives, so KIN withholds those.
MIN_SPEECH = 8.0                         # seconds of speech a sample needs before Whisper hears it
SAMPLE_MIN_PROB = 0.60                   # a sample under this names no language, it neither votes nor vetoes
MIN_VOTES = 2
MIN_PROB = 0.80                          # the mean probability of the agreed language over the votes

# Whisper's 99 languages plus yue (large-v3 only), as the ISO 639-2/B codes the classifier uses. no and nn are both
# Norwegian, zh and yue Chinese.
WHISPER = {"af": "afr", "am": "amh", "ar": "ara", "as": "asm", "az": "aze", "ba": "bak", "be": "bel", "bg": "bul", "bn": "ben",
           "bo": "tib", "br": "bre", "bs": "bos", "ca": "cat", "cs": "cze", "cy": "wel", "da": "dan", "de": "ger", "el": "gre",
           "en": "eng", "es": "spa", "et": "est", "eu": "baq", "fa": "per", "fi": "fin", "fo": "fao", "fr": "fre", "gl": "glg",
           "gu": "guj", "ha": "hau", "haw": "haw", "he": "heb", "hi": "hin", "hr": "hrv", "ht": "hat", "hu": "hun", "hy": "arm",
           "id": "ind", "is": "ice", "it": "ita", "ja": "jpn", "jw": "jav", "ka": "geo", "kk": "kaz", "km": "khm", "kn": "kan",
           "ko": "kor", "la": "lat", "lb": "ltz", "ln": "lin", "lo": "lao", "lt": "lit", "lv": "lav", "mg": "mlg", "mi": "mao",
           "mk": "mac", "ml": "mal", "mn": "mon", "mr": "mar", "ms": "may", "mt": "mlt", "my": "bur", "ne": "nep", "nl": "dut",
           "nn": "nor", "no": "nor", "oc": "oci", "pa": "pan", "pl": "pol", "ps": "pus", "pt": "por", "ro": "rum", "ru": "rus",
           "sa": "san", "sd": "snd", "si": "sin", "sk": "slo", "sl": "slv", "sn": "sna", "so": "som", "sq": "alb", "sr": "srp",
           "su": "sun", "sv": "swe", "sw": "swa", "ta": "tam", "te": "tel", "tg": "tgk", "th": "tha", "tk": "tuk", "tl": "tgl",
           "tr": "tur", "tt": "tat", "uk": "ukr", "ur": "urd", "uz": "uzb", "vi": "vie", "yi": "yid", "yo": "yor", "zh": "chi",
           "yue": "chi"}
# The 639-2/T codes and other spellings a tag or codes() may carry, mapped to the /B code above.
ALIAS = {"sqi": "alb", "hye": "arm", "eus": "baq", "mya": "bur", "zho": "chi", "cmn": "chi", "yue": "chi", "ces": "cze", "nld": "dut",
         "fra": "fre", "kat": "geo", "deu": "ger", "ell": "gre", "isl": "ice", "mkd": "mac", "mri": "mao", "msa": "may", "fas": "per",
         "ron": "rum", "slk": "slo", "bod": "tib", "cym": "wel", "nob": "nor", "nno": "nor", "pob": "por", "fil": "tgl", "jap": "jpn"}
# Languages Whisper names but cannot tell from a close neighbour. Belarusian speech may read as Russian, so an answer
# is withheld whenever Belarusian is in play.
WEAK = {"bel"}
# Close relatives Whisper confuses on clean speech (FLEURS). Galician may read as Spanish and Hindi as Urdu, both with
# a high probability.
# An answer that is a relative of an expected language, and not that language, is withheld.
KIN = [{"srp", "hrv", "bos"}, {"hin", "urd"}, {"glg", "spa"}, {"glg", "por"}, {"cat", "spa"}, {"bul", "mac"}, {"rus", "ukr"},
       {"cze", "slo"}, {"nor", "dan", "swe"}, {"ind", "may"}, {"dut", "afr"}]
UNTAGGED = {"und", "mul", "zxx", ""}


def code(lang):
    """A tag, a codes() member or a Whisper code -> the 639-2/B code, lower case. Unknown codes pass through."""
    c = (lang or "").lower()
    return WHISPER.get(c) or ALIAS.get(c, c)


def identifiable(lang):
    """Whisper can name this language and tell it from its neighbours. Irish (gle) and Scottish Gaelic (gla) fail."""
    return code(lang) in set(WHISPER.values()) - WEAK


def combine(samples, expect=()):
    """Per-sample results -> (639-2/B code or None, mean probability, reason when None).

    expect holds the track's tag and the app's original language. It only withholds an answer and never picks one.
    When Whisper cannot identify one of them, or the answer is a close relative of one (KIN), the result is None.
    Otherwise the votes must number MIN_VOTES, agree on one language, and average MIN_PROB. A vote is a sample
    that names a language at SAMPLE_MIN_PROB or more.
    """
    blind = sorted({code(c) for c in expect if code(c) not in UNTAGGED and not identifiable(c)})
    if blind: return None, 0.0, f"whisper cannot identify {', '.join(blind)}"
    votes = [s for s in samples if s.get("lang") and s["prob"] >= SAMPLE_MIN_PROB]
    if len(votes) < MIN_VOTES: return None, 0.0, f"{len(votes)} of {len(samples)} samples name a language clearly"
    langs = sorted({s["lang"] for s in votes})
    prob = round(sum(s["prob"] for s in votes) / len(votes), 3)
    if len(langs) > 1: return None, prob, "the samples disagree: " + ", ".join(s["lang"] for s in votes)
    if langs[0] in WEAK: return None, prob, f"whisper cannot tell {langs[0]} from its neighbours"
    want = {code(c) for c in expect}
    kin = want & set().union(*(k for k in KIN if langs[0] in k)) - {langs[0]}
    if kin and langs[0] not in want: return None, prob, f"whisper cannot tell {langs[0]} from {', '.join(sorted(kin))}"
    if prob < MIN_PROB: return None, prob, f"{langs[0]} at {prob:.2f} is under the threshold"
    return langs[0], prob, None


def _db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    # dur is the duration the caller passed, rounded. The cut positions follow from it, so it is part of the key.
    db.execute("CREATE TABLE IF NOT EXISTS lid (path TEXT, size INTEGER, mtime_ns INTEGER, idx INTEGER, model TEXT, dur INTEGER,"
               " samples TEXT, at REAL, PRIMARY KEY (path, size, mtime_ns, idx, model, dur))")
    return db


def tag(model):
    """The model name in the cache key and the result. The pinned model carries its revision, so a bump misses."""
    return f"{model}@{MODEL_REVISION[:7]}" if model == MODEL else model


def cache_get(cache, path, index, model, duration_s):
    """The cached samples for this file state, stream and duration, or None."""
    st = os.stat(path)
    with closing(_db(cache)) as db, db:
        row = db.execute("SELECT samples FROM lid WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND model=? AND dur=?",
                         (path, st.st_size, st.st_mtime_ns, index, model, round(duration_s))).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(cache, path, index, model, duration_s, samples, st):
    with closing(_db(cache)) as db, db:
        db.execute("INSERT OR REPLACE INTO lid VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (path, st.st_size, st.st_mtime_ns, index, model, round(duration_s), json.dumps(samples), time.time()))


def carry(path, before, cache=CACHE):
    """Move the cached results of path from its os.stat() before an in-place edit to its stat now.
    mkvpropedit changes the mtime but never the audio, so the hook calls this in-process after each edit. It never
    raises and never creates the cache, because a cache problem must not fail an edit that already happened."""
    if not os.path.exists(cache): return
    try:
        st = os.stat(path)
        with closing(_db(cache)) as db, db:
            db.execute("UPDATE OR REPLACE lid SET size=?, mtime_ns=? WHERE path=? AND size=? AND mtime_ns=?",
                       (st.st_size, st.st_mtime_ns, path, before.st_size, before.st_mtime_ns))
    except (sqlite3.Error, OSError):
        pass


def extract(path, index, start, secs=SAMPLE_SECS):
    """secs of one audio stream from start, as mono 16 kHz signed 16-bit PCM. Read-only."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.1f}", "-i", path, "-t", f"{secs:.1f}",
                        "-map", f"0:a:{index}", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True, timeout=120)
    if r.returncode: raise RuntimeError(f"ffmpeg exited {r.returncode}: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout


def fetch(model_dir=MODEL_DIR):
    """Deploy time only. Download the pinned model into model_dir/MODEL unless it is whole, then check model.bin.
    Returns "present" or "downloaded". Raises on a wrong sha256 or a missing file, so a bad model never reaches the hook."""
    dest = os.path.join(model_dir, MODEL)
    whole = lambda: all(os.path.exists(os.path.join(dest, n)) for n in MODEL_FILES)
    def sha():
        h = hashlib.sha256()
        with open(os.path.join(dest, "model.bin"), "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""): h.update(block)
        return h.hexdigest()
    if os.path.exists(os.path.join(dest, "model.bin")) and whole() and sha() == MODEL_SHA256: return "present"
    from faster_whisper import download_model
    download_model(MODEL, output_dir=dest, revision=MODEL_REVISION)
    if sha() != MODEL_SHA256: raise RuntimeError(f"{dest}/model.bin has sha256 {sha()}, the pin is {MODEL_SHA256}")
    if not whole(): raise RuntimeError(f"{dest} lacks one of {', '.join(MODEL_FILES)} after the download")
    return "downloaded"


def load(model=MODEL, model_dir=MODEL_DIR, threads=THREADS):
    """The Whisper model from its local directory. int8 on CPU. Never downloads."""
    from faster_whisper import WhisperModel
    return WhisperModel(os.path.join(model_dir, model), device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True)


def detect(whisper, pcm):
    """One sample -> {"speech": seconds, "lang": 639-2/B or None, "prob", "top": the three best Whisper codes}.
    Only the speech Silero VAD finds reaches Whisper. Whisper codes of one language (no and nn) add up."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    chunks = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500)) if len(audio) else []
    speech = round(sum(c["end"] - c["start"] for c in chunks) / 16000, 1)
    if speech < MIN_SPEECH: return {"speech": speech, "lang": None, "prob": 0.0, "top": []}
    _, _, probs = whisper.detect_language(np.concatenate([audio[c["start"]:c["end"]] for c in chunks]))
    by = {}
    for w, p in probs: by[code(w)] = by.get(code(w), 0.0) + p
    lang = max(by, key=by.get)
    return {"speech": speech, "lang": lang, "prob": round(by[lang], 3), "top": [[w, round(p, 3)] for w, p in probs[:3]]}


def identify(path, audio_index, duration_s, expect=(), model=MODEL, model_dir=MODEL_DIR, cache=CACHE, threads=THREADS, fresh=False):
    """The spoken language of ffmpeg audio stream audio_index of path. fresh skips the cached samples and hears again.

    Returns {"lang": 639-2/B code or None, "prob", "why": the reason when lang is None, "samples": per sample
    {"at", "speech", "lang", "prob", "top"}, "engine", "model", "cached", "took": wall seconds}.
    expect is the tag and the app's original language, see combine(). Raises when ffmpeg or the model fails,
    and nothing is cached then.
    """
    t0 = time.time()
    key = tag(model)
    out = {"engine": ENGINE, "model": key}
    if any(code(c) not in UNTAGGED and not identifiable(c) for c in expect):   # no model run can help
        lang, prob, why = combine([], expect)
        return dict(out, lang=lang, prob=prob, why=why, samples=[], cached=False, took=round(time.time() - t0, 2))
    if not duration_s or duration_s <= 0:   # a probe with no duration: no cut, no cache row
        return dict(out, lang=None, prob=0.0, why="no duration", samples=[], cached=False, took=round(time.time() - t0, 2))
    st = os.stat(path)
    samples = None if fresh else cache_get(cache, path, audio_index, key, duration_s)
    cached = samples is not None
    if not cached:
        # One model per host at a time. It takes hundreds of MB, and the host may have no swap. The subhunt and the hook worker both call.
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        with open(cache + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            samples = None if fresh else cache_get(cache, path, audio_index, key, duration_s)   # the other caller may have just heard it
            cached = samples is not None
            if not cached:
                samples = hear(path, audio_index, duration_s, load(model, model_dir, threads))
                cache_put(cache, path, audio_index, key, duration_s, samples, st)
    lang, prob, why = combine(samples, expect)
    return dict(out, lang=lang, prob=prob, why=why, samples=samples, cached=cached, took=round(time.time() - t0, 2))


def hear(path, audio_index, duration_s, whisper):
    """Cut and hear samples in SAMPLE_AT order until SPEECH_SAMPLES hold speech. A position whose window overlaps
    an earlier cut is skipped, so a short file or a probe with no duration gets one cut and never votes twice."""
    samples = []
    for f in SAMPLE_AT:
        if sum(x["speech"] >= MIN_SPEECH for x in samples) >= SPEECH_SAMPLES: break
        at = round(max(0.0, min(duration_s * f, duration_s - SAMPLE_SECS)))
        if any(abs(at - x["at"]) < SAMPLE_SECS for x in samples): continue
        samples.append(dict(at=at, **detect(whisper, extract(path, audio_index, at))))
    return samples


def lower_priority(threads):
    """The CLI yields to the apps. Under memory pressure the kernel kills it first, and it runs at nice 10.
    numpy's BLAS would take every core, so OMP_NUM_THREADS is set before numpy loads."""
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    try:
        with open("/proc/self/oom_score_adj", "w") as f: f.write("1000")
        os.nice(10)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Identify the spoken language of one audio stream. Prints one JSON line.")
    ap.add_argument("path", nargs="?"); ap.add_argument("index", type=int, nargs="?"); ap.add_argument("duration", type=float, nargs="?")
    ap.add_argument("--fetch", action="store_true", help="deploy time: download the pinned model and check its sha256")
    ap.add_argument("--expect", nargs="*", default=[], help="the track's tag and the app's original language, as 639-2 codes")
    ap.add_argument("--model", default=MODEL); ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--cache", default=CACHE); ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--fresh", action="store_true", help="hear the stream again, past the cache")
    a = ap.parse_args(argv)
    if a.fetch:
        print(fetch(a.model_dir))
        return 0
    if a.duration is None: ap.error("PATH, INDEX and DURATION are required")
    try:
        r = identify(a.path, a.index, a.duration, a.expect, a.model, a.model_dir, a.cache, a.threads, a.fresh)
    except Exception as e:   # the hook reads one JSON line either way
        r = {"lang": None, "prob": 0.0, "why": f"error: {e}", "samples": [], "engine": ENGINE, "model": a.model, "error": str(e)}
    print(json.dumps(r))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    lower_priority(THREADS)
    sys.exit(main())
