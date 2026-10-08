# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The regression corpus: every shape of planted subtitle lines that a review of the timing check found, on every
commit. Each case plants lines off their speech, so its truth is known. Each case scores what AMG did in these kinds:

- wrong_silence: 3 or more lines in a row sit 0.5 s or more off after the run, and no alert posts.
- wrong_alert: an alert posts, and no 3 lines in a row sit 0.5 s or more off after the run.
- wrong_text: a text says "left as they are" while the plan moves lines, or says "0.0 s", or the alert and the change
  post both name the moved lines.
- wrong_move: a line within 0.5 s of its speech before the plan sits 0.5 s or more off after it.
- broken: a safety rule of a move broke, see subsync.INVARIANTS.

After the run means the times the file holds after the write. A failed write leaves the old times. A dry run counts
the times --apply would write. fixtures/known_failures.json names the kinds each case fails today. A case passes only
when its kinds are the listed ones. So a new failure fails, and a case that stops failing fails until its entry leaves
the list in the same commit. AMG_CORPUS_OUT=FILE adds each case's record to FILE, see tools/bench/regressions.py.
Every case is made up."""
import json
import os
import random
import re

import pytest

import test_arr_subsync as T
from test_arr_media_guard import hook, talk, english_film, hearing, two_lines, posted, last_decided, env, plex_analyzed, state_dir_in_tmp  # noqa: F401
from arr_media_guard import align, remux, runner, subsync as s

KNOWN = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "known_failures.json")))
OUT = os.environ.get("AMG_CORPUS_OUT")
OFF, RUN = 0.5, 3   # a line this many seconds off its speech is off, and this many off lines in a row need an alert
CHANGES = ("Converted to MKV", "File repaired", "Subtitles retimed", "Tracks changed")   # the titles of report.changes()
ZERO = re.compile(r"(?<![\d.])0\.0 s\b")
MOVED = re.compile(r"[Mm]oved \*\*(\d+) lines?\*\*")


def plant(runs):
    """A where() for talk.cues(): the lines of each run (first, end, seconds) sit that many seconds off."""
    return lambda i: next((x for a, b, x in runs if a <= i < b), 0.0)


def named(runs):
    return " ".join(f"{a}-{b}:{x:+g}" for a, b, x in runs) or "none"


def off_runs(after):
    """[first, last, lines, how far the first sits off] of each run of RUN or more lines OFF or more off."""
    out, cur = [], []
    for i, x in enumerate(after + [0.0]):
        if abs(x) >= OFF:
            cur.append(i)
            continue
        if len(cur) >= RUN:
            out.append([cur[0], cur[-1], len(cur), round(after[cur[0]], 2)])
        cur = []
    return out


def gate(case, before, after, alert=None, texts=(), moved=False, more=()):
    """Scores one case, adds its record to OUT, and checks its kinds against the known list. alert None: the case has
    no alert to judge, only its moves."""
    runs, got = off_runs(after), set(more)
    if alert is not None and runs and not alert:
        got.add("wrong_silence")
    if alert and not runs:
        got.add("wrong_alert")
    if any(abs(b) < OFF <= abs(a) for b, a in zip(before, after)):
        got.add("wrong_move")
    if any(ZERO.search(t) or moved and "left as they are" in t for t in texts):
        got.add("wrong_text")
    if OUT:
        with open(OUT, "a") as f:
            f.write(json.dumps({"id": case, "kinds": sorted(got), "alert": alert, "runs": runs,
                                "wrong": sum(abs(b) < OFF <= abs(a) for b, a in zip(before, after)), "texts": [t[:300] for t in texts]}) + "\n")
    want = set(KNOWN.get(case, ()))
    assert got == want, (f"new failure {sorted(got - want)}. " if got - want else "") + \
        (f"no longer fails {sorted(want - got)}: take it off tests/fixtures/known_failures.json. " if want - got else "") + f"runs {runs}, texts {texts}"


# --- planted lines through an import and its deep analysis --------------------------------------------------------------

def shapes():
    """Blocks of lines off, from the reviews of the block and settle fixes: a short third block or an off stretch beside
    two blocks, a step to the file's end or from its start, and two blocks with lines left at their edges."""
    out = []
    for base in ([(0, 40, -1.1), (300, 345, -1.1)], [(0, 45, 1.1), (296, 350, 1.1)]):
        for a3, n3 in ((150, 8), (150, 16), (170, 8), (200, 12), (230, 24), (110, 20), (250, 30)):
            for s3 in (1.2, -1.2, 0.9):
                out.append(("third", base + [(a3, a3 + n3, s3)]))
        b = base[1]
        for (lo, hi), s2 in (((b[1], b[1] + 15), 0.6), ((b[1], b[1] + 20), -1.0), ((b[0] - 15, b[0]), 0.8), ((b[1], b[1] + 7), 1.6),
                             ((b[1], b[1] + 15), 2.0), ((b[0] - 20, b[0]), -0.9), ((40, 55), 0.7)):
            out.append(("adjacent", base + [(lo, hi, s2)]))
    for n in (300, 280, 260, 250, 240, 230, 220, 210):
        for x in (1.2, -1.2, 0.9, -0.9):
            out.append(("tail", [(n, 400, x)]))
    for n in (100, 120, 140, 160, 180):
        for x in (1.2, -1.2):
            out.append(("head", [(0, n, x)]))
    for bl, sh in [([(0, 40), (300, 345)], 1.1), ([(0, 35), (305, 340)], 1.0), ([(0, 50), (296, 350)], 1.0),
                   ([(0, 45), (296, 350)], 1.1), ([(0, 40), (300, 345)], 1.0), ([(0, 40), (300, 345)], 1.2)]:
        for x in (sh, -sh):
            out.append(("builder", [(a, b, x) for a, b in bl]))
    for a2 in (290, 295, 300, 305, 310):
        for n in (25, 30, 35, 40, 50):
            for x in (1.0, -1.0):
                out.append(("edge", [(0, 40, x), (a2, a2 + n, x)]))
    return out


