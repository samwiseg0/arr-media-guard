#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Synthetic timing cases: made-up dialogue, the words a hearing gives, and a subtitle track of one kind. See
tools/bench/README.md.

    python tools/bench/synth.py KIND --out CASE.json [--seed N] [--lines 400] [--set NAME=VALUE ...]

KIND is one of:
  right   cues on their speech, with author scatter: each cue starts a little late or early, sigma seconds (gauss)
  drift   a right track moved by a frame-rate ratio and an offset: start = rate * t + offset (rate 1001/1000, offset -0.3)
  step    a right track with the cues from share of the duration on moved by shift seconds (share 0.5, shift 1.2)
  block   a right track with the cues that start in [start, start + length) moved by shift (start 300, length 60, shift -1.5)
  scene   each scene of scene_lines lines sits off by its own lag, gauss with lag_sd seconds (lag_sd 0.45)
  live    live captions: each line shows lag seconds after its speech, lag uniform from lag_lo to lag_hi, until the
          next line shows (lag_lo 2, lag_hi 9)
  rollup  a right track as 3-line roll-up captions: each cue shows the two lines before its own, joined by \\N

The speech: line i starts at first + gap * i + pause * (i // scene_lines), one word every step seconds. The hearing
moves each line's words by up to noise seconds either way, as Whisper's word times do.

CASE.json holds "duration", "cues" [[start, end, text]] of the track, "words" [[time, word]] of the whole hearing,
"onsets" [[time, seconds of silence before it]] of each line's speech, "truth" [{"start", "word_start"}] per cue, and
"params". window(case, start, seconds) gives one heard window in the form of lid.listen(). Every word is made up."""
import argparse, bisect, json, random
from fractions import Fraction

WORDS = ("garden window bicycle pancake lantern river mountain rocket pillow marble ladder violin carpet thunder biscuit helmet puzzle "
         "orange shovel candle blanket kitten meadow pencil trumpet wagon basket feather hammer island jacket kettle lemon mirror noodle "
         "pepper quilt saddle tunnel umbrella velvet walnut yogurt zipper anchor button castle dragon engine forest glacier harbor").split()
NAMES, STOP = ["mira", "tobin", "juna", "pell"], "the a to and you it is of that we".split()
DEFAULTS = {"first": 60.0, "gap": 2.5, "pause": 5.0, "scene_lines": 12, "step": 0.3, "show": 2.2, "lead": 0.05, "noise": 0.2, "sigma": 0.15,
            "rate": "1001/1000", "offset": -0.3, "share": 0.5, "shift": 1.2, "start": 300.0, "length": 60.0, "lag_sd": 0.45, "lag_lo": 2.0, "lag_hi": 9.0}
KINDS = ("right", "drift", "step", "block", "scene", "live", "rollup")


def lines_of(seed, n):
    """n lines of made-up dialogue: a name, then content words with a stopword between some of them."""
    r, out = random.Random(seed), []
    for _ in range(n):
        ws = [r.choice(NAMES)]
        for _ in range(r.randint(3, 5)):
            ws += ([r.choice(STOP)] if r.random() < 0.4 else []) + [r.choice(WORDS)]
        out.append(" ".join(ws).capitalize() + ".")
    return out


def make(kind, seed=1, lines=400, **over):
    """The case dict of one KIND, see the module docstring. over sets any of DEFAULTS."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    p = dict(DEFAULTS, **over)
    r, text = random.Random(seed), lines_of(seed, lines)
    at = [p["first"] + p["gap"] * i + p["pause"] * (i // p["scene_lines"]) for i in range(lines)]   # where line i is spoken
    duration = at[-1] + 60
    err = [r.uniform(-p["noise"], p["noise"]) for _ in text]
    words = sorted((round(at[i] + p["lead"] + err[i] + p["step"] * k, 3), w) for i, x in enumerate(text) for k, w in enumerate(x.rstrip(".").split()))
    late = [r.gauss(0, p["sigma"]) for _ in text]
    if kind == "scene":
        lag = [r.gauss(0, p["lag_sd"]) for _ in range(lines // p["scene_lines"] + 1)]
        late = [lag[i // p["scene_lines"]] + r.gauss(0, p["sigma"]) for i in range(lines)]
    starts = [at[i] + p["lead"] + late[i] for i in range(lines)]
    if kind == "live":
        starts = [at[i] + r.uniform(p["lag_lo"], p["lag_hi"]) for i in range(lines)]
        for i in range(1, lines):   # captions keep their order
            starts[i] = max(starts[i], starts[i - 1] + 0.01)
        ends = starts[1:] + [starts[-1] + p["show"]]
    else:
        ends = [s + p["show"] for s in starts]
    cues = [(s, e, x) for s, e, x in zip(starts, ends, text)]
    if kind == "drift":
        rate = float(Fraction(p["rate"]))
        cues = [(rate * s + p["offset"], rate * e + p["offset"], x) for s, e, x in cues]
    elif kind == "step":
        cut = p["share"] * duration
        cues = [(s + p["shift"], e + p["shift"], x) if s >= cut else (s, e, x) for s, e, x in cues]
    elif kind == "block":
        cues = [(s + p["shift"], e + p["shift"], x) if p["start"] <= s < p["start"] + p["length"] else (s, e, x) for s, e, x in cues]
    elif kind == "rollup":
        cues = [(s, e, "\\N".join(text[max(0, i - 2):i + 1])) for i, (s, e, _) in enumerate(cues)]
    quiet = [at[i] - (at[i - 1] + p["step"] * len(text[i - 1].split())) if i else at[0] for i in range(lines)]
    return {"kind": kind, "seed": seed, "params": p, "duration": round(duration, 3),
            "cues": [[round(s, 3), round(e, 3), x] for s, e, x in cues], "words": [[t, w] for t, w in words],
            "onsets": [[round(at[i] + p["lead"], 3), round(quiet[i], 3)] for i in range(lines)],
            "truth": [{"start": round(c[0], 3), "word_start": round(at[i] + p["lead"], 3)} for i, c in enumerate(cues)]}


def window(case, start, secs=10.0):
    """One heard window of case, as lid.listen() gives it: {"at", "secs", "words": [[seconds from start, word]]}."""
    ts = [w[0] for w in case["words"]]
    got = case["words"][bisect.bisect_left(ts, start):bisect.bisect_left(ts, start + secs)]
    return {"at": start, "secs": secs, "words": [[round(t - start, 2), w] for t, w in got]}


def main():
    ap = argparse.ArgumentParser(description="Write one synthetic timing case.")
    ap.add_argument("kind", choices=KINDS), ap.add_argument("--out", required=True), ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--lines", type=int, default=400), ap.add_argument("--set", nargs="*", default=[], metavar="NAME=VALUE")
    a = ap.parse_args()
    over = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        if k not in DEFAULTS:
            ap.error(f"unknown parameter {k}")
        over[k] = v if isinstance(DEFAULTS[k], str) else type(DEFAULTS[k])(v)
    json.dump(make(a.kind, a.seed, a.lines, **over), open(a.out, "w"))


if __name__ == "__main__":
    main()
