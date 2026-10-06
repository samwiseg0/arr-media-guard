#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Replay the timing path of one video from a word cache, with no new hearing. See tools/bench/README.md.

    python tools/bench/replay.py VIDEO --cache WORDS.sqlite --out RESULT.json [--code DIR] [--plant SPEC]
                                 [--original LANGUAGE] [--hear --lid-dir DIR] [--work DIR]

It runs the --sub-time check of the package in DIR (this checkout by default) as a dry run, so the file never changes.
The words come from WORDS.sqlite, a cache in the format of lid.py. AMG keys each clip by the file's path, size and
mtime, the audio stream, the model, the language and the starts of the clip's windows. A clip the cache lacks is put
together from the same windows of other clips. A window no clip holds hears nothing, and the result counts it as
missing. --hear runs lid.py for those calls instead and adds the new words to the cache. It needs --lid-dir, the
LID_DIR of the install: the folder with the Whisper venv and models (docs/design.md, "Audio language detection").

Only these parts of the run change:
- checks.lid_run() and checks.lid_speech() serve the cache.
- subtitles.subtitle_cues() moves the cues by --plant, so a planted case needs no copy of the file.
- process.Ctx() takes --original as the original language, as Sonarr or Radarr would name it.
- remux.resub() runs dry, and the replay keeps the plan it was asked to write.