EDGES = [[(0, 35, 1.0), (305, 340, 1.0)], [(0, 40, -1.0), (305, 345, -1.0)], [(0, 35, -1.0), (305, 340, -1.0)],
         [(0, 40, -1.0), (295, 320, -1.0)], [(0, 40, -1.0), (305, 340, -1.0)]]   # blocks that leave lines at an edge
RUNS = list(dict.fromkeys([(f"shape {k} {named(r)}", tuple(r), "ok") for k, r in shapes()] +
                          [(f"{m} {named(r)}", tuple(r), m) for r in EDGES for m in ("fail", "all", "dry")]))


@pytest.mark.parametrize("case, runs, mode", RUNS, ids=[c[0] for c in RUNS])
def test_planted_lines(env, monkeypatch, settings, case, runs, mode):
    """An English film with the lines of runs off, through the import and its deep analysis (dry: a --sub-time dry
    run). The write succeeds, or fails (fail), and every change posts too (all)."""
    settings(subtitles="deep", **({"discord_posts": "all"} if mode == "all" else {}))
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, where=plant(runs))
    hearing(env, monkeypatch, {"s1": track})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plan = {}

    def resub(path, j, st, apply, fixes, drop=(), ends=None, timed=None, **k):
        plan.update(rows=next(iter((timed or {}).values()), None), fix=next(iter((fixes or {}).values()), None))
        if not apply:
            return ("would_remux_subtitles", "would remux subtitles: x", {})
        if mode == "fail":
            return ("subtitle_remux_failed", "subtitle remux failed: proof", {"warnings": None})
        return ("subtitles_remuxed", "subtitles remuxed", {"warnings": None, "new_size": 1000})
    monkeypatch.setattr(hook, "resub", resub)
    if mode == "dry":
        env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
        hook.main(["--sub-time", env["path"]])
        rec = last_decided(env)
        texts = [e.get("description") or "" for e in hook.report.render(rec, "embed") if isinstance(e, dict)] if rec.get("findings") else []
        dry_only = lambda f: all(x.get("code") == "not_retimed" and str(x.get("result", "")).startswith("would remux") for x in f.get("lines") or [{}])
        alert = any(not dry_only(f) for f in rec.get("findings") or ())
    else:
        hook.main([])
        posts = posted(env)
        texts = [d for _, d in posts]
        alert = any(t not in CHANGES for t, _ in posts)
    rows = {round(r[0], 2): r[3] for r in plan.get("rows") or ()}   # time_plan() rows: (start, text, end, new start, new end)
    new = lambda a: rows.get(round(a, 2), a) if rows else s.moved(round(a * 1000), plan["fix"]) / 1000 if plan.get("fix") else a
    truth = [talk.FIRST + talk.GAP * i for i in range(len(track))]
    before = [a - t for (a, _, _), t in zip(track, truth)]
    after = before if mode == "fail" else [new(a) - t for (a, _, _), t in zip(track, truth)]
    moved = any(abs(new(a) - a) > 0.005 for a, _, _ in track)
    counts = [MOVED.findall(d) for t, d in posts] if mode == "all" else []
    twice = {"wrong_text"} if any(a and set(a) & set(b) for a in counts for b in counts if a is not b) else ()
    gate(case, before, after, alert, texts, moved, twice)


