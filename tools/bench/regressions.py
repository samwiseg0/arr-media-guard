# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The counts of a run of the regression corpus, tests/test_regressions.py, per failure kind, and the cases that
changed from a base run.

usage: regressions.py RECORDS.jsonl [--base RECORDS.jsonl] [--known FILE]

RECORDS comes from AMG_CORPUS_OUT=RECORDS.jsonl pytest tests/test_regressions.py. --known writes the known-failure
list of RECORDS to FILE, as tests/fixtures/known_failures.json holds it."""
import argparse
import json

KINDS = ("wrong_silence", "wrong_alert", "wrong_text", "wrong_move", "broken")


def load(path):
    return {r["id"]: r for r in map(json.loads, open(path))}


def counts(recs):
    out = {k: sum(k in r["kinds"] for r in recs.values()) for k in KINDS}
    out["wrong_silence_1s"] = sum("wrong_silence" in r["kinds"] and max(abs(x[3]) for x in r["runs"]) >= 1.0 for r in recs.values())
    return out


def line(name, recs):
    c = counts(recs)
    return (f"{name}: {len(recs)} cases, wrong_silence {c['wrong_silence']} ({c['wrong_silence_1s']} at 1.0 s or more), "
            f"wrong_alert {c['wrong_alert']}, wrong_text {c['wrong_text']}, wrong_move {c['wrong_move']}, broken {c['broken']}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("records"), ap.add_argument("--base"), ap.add_argument("--known")
    a = ap.parse_args(argv)
    recs = load(a.records)
    print(line(a.records, recs))
    if a.base:
        base = load(a.base)
        print(line(a.base, base))
        for k in KINDS:
            new = sorted(c for c, r in recs.items() if k in r["kinds"] and k not in base.get(c, {"kinds": ()})["kinds"])
            gone = sorted(c for c, r in base.items() if k in r["kinds"] and c in recs and k not in recs[c]["kinds"])
            for c in new:
                print(f"  + {k} {c}")
            for c in gone:
                print(f"  - {k} {c}")
        print("  cases only in one run:", sorted(set(recs) ^ set(base)) or "none")
    if a.known:
        with open(a.known, "w") as f:   # one case a line, so a diff names each case that joins or leaves the list
            f.write("{\n" + ",\n".join(f"{json.dumps(c)}: {json.dumps(r['kinds'])}" for c, r in sorted(recs.items()) if r["kinds"]) + "\n}\n")


if __name__ == "__main__":
    main()