RESULT holds the result line and alerts of the run, and per subtitle track its verdict and fix. "cues" lists per track
[the cue's start in the file, its start after the plant, its start in the plan] for each cue in time order. score.py
reads it. --plant SPEC is one of +S or -S (every cue S seconds later or earlier), step:SHARE:S (the cues from SHARE of
the duration on), blk:FROM:LEN:S (the cues that start in that span) or rate:P/Q (every time times P/Q)."""
import argparse, json, os, resource, shutil, sqlite3, sys, time
from fractions import Fraction

ap = argparse.ArgumentParser(description="Replay the timing path of one video from a word cache.")
ap.add_argument("video"), ap.add_argument("--cache", required=True), ap.add_argument("--out", required=True)
ap.add_argument("--code", default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ap.add_argument("--plant"), ap.add_argument("--original"), ap.add_argument("--hear", action="store_true"), ap.add_argument("--lid-dir")
ap.add_argument("--work", help="a folder for the run's state, removed at the end. Default: beside --out")
a = ap.parse_args()
if a.hear and not a.lid_dir:
    ap.error("--hear needs --lid-dir")
video, cache = os.path.abspath(a.video), os.path.abspath(a.cache)
work = os.path.abspath(a.work or f"{a.out}.work-{os.getpid()}")
for d in ("state", "home", "tmp"):
    os.makedirs(f"{work}/{d}", exist_ok=True)
for k in [k for k in os.environ if k.split("_")[0] in ("SONARR", "RADARR", "PLEX", "DISCORD")]:
    del os.environ[k]   # a replay never reaches an app, Plex or Discord
os.environ.update(STATE_DIR=f"{work}/state", LOG=f"{work}/log.jsonl", HOME=f"{work}/home", TMPDIR=f"{work}/tmp", KEEP_ORIGINALS_DAYS="0",
                  POLICY_FILE=os.path.join(a.code, "examples", "policy.json"), AMG_INVARIANTS="1", **({"LID_DIR": a.lid_dir} if a.lid_dir else {}))
sys.path.insert(0, a.code)
from arr_media_guard import checks, cli, decide, lid, process, remux, subsync, subtitles   # noqa: E402

TAG = lid.tag(lid.MODEL)
words = {"exact": 0, "joined": 0, "missing": 0, "speech_missing": 0, "missing_at": []}


def rows_of(path, index, lang):
    """({clip key: windows}, {(start, seconds): the first window heard there}) of path in the cache."""
    with sqlite3.connect(f"file:{cache}?mode=ro", uri=True, timeout=60) as db:
        rows = db.execute("SELECT win, words FROM words WHERE path=? AND idx=? AND model=? AND lang=?", (path, index, TAG, lang)).fetchall()
    exact, one = {w: json.loads(x) for w, x in rows}, {}
    for w, x in sorted(rows):
        for win in json.loads(x):
            one.setdefault((round(win["at"], 1), float(win["secs"])), win)
    return exact, one


def joined(starts, secs, more, lang, one):
    """The windows of starts from other clips, with the rule of lid.listen() for more, or None when one is missing."""
    ws = [one.get((round(s, 1), float(secs))) for s in starts]
    if None in ws:
        return None
    few = subsync.short(ws, lid.code(lang)) if more else []
    for k in (few if len(few) < len(ws) or len(ws) == 1 else []):
        if k < len(more) and more[k] is not None:
            w = one.get((round(more[k], 1), float(lid.THIRD_SECS)))
            if w is None:
                return None
            ws.append(w)
    return ws


def heard(f, path, *args):
    """f with the run's own cache, seeded with the rows of path, then its new rows go to the cache."""
    mine = f"{work}/state/lid.sqlite"
    with lid._db(mine) as db:
        db.execute("ATTACH ? AS c", (cache,))
        for t in ("words", "speech"):
            db.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM c.{t} WHERE path=?", (path,))
    r = f(path, *args)
    with sqlite3.connect(cache, timeout=120) as db:
        db.execute("ATTACH ? AS mine", (mine,))
        for t in ("words", "speech"):
            db.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM mine.{t}")
    return r


real_lid_run, real_speech, real_cues, real_resub, real_ctx = checks.lid_run, checks.lid_speech, subtitles.subtitle_cues, remux.resub, process.Ctx


def lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words_=None, then=None, yield_to=None, **kw):
    words_ = words_ or kw.get("words")
    if not words_:   # a language check
        return heard(real_lid_run, path, index, j, expect, timeout, fresh, keep, None, then, yield_to) if a.hear else {"why": "the replay serves no language check"}
    lang, starts, secs = words_[0], list(words_[1]), words_[2] if len(words_) > 2 else subsync.WINDOW
    more, group = (words_[3] if len(words_) > 3 else None), (words_[4] if len(words_) > 4 else None)
    exact, one = rows_of(path, index, lid.code(lang))
    out, miss = [], []
    for g in ([starts[i:i + group] for i in range(0, len(starts), group)] if group else [starts]):
        key = json.dumps([[round(s, 1), secs] for s in g])
        if key in exact:
            out += exact[key]
            words["exact"] += len(g)
        elif (w := joined(g, secs, None if group else more, lang, one)) is not None:
            out += w
            words["joined"] += len(g)
        else:
            miss.append(g)
    if miss and a.hear:
        return heard(real_lid_run, path, index, j, expect, timeout, fresh, keep, words_, then, yield_to)
    for g in miss:   # a window no clip holds hears nothing
        words["missing"] += len(g)
        words["missing_at"] += [round(s, 1) for s in g]
        out += [{"at": s, "secs": secs, "words": []} for s in g]
    return {"windows": out, "cached": True, "reused": 0, "took": 0.0, "profile": {}, "model": TAG, "cpu": 0.0}


def lid_speech(path, index, j, timeout, yield_to=None):
    st = os.stat(path)
    with sqlite3.connect(f"file:{cache}?mode=ro", uri=True, timeout=60) as db:
        row = db.execute("SELECT spans FROM speech WHERE path=? AND size=? AND mtime_ns=? AND idx=? ORDER BY at DESC",
                         (path, st.st_size, st.st_mtime_ns, index)).fetchone()
    if row:
        return {"spans": json.loads(row[0]), "cached": True, "took": 0.0, "cpu": 0.0}
    if a.hear:
        return heard(real_speech, path, index, j, timeout, None)
    words["speech_missing"] += 1
    return {"why": "the word cache holds no spans of speech for this file"}


def plant(cues, spec, duration):
    """cues [(start, end, text)] moved by spec, see the module docstring."""
    if not spec:
        return list(cues)
    if spec[0] in "+-":
        return [(s + float(spec), e + float(spec), x) for s, e, x in cues]
    kind, *v = spec.split(":")
    if kind == "blk":
        lo, n, sh = map(float, v)
        return [(s + sh, e + sh, x) if lo <= s < lo + n else (s, e, x) for s, e, x in cues]
    if kind == "step":
        at, sh = float(v[0]) * duration, float(v[1])
        return [(s + sh, e + sh, x) if s >= at else (s, e, x) for s, e, x in cues]
    if kind == "rate":
        r = float(Fraction(v[0]))
        return [(s * r, e * r, x) for s, e, x in cues]
    raise ValueError(f"unknown plant {spec}")


