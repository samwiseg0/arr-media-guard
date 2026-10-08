#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Spoken-language identification for arr-media-guard. It hears the language of one audio stream.

decide.py trusts track language tags, and tags lie. A Spanish or Japanese film may carry an eng tag, and some
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

    /opt/arr-media-guard-lid/venv/bin/python lid.py PATH INDEX DURATION --cache FILE --model-dir DIR [--fresh] --expect TAG ORIGINAL...

INDEX is the ffmpeg audio index (0:a:INDEX), the same index the hook's sample() uses. The CLI prints one JSON line.
--keep-pcm keeps the audio of each sample in the cache for an hour, so the subtitle check can cut its windows from it.

The subtitle check (docs/design.md, "Subtitle match") asks for the words of short windows instead:

    /opt/arr-media-guard-lid/venv/bin/python lid.py PATH INDEX DURATION --cache FILE --model-dir DIR --words LANG START...

listen() transcribes every window in one Whisper run and caches the words by the same file identity. The speech
layout check asks for the spans of speech of the whole stream with --speech, see speech().
At install, "lid.py --fetch" downloads the pinned model once and checks its sha256. docs/design.md, section
"Audio language detection", has the rules and the install.
"""
import argparse, fcntl, hashlib, json, os, resource, sqlite3, subprocess, sys, time
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
# The subtitle check. Its two windows go through Whisper as one clip, so the encoder runs once. Three windows of WORD_SECS
# in one clip made it run twice in most runs, at more CPU per window. More threads cost more CPU time than they save in
# wall time, so the check runs one thread.
WORD_SECS = 10.0                         # seconds of each window, subsync.WINDOW. A float, as the hook passes it, so the cache keys agree.
THIRD_SECS = 24                          # seconds of a third window, subsync.THIRD
MAX_COMPRESSION = 2.4                    # a segment whose text compresses more is a loop, faster-whisper's own threshold
WORD_THREADS = 1
PCM_KEEP = 3600                          # seconds a kept language sample stays in the cache for the subtitle check
SPEECH_SECS = 600                        # seconds of audio Silero VAD reads at a time, so a film never sits in memory whole

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


WHISPER_CODE = {v: k for k, v in WHISPER.items() if k not in ("nn", "yue")}   # 639-2/B -> the code Whisper takes


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
    # The subtitle check: the words of its windows, the audio the language check kept, and each file's verdicts.
    db.execute("CREATE TABLE IF NOT EXISTS words (path TEXT, size INTEGER, mtime_ns INTEGER, idx INTEGER, model TEXT, lang TEXT,"
               " win TEXT, words TEXT, at REAL, PRIMARY KEY (path, size, mtime_ns, idx, model, lang, win))")
    db.execute("CREATE TABLE IF NOT EXISTS pcm (path TEXT, size INTEGER, mtime_ns INTEGER, idx INTEGER, start INTEGER, secs INTEGER,"
               " pcm BLOB, at REAL, PRIMARY KEY (path, size, mtime_ns, idx, start, secs))")
    db.execute("CREATE TABLE IF NOT EXISTS subcheck (path TEXT, size INTEGER, mtime_ns INTEGER, result TEXT, pending INTEGER,"
               " at REAL, PRIMARY KEY (path, size, mtime_ns))")
    # The speech layout check: the spans of speech of a whole audio stream, by the VAD settings that drew them.
    db.execute("CREATE TABLE IF NOT EXISTS speech (path TEXT, size INTEGER, mtime_ns INTEGER, idx INTEGER, how TEXT, spans TEXT,"
               " at REAL, PRIMARY KEY (path, size, mtime_ns, idx, how))")
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


def carry(path, before, cache=CACHE, was=None):
    """Move the cached results of path from its os.stat() before an in-place edit to its stat now.
    mkvpropedit changes the mtime but never the audio, so the hook calls this in-process after each edit. It never
    raises and never creates the cache, because a cache problem must not fail an edit that already happened.
    was is the old path of a conversion, whose proof shows the same audio. Only the subtitle check's words and the
    spans of speech move then."""
    if not os.path.exists(cache): return
    try:
        st = os.stat(path)
        with closing(_db(cache)) as db, db:
            for table in ("words", "speech") if was else ("lid", "words", "pcm", "speech"):   # never subcheck: a changed file needs a new verdict
                db.execute(f"UPDATE OR REPLACE {table} SET path=?, size=?, mtime_ns=? WHERE path=? AND size=? AND mtime_ns=?",
                           (path, st.st_size, st.st_mtime_ns, was or path, before.st_size, before.st_mtime_ns))
    except (sqlite3.Error, OSError):
        pass


def verdict_get(cache, path):
    """(the subtitle check's cached result, pending) for path as it is now, or None. pending is True when the result
    asked for an action that no apply made yet. It never raises and never creates the cache."""
    try:
        st = os.stat(path)
        if not os.path.exists(cache): return None
        with closing(_db(cache)) as db, db:
            row = db.execute("SELECT result, pending FROM subcheck WHERE path=? AND size=? AND mtime_ns=?", (path, st.st_size, st.st_mtime_ns)).fetchone()
        return (json.loads(row[0]), bool(row[1])) if row else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def verdicts(cache):
    """[(path, size, mtime_ns, the saved result)] of every saved subtitle result, of the file as it is now or as it was.
    It never raises and never creates the cache."""
    try:
        if not os.path.exists(cache): return []
        with closing(_db(cache)) as db, db:
            rows = db.execute("SELECT path, size, mtime_ns, result FROM subcheck").fetchall()
    except (sqlite3.Error, OSError):
        return []
    out = []
    for path, size, mtime_ns, result in rows:
        try:
            r = json.loads(result)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append((path, size, mtime_ns, r))
    return out


def verdict_put(cache, path, result, pending):
    """Cache the subtitle check's result for path as it is now. It never raises, because the check is done."""
    try:
        st = os.stat(path)
        with closing(_db(cache)) as db, db:
            db.execute("INSERT OR REPLACE INTO subcheck VALUES (?, ?, ?, ?, ?, ?)",
                       (path, st.st_size, st.st_mtime_ns, json.dumps(result), int(pending), time.time()))
    except (sqlite3.Error, OSError):
        pass


def kept_pcm(cache, path, index):
    """[(start, seconds)] of the audio of stream index that the language check kept for path as it is now. It never raises."""
    try:
        st = os.stat(path)
        if not os.path.exists(cache): return []
        with closing(_db(cache)) as db, db:
            return db.execute("SELECT start, secs FROM pcm WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND at>?",
                              (path, st.st_size, st.st_mtime_ns, index, time.time() - PCM_KEEP)).fetchall()
    except (sqlite3.Error, OSError):
        return []


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


LOADED = {}   # (model, model_dir) -> the model this process loaded, so the language check and the subtitle check share it


def load(model=MODEL, model_dir=MODEL_DIR, threads=THREADS):
    """The Whisper model from its local directory. int8 on CPU. Never downloads. A process loads it once: a later call
    gets the model loaded before, whatever its thread count, because a load costs a few CPU seconds."""
    if (model, model_dir) not in LOADED:
        from faster_whisper import WhisperModel
        LOADED[model, model_dir] = WhisperModel(os.path.join(model_dir, model), device="cpu", compute_type="int8", cpu_threads=threads,
                                                local_files_only=True)
    return LOADED[model, model_dir]


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


def identify(path, audio_index, duration_s, expect=(), model=MODEL, model_dir=MODEL_DIR, cache=CACHE, threads=THREADS, fresh=False, keep=False):
    """The spoken language of ffmpeg audio stream audio_index of path. fresh skips the cached samples and hears again.
    keep puts the audio of each new sample in the cache for PCM_KEEP, for the subtitle check, see listen().

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
                kept = {}
                samples = hear(path, audio_index, duration_s, load(model, model_dir, threads), kept if keep else None)
                cache_put(cache, path, audio_index, key, duration_s, samples, st)
                with closing(_db(cache)) as db, db:
                    db.execute("DELETE FROM pcm WHERE at<?", (time.time() - PCM_KEEP,))
                    db.executemany("INSERT OR REPLACE INTO pcm VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   [(path, st.st_size, st.st_mtime_ns, audio_index, at, SAMPLE_SECS, pcm, time.time()) for at, pcm in kept.items()])
    lang, prob, why = combine(samples, expect)
    return dict(out, lang=lang, prob=prob, why=why, samples=samples, cached=cached, took=round(time.time() - t0, 2))


def hear(path, audio_index, duration_s, whisper, kept=None):
    """Cut and hear samples in SAMPLE_AT order until SPEECH_SAMPLES hold speech. A position whose window overlaps
    an earlier cut is skipped, so a short file or a probe with no duration gets one cut and never votes twice.
    kept, a dict, gets {start: the audio} of each sample."""
    samples = []
    for f in SAMPLE_AT:
        if sum(x["speech"] >= MIN_SPEECH for x in samples) >= SPEECH_SAMPLES: break
        at = round(max(0.0, min(duration_s * f, duration_s - SAMPLE_SECS)))
        if any(abs(at - x["at"]) < SAMPLE_SECS for x in samples): continue
        pcm = extract(path, audio_index, at)
        if kept is not None:
            kept[at] = pcm
        samples.append(dict(at=at, **detect(whisper, pcm)))
    return samples


def transcribe(whisper, pcm, lang):
    """[[seconds from the clip start, word]] of one clip, in the language lang (639-2/B). Silero VAD drops the silence,
    and faster-whisper maps the times back. Greedy decoding with no temperature fallback keeps the cost down, and so
    does a chunk as long as the clip: Whisper pads to 30 seconds by default, and the padding cost decode time. Each
    segment decodes on its own, not on the text before it, so a loop cannot carry over. A segment whose text
    compresses more than MAX_COMPRESSION is a loop, one phrase said again and again, and it drops."""
    import numpy as np
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    segs, _ = whisper.transcribe(audio, language=WHISPER_CODE.get(code(lang)), beam_size=1, temperature=0.0, word_timestamps=True,
                                 vad_filter=True, condition_on_previous_text=False, compression_ratio_threshold=MAX_COMPRESSION,
                                 chunk_length=max(1, -(-len(pcm) // 32000)))
    return kept_words(segs)


def kept_words(segs):
    """[[start, word]] of faster-whisper segments, with each segment that compresses past MAX_COMPRESSION left out."""
    return [[round(w.start, 2), w.word.strip()] for s in segs if s.compression_ratio <= MAX_COMPRESSION for w in s.words or [] if w.word.strip()]


def clip_of(path, audio_index, starts, secs, kept):
    """(the audio of the windows of secs from each start as one clip, the windows cut from a kept sample). A short cut
    at the end pads with silence, so the times stay."""
    clips, reused, n = [], 0, int(secs * 16000) * 2
    for s in starts:
        k = next(((a, p) for a, w, p in kept if a <= s and s + secs <= a + w), None)
        if k:
            b = int((s - k[0]) * 16000) * 2
            clips.append(k[1][b:b + n])
            reused += 1
        else:
            clips.append(extract(path, audio_index, s, secs))
    return b"".join(c[:n].ljust(n, b"\0") for c in clips), reused


def cpu_now():
    """CPU seconds of this process and its children so far: the model, and ffmpeg."""
    use = [resource.getrusage(w) for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    return sum(u.ru_utime + u.ru_stime for u in use)


def listen(path, audio_index, starts, lang, secs=WORD_SECS, model=MODEL, model_dir=MODEL_DIR, cache=CACHE, threads=WORD_THREADS,
           more=None, more_secs=THIRD_SECS):
    """The words Whisper hears in the windows of secs from each start, on ffmpeg audio stream audio_index of path, in
    the language lang (639-2/B). The windows go through Whisper as one clip, so the encoder runs once. A window inside
    a sample the language check kept is cut from that sample, so no audio is decoded twice. more holds a second window
    per start, or None: when a window hears under subsync.MIN_WORDS words and another does not, or it is the only
    window, its window in more, of more_secs, is heard too, in this process with the model loaded.
    Returns {"windows": [{"at", "secs", "words": [[seconds from at, word], ...]}], "cached", "reused": windows cut from a
    kept sample, "model", "took": wall seconds, "profile": CPU and wall seconds of the model load, the audio decode and
    Whisper}. The words are cached by path, size, mtime, stream, model, language and the first windows. Raises when
    ffmpeg or the model fails, and nothing is cached then."""
    t0, key, win = time.time(), tag(model), json.dumps([[round(s, 1), secs] for s in starts])
    if code(lang) not in WHISPER_CODE:
        raise ValueError(f"whisper cannot transcribe {lang}")
    st = os.stat(path)
    row = words_get(cache, path, audio_index, key, lang, starts, secs)
    if row is not None:
        return {"windows": row, "cached": True, "reused": 0, "model": key, "took": round(time.time() - t0, 2)}
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    with open(cache + ".lock", "w") as lock:   # the one model per host, as identify() takes it
        fcntl.flock(lock, fcntl.LOCK_EX)
        row = words_get(cache, path, audio_index, key, lang, starts, secs)   # the other caller may have just heard it
        if row is not None:
            return {"windows": row, "cached": True, "reused": 0, "model": key, "took": round(time.time() - t0, 2)}
        with closing(_db(cache)) as db, db:
            kept = db.execute("SELECT start, secs, pcm FROM pcm WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND at>?",
                              (path, st.st_size, st.st_mtime_ns, audio_index, time.time() - PCM_KEEP)).fetchall()
        prof = {}

        def step(name, f, *a):   # CPU and wall seconds of one step, summed per name
            c, w = cpu_now(), time.time()
            got = f(*a)
            prof[name] = [round(prof.get(name, [0, 0])[0] + cpu_now() - c, 2), round(prof.get(name, [0, 0])[1] + time.time() - w, 2)]
            return got

        whisper = []

        def hear_all(ws, n):   # the windows ws of n seconds as one clip -> [{"at", "secs", "words"}]
            clip, used = step("decode", clip_of, path, audio_index, ws, n, kept)
            if not whisper:
                whisper.append(step("load", load, model, model_dir, threads))
            heard = step("whisper", transcribe, whisper[0], clip, lang)
            return [{"at": s, "secs": n, "words": [[round(t - k * n, 2), w] for t, w in heard if k * n <= t < (k + 1) * n]}
                    for k, s in enumerate(ws)], used
        windows, reused = hear_all(starts, secs)
        if more:
            from . import subsync
            few = subsync.short(windows, code(lang))
            extra = [more[k] for k in few if k < len(more) and more[k] is not None] if len(few) < len(windows) or len(windows) == 1 else []
            if extra:
                got, used = hear_all(extra, more_secs)
                windows, reused = windows + got, reused + used
        with closing(_db(cache)) as db, db:
            db.execute("INSERT OR REPLACE INTO words VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (path, st.st_size, st.st_mtime_ns, audio_index, key, code(lang), win, json.dumps(windows), time.time()))
    return {"windows": windows, "cached": False, "reused": reused, "model": key, "took": round(time.time() - t0, 2), "profile": prof}


def speech_how():
    """The VAD model and settings that draw the spans of speech, in the cache key of speech(). faster-whisper bundles
    the Silero VAD model, so its version names the model. A change of either reads the audio again."""
    from importlib import metadata
    from . import subsync
    try:
        model = f"silero@faster-whisper-{metadata.version('faster-whisper')}"
    except metadata.PackageNotFoundError:
        model = "silero@unknown"
    return f"{model}/{subsync.VAD_ON}/{subsync.VAD_OFF}/{subsync.VAD_GAP}"


def start_delay(path, audio_index):
    """Seconds ffmpeg audio stream audio_index of path starts after the file starts, or 0 when ffprobe gives none. A
    decode to raw audio starts at the stream's first packet, but the cues and the speech onsets run on the file's
    clock. Without the delay, the speech of a delayed stream reads early by it."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{audio_index}", "-show_entries", "stream=start_time:format=start_time",
                        "-of", "json", path], capture_output=True, text=True, errors="replace", timeout=60)
    try:
        j = json.loads(r.stdout)
        return round(float(j["streams"][0]["start_time"]) - float(j["format"]["start_time"]), 3)
    except (ValueError, KeyError, IndexError, TypeError):
        return 0.0