# --- an import's held word-check alert, judged by the whole-file timing of its deep analysis ----------------------------

def whole_timed(monkeypatch, track, sync, spoken=lambda i: True):
    """(rec, where each line of track starts after the plan) of the whole-file timing of track, see
    subtitles.sub_whole(), with the word check sync and the judge's outcome in rec["subjudge"]. Whisper hears every line
    that spoken(i) keeps, on the grid of the whole-file hearing, see talk.heard(). The moves go to time_plan()'s place
    in start order, as the remux writes them."""
    ws = [dict(w, secs=s.WINDOW) for w in talk.heard(s.whole_grid(talk.DURATION), keep=spoken)]
    monkeypatch.setattr(hook, "whole_heard", lambda path, j, idx, lang, deep=False: {
        "windows": ws, "heard": [[0.0, talk.DURATION]], "spans": [], "onsets": [],
        "facts": {"windows": 0, "kept": len(ws), "cpu": 0.0, "took": 0.0, "failed": [], "runs": 0, "cached": 0}})
    whole, _ = hook.sub_whole("/m/f.mkv", {}, {"s1": ("eng", 0, track)}, sync)
    starts = {n: m["start"] for n, m in whole["s1"]["moves"].items() if m["why"] != "end"}
    rec = {"subcheck": sync, "whole": whole, "file_duration": talk.DURATION, **({"subremux": {"done": True, "timed": ["s1"]}} if starts else {})}
    rec["subjudge"] = hook.outcomes(rec, sync, {"s1": sorted(a for a, _, _ in track)})
    rows = remux.time_plan(track, None, (), starts=starts) if starts else None
    return rec, [r[3] for r in rows] if rows else [a for a, _, _ in track]


def unsure_sync(late=-1.1):
    """The word check of a track with steps from one unsure window at 700 s (lines 256 to 260): its heard words sit in
    time, and its cue starts late seconds off. The other windows sit in time."""
    w = {"at": 700.0, "words": 20, "overlap": 1.0, "offset": -0.2, "cues": 6, "late": late}
    return {"s1": {"verdict": "match", "why": "x", "windows": [{"at": 300.0, "words": 20, "overlap": 1.0, "offset": 0.0, "cues": 6, "late": 0.0}, w],
                   "timing": {"fix": None, "piecewise": True, "offsets": [0.0, late], "why": "steps",
                              "unsure": {"at": [700.0], "timing": {"fix": None, "why": "in time", "offset": 0.0}}}}}