BASE, DUR, ASKED = {}, [0.0], []


def subtitle_cues(path, j, want, full=False):
    out = real_cues(path, j, want, full)
    for p, cues in list(out.items()):
        if cues and isinstance(cues, list) and isinstance(cues[0], tuple):
            BASE.setdefault(p, list(cues))
            DUR[0] = decide.duration(j)
            if a.plant:
                new = plant(cues, a.plant, DUR[0])
                if type(cues) is not list:   # a Cut keeps its type and its facts
                    new = type(cues)(new)
                    new.__dict__.update(cues.__dict__)
                out[p] = new
    return out


def resub(path, j, st, apply, fixes, drop=(), ends=None, timed=None, recode=None, strip=None):
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    ASKED.append({"ids": ids, "fixes": {str(k): v for k, v in fixes.items()}, "timed": {str(k): [list(r) for r in v] for k, v in (timed or {}).items()}})
    return real_resub(path, j, st, False, fixes, drop, ends, timed, recode, strip)


checks.lid_run, checks.lid_speech, subtitles.subtitle_cues, remux.resub = lid_run, lid_speech, subtitle_cues, resub
if not a.hear:   # the cache stands in for the Whisper install
    checks.lid_ready = lambda: True
process.Ctx = lambda app, path, label, original, *x, **k: real_ctx(app, path, label, original or a.original, *x, **k)

t0, c0 = time.time(), resource.getrusage(resource.RUSAGE_SELF)
error = None
try:
    cli.main(["--sub-time", video])
except SystemExit as ex:
    error = f"exit {ex.code}" if ex.code else None
except Exception as ex:   # a safety self-check or a crash is a finding of the replay
    error = f"{type(ex).__name__}: {ex}"[:500]
c1, ch = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
log = [json.loads(x) for x in open(f"{work}/log.jsonl")] if os.path.exists(f"{work}/log.jsonl") else []
rec = ([r for r in log if "schema" in r] or [{}])[-1]
if not error and rec.get("outcome") == "error":
    error = rec.get("result")
ask = ASKED[-1] if ASKED else {"ids": [], "fixes": {}, "timed": {}}
tracks, cues = {}, {}
for p, x in (rec.get("subcheck") or {}).items():
    t = x.get("timing") or {}
    tracks[p] = {k: t.get(k) for k in ("fix", "unfixed", "piecewise", "offsets", "why", "sweep_fit")} | {"verdict": x.get("verdict")}
    k = int(p[1:]) - 1 if p[:1] == "s" and p[1:].isdigit() else None
    if p in BASE and k is not None and k < len(ask["ids"]):
        tid = str(ask["ids"][k])
        plan = {round(r[0], 3): r[3] for r in ask["timed"].get(tid, [])}
        fix = ask["fixes"].get(tid)
        base = sorted(BASE[p])
        moved = plant(base, a.plant, DUR[0])
        new = lambda s: plan[round(s, 3)] if round(s, 3) in plan else round(subsync.moved(s * 1000, fix) / 10) / 100 if fix else s
        cues[p] = [[b[0], m[0], new(m[0])] for b, m in zip(base, moved)]
json.dump({"video": video, "code": a.code, "plant": a.plant, "error": error, "result": rec.get("result"), "alerts": rec.get("alerts"),
           "findings": rec.get("findings"), "tracks": tracks, "cues": cues, "words": words, "wall": round(time.time() - t0, 1),
           "cpu": round(c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime + ch.ru_utime + ch.ru_stime, 1)}, open(a.out, "w"), default=str)
shutil.rmtree(work, ignore_errors=True)
print(f'{os.path.basename(video)}: {rec.get("result")} | ' + "; ".join(f'{p} {t["verdict"]} {t["fix"] or t["why"]}' for p, t in tracks.items())
      + f' | words {words["exact"]} exact, {words["joined"]} joined, {words["missing"]} missing' + (f" | ERROR {error}" if error else ""))
