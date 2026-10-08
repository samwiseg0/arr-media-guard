# Timing bench

Tools to test a change to the subtitle timing check on real files in minutes, without hearing the audio again.
A *word cache* is the SQLite file that `lid.py` keeps (`STATE_DIR/lid.sqlite` of an install): the words Whisper heard in
each clip, keyed by the file's path, size and mtime, the audio stream, the model, the language and the clip's windows.

## replay.py: the timing path of one file

```
python tools/bench/replay.py VIDEO --cache WORDS.sqlite --out RESULT.json [--plant SPEC] [--original English]
```

It runs `--sub-time` of this checkout on VIDEO as a dry run, with the words of the cache. The file never changes.
A window the cache lacks hears nothing, and RESULT counts it under `words.missing`. With `--hear --lid-dir DIR`, the
replay runs `lid.py` for those windows and adds them to the cache, so the next replay of any commit finds them.
`--code DIR` replays another tree, for example a `git archive` of a commit.

`--plant` moves the cues in memory before the check reads them, so one file gives many cases:
`+2.0` or `-1.5` (every cue), `step:0.6:-0.9` (the cues from 60% of the duration on), `blk:300:60:-1.5` (the cues that
start from 300 s to 360 s), `rate:1001/1000` (every time). RESULT holds the result line, the alerts, each track's
verdict and fix, and per track the cues as `[start in the file, start after the plant, start in the plan]`.

## score.py: both kinds of error

```
python tools/bench/score.py RESULT.json TRUTH.jsonl [--track s1]
```

TRUTH holds one JSON object per cue: `{"start": its start in the file, "word_start": when its first word is spoken}`.
A cue is right when it starts within 0.5 s of its first word. The score counts the cues right before and after the
plan, the cues the plan put right (`fixed`), and the cues it put wrong (`wrong`, a wrong edit). Each count also comes
per 1,000 cues. Report both kinds: a gain in right cues hides nothing only when the wrong edits stay near zero.

## synth.py: synthetic cases

```
python tools/bench/synth.py KIND --out CASE.json [--seed N] [--set shift=1.2 ...]
```

KIND is `right`, `drift`, `step`, `block`, `scene` (each scene off by its own lag), `live` (live captions) or `rollup`
(3-line roll-up captions). CASE holds the track's cues, the words a hearing gives with Whisper-like time noise, the
speech onsets and a truth row per cue, all made up. `synth.window(case, start, seconds)` gives one heard window in the
form of `lid.listen()`, for a harness that calls the timing functions of `subsync.py` directly.

## The regression corpus

`tests/test_regressions.py` runs every planted shape that a review of the timing check found, on every commit: blocks
and steps through an import and its deep analysis (with a failed write, `DISCORD_POSTS=all` and a dry run), unsure
word-check windows, held import alerts, and the jump finder's hostile shapes. Each case knows the lines it planted off
and scores the outcome in kinds: `wrong_silence` (3 or more lines in a row 0.5 s or more off, and no alert),
`wrong_alert` (an alert, and no such lines), `wrong_text` ("left as they are" after a move, "0.0 s", or the moved
lines named twice), `wrong_move` (a line within 0.5 s of its speech moved 0.5 s or more off) and `broken` (a safety
rule broke). `tests/fixtures/known_failures.json` lists the kinds each case fails today. A case passes only when its
kinds match the list, so a new failure fails, and so does a case that stops failing until its entry leaves the list.

```
AMG_CORPUS_OUT=new.jsonl pytest -n 2 tests/test_regressions.py
python tools/bench/regressions.py new.jsonl [--base old.jsonl] [--known tests/fixtures/known_failures.json]
```

`regressions.py` counts the cases per kind, names each case that gained or lost a kind against `--base`, and with
`--known` writes the list from the run. Write it only when each change in it is the one the commit means to make.

The tests of these tools are in `tests/test_bench.py`.