UNSURE = [
    ("in time", [], None),
    ("5 lines early, 2 in the window", [(253, 258, -1.1)], None),
    ("5 lines early, 2 in the window, other side", [(259, 264, -1.1)], None),
    ("5 lines late, 2 in the window", [(253, 258, 1.1)], None),
    ("4 lines 2 s early, 2 in the window", [(254, 258, -2.0)], None),
    ("3 lines early, 2 in the window", [(255, 258, -1.1)], None),
    ("2 lines early in the window", [(256, 258, -1.1)], None),
    ("part leans 0.55, window 0.8 late", [(256, 261, 0.8), (200, 300, 0.55)], None),
    ("part leans 0.6, window 0.85 late", [(256, 261, 0.85), (200, 300, 0.6)], None),
    ("whole part 0.45 late, window 0.74 late", [(256, 261, 0.74), (150, 350, 0.45)], None),
    ("whole part 0.5 late, window 0.79 late", [(256, 261, 0.79), (150, 350, 0.5)], None),
    ("8 lines early, 6 a song with no words heard", [(250, 258, -1.1)], (250, 256)),
    ("10 lines early, 8 a song, 3 heard in time", [(250, 260, -1.1)], (250, 258))]


@pytest.mark.parametrize("name, runs, song", UNSURE, ids=[c[0] for c in UNSURE])
def test_an_unsure_window(monkeypatch, name, runs, song):
    """The steps of unsure_sync() through the whole-file timing and the timing judge, see judge.outcomes(). The alert
    posts while lines are still off. song: lines that hold no heard words."""
    track = talk.cues(talk.RIGHT, where=plant(runs))
    rec, new = whole_timed(monkeypatch, track, unsure_sync(), (lambda i: not song[0] <= i < song[1]) if song else (lambda i: True))
    truth = [talk.FIRST + talk.GAP * i for i in range(len(track))]
    gate(f"unsure {name}", [a - x for (a, _, _), x in zip(track, truth)], [a - x for a, x in zip(new, truth)], hook.still_off(rec["subjudge"]["s1"]),
         moved=bool(rec.get("subremux")))


HELD = [   # word-check windows (audio time, offset, late, cues), lines 76 to 87 1.0 s early, and the first lines truly late
    ("an unsure window heard in time", ((60.0, 0.03, 0.0, 5), (140.0, 0.18, 0.1, 5), (220.0, -0.2, -1.1, 5)), False, 0),
    ("a sure window 1.2 s late", ((60.0, 1.2, 1.2, 5), (140.0, 0.18, 0.1, 5), (220.0, -0.2, -1.1, 5)), False, 5),
    ("a sure window 1.2 s late, a block elsewhere", ((60.0, 1.2, 1.2, 5), (140.0, 0.18, 0.1, 5), (220.0, -0.2, -1.1, 5)), True, 5)]


@pytest.mark.parametrize("name, shape, block, late", HELD, ids=[c[0] for c in HELD])
def test_a_held_import_alert(monkeypatch, name, shape, block, late):
    """An import's steps alert, held for the deep analysis. The window at 220 s was misheard, so its lines sit in time.
    The first late lines sit 1.2 s late, and with block lines 76 to 87 sit 1.0 s early. The held alert posts unless
    runner.times_judged(), and the deep analysis posts its own alerts from the timing judge."""
    t = T.checked(*shape)
    sync = {"s1": {"verdict": "match", "why": "x", "windows": [{"at": at, "secs": 20.0, "words": 20, "offset": o, "late": x} for at, o, x, _ in shape],
                   "timing": t}}
    track = talk.cues(talk.RIGHT, where=plant([(0, late, 1.2)] + ([(76, 88, -1.0)] if block else [])))
    rec, new = whole_timed(monkeypatch, track, sync)
    truth = [talk.FIRST + talk.GAP * i for i in range(len(track))]
    alert = not runner.times_judged(rec, "s1") or hook.still_off(rec["subjudge"]["s1"])
    gate(f"held {name}", [a - x for (a, _, _), x in zip(track, truth)], [a - x for a, x in zip(new, truth)], alert)