def speech(path, audio_index, cache=CACHE, gate=None, queue=None):
    """The spans of speech of the whole of ffmpeg audio stream audio_index of path, for the speech layout check
    (docs/design.md, "Incorrect subtitle identification"). One ffmpeg read at nice 19 and idle I/O decodes the
    stream, mono at 16 kHz. Silero VAD, which faster-whisper bundles, gives a speech probability for each frame,
    SPEECH_SECS of audio at a time, and subsync.voiced() joins them into spans. The spans move by start_delay(), so
    they run on the file's clock. The spans are cached by path, size, mtime, stream and the VAD model and settings, see
    speech_how(). Silero VAD needs no Whisper model, so the read takes no turn at it. After each SPEECH_SECS it yields
    when waits() names a job or a hearing at gate and queue: it stops the read and caches nothing. Returns {"spans":
    [[start, end]] in seconds, "cached", "took": wall seconds}, or {"yielded": True, "cached", "took"}. Raises when
    ffmpeg or ffprobe fails, and nothing is cached then."""
    from . import subsync
    t0, how, st = time.time(), speech_how(), os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns, audio_index, how)
    with closing(_db(cache)) as db, db:
        row = db.execute("SELECT spans FROM speech WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND how=?", key).fetchone()
    if row:
        return {"spans": json.loads(row[0]), "cached": True, "took": round(time.time() - t0, 2)}
    import numpy as np
    from faster_whisper.vad import get_vad_model
    model, probs = get_vad_model(), []
    p = subprocess.Popen(["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
                          "-map", f"0:a:{audio_index}", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while chunk := p.stdout.read(SPEECH_SECS * 16000 * 2):
            audio = np.frombuffer(chunk[:len(chunk) // 2 * 2], np.int16).astype(np.float32) / 32768
            probs.append(model(np.pad(audio, (0, -len(audio) % 512))))   # the model reads frames of 512 samples
            if waits(gate, queue):
                p.kill()
                return {"yielded": True, "cached": False, "took": round(time.time() - t0, 2)}
    except BaseException:
        p.kill()
        raise
    finally:
        p.stdout.close()
        p.wait()
    if p.returncode:
        raise RuntimeError(f"ffmpeg exited {p.returncode} reading audio stream {audio_index}")
    late = start_delay(path, audio_index)
    spans = [[round(a + late, 3), round(b + late, 3)] for a, b in subsync.voiced(np.concatenate(probs).tolist() if probs else [])]
    with closing(_db(cache)) as db, db:
        db.execute("INSERT OR REPLACE INTO speech VALUES (?, ?, ?, ?, ?, ?, ?)", (*key, json.dumps(spans), time.time()))
    return {"spans": spans, "cached": False, "took": round(time.time() - t0, 2)}


def waits(gate, queue):
    """Another hearing waits for the host's model: it holds gate, the lid.turn.gate file of the hook, while it waits for
    its turn. Or an import job waits in the queue of queue, the hook's state store, or as a file in the queue folder
    beside it. Either one makes sweep() yield. A store that does not read is no import."""
    if queue:
        folder = os.path.join(os.path.dirname(queue), "queue")   # the job files of a busy store, see runner.queue_job()
        if os.path.isdir(folder) and any(not n.startswith(".") for n in os.listdir(folder)):
            return True
        try:
            with closing(sqlite3.connect(f"file:{queue}?mode=ro", uri=True, timeout=5)) as db:   # the jobs table of the hook's store.py
                if db.execute("SELECT 1 FROM jobs WHERE claimed = 0 AND due <= ? AND name NOT LIKE 'deep-analysis-%'",
                              (time.time_ns(),)).fetchone():
                    return True
        except sqlite3.Error:
            pass
    if not gate:
        return False
    with open(gate, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def sweep(path, audio_index, starts, lang, group, secs=WORD_SECS, gate=None, queue=None, **kw):
    """listen() over the windows of starts, group of them at a time, in this one process, for the hearings of --sub-time
    and the deep analysis, such as the whole-file hearing. The model loads once, each group is one clip, and each
    group's words are cached as listen() caches them. Before each group after the first it yields when another hearing or a job waits, see waits(): it returns what it heard,
    with "yielded", and the caller hears the rest later. Returns listen()'s answer over all windows, "cached" when
    every group was."""
    out = {"windows": [], "cached": True, "reused": 0, "took": 0.0, "profile": {}}
    for k in range(0, len(starts), group):
        if k and waits(gate, queue):
            out["yielded"] = True
            break
        r = listen(path, audio_index, starts[k:k + group], lang, secs, **kw)
        out["windows"] += r["windows"]
        out.update(cached=out["cached"] and r["cached"], reused=out["reused"] + r["reused"], took=round(out["took"] + r["took"], 2), model=r["model"])
        for step, (cpu, wall) in (r.get("profile") or {}).items():
            got = out["profile"].get(step, [0, 0])
            out["profile"][step] = [round(got[0] + cpu, 2), round(got[1] + wall, 2)]
    return out


def words_get(cache, path, audio_index, model, lang, starts, secs=WORD_SECS):
    """The cached windows of listen() for path as it is now, or None."""
    st = os.stat(path)
    with closing(_db(cache)) as db, db:
        row = db.execute("SELECT words FROM words WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND model=? AND lang=? AND win=?",
                         (path, st.st_size, st.st_mtime_ns, audio_index, model, code(lang),
                          json.dumps([[round(s, 1), secs] for s in starts]))).fetchone()
    return json.loads(row[0]) if row else None


def windows_get(cache, path, audio_index, model, lang, grid=None, why=None):
    """[{"at", "secs", "words"}] of every window the cache holds for path as it is now, stream audio_index, model and
    language lang, in time order, whichever hearing heard it: the word check, an earlier deep analysis, or a run that
    stopped part way. With grid, a set of (at, secs), only those windows count. A window that two hearings heard comes
    once, from the first. A row or a window that does not read is left out on its own, and why, a list, gets the
    reason once. It never raises and never creates the cache."""
    bad = lambda x: why is not None and x not in why and why.append(x)
    try:
        st = os.stat(path)
        if not os.path.exists(cache): return []
        with closing(_db(cache)) as db, db:
            rows = db.execute("SELECT words FROM words WHERE path=? AND size=? AND mtime_ns=? AND idx=? AND model=? AND lang=? ORDER BY at",
                              (path, st.st_size, st.st_mtime_ns, audio_index, model, code(lang))).fetchall()
    except (sqlite3.Error, OSError) as ex:
        bad(f"the word cache does not read: {ex}")
        return []
    one = {}
    for (x,) in rows:
        try:
            ws = json.loads(x)
        except ValueError:
            bad("a row of the word cache does not read")
            continue
        for w in ws if isinstance(ws, list) else ():
            try:
                key = (round(float(w["at"]), 1), float(w["secs"]))
                words = [[float(t), str(v)] for t, v in w["words"]]
            except (KeyError, TypeError, ValueError):
                bad("a window of the word cache does not read")
                continue
            if grid is None or key in grid:
                one.setdefault(key, dict(w, at=float(w["at"]), secs=key[1], words=words))
    return [one[k] for k in sorted(one)]


def jobs(path, spec, cache, model=MODEL, model_dir=MODEL_DIR):
    """The subtitle check's hearings in the process of a language check, so the model loads once. spec is a JSON file
    of [{"index", "lang", "cues": [[start, end, text], ...], "duration"}], one per audio stream. The windows come from
    subsync.windows() with the samples the language check kept, as the hook picks them, so the hook's own check
    later finds the words in the cache. Returns [{"index", "starts"} or {"index", "error"}]."""
    from . import subsync
    out = []
    with open(spec) as f:
        todo = json.load(f)
    for j in todo:
        try:
            stop = subsync.decide.STOPWORDS.get(code(j["lang"]), frozenset())
            first = subsync.windows(j["cues"], j["duration"], stop, kept_pcm(cache, path, j["index"]))
            if first:
                more = subsync.windows(j["cues"], j["duration"], stop, secs=THIRD_SECS, taken=first)
                listen(path, j["index"], first, j["lang"], cache=cache, model=model, model_dir=model_dir, more=more)
            out.append({"index": j["index"], "starts": first})
        except Exception as e:   # the hook's own check tries again
            out.append({"index": j.get("index"), "error": str(e)[:200]})
    return out


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
    ap.add_argument("--keep-pcm", action="store_true", help="keep the samples' audio for the subtitle check")
    ap.add_argument("--words", nargs="+", metavar="LANG START", help="the subtitle check: the words of a window from each START")
    ap.add_argument("--secs", type=float, default=WORD_SECS, help="the seconds of each --words window")
    ap.add_argument("--more", nargs="+", help="a second window per --words START, - for none, heard when its window hears too little")
    ap.add_argument("--group", type=int, help="hear the --words windows this many at a time in this process, for the whole-file hearing, see sweep()")
    ap.add_argument("--yield-gate", help="sweep() and the speech read yield while another hearing holds this gate file, see waits()")
    ap.add_argument("--yield-queue", help="sweep() and the speech read yield while an import job waits in this state store, see waits()")
    ap.add_argument("--then-words", metavar="FILE", help="after the language check, the subtitle check's hearings in FILE, see jobs()")
    ap.add_argument("--speech", action="store_true", help="the spans of speech of the whole stream, for the speech layout check, see speech()")
    a = ap.parse_args(argv)
    if a.fetch:
        print(fetch(a.model_dir))
        return 0
    if a.duration is None: ap.error("PATH, INDEX and DURATION are required")
    try:
        if a.speech:
            r = speech(a.path, a.index, a.cache, a.yield_gate, a.yield_queue)
        elif a.words:
            more = [None if x == "-" else float(x) for x in a.more] if a.more else None
            if a.group:
                r = sweep(a.path, a.index, [float(x) for x in a.words[1:]], a.words[0], a.group, a.secs, a.yield_gate, a.yield_queue, model=a.model,
                          model_dir=a.model_dir, cache=a.cache)
            else:
                r = listen(a.path, a.index, [float(x) for x in a.words[1:]], a.words[0], a.secs, model=a.model, model_dir=a.model_dir, cache=a.cache,
                           more=more)
        else:
            threads = min(a.threads, WORD_THREADS) if a.then_words else a.threads   # the shared model runs the subtitle check's thread count
            r = identify(a.path, a.index, a.duration, a.expect, a.model, a.model_dir, a.cache, threads, a.fresh, a.keep_pcm or bool(a.then_words))
            if a.then_words:
                r["words"] = jobs(a.path, a.then_words, a.cache, a.model, a.model_dir)
    except Exception as e:   # the hook reads one JSON line either way
        r = {"lang": None, "prob": 0.0, "why": f"error: {e}", "samples": [], "engine": ENGINE, "model": a.model, "error": str(e)}
    r["cpu"] = round(cpu_now(), 2)   # the model load and ffmpeg too
    print(json.dumps(r))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    # Run by path in the venv. The folder above the package takes the place of the package folder on sys.path, so jobs()
    # imports the package's subsync, and no module of the package hides a module of the venv.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    __package__ = "arr_media_guard"
    lower_priority(WORD_THREADS if {"--words", "--then-words", "--speech"} & set(sys.argv) else THREADS)
    sys.exit(main())
