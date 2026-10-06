#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Score the plan of a replay against a truth file. See tools/bench/README.md.

    python tools/bench/score.py RESULT.json TRUTH.jsonl [--track s1] [--within 0.5]

RESULT is the output of replay.py, or of synth.py, whose "cues" list per track [the cue's start in the file, its start
after the plant, its start in the plan]. TRUTH holds one JSON object per cue with "start", the cue's start in the file,
and "word_start", the time its first word is spoken, or null when that is not known. A cue's error is its start minus
its word start. The score pairs each cue with a truth row of the same start, within 11 ms, and reports both kinds
of error: the cues within --within seconds of their speech before and after, the cues the plan put right (fixed),
and the cues it put wrong (a wrong edit: within before, outside after). Each count comes per 1,000 scored cues too."""
import argparse, json, statistics


def stats(xs, within):
    m = statistics.median(xs)
    return {"median": round(m, 3), "mad": round(statistics.median(abs(x - m) for x in xs), 3), "within": sum(abs(x) <= within for x in xs), "n": len(xs)}


def score(cues, truth, within=0.5):
    """{"before", "after", "fixed", "wrong", "moved", "per_1000", "unpaired"} of cues [[start, planted, planned]] against
    truth rows [{"start", "word_start"}]."""
    ts, j, pairs, unpaired = sorted(truth, key=lambda t: t["start"]), 0, [], 0
    for start, planted, planned in sorted(cues):   # both in time order, so cues that start together pair in order
        while j < len(ts) and ts[j]["start"] < start - 0.011:
            j += 1
        if j < len(ts) and abs(ts[j]["start"] - start) <= 0.011:
            if ts[j].get("word_start") is not None:
                pairs.append((planted - ts[j]["word_start"], planned - ts[j]["word_start"]))
            j += 1
        else:
            unpaired += 1
    if not pairs:
        return {"n": 0, "unpaired": unpaired}
    before, after = [b for b, _ in pairs], [x for _, x in pairs]
    out = {"before": stats(before, within), "after": stats(after, within), "fixed": sum(abs(x) <= within < abs(b) for b, x in pairs),
           "wrong": sum(abs(b) <= within < abs(x) for b, x in pairs), "moved": sum(abs(x - b) > 0.005 for b, x in pairs), "unpaired": unpaired}
    out["per_1000"] = {k: round(1000 * out[k] / len(pairs), 1) for k in ("fixed", "wrong", "moved")}
    return out


def main():
    ap = argparse.ArgumentParser(description="Score the plan of a replay against a truth file.")
    ap.add_argument("result"), ap.add_argument("truth"), ap.add_argument("--track", default="s1"), ap.add_argument("--within", type=float, default=0.5)
    a = ap.parse_args()
    r = json.load(open(a.result))
    if a.track not in r.get("cues", {}):
        ap.error(f"the result holds no cues of {a.track}: {sorted(r.get('cues', {}))}")
    truth = [json.loads(x) for x in open(a.truth) if x.strip()]
    print(json.dumps(score(r["cues"][a.track], truth, a.within)))


if __name__ == "__main__":
    main()