# --- mid-file jumps and other hostile shapes through the whole-file timing --------------------------------------------

def jump_shapes():
    """Hostile shapes, first written for the jump check that the whole-file timing replaced: the part nearest the
    speech itself off, a jump beside a short block, a block with no jump, live captions, DVD scatter, drift plus a jump,
    a jump near an end, music with no words heard, three jumps, roll-up, Whisper late over a stretch, two cues on one
    start, and back-to-back cues."""
    for a, b in ((0.6, -0.6), (-0.6, 0.6), (0.7, -0.5), (0.6, 1.4), (-0.7, 0.4), (0.45, -0.45), (0.74, -0.74), (0.8, -0.8), (1.0, -1.0), (0.55, -0.55), (0.65, -0.35), (0.7, 0.0)):
        for at in (120, 200, 300):
            yield f"S1 nearest-off {a}/{b} at {at}", T.track(late=lambda i, a=a, b=b, at=at: b if i >= at else a), {}
    for n in (8, 15, 25, 40):
        for d in (1.0, -1.0):
            yield f"S2 block {n} before jump {d}", T.track(late=lambda i, n=n, d=d: d if i >= 200 else (-d if 200 - n <= i < 200 else 0.0)), {}
            yield f"S2 block {n} after jump {d}", T.track(late=lambda i, n=n, d=d: (2 * d if 200 <= i < 200 + n else d) if i >= 200 else 0.0), {}
            yield f"S2 block {n} at 100 jump at 250 {d}", T.track(late=lambda i, n=n, d=d: d if i >= 250 else (d if 100 <= i < 100 + n else 0.0)), {}
    for n in (40, 60, 80, 120, 160):
        for d in (1.0, -1.0, 0.8, 1.5):
            for lo in (60, 150):
                yield f"S3 block {n} at {lo} by {d}", T.track(late=lambda i, n=n, d=d, lo=lo: d if lo <= i < lo + n else 0.0), {}
    for seed in range(4):
        r = random.Random(seed)
        late = [r.uniform(2, 6) for _ in range(T.LINES)]
        yield f"S4 live {seed}", T.track(late=lambda i, late=late: late[i]), {}
        yield f"S4 live+jump {seed}", T.track(late=lambda i, late=late: late[i] + (1.5 if i >= 200 else 0)), {}
    for sd in (0.45, 0.6, 0.8):
        for seed in range(40):
            r = random.Random(1000 + seed)
            lag = [r.gauss(0, sd) for _ in range(T.LINES // T.SCENE + 1)]
            late = [lag[i // T.SCENE] + r.gauss(0, 0.15) for i in range(T.LINES)]
            yield f"S5 scatter {sd} {seed}", T.track(late=lambda i, late=late: late[i]), {}
    for slope in (0.001, -0.001, 0.0015, 0.0025):
        for d in (1.0, -1.0, 0.6, -0.6, 0.0):
            for at in (100, 200, 330):
                yield f"S6 drift {slope} jump {d} at {at}", T.track(late=lambda i, slope=slope, d=d, at=at: (d if i >= at else 0.0) + slope * (T.AT[i] - T.LENGTH / 2)), {}
    for at in (5, 15, 25, 40, 360, 375, 385, 395):
        for d in (1.0, -1.0, 1.5, 0.6):
            yield f"S7 edge jump {d} at {at}", T.track(late=lambda i, at=at, d=d: d if i >= at else 0.0), {}
    for d in (1.0, -1.0):
        for lo, n in ((180, 30), (190, 20), (195, 10)):
            yield f"S8 mute {lo}+{n} jump {d} at 200", T.track(late=lambda i, d=d: d if i >= 200 else 0.0), {"deaf": tuple(range(lo, lo + n))}
        yield f"S8 mute 150+30 lyrics off {d} no jump", T.track(late=lambda i, d=d: 2 * d if 150 <= i < 180 else 0.0), {"deaf": tuple(range(150, 180))}
    for lv in ((0, 0.8, 1.6, 2.4), (0, 1.0, 0, 1.0), (0, 1.0, 2.0, 1.0), (0, -1.0, 0, -1.0), (0, 0.6, 1.2, 1.8), (0, 1.0, 0.0, -1.0), (0, 1.0, 1.0 + 0.6, 1.0)):
        for cuts in ((100, 200, 300), (130, 260, 330), (60, 200, 340)):
            yield f"S9 three {lv} at {cuts}", T.track(late=lambda i, lv=lv, c=cuts: lv[sum(i >= x for x in c)]), {}
    for at in (40, 200, 360):
        for d in (1.0, -0.8):
            yield f"S10 rollup jump {d} at {at}", T.rolled_up(T.track(late=lambda i, at=at, d=d: d if i >= at else 0.0)), {}
    for lo, d in ((150, 0.8), (200, -0.8), (250, 1.2), (0, 0.8)):
        yield f"S11 sweep delay {d} from {lo}", T.track(), {"delay": {i: d for i in range(lo, T.LINES)}}
    for d in (1.0, -1.0):
        trk = T.track(late=lambda i, d=d: d if i >= 200 else 0.0)
        yield f"S12 tie at jump {d}", [((trk[200][0], trk[200][1], c[2]) if i == 199 else c) for i, c in enumerate(trk)], {}
        yield f"S12 tie after jump {d}, 200 deaf", [((trk[201][0], trk[201][1], c[2]) if i == 200 else c) for i, c in enumerate(trk)], {"deaf": (200,)}
    for d in (1.0, 2.0, 3.0):
        trk = T.track(late=lambda i, d=d: d if i >= 200 else 0.0, show=lambda i: 2.5)
        yield f"S13 b2b jump {d}, 200-202 deaf", trk, {"deaf": (200, 201, 202)}
        yield f"S13 b2b jump {d}", trk, {}
        yield f"S13 b2b jump -{d}, 198-199 deaf", T.track(late=lambda i, d=d: -d if i < 200 else 0.0, show=lambda i: 2.5), {"deaf": (198, 199)}


JUMPS = list(jump_shapes())


@pytest.mark.parametrize("name, trk, kw", JUMPS, ids=[c[0] for c in JUMPS])
def test_a_jump_shape(name, trk, kw):
    """trk through align.run() and the plan that remux.time_plan() writes from its moves. Whisper hears the whole file
    on the grid of subsync.whole_grid(), see T.hear(). It hears no word of the lines in kw "deaf", and each line in kw
    "delay" that much later. Each line's speech starts at T.AT. Only moves are judged here. A start counts in ms, as
    the plan writes it, so a line 0.49996 s off sits 0.5 s off before and after."""
    before = [round(a, 3) - x for (a, _, _), x in zip(trk, T.AT)]
    try:
        got = align.run(sorted(trk, key=lambda c: c[0]), T.hear(s.whole_grid(T.LENGTH), mute=kw.get("deaf", ()), delay=kw.get("delay", {})))
        starts = {n: m["start"] for n, m in got["moves"].items() if m["why"] != "end"}
        rows = remux.time_plan(trk, None, (), starts=starts) if starts else None
    except s.Broken:
        return gate(f"jump {name}", before, before, more={"broken"})
    gate(f"jump {name}", before, [r[3] - x for r, x in zip(rows, T.AT)] if rows else before, moved=bool(rows))
