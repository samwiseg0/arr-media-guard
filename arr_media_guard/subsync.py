#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The subtitle match check of arr-media-guard (docs/design.md, "Subtitle match"). Stdlib only.

A text subtitle belongs to the audio when the words Whisper hears in the audio are the words of the cues at the same
time. lid.listen() hears two short windows of the audio. This module compares those words with the cues.

- windows() picks one early and one late window where the cues are dense, so the windows hold speech. They lie near
  the ends of the file, so a drift shows.
- A window's overlap is the share of its heard content words that match cue words in order, near one offset. The
  search tries the offsets that the shared words point to, over the whole cue list. So a shifted or drifting track
  still matches.
- check() gives match, mismatch or unknown. Two windows must hold MIN_WORDS heard content words, and they must agree.
  When one window holds too few, the caller hears a third window in the same third of the file, see short().
- timing() judges the drift by the error it leaves at the file's start and end. It fits one offset and one frame-rate
  ratio. A ratio other than 1 needs a middle window that confirms it, so a cut between the windows never reads as a
  drift.
- layout() checks a subtitle that no word check reads by where its lines show: over the spans of speech that Silero
  VAD found in the audio, see voiced().
- whole_starts() plans the windows of the whole-file hearing on whole_grid(), less the windows heard already. align.py
  times every line against those words, and TIMING holds its thresholds.
"""
import bisect
import collections
import difflib
import itertools
import json
import math
import os
import re
import statistics
import tempfile
from fractions import Fraction

from . import decide

# The thresholds leave a wide gap between a right track, which matches most heard words of a window, and a wrong
# track, which matches few, mostly names (docs/design.md, "Subtitle match").
WINDOW = 10.0     # seconds of each window. Both go through Whisper as one clip of 20 seconds, so the encoder runs once. On
                  # dense dialogue two windows of 12 seconds cost more to decode, and two of 15 made the encoder run twice.
EDGE = 0.05       # the share of the duration at each end that no window takes: intro songs and credits
# Where the windows lie, as shares of the duration. The early and the late window sit near the ends, so the line
# through them covers most of the file.
PARTS = ((EDGE, 0.25), (0.75, 1 - EDGE))
MIDDLE = 0.1      # the middle window, which confirms a ratio, lies within this share of the span between the early and the
                  # late window at each side of its centre. A cut anywhere between them then puts it 0.4 of the cut off their line.
MIN_WORDS = 8     # heard content words a window needs, else the check names nothing. A wrong track's window may hold little speech.
THIRD = 24.0      # seconds of the third window, heard when one window heard too little. It is still one clip and one encoder run.
LOOP = 4          # words of the longest phrase whose repeats count once: Whisper loops on a word or a short phrase
MIN_TRACK_CUES = 20   # a track with fewer cues gets no verdict: a read that failed, or a track of signs
MATCH = 0.5       # a window at or above this overlap matches
MISMATCH = 0.3    # a window at or below this overlap does not match
PAD = 3.0         # seconds the cue side of a window reaches past the heard words at each end. A drift of 25/23.976 moves 0.5 s in a window.
BIN = 1.0         # seconds of one offset bin in the search
TRIES = 5         # the offset bins with the most shared words that the search compares in full
ANCHORS = 50      # places in the cue list one heard word may point to
# The timing thresholds, each defined once (docs/development.md, "How to add a check"). A stage reads a value through
# the name of its meaning, never through a number of its own. Values are seconds, except the counts, costs and shares
# that say so.
TIMING = {
    "in line": 0.3,      # lines this close to their line sit on it, which a fix may leave
    "move": 0.5,         # a segment of the whole-file timing moves from this far off when a second clock agrees, or when
                         # it holds "whole anchors"
    "move alone": 1.0,   # a segment of "alone anchors" moves from this far off on Whisper alone
    "off": 0.75,         # lines this far off need a fix
    "alert": 0.75,       # heard evidence this far off after the moves posts: a word-check window, or a stretch of the
                         # whole-file timing
    "alert rows": 1.0,   # the slices of a reference this far apart read as steps
    "dense run": 10,     # anchored lines of one stretch that must sit "alert" off to post. Shorter runs posted the scene
                         # lag of real tracks in time
    "in sync": 0.05,     # an offset under this reads "in sync" in an alert
    # The whole-file timing of align.py. A count of anchors or lines, a cost and a share say so.
    "speech lead": -0.03,   # a right track's line starts this long after its first spoken word, the median
    "spoken gap": 1.0,      # seconds between two heard words under which an anchor steps back over a leading stopword
    "short line": 0.05,     # a line shown under this never anchors. A line of 1 ms that repeated a neighbour's text
                            # paired with that neighbour's speech, 3 s off.
    "word run": 2,          # content words in a row that match before a pair of the alignment counts
    "fit cap": 1.0,         # an anchor this far or farther from the curve costs the same in the fit: an outlier
    "fit change": 5.0,      # the cost of a change point of the curve, in units of "fit cap"
    "fine change": 1.0,     # the same in the second pass, which finds local blocks against the first pass's curve
    "fit rate": 2.0,        # the extra cost, in units of "fit cap", to enter a segment at a frame-rate ratio other than 1
    "fit step": 0.02,       # one level of the fit's grid
    "fit anchors": 10,      # anchored lines a track needs before any line moves
    "judge anchors": 20,    # anchored lines a track needs before its times are judged
    "whole anchors": 30,    # a segment of this many anchors moves from "move" off on Whisper alone, with every line
                            # of its span
    "alone anchors": 6,     # anchors a smaller segment needs to move at "move alone" on Whisper alone
    "mid anchors": 15,      # anchors a smaller segment needs to move at "off" on Whisper alone
    "tight anchors": 3,     # anchors a smaller segment needs to move at "tight move" on Whisper alone, when its anchors
    "tight move": 2.0,      # sit within "tight spread" of its line: a jump near a file end. An author's scatter rarely
    "tight spread": 0.15,   # puts 3 lines that far off together.
    "post spread": 0.4,     # the same spread for a stretch that posts. A post edits nothing, so it takes looser evidence.
    "block most": 10.0,     # a segment under "whole anchors" moves this far at most. Lines farther off their neighbours
                            # are more likely lines the audio does not hold, as a recap or another cut.
    "line reach": 15.0,     # an unanchored line this close to its segment's anchors on each side moves with it
    "onset around": 1.0,    # an anchored line with one onset this close times the offset between the two clocks
    "onset lines": 10,      # lines that time that offset, at least, before the onsets vote
    "onset share": 0.2,     # the share of a segment's lines with an onset where it puts them, at least, when onsets agree
    "shown": 0.5,           # two line starts sit this far apart after a move, unless they sat closer. A line shows this
                            # long at least, unless it showed less or the next line starts sooner.
    "pile": 1.0,            # a line cut more than this short of its move stays, so later lines never pile up behind it
    "near gap": 0.1,        # a line that ended this close before the next one keeps the same small gap after a move
    "touch": 0.02,          # a line that ends this little past the next line's start runs up to it. It never counts as
                            # two lines shown at once.
    "end hold": 7.0,        # such a line shows this long at most, or its old length when that is longer
    "live scatter": 0.6,    # anchors this far from the curve at their median make the track live captions: each line
                            # moves onto its own speech
}
ALERT, ALERT_ROWS, IN_SYNC = TIMING["alert"], TIMING["alert rows"], TIMING["in sync"]
# The timing. A cue starts when its first words are spoken. The subtitle's author and Whisper's word times both add
# spread, so on a right track some cues sit more than TOLERANCE off, and its windows differ a little.
CUE_LEAD = -0.05  # seconds a right track's cues start after the first heard word, the median over right tracks. A fix keeps it.
TOLERANCE = TIMING["in line"]   # seconds a fix may leave at the file's start and end, and at a middle window
AGREE = 0.8       # the share of matched cues whose heard words must fall in the fixed span
MIN_CUES = 3      # cues each window needs whose first content word matched, before a fix
MIN_SHIFT = TIMING["off"]  # seconds a cue at the file's start or end may sit off before the times need a fix. Right tracks sat closer,
                  # and a 24/23.976 drift of an 18-minute episode sat farther.
UNSURE_GAP = 0.8  # seconds between the two offsets of a word-check window, see unsure_window(). Real off windows whose
                  # words sat in time and whose cue starts sat off had them up to 0.69 s apart. A right window with two
                  # cues shown early over a sound had them 0.95 s apart.
# The frame-rate ratios of a subtitle timed for another video: 25 against 23.976 (24000/1001), 25 against 24, and 24
# against 23.976. 1 is a plain offset.
RATES = (Fraction(1), Fraction(25025, 24000), Fraction(24000, 25025), Fraction(25, 24), Fraction(24, 25), Fraction(1001, 1000),
         Fraction(1000, 1001))
MUSIC = re.compile(r"[♪♫#]")   # a cue of song lyrics, tested after the tags are gone: a font colour holds a #
NOISE = re.compile(r"<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|\\[Nnh]")   # tags, ASS blocks, sounds in brackets, ASS breaks
TOKEN = re.compile(r"[^\W_]+")


def words(text, stop=frozenset()):
    """The content words of a cue or of heard speech: lower case, with no tags, no sounds in brackets, no apostrophes,
    no stopword of the language and no one-letter word. Whisper writes "don't" and a subtitle may write "don’t"."""
    t = NOISE.sub(" ", text).lower().replace("’", "").replace("'", "")
    return [w for w in TOKEN.findall(t) if len(w) > 1 and w not in stop]


def flat(cues, stop=frozenset()):
    """[(time, cue index, word, its place in the cue from 0)] of the content words of cues [(start, end, text)], in time
    order. A word's time is its place in the cue's span, which is only a guess, because speech often ends before the
    cue does. The search uses it. The timing uses the cue starts only, see timing()."""
    out = []
    for i, (s, e, text) in enumerate(cues):
        ws = words(text, stop)
        out += [(s + (k + 0.5) / len(ws) * (e - s), i, w, k) for k, w in enumerate(ws)]
    out.sort()
    return out


def word_times(cues, stop=frozenset()):
    """The times of the content words of cues, see flat(), in order. Song lyrics never count, because Whisper hears
    singing badly. windows() picks its windows by them."""
    return [x[0] for x in flat([c for c in cues if not MUSIC.search(NOISE.sub(" ", c[2]))], stop)]


def windows(cues, duration, stop=frozenset(), have=(), secs=WINDOW, taken=(), parts=PARTS, ts=None):
    """[a start in seconds per part], or [] when a part of the file holds no cue. parts are shares of the duration,
    PARTS by default: one early and one late window near the ends of the file, for the drift. In each part it takes the
    window of secs that holds the most cue content words, see word_times(). ts is word_times() of cues when the caller
    has it, so a caller that picks a window in each minute builds it once.
    have lists (start, seconds) of audio that is decoded already, the language check's samples. A window inside one of
    them wins when it holds 3/4 of the best count, so that audio is not decoded again. taken lists windows heard already.
    A window that overlaps one never comes back, and a part with no other place gives None, so the caller can pick a
    third window in the same part."""
    ts = word_times(cues, stop) if ts is None else ts
    count = lambda s: bisect.bisect_left(ts, s + secs) - bisect.bisect_left(ts, s)
    free = lambda s: all(s + secs <= a or s >= a + WINDOW for a in taken)
    out = []
    for lo, hi in ((a * duration, b * duration) for a, b in parts):
        inner = ts[bisect.bisect_left(ts, lo):bisect.bisect_right(ts, hi - secs)]   # the words of the part, in order
        starts = [s for s in (max(lo, t - 0.5) for t in inner) if free(s)]   # half a second before a word
        if not starts:
            if not taken:
                return []
            out.append(None)
            continue
        best = max(starts, key=count)
        inside = [a + k for a, n in have for k in range(int(n - secs) + 1) if lo <= a + k <= hi - secs and free(a + k)]
        kept = max(inside, key=count, default=None)
        out.append(round(kept if kept is not None and count(kept) >= 0.75 * count(best) else best, 1))
    return out


def said(window, stop):
    """[(audio time, word)] of the content words of one heard window. A phrase of up to LOOP words said again and again
    right after itself counts once, because Whisper can loop on a word or a short line, and a loop must never read as a
    mismatch. A chant the subtitle holds counts once too. That only shrinks the heard side, and the rest still matches."""
    out = []
    for t, x in window["words"]:
        for w in words(x, stop):
            out.append((window["at"] + t, w))
            for n in range(1, LOOP + 1):   # the last n words repeat the n before them: drop the repeat
                if len(out) >= 2 * n and [v for _, v in out[-n:]] == [v for _, v in out[-2 * n:-n]]:
                    del out[-n:]
                    break
    return out


def short(heard, lang):
    """The places in heard, lid.listen()'s windows, of the windows with under MIN_WORDS content words."""
    stop = decide.STOPWORDS.get(lang, frozenset())
    return [k for k, w in enumerate(heard) if len(said(w, stop)) < MIN_WORDS]


def match_window(heard, fl, index):
    """The best alignment of one window. heard is [(audio time, word)] of its content words, fl is flat() of the cues
    and index maps a word to its places in fl. Each heard word that the cues hold points to an offset, cue time minus
    heard time. The TRIES offset bins with the most words are compared in full: the heard words against the cue words
    from the window's start to its end at that offset, PAD wider, in order (difflib). The offset with the most matched
    words wins, the smaller one on a tie. Returns {"overlap": matched share of the heard words, "words", "offset": the
    median of cue time minus heard time over the matched words, "pairs": [(audio time, fl item)]}."""
    said = [w for _, w in heard]
    best = (0, 0.0, [])
    if said:
        ts, bins = [x[0] for x in fl], collections.Counter()
        for t, w in heard:
            for p in index.get(w, ())[:ANCHORS]:
                bins[round((fl[p][0] - t) / BIN)] += 1
        for b, _ in bins.most_common(TRIES):
            d = b * BIN
            part = fl[bisect.bisect_left(ts, heard[0][0] + d - PAD):bisect.bisect_right(ts, heard[-1][0] + d + PAD)]
            sm = difflib.SequenceMatcher(None, said, [x[2] for x in part], autojunk=False)
            pairs = [(heard[i + k][0], part[j + k]) for i, j, n in sm.get_matching_blocks() for k in range(n)]
            if (len(pairs), -abs(d)) > (len(best[2]), -abs(best[1])):
                best = (len(pairs), d, pairs)
    pairs = best[2]
    return {"overlap": round(len(pairs) / len(said), 3) if said else 0.0, "words": len(said),
            "offset": round(statistics.median(x[0] - t for t, x in pairs), 3) if pairs else None, "pairs": pairs}


def check(heard, cues, lang, duration, gain=True):
    """Whether the subtitle cues belong to the audio. heard is lid.listen()'s windows, [{"at": start, "words":
    [[seconds from start, word], ...]}]. cues is [(start, end, text)] in seconds, lang the subtitle's 639-2 language,
    whose stopwords drop out, and duration the file's in seconds. gain goes to timing().

    Returns {"verdict": "match", "mismatch" or "unknown", "why", "windows": [{"at", "words", "overlap", "offset",
    "cues", "late", and "secs" for a longer window}], "timing": timing() of a match, else None}. late is the median cue
    start less heard time of the window's anchors, see anchors(), less CUE_LEAD, or None under
    MIN_CUES anchors. A short phrase said again right after itself counts once, see said(). A window inside a longer
    window heard later is the same audio, and the longer one stands for it. A window with under MIN_WORDS heard content
    words names nothing, and the verdict needs two windows that do. All of those at MATCH or more is a match. All at
    MISMATCH or less is a mismatch. Anything else is unknown. A track under MIN_TRACK_CUES cues is unknown too."""
    stop = decide.STOPWORDS.get(lang, frozenset())
    cues = spoken(unflashed(sorted(cues)))   # the ends of a flash track say nothing, see flash(), so its spans take the new ends
    if len(cues) < MIN_TRACK_CUES:
        return {"verdict": "unknown", "why": f"the track holds {len(cues)} cues, under {MIN_TRACK_CUES}", "windows": [], "timing": None}
    fl = flat(cues, stop)
    index = collections.defaultdict(list)
    for p, x in enumerate(fl):
        index[x[2]].append(p)
    end = lambda w: w["at"] + w.get("secs", WINDOW)
    heard = [w for w in heard if not any(x.get("secs", WINDOW) > w.get("secs", WINDOW) and x["at"] <= w["at"] and end(w) <= end(x) for x in heard)]
    got = [dict(match_window(said(w, stop), fl, index), at=w["at"], secs=w.get("secs", WINDOW)) for w in heard]
    late = lambda e: round(statistics.median(c - t for t, c in e) - CUE_LEAD, 2) if len(e) >= MIN_CUES else None
    out = {"windows": [{"at": w["at"], "words": g["words"], "overlap": g["overlap"], "offset": g["offset"], "cues": len({x[1] for _, x in g["pairs"]}),
                        "late": late(anchors(g["pairs"], cues)), **({"secs": w["secs"]} if w.get("secs", WINDOW) != WINDOW else {})}
                       for w, g in zip(heard, got)], "timing": None}
    few = sum(g["words"] < MIN_WORDS for g in got)
    got = sorted((g for g in got if g["words"] >= MIN_WORDS), key=lambda g: g["at"])   # in time order, for the ratio
    lap = ", ".join(f'{g["overlap"]:.0%}' for g in got)
    if len(got) < 2:
        return dict(out, verdict="unknown", why=f"{few} of {len(got) + few} windows hold under {MIN_WORDS} heard words")
    if all(g["overlap"] >= MATCH for g in got):
        return dict(out, verdict="match", why=f"the heard words match the cues at {lap}", timing=timing(got, cues, duration, gain))
    if all(g["overlap"] <= MISMATCH for g in got):
        return dict(out, verdict="mismatch", why=f"the heard words match the cues at {lap} at best")
    return dict(out, verdict="unknown", why=f"the heard words match the cues at {lap}, between {MISMATCH:.0%} and {MATCH:.0%} or apart")


LINE_PARTS = ((0.28, 0.38), (0.62, 0.72))   # the parts of the file, as windows() takes them, of the windows that confirm a fix
LINE_SHIFT = 1.5  # seconds a fix moves the cues at both file ends, under which it needs the windows of LINE_PARTS


def needs_line(fix, duration):
    """A fix that two windows alone must not carry: one that moves the cues under LINE_SHIFT at both file ends. A short
    patch of a right track that sits early or late can put one window off, and a line through two windows then passes
    for a small shift or a slight ratio. A fix that moves the cues more, as a frame-rate drift does, keeps the rules of
    fit(), which hold on real drifts."""
    return bool(fix) and all(abs(moved(t * 1000, fix) / 1000 - t) < LINE_SHIFT for t in (0.0, duration))


def on_line(heard, cues, lang, fix, parts):
    """Whether each part of parts, [(window start, the start of its longer window or None)], holds a heard window with
    MIN_CUES anchors, see anchors(), whose median sits within TOLERANCE of the line of fix, as fit() judges its windows.
    The longer window counts when the window heard too little. A part with no such window confirms nothing, so a
    window too thin to judge never lets a fix through."""
    stop, cues = decide.STOPWORDS.get(lang, frozenset()), spoken(unflashed(sorted(cues)))
    fl, index = flat(cues, stop), collections.defaultdict(list)
    for p, x in enumerate(fl):
        index[x[2]].append(p)
    r, o, got = float(Fraction(fix["rate"])), fix["offset"], {w["at"]: w for w in heard}
    held = lambda a: anchors(match_window(said(got[a], stop), fl, index)["pairs"], cues) if a in got else []
    for a, b in parts:
        e = held(a) if len(held(a)) >= MIN_CUES or b is None else held(b)
        if len(e) < MIN_CUES or abs(statistics.median(c - r * t for t, c in e) - CUE_LEAD * r - o) > TOLERANCE:
            return False
    return True


def anchors(pairs, cues):
    """[(heard time, cue start)] of the matched cues whose first content word matched, one pair each. A cue starts when
    its first words are spoken, so these pairs measure the offset without the guess of flat(). A later word of the cue
    would add the time of the words before it."""
    out = {}
    for t, x in sorted(pairs):
        if x[3] == 0 and x[1] not in out:
            out[x[1]] = (t, cues[x[1]][0])
    return list(out.values())


def agree(pairs, cues, rate, offset):
    """The share of the matched cues whose heard words fall in the cue's span after the fix, TOLERANCE wider. A cue's
    heard words count by their median, so one word the search paired with a neighbour cue does not decide."""
    by = collections.defaultdict(list)
    for t, x in pairs:
        by[x[1]].append(t)
    r = float(rate)
    ok = sum((cues[i][0] - offset) / r - TOLERANCE <= statistics.median(ts) <= (cues[i][1] - offset) / r + TOLERANCE for i, ts in by.items())
    return ok / len(by) if by else 0.0


def shares(pairs, cues, fix):
    """(the share of the matched cues in their spans as the cues are, the share after fix), see agree(). Both keep
    CUE_LEAD. A fix must raise the share, see timing()."""
    r = Fraction(fix["rate"])
    return agree(pairs, cues, Fraction(1), CUE_LEAD), agree(pairs, cues, r, fix["offset"] + CUE_LEAD * float(r))


def timing(got, cues, duration, gain=True):
    """fit() of the windows of got with MIN_CUES anchors or more, see anchors(). A window with fewer names no offset.
    When the other windows leave the fit short of evidence, "few" lists the start of each such window, for a longer
    window. A fix holds "spans", see shares(). With gain, a fix must put more matched cues in their spans than the
    cues have as they are, else the times stay with "unfixed". A right track whose line ran 7 ms past MIN_SHIFT at the
    file's start got a fix of 1001/1000, which moved 107 lines in time out of half a second. --sub-time and the deep
    analysis pass gain False, as the whole-file timing takes the place of the fix, see subtitles.sub_whole(). When the
    windows fit no ratio and some are unsure, see unsure_window(), the fit runs again without them. When that fit gives
    no fix and waits for no middle window, "unsure" holds the starts of the unsure windows in "at" and that fit in
    "timing". The steps stand."""
    ends = [anchors(g["pairs"], cues) for g in got]
    few = [g.get("at") for g, e in zip(got, ends) if len(e) < MIN_CUES]
    pairs = [p for g in got for p in g["pairs"]]
    kept = [(g, e) for g, e in zip(got, ends) if len(e) >= MIN_CUES]
    r = fit([g for g, _ in kept], [e for _, e in kept], pairs, cues, duration)
    sure = [(g, e) for g, e in kept if not unsure_window(g, e)]
    if r.get("piecewise") and len(sure) < len(kept):
        s = fit([g for g, _ in sure], [e for _, e in sure], pairs, cues, duration)
        if not (s["fix"] or s.get("confirm")):   # a fix there would move the cues of an unsure window off its words
            r = dict(r, unsure={"at": [g.get("at") for g, e in kept if unsure_window(g, e)], "timing": s})
    if r["fix"]:
        was, now = shares(pairs, cues, r["fix"])
        r = dict(r, spans=[round(was, 3), round(now, 3)])
        if gain and now <= was:
            r = {"fix": None, "unfixed": r["fix"]["offset"], "spans": r["spans"], "why": f'{r["why"]}, but {was:.0%} sit in them as they are, so the times stay'}
    return dict(r, few=few) if few and not (r["fix"] or r.get("piecewise") or "unfixed" in r or r["why"] == "in time") else r


def unsure_window(g, e):
    """Whether word-check window g, of match_window(), with anchors e, see anchors(), is unsure. Its two offsets then
    disagree on whether it sits off. Its heard words, g's "offset", put it under MIN_SHIFT off. Its cue starts, its
    "late" in check(), put it MIN_SHIFT or more off, and the two lie UNSURE_GAP or more apart. Two cues shown a second
    early over a sound before their words move the cue starts, and the words still sit in time. timing() names such a
    window, and only the whole-file timing judges its lines."""
    late = round(statistics.median(c - t for t, c in e) - CUE_LEAD, 2)
    return g.get("offset") is not None and abs(g["offset"]) < MIN_SHIFT <= abs(late) and abs(g["offset"] - late) >= UNSURE_GAP


def fit(got, ends, pairs, cues, duration, lead=CUE_LEAD, unit="window"):
    """The fix of a matched track's times, from match_window() of its windows in time order, their anchors ends and
    the matched pairs of all windows. Returns {"fix": {"rate": "p/q", "offset": seconds} or None, "why"}.
    "piecewise" marks windows that no ratio explains, a different cut. "unfixed" holds the offset of a track that is
    off and gets no fix. "confirm" asks the caller for a middle window, see below and middle(). The fix maps audio
    time to cue time as cue = rate * audio + offset, so a cue moves to (cue - offset) / rate. A cue keeps CUE_LEAD
    after its first word, as on a right track.

    Each window's median offset at a ratio gives a point, and the line through the first and the last point says
    where the cues sit at the file's start and end. The track is in time when at ratio 1 the cues there, and at
    every window, sit under MIN_SHIFT off. So the drift is judged by the error it causes at the file's ends.

    A ratio fits when three things hold. Every window's median lies within TOLERANCE of one offset. A middle window
    lies within TOLERANCE of the line through the first and the last. That line stays under MIN_SHIFT from the
    offset at the file's ends. A cut of a second between two windows can put them all within TOLERANCE of one offset
    at a ratio near 1, but the middle window then sits off the line. The windows of a right track differ a little,
    and a line through them moves that noise out to the ends, so the fit takes the windows themselves. No ratio that
    fits is a different cut, or a track too messy to fix.

    The ratio with the least error at the file's ends wins, and a plain offset wins a near tie. A ratio other than 1
    needs a middle window, see middle(), because windows near the ends cannot tell a drift from a cut between them.
    Two ratios that fit, a plain offset among them, and move the cues apart give no fix. AGREE of the matched cues must fall in their spans
    after the fix. lead is CUE_LEAD, and 0 for the parts of reference() that pair cue starts with cue starts. unit names a
    window or a part in the why."""
    if len(got) < 2:
        return {"fix": None, "why": f"under two windows hold {MIN_CUES} cues whose first words matched"}
    at = [statistics.median(t for t, _ in e) for e in ends]   # each window's place in the audio
    if not at[0] < duration / 2 < at[-1]:   # a later hearing can leave windows in one half, and a cut in the other goes unseen
        return {"fix": None, "why": "the windows that heard enough lie in one half of the file"}

    def line(r):   # (each window's median at ratio r, where the line through the first and the last sits at the file's
        # start and end, the largest miss of a middle window from that line)
        m = [statistics.median(c - float(r) * t for t, c in e) for e in ends]
        slope = (m[-1] - m[0]) / (at[-1] - at[0])
        on = lambda t: m[0] + slope * (t - at[0])
        return m, on(0), on(duration), max((abs(m[k] - on(at[k])) for k in range(1, len(m) - 1)), default=0.0)
    _, a0, a1, _ = line(1)
    d = [round(statistics.median(c - t for t, c in e) - lead, 2) for e in ends]
    if max(abs(a0 - lead), abs(a1 - lead), *(abs(x) for x in d)) < MIN_SHIFT:
        return {"fix": None, "why": "in time", "offset": round((a0 + a1) / 2 - lead, 3)}
    fits = {}
    for r in RATES:   # every window within TOLERANCE of one offset and of the line through the others, and the ends under MIN_SHIFT
        m, b0, b1, miss = line(r)
        mid, half = (max(m) + min(m)) / 2, (max(m) - min(m)) / 2
        if half <= TOLERANCE and miss <= TOLERANCE and max(abs(b0 - mid), abs(b1 - mid)) < MIN_SHIFT:
            fits[r] = (round(mid - lead * float(r), 3), max(abs(b0 - mid), abs(b1 - mid)))
    span = "".join(f", {x:+.2f} s" for x in d[1:-1]) + f" and {d[-1]:+.2f} s late"
    if not fits:
        return {"fix": None, "piecewise": True, "offsets": d,
                "why": f"the cues are off by {d[0]:+.2f} s early{span}, which no frame-rate ratio explains"}
    rate = min(fits, key=lambda r: fits[r][1])   # the least error at the file's ends, and a plain offset on a near tie
    rate = Fraction(1) if Fraction(1) in fits and fits[Fraction(1)][1] <= fits[rate][1] + TOLERANCE / 2 else rate
    to = lambda r, t: (t - fits[r][0]) / float(r)   # where a cue at t moves
    offset, name = fits[rate][0], f"{rate.numerator}/{rate.denominator}"
    if rate == 1 and abs(offset) < MIN_SHIFT:
        # A plain shift under MIN_SHIFT: every window sits in time, and only the line through two of them ran past
        # MIN_SHIFT at a file's end. A slow trend of a right track does that, and a fix would move it off.
        return {"fix": None, "why": "in time", "offset": offset}
    what = f"{offset:+.2f} s" + ("" if rate == 1 else f" and the ratio {name}")
    if rate != 1 and not any(abs((a - at[0]) / (at[-1] - at[0]) - 0.5) <= MIDDLE for a in at[1:-1]):   # no middle window yet
        return {"fix": None, "confirm": {"rate": name, "offset": offset, "ends": [round(at[0], 1), round(at[-1], 1)]},
                "why": f"a fix of {what} waits for a middle window to confirm the ratio"}
    near = [r for r in fits if fits[r][1] <= fits[rate][1] + TOLERANCE / 2]   # the fits as good as the best, within the noise
    if any(abs(to(r, t) - to(rate, t)) > TOLERANCE for r in near for t in (0, duration)):   # a plain offset too
        return {"fix": None, "unfixed": fits[rate][0], "why": "the ratios " + ", ".join(f"{r.numerator}/{r.denominator}" for r in near)
                + f" fit the {unit}s and move the cues apart, so the ratio is not certain"}
    share = agree(pairs, cues, rate, offset + lead * float(rate))
    if share < AGREE:
        return {"fix": None, "unfixed": offset, "why": f"after a fix of {what}, {share:.0%} of the matched cues fall in their spans, under {AGREE:.0%}"}
    return {"fix": {"rate": name, "offset": offset},
            "why": f"a fix of {what} puts every {unit} within {TOLERANCE} s and {share:.0%} of the matched cues in their spans"}


def middle(confirm, duration):
    """The part of the file, as windows() takes it, of the middle window that confirms a ratio. confirm is timing()'s:
    the fix, and "ends", the audio times of the first and the last window. The part is MIDDLE of their span at each
    side of its centre, in cue time, where the fix puts the cues of that audio. At a third of the span, a cut of a second
    moves the middle window only a third of a second off the line, and the spread of a right track can hide that."""
    a, b = confirm["ends"]
    rate = float(Fraction(confirm["rate"]))
    return (tuple((rate * ((a + b) / 2 + k * MIDDLE * (b - a)) + confirm["offset"]) / duration for k in (-1, 1)),)


FAR = (tuple(r for r in RATES if r > 1.01), tuple(r for r in RATES if r < 0.99))   # the ratios far from 1: faster, slower


def drift(starts, hint=None, groups=FAR):
    """[window start] of the windows that hear the speech of dense cues when the track drifts far from 1. starts are the
    cue times where the windows of the first hearing start. At 25/23.976 the speech of a cue at 20 minutes is 50 seconds
    earlier in the audio, so a window at cue time can hear silence. For each start and each group of groups, the window
    lies where that ratio puts the speech. hint is (audio time, offset) of a window whose words matched, the offset cue
    time minus audio time, so the fix goes through it. Else the cues start with the audio. The ratios of a group lie
    within 0.1 percent of each other, so one window hears them all."""
    t, d = hint or (0.0, 0.0)
    return [round(max(0.0, statistics.fmean(t + (s - t - d) / float(r) for r in g)), 1) for s in starts for g in groups]


def moved(ms, fix):
    """A cue time in ms after fix: (time - offset) / rate."""
    rate = Fraction(fix["rate"])
    return round((ms / 1000 - fix["offset"]) / float(rate) * 1000)


# Flash cues (docs/design.md, "Subtitle match"). A track whose cues each show for a tenth of a second has right starts
# and ends too short to read. The fix only lengthens a cue, so a right track keeps its ends.
FLASH = 0.5       # seconds of the median cue of a flash track. Right text tracks show their median cue for a second or more.
GAP = 0.5         # the share of its short cues that must end over FRAMES before the next cue starts. Signs and karaoke run
                  # on into the next event.
FRAMES = 0.083    # seconds of two frames, which a new end leaves before the next cue starts
READ_RATE = 17    # visible characters a second of the reading time
HOLD = (3.0, 7.0) # seconds a new end may reach past the start: max(3, twice the reading time), 7 at most
TAG = re.compile(r"<[^>]*>|\{[^}]*\}|\\[Nnh]")   # tags, ASS blocks and ASS breaks, which a viewer never sees
BLANK = re.compile(r"[\s\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")   # spaces, and the zero-width and direction marks


def flash(cues):
    """The new ends of a track whose cues flash, one per cue of cues [(start, end, text)] in their order, or None.
    Only a cue with a visible character counts, and a text of None counts as one. A track flashes when it holds
    MIN_TRACK_CUES such cues, their median shows under FLASH seconds, and GAP of the short ones end over FRAMES before
    the next cue. Only a short cue gets a new end: the next later start less FRAMES, but at most the start plus HOLD,
    twice the reading time, 3 s at least and 7 s at most. The reading time counts the visible characters at READ_RATE a
    second. A cue whose end is later already keeps it."""
    shown = lambda text: len(BLANK.sub("", TAG.sub("", text))) if text is not None else 0
    seen = [(s, e) for s, e, text in cues if text is None or shown(text)]
    if len(seen) < MIN_TRACK_CUES or statistics.median(e - s for s, e in seen) >= FLASH:
        return None
    starts = sorted({s for s, _, _ in cues}) + [float("inf")]
    later = lambda s: starts[bisect.bisect_right(starts, s)]
    short = [(s, e) for s, e in seen if e - s < FLASH]
    if sum(later(s) - e > FRAMES for s, e in short) < GAP * len(short):
        return None
    cap = lambda s, text: s + min(max(HOLD[0], 2 * shown(text) / READ_RATE), HOLD[1])
    return [round(max(e, min(later(s) - FRAMES, cap(s, text))), 3) if e - s < FLASH and (text is None or shown(text)) else e for s, e, text in cues]


# The reference timing of --sub-time (docs/design.md, "Subtitle match"). A track the word check cannot read gets its
# times from a reference: a track or sidecar of the same file whose words matched the audio and whose times are right
# or fixed. The fit reads no words. It compares when the cues show, so a translation from another source still fits
# where it splits or merges lines.
SEARCH = 120.0    # seconds of offset the search for the reference covers each way
STEP = 0.1        # seconds of one offset bin of that search
NULL = (-53, -37, -23, -13, -7, 7, 13, 23, 37, 53)   # seconds from the best offset where the fit measures the overlap by chance
FIT = 0.3         # the least lift over chance of a fit. A right pair lifts well over it, and another episode's track or
                  # another film's lifts near 0.
PAIR = 0.5        # seconds a cue start may sit from a reference cue start to anchor its part
SPAN = 10.0       # seconds a cue counts at most in the overlap, so a long sign does not outweigh the dialogue. A picture cue
                  # also ends there. The overlap of cue spans, and this cap, come from alass and the AutoSubSync research.
SLICES = 10       # slices of the reference's span, each fit at its own offset. Every slice must sit within TOLERANCE of the
                  # fix, so a cut in steps never passes as a ratio. Five slices hold more cues each, but on real tracks
                  # they let a cut in steps pass several times as often.
SPARSE = 6        # cue starts a slice needs on each side, else it is left out: the credits, or a song only one side
                  # times. Chance pairs of so few starts can outvote the right offset.
HALF = 3          # slices each half of the span needs that are not left out, so the fix holds at both ends
REACH = 60.0      # seconds of offset a slice searches each way from the offset of the whole track's search, so a cut of a
                  # minute shows. A search as wide as SEARCH let chance put a slice of a right track far off.
CLEAR = 1.5       # a slice's peak over 1 s from the search's offset needs more than CLEAR times the votes of any other
                  # offset. A peak at that offset needs no margin: a right slice often holds a second peak one line away.
WEAK = 2          # slices with no clear offset whose own peaks lie within TOLERANCE of one offset, MIN_SHIFT or more off,
                  # alert with that offset. No fix takes it. One such slice alone may be chance, or a scene one side drops.


def spans(cues):
    """[[start, end]] of the time cues [(start, end, ...)] show a line, each cue SPAN seconds at most, overlaps merged."""
    out = []
    for s, e, *_ in sorted(cues):
        e = min(max(s, e), s + SPAN)
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def shown(a, b, rate=1, offset=0.0):
    """The share of the shorter of the spans a and b that the other covers too, after a moves to (t - offset) / rate."""
    a, i, j, both = [((s - offset) / float(rate), (e - offset) / float(rate)) for s, e in a], 0, 0, 0.0
    while i < len(a) and j < len(b):
        both += max(0.0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    least = min(sum(e - s for s, e in a), sum(e - s for s, e in b))
    return both / least if least > 0 else 0.0


def lift(a, b, rate, offset):
    """shown() at the offset over its mean at the NULL offsets, as a share of what was left above that mean. 0 is
    chance, 1 a perfect fit. Dense cues cover much of each other at any offset, so the raw share alone misleads."""
    base = statistics.fmean(shown(a, b, rate, offset + d) for d in NULL)
    return (shown(a, b, rate, offset) - base) / (1 - base) if base < 1 else 0.0


def near(ts, rs, rate, offset, tol):
    """[(reference start, cue start, cue index)] of the cue starts ts that lie within tol of their nearest reference
    start of rs after the fit, cue = rate * reference + offset."""
    m, out = [float(rate) * a + offset for a in rs], []
    for i, t in enumerate(ts):
        k = bisect.bisect_left(m, t)
        k = min((x for x in (k - 1, k) if 0 <= x < len(m)), key=lambda x: abs(m[x] - t), default=None)
        if k is not None and abs(m[k] - t) <= tol:
            out.append((rs[k], t, i))
    return out


def voted(ts, rs, rate, around=0.0, reach=SEARCH):
    """{offset bin: votes} of the pairs of a cue start ts and a reference start rs at rate, as cue = rate * reference +
    offset, whose offset lies within reach of around. A bin is STEP wide."""
    m, votes = [float(rate) * a for a in rs], collections.Counter()
    for t in ts:
        for x in m[bisect.bisect_left(m, t - around - reach):bisect.bisect_right(m, t - around + reach)]:
            votes[round((t - x) / STEP)] += 1
    return votes


def refined(ts, rs, rate, offset):
    """offset moved to the median of the pairs near it, at 1 s and then at PAIR, see near()."""
    for tol in (1.0, PAIR):
        got = near(ts, rs, rate, offset, tol)
        offset = statistics.median(t - float(rate) * a for a, t, _ in got) if got else offset
    return round(offset, 3)


def align(ts, rs, rate, around=0.0, reach=SEARCH, clear=0):
    """The offset that puts the most cue starts ts near reference starts rs at rate, as cue = rate * reference +
    offset, or None. Each pair whose offset lies within reach of around votes for it, see voted(), and three
    neighbour bins count as one. The peak nearest around wins a tie. With clear, a peak over 1 s from around needs more
    than clear times the votes of every offset over 1 s from it, else there is no offset. The median of the pairs near
    the peak refines it, see refined()."""
    votes = voted(ts, rs, rate, around, reach)
    if not votes:
        return None
    three = lambda b: votes[b - 1] + votes[b] + votes[b + 1]
    peak = min(votes, key=lambda b: (-three(b), abs(b * STEP - around)))
    if clear and abs(peak * STEP - around) > 1.0 and any(clear * three(b) >= three(peak) for b in votes if abs(b - peak) * STEP > 1.0):
        return None
    return refined(ts, rs, rate, STEP * peak)


def searched(mine, theirs):
    """(the lift, the rate, the offset) of the cue spans mine, see spans(), against the spans theirs. Each ratio of
    RATES tries the offset that align() gives for the span starts, and the one where the cues show over the most of
    theirs wins. Each span votes once, so a karaoke song or a drawing of thousands of short events outvotes no
    dialogue. With no start within SEARCH, the lift is that of rate 1 at offset 0: chance."""
    ts, rs = [a for a, _ in mine], [a for a, _ in theirs]
    tried = [(shown(mine, theirs, r, o), r, o) for r in RATES if (o := align(ts, rs, r)) is not None]
    _, r, o = max(tried, key=lambda x: (x[0], x[1] == 1), default=(0.0, Fraction(1), 0.0))
    return round(lift(mine, theirs, r, o), 3), r, o


def unflashed(cues):
    """cues [(start, end, text or nothing)] with the new ends of flash() when they flash, else as they are. A picture
    cue has no text, and an empty text counts as unknown, so it counts as a visible cue."""
    ends = flash([(c[0], c[1], c[2] if len(c) > 2 and c[2] else None) for c in cues])
    return [(c[0], n, *c[2:]) for c, n in zip(cues, ends)] if ends else cues


def sliced(cues, ts, ref, rate, offset, duration, lead=0.0):
    """fit() of cues against the reference cues ref in SLICES slices of the reference's span, after the search put the
    cues at rate and offset. Each slice pairs cue starts with reference starts at its own best offset, so a cut shows
    as slices at different offsets. fit() judges the slices by the rules of the word check: every slice within
    TOLERANCE of one line, a middle slice on it for a ratio, and the file's ends under MIN_SHIFT. lead is 0 when both
    sides are cue starts, and LAYOUT_LEAD against the spans of speech, see layout_fix().

    A slice where the cues or the reference start under SPARSE times, such as the credits, is left out. Each half of
    the span needs HALF slices that are not, else no fix. A slice searches its offset within REACH of the search's,
    and a peak away from the search's offset must be clear, see align(). A slice with no clear peak, such as one that
    holds a cut, is left out too, and then no fix moves the cues. The other slices still alert when they sit at
    different offsets, and an offset they agree on is "unfixed". When they do not alert, WEAK or more left-out slices
    whose own peaks agree, each with MIN_CUES pairs, alert with that peak. It joins the offsets of the other slices as
    "piecewise" when they show a step, see stepped(), so the alert names the parts that sit off. Else it is "unfixed".
    A slice with under MIN_CUES pairs gives no fix."""
    rs = [c[0] for c in ref]
    lo, hi = rs[0], rs[-1]
    ends, pairs, kept, unclear, weak = [], [], [], None, []
    for k in range(SLICES):
        a, b = lo + k * (hi - lo) / SLICES, lo + (k + 1) * (hi - lo) / SLICES
        mine = [t for t in ts if a <= (t - offset) / float(rate) < b]
        if min(len(mine), bisect.bisect_left(rs, b) - bisect.bisect_left(rs, a)) < SPARSE:
            continue
        here = align(mine, rs, rate, offset, REACH, CLEAR)
        if here is None:
            unclear = unclear or f"slice {k + 1} of {SLICES} has no clear offset within {REACH:.0f} s of the fit, so the times stay"
            peak = align(mine, rs, rate, offset, REACH)   # its own peak, which only an alert may take
            got = near(mine, rs, rate, peak, PAIR) if peak is not None else []
            if len(got) >= MIN_CUES:
                weak.append((k, statistics.median(t - x for x, t, _ in got) - lead))
            continue
        got = near(mine, rs, rate, here, PAIR)
        if len(got) < MIN_CUES:
            return {"fix": None, "why": f"slice {k + 1} of {SLICES} holds {len(got)} cues near the reference, under {MIN_CUES}, so the times stay"}
        first = ts.index(mine[0])
        ends.append([(x, t) for x, t, _ in got])
        pairs += [(x, (None, first + i)) for x, _, i in got]
        kept.append(k)
    group = max(([w for w in weak if abs(w[1] - u) <= TOLERANCE] for _, u in weak), key=len, default=[])
    agreed = round(statistics.median(v for _, v in group), 2) if len(group) >= WEAK else None
    if agreed is not None and abs(agreed) >= MIN_SHIFT:
        why = f"{len(group)} slices have no clear offset within {REACH:.0f} s of the fit, and their own peaks agree on {agreed:+.2f} s, so the times stay"
        at = {k: round(statistics.median(t - x for x, t in e) - lead, 2) for k, e in zip(kept, ends)} | {k: agreed for k, _ in group}
        steps = [at[k] for k in sorted(at)]
        weakly = {"fix": None, "piecewise": True, "offsets": steps, "why": why} if stepped(steps) else {"fix": None, "unfixed": agreed, "why": why}
    else:
        weakly = {"fix": None, "why": unclear}
    early = sum(k < SLICES / 2 for k in kept)
    if min(early, len(kept) - early) < HALF:
        return weakly if unclear else {"fix": None, "why": f"the early half of the reference holds {early} slices with enough cues, "
                                       f"and the late half {len(kept) - early}, under {HALF}, so the times stay"}
    timing = fit(kept, ends, pairs, cues, duration, lead=lead, unit="slice")
    if unclear and not timing.get("piecewise"):
        off = timing["fix"]["offset"] if timing.get("fix") else timing.get("unfixed")
        return weakly if off is None else {"fix": None, "why": unclear, "unfixed": off}
    if "confirm" in timing:   # a slice cannot be heard again: its place is fixed
        timing = {"fix": None, "why": "the middle slice lies too far from the centre to confirm the ratio " + timing["confirm"]["rate"]}
    return timing


def reference(cues, refs, duration):
    """The times of a subtitle from a reference (docs/design.md, "Subtitle match"). cues is [(start, end, ...)] in
    seconds, refs {name: the cues of a reference in audio time: moved by its own fix, or by its own measured offset
    when it is in time}, duration the file's. So a fix moves the cues to the audio, and the in-time rule judges them
    against the audio.

    The search tries each ratio of RATES at the offset that the cue starts point to, see align(), and keeps the one
    where the cues show over the most of the reference's, see shown(). Starts alone miss where a translation splits or
    merges lines. The best reference wins by lift(). A lift under FIT is a weak fit, such as another episode's track:
    it only reports. A fit then fits the slices, see sliced(). A fix must agree with every other reference the cues fit
    at FIT or more: both must move the cues to within TOLERANCE of each other at the file's start and end.

    Flash cues get their new ends first, on both sides, see flash(): ends of a tenth of a second overlap nothing.

    Returns {"verdict": "fit", "weak" or "unknown", "why", "reference", "score": the lift, "rate", "offset": the best
    ratio and offset of the search, "timing": fit() or None}."""
    cues, refs = unflashed(sorted(cues)), {k: unflashed(sorted(c)) for k, c in refs.items()}
    if len(cues) < MIN_TRACK_CUES:
        return {"verdict": "unknown", "why": f"the track holds {len(cues)} cues, under {MIN_TRACK_CUES}", "timing": None}
    refs = {k: c for k, c in refs.items() if len(c) >= MIN_TRACK_CUES}
    if not refs:
        return {"verdict": "unknown", "why": "no track or sidecar of the file matched the audio in its words with its times right or fixed",
                "timing": None}
    ts, mine, found = [c[0] for c in cues], spans(cues), []
    for name, ref in sorted(refs.items()):
        score, r, o = searched(mine, spans(ref))
        found.append((score, name, r, o))
    score, name, rate, offset = max(found, key=lambda x: x[0])
    out = {"reference": name, "score": score, "rate": f"{rate.numerator}/{rate.denominator}", "offset": offset, "timing": None}
    if score < FIT:
        return dict(out, verdict="weak", why=f"the cues fit {name} at a lift of {score:.2f} over chance, under {FIT}, so they may belong "
                    "to another episode or cut")
    timing = sliced(cues, ts, refs[name], rate, offset, duration)
    fix = timing.get("fix")
    to = lambda f, t: moved(t * 1000, f) / 1000 if f else t
    says = lambda f: f'{f["offset"]:+.2f} s' + ("" if f["rate"] == "1/1" else f' and the ratio {f["rate"]}')
    for lifted, other, r, o in found:   # a second reference must give the same times
        if fix and other != name and lifted >= FIT:
            them = sliced(cues, ts, refs[other], r, o, duration)
            if any(abs(to(fix, t) - to(them.get("fix"), t)) > TOLERANCE for t in (0, duration)):   # a fix moves some cue MIN_SHIFT or more
                timing = {"fix": None, "unfixed": fix["offset"], "why": f"a fix of {says(fix)} fits {name}, but {other} "
                          + (f"needs a fix of {says(them['fix'])}" if them.get("fix") else f"says {them['why'].removesuffix(', so the times stay')}") + ", so the times stay"}
                break
    return dict(out, verdict="fit", why=f"the cues fit {name} at a lift of {score:.2f} over chance", timing=timing)


# The speech layout (docs/design.md, "Incorrect subtitle identification" and "Foreign subtitle timing"). A subtitle in
# no main audio language has no words to compare, and often no reference. Its cues still show while people speak.
# lid.speech() gives the spans of speech of the whole audio from Silero VAD, and layout() fits the cue spans to them as
# reference() fits a track to a reference.
VAD_FRAME = 512 / 16000   # seconds of one frame of Silero VAD, which gives one speech probability a frame
VAD_ON = 0.5      # the probability at which a span of speech starts, Silero's own threshold
VAD_OFF = 0.35    # the probability under which it ends, Silero's own threshold less 0.15
VAD_GAP = 0.5     # seconds of silence under which two spans of speech join
LAYOUT = 0.35     # the least lift over chance of the cue spans over the speech spans. Verified right tracks lifted 0.49
                  # or more, and another episode's or show's track 0.27 at most, see docs/design.md
LAYOUT_SEARCH = 300.0   # seconds of offset the layout search covers each way. A wider search than SEARCH found right tracks
                        # shifted 200 s at 1.5 times the CPU, and no other episode's track fit better.
LAYOUT_PEAKS = 3  # offsets of the most votes that each ratio tries. The one vote peak of align() missed the right offset of
                  # some right tracks, as a span of speech can hold several lines.
LAYOUT_MIN = 900.0     # seconds a file runs at least before layout() judges. A shorter file holds few lines, and chance fits
                       # score higher on it: 0.71 at 5 minutes, 0.32 at 15.
LAYOUT_SPEECH = 60.0   # seconds of speech the audio needs before layout() judges
LAYOUT_SHARE = 0.5     # the share of the speech time the lines must show for, else they do not follow the speech, such as
                       # a track of sound captions and songs
LAYOUT_CHANCE = 0.9    # the overlap by chance at which the fit tells nothing: the lines cover the speech at any offset,
                       # such as live captions that roll on with no gap
LAYOUT_ACTION = "alert"   # what a layout mismatch does: "alert" only, or "remove" the track as a word-check mismatch
                          # does. It alerts until a soak of the logged lifts shows how far right tracks stay from LAYOUT.


def voiced(probs):
    """[[start, end]] in seconds of the speech in probs, Silero VAD's probability for each frame of VAD_FRAME. A span
    starts at a frame of VAD_ON or more and ends at the first frame under VAD_OFF. Spans under VAD_GAP apart join."""
    out, on = [], None
    for k, p in enumerate([*probs, 0.0]):
        if on is None and p >= VAD_ON:
            on = k
        elif on is not None and p < VAD_OFF:
            if out and on * VAD_FRAME - out[-1][1] < VAD_GAP:
                out[-1][1] = round(k * VAD_FRAME, 3)
            else:
                out.append([round(on * VAD_FRAME, 3), round(k * VAD_FRAME, 3)])
            on = None
    return out


def peaks(ts, rs, rate, k=LAYOUT_PEAKS):
    """The k offsets with the most votes of voted() within LAYOUT_SEARCH, each over 1 s from the others, refined, see
    refined()."""
    votes, out = voted(ts, rs, rate, reach=LAYOUT_SEARCH), []
    three = lambda b: votes[b - 1] + votes[b] + votes[b + 1]
    for b in sorted(votes, key=lambda b: (-three(b), abs(b))):
        if len(out) < k and all(abs(b - o) * STEP > 1.0 for o in out):
            out.append(b)
    return [refined(ts, rs, rate, b * STEP) for b in out]


def layout(cues, speech, duration):
    """The speech layout check of a subtitle that no word check reads (docs/design.md, "Incorrect subtitle
    identification"). cues is [(start, end, ...)] in seconds, speech the voiced() spans of the audio it plays with,
    duration the file's. Each ratio of RATES tries the LAYOUT_PEAKS offsets that the cue starts point to most, and the
    fit with the most lift over chance wins. So a whole-track offset or a frame-rate error still fits. Each span of the
    cues, see spans(), votes once, so the events of a karaoke song or a drawing outvote no dialogue. A lift under LAYOUT
    is a mismatch: the lines show where no one speaks, as another episode's do. A file under LAYOUT_MIN, too little
    speech, lines that show for under LAYOUT_SHARE of the speech, and an overlap by chance of LAYOUT_CHANCE or more give
    unknown.

    Returns {"verdict": "fit", "mismatch" or "unknown", "why", "score": the lift, "rate", "offset"}."""
    cues = unflashed(sorted(cues))
    if duration < LAYOUT_MIN:
        return {"verdict": "unknown", "why": f"the file runs {duration / 60:.0f} min, under {LAYOUT_MIN / 60:.0f}"}
    if len(cues) < MIN_TRACK_CUES:
        return {"verdict": "unknown", "why": f"the track holds {len(cues)} cues, under {MIN_TRACK_CUES}"}
    mine, rs = spans(cues), [a for a, _ in speech]
    ts = [a for a, _ in mine]
    heard, lines = sum(b - a for a, b in speech), sum(b - a for a, b in mine)
    if heard < LAYOUT_SPEECH:
        return {"verdict": "unknown", "why": f"the audio holds {heard:.0f} s of speech, under {LAYOUT_SPEECH:.0f}"}
    if lines < LAYOUT_SHARE * heard:
        return {"verdict": "unknown", "why": f"its lines show for {lines:.0f} s, under {LAYOUT_SHARE:.0%} of the {heard:.0f} s of speech, so they "
                "do not follow the speech"}
    tried = [(lift(mine, speech, r, o), r, o) for r in RATES for o in peaks(ts, rs, r)]
    score, rate, offset = max(tried, key=lambda x: (x[0], x[1] == 1), default=(0.0, Fraction(1), 0.0))
    out = {"score": round(score, 3), "rate": f"{rate.numerator}/{rate.denominator}", "offset": offset}
    chance = statistics.fmean(shown(mine, speech, rate, offset + d) for d in NULL)
    if chance >= LAYOUT_CHANCE:
        return dict(out, verdict="unknown", why=f"its lines cover {chance:.0%} of the speech at any offset, so where they show tells nothing")
    if score < LAYOUT:
        return dict(out, verdict="mismatch", why=f"its lines line up with the speech at a lift of {score:.2f} over chance, under {LAYOUT}, "
                    "so they may belong to another episode")
    return dict(out, verdict="fit", why=f"its lines line up with the speech at a lift of {score:.2f} over chance")


LAYOUT_LEAD = -0.2   # seconds a right track's lines start after the speech starts, the median over verified right tracks.
                     # A line shows a little before VAD hears the speech. A fix keeps it.
ONSET_LEAD = -0.1    # seconds a right track's lines start after their speech onsets, the median over verified right tracks
ONSET_PARTS = 10     # parts of the file, spread evenly, whose onsets confirm a fix of layout_fix(), see onset_parts()
ONSET_PART = 120.0   # seconds of each part at most
LAYOUT_ONSET_SHARE = 0.0   # the share of a half's starts that must have an onset where a fix of layout_onsets() puts them.
                           # A whole track asks none: under a music bed silencedetect marks few onsets.
LAYOUT_FIX = "write"   # what a fix of layout_fix() that the speech onsets confirm does: "alert" only, or "write" the new
                       # times. On planted steps of a review's shapes no right line moved, but no rule makes a right line
                       # safe for sure, and runs of lines at the ends stay where they are, see shifted() and docs/design.md.
LAYOUT_EDGE = 0.08   # the share of the cue spans at each end of the file, MIN_TRACK_CUES at least, that must line up with
                     # the speech best at a fix of layout_fix(), see edges()
LAYOUT_PARTS = 20    # parts of the cue spans, equal by count, none of which may vote for another offset, see edges()


def stepped(offsets):
    """The slices of a piecewise fit, at offsets, show a step: two or more of them lie within TOLERANCE of one offset
    at least TIMING "move" from the offset most slices share, as a mid-file jump leaves them. One slice alone off may
    be noise, such as an end song a few lines time."""
    group = lambda u, xs: [v for v in xs if abs(v - u) <= TOLERANCE]
    main = max(offsets, key=lambda u: len(group(u, offsets)))
    off = [u for u in offsets if abs(u - main) >= TIMING["move"]]
    return any(len(group(u, off)) >= 2 for u in off)


def edges(mine, speech, rate, offset):
    """Why the cue spans mine, see spans(), do not line up with the speech spans at a fix of layout_fix(), or None
    when they do. offset maps them as cue = rate * speech + offset, with no LAYOUT_LEAD. The slices of sliced() each
    hold a tenth of the speech, so a step in the first or last tenth of the lines can pass every slice. Two rules catch
    it. The first and the last LAYOUT_EDGE of the spans must show over the speech near them at least as much within
    TOLERANCE of the fix as at any offset 1 s or more from it, within REACH. And no part of LAYOUT_PARTS, equal by
    count, may have a clear vote peak over TOLERANCE from the fix, see align(), unless the speech holds under SPARSE
    starts there."""
    rs, m, grid = [a for a, _ in speech], max(MIN_TRACK_CUES, int(LAYOUT_EDGE * len(mine))), round(REACH / STEP)
    for name, block in (("first", mine[:m]), ("last", mine[-m:])):
        a, b = (block[0][0] - offset) / float(rate), (block[-1][1] - offset) / float(rate)
        near = [x for x in speech if x[1] >= a - REACH - SPAN and x[0] <= b + REACH + SPAN]
        if near:
            got = {k: shown(block, near, rate, offset + k * STEP) for k in range(-grid, grid + 1)}
            at, far = max(got[k] for k in got if abs(k) <= round(TOLERANCE / STEP)), max((k for k in got if abs(k) >= round(1.0 / STEP)), key=got.get)
            if got[far] > at:
                return f"the {name} {len(block)} lines line up with the speech better {far * STEP:+.1f} s from it"
    ts = [a for a, _ in mine]
    for i in range(LAYOUT_PARTS):
        part = ts[i * len(ts) // LAYOUT_PARTS:(i + 1) * len(ts) // LAYOUT_PARTS]
        if not part:
            continue
        a, b = (part[0] - offset) / float(rate), (part[-1] - offset) / float(rate)
        here = align(part, rs, rate, offset, REACH, CLEAR) if bisect.bisect_right(rs, b + 1) - bisect.bisect_left(rs, a - 1) >= SPARSE else None
        if here is not None and abs(here - offset) > TOLERANCE:
            return f"part {i + 1} of {LAYOUT_PARTS} of its lines lines up with the speech {here - offset:+.2f} s from it"
    return None


def layout_fix(cues, speech, duration, lay):
    """The times of a subtitle that layout() fit, as fit() gives them, or None when lay is no fit. The cue spans, see
    spans(), fit the speech spans in slices at the ratio and offset of lay, as reference() fits a reference, see
    sliced(). So a track off by one shift, or timed for another frame rate, gets a fix that keeps LAYOUT_LEAD. Slices
    at different offsets, as of another cut, are "piecewise" and get no fix, and alert only when stepped() shows a
    step. A fix at ratio 1 moves only the lines that agree with it, see shifted(): a run of lines at the file's start
    or end that has no evidence at the fix keeps its times. Its "keep" holds the kept runs, and "kept" counts their
    lines. A fix at another ratio moves every line, unless an end sits on speech now, see sat_on_speech().
    The lines after the fix must then line up with the speech at ratio 1, within TOLERANCE of LAYOUT_LEAD, and every
    part of the moved lines must line up there, see edges(). A kept run keeps its own rule in shifted(): an opening
    song in time lines up with the speech nowhere, and edges() would read it as off. Else the times stay. The speech
    onsets confirm the moved lines later, see layout_onsets()."""
    if lay["verdict"] != "fit":
        return None
    mine = [tuple(x) for x in spans(unflashed(sorted(cues)))]
    timing = sliced(mine, [a for a, _ in mine], speech, Fraction(lay["rate"]), lay["offset"], duration, LAYOUT_LEAD)
    if timing.get("piecewise") and not stepped(timing["offsets"]):
        return {"fix": None, "offsets": timing["offsets"], "why": f'{timing["why"]}, but no two slices agree on another offset, so the times stay'}
    fix = timing.get("fix")
    if not fix:
        return timing
    unfixed = lambda why: {"fix": None, "unfixed": fix["offset"], "why": f'{timing["why"]}, but {why}, so the times stay'}
    cs = sorted(cues)
    if fix["rate"] == "1/1":
        moves, why, keep = shifted(cs, speech, fix)
    else:   # a frame-rate error covers the whole file, so every line moves, unless an end sits on speech now
        moves, why, keep = [True] * len(cs), sat_on_speech(cs, speech, fix), []
    if why:
        return unfixed(why)
    to = lambda x: moved(x * 1000, fix) / 1000
    moved_cues = [(to(a), to(b), *x) if m else (a, b, *x) for (a, b, *x), m in zip(cs, moves)]
    again = layout(moved_cues, speech, duration)
    if again["verdict"] != "fit" or again["rate"] != "1/1" or abs(again["offset"] - LAYOUT_LEAD) > TOLERANCE:
        return unfixed("the lines it moves do not line up with the speech in time")
    off = edges([tuple(x) for x in spans(unflashed(sorted(c for c, m in zip(moved_cues, moves) if m)))], speech, Fraction(1), LAYOUT_LEAD)
    if off:
        return unfixed(off)
    if INVARIANTS:
        nearer(cues, moved_cues, speech, fix)
        check_kept(cs, moves, fix, speech) if fix["rate"] == "1/1" else check_ratio(cs, moves, fix, speech)
    if not keep:
        return timing
    runs = " and ".join(f'the {"first" if lo is None else "last"} ' + (f"{n} lines" if n > 1 else "line") for lo, _, n in keep)
    keeps = "keep their times" if len(keep) > 1 or keep[0][2] > 1 else "keeps its time"
    return dict(timing, keep=keep, kept=sum(n for *_, n in keep), why=f'{timing["why"]}, and {runs} {keeps}, as no speech confirms them at the fix')


# The partial shift of layout_fix() (docs/design.md, "Foreign subtitle timing"). A track from another cut can have a
# cold open in time and the rest shifted. A fix then moves only the lines that agree with it. Each line votes on
# whether it stays: a speech start (VAD) within TOLERANCE of where the fix puts it, and one where it sits. A run at an
# end of the file whose votes rise stays.
KEEP_VOTES = (0.55, 1.57, 1.1, 0.25)   # a line's vote that it stays, from VAD: no speech start at the fix, one there,
                     # one where it sits, none there. 48% of the lines of verified right tracks start within TOLERANCE of
                     # a speech start, and 10% at an offset by chance. A line that stays sits right (48%) or where no one
                     # speaks (10%), so 30% is taken where it sits. Each vote is the log of the ratio of the two chances.
KEEP_SLACK = 4.0     # votes the running sum of a run that stays may fall below its peak and still keep its lines: two
                     # chance speech starts at the fix, with a line beside them. Right lines with no speech under them
                     # drew two such starts in a row on a review's plants, and a slack of one start moved them.
SAT_STARTS = 2       # lines with a speech start where they sit and none at a frame-rate fix that make an end sit on
                     # speech now, see sat_on_speech()
SAT_CHANCE = 0.05    # the chance at most of as many such starts in that run. On planted subtitles
                     # with one end in time and the rest at another frame rate, a lower bar lost real fixes, and a
                     # higher one moved right lines with speech under them more often.
KEEP_PASS = 1.0      # seconds the first or last moved line with evidence must pass where a line that stays sits before
                     # that line moves too. A fix lands within TOLERANCE of the speech, and one planted fix landed 0.33 s off.


def near_times(starts, times, shift, tol):
    """[some time of the sorted times lies within tol of t - shift] for each t of starts."""
    return [bool(times[bisect.bisect_left(times, t - shift - tol):bisect.bisect_right(times, t - shift + tol)]) for t in starts]


def kept_run(votes):
    """The number of lines from the start of votes, each line's vote that it stays, that keep their times: every line
    up to the last place where the running sum of the votes lies within KEEP_SLACK of its peak. So a chance speech
    start at the fix near the end of a run that stays cuts no line off."""
    run = list(itertools.accumulate(votes, initial=0.0))
    top = max(run)
    return max(k for k, v in enumerate(run) if v >= top - KEEP_SLACK)


def stay_votes(cues, speech, fix):
    """(span starts, where fix puts them, here, there, votes) of the spans of cues, sorted, see spans() and
    shifted(): here and there flag a VAD speech start within TOLERANCE of a span start, less LAYOUT_LEAD, where it
    sits and where fix puts it. votes are each span's KEEP_VOTES that it stays."""
    ts, rs = [a for a, _ in spans(unflashed(cues))], [a for a, _ in speech]
    to = [moved(t * 1000, fix) / 1000 for t in ts]
    here = near_times(ts, rs, LAYOUT_LEAD, TOLERANCE)
    there = [bool(rs[bisect.bisect_left(rs, x - LAYOUT_LEAD - TOLERANCE):bisect.bisect_right(rs, x - LAYOUT_LEAD + TOLERANCE)]) for x in to]
    w = KEEP_VOTES
    return ts, to, here, there, [(-w[1] if y else w[0]) + (w[2] if x else -w[3]) for x, y in zip(here, there)]


def shifted(cues, speech, fix):
    """(moves, why, keep) of a fix of layout_fix() for cues, sorted: moves flags each cue that moves, why says why
    nothing may move or is None, and keep holds the runs that stay as [from, to, lines] in cue seconds, from None at
    the file's start or to None at its end. Each span of the cues votes that it stays, see stay_votes(), and
    kept_run() gives the lines that stay at each end. The first and the last span that move, the core's edges, need
    a speech start where the fix puts them and none where they sit, so lines with no evidence at the fix stay. A line
    that stays where the core's first or last moved line would pass it moves too, when the move goes toward its end
    of the file and passes it by KEEP_PASS or more. A moved line lands on its speech, so it passes a right line by the
    fix's error at most. A line that only a line moved this way would pass, any other pass, a run that stays but lines
    up with the speech elsewhere, see align(), and under MIN_TRACK_CUES lines to move give why."""
    ts, to, here, there, votes = stay_votes(cues, speech, fix)
    rs = [a for a, _ in speech]
    lo, hi = kept_run(votes), len(ts) - kept_run(votes[::-1])
    while lo < hi and not (there[lo] and not here[lo]):   # the core's first line has its own evidence at the fix
        lo += 1
    while hi > lo and not (there[hi - 1] and not here[hi - 1]):
        hi -= 1
    if hi - lo < MIN_TRACK_CUES:
        return None, f"only {hi - lo} lines would move", []
    span = [max(0, bisect.bisect_right(ts, c[0]) - 1) for c in cues]
    new = [moved(c[0] * 1000, fix) / 1000 for c in cues]
    core = [k for k in range(len(cues)) if lo <= span[k] < hi]
    first, last = new[core[0]], new[core[-1]]
    move = set(core)
    for k in range(len(cues)):   # a line the core passes: only the core's own first or last line may move it
        if span[k] < lo and cues[k][0] > first or span[k] >= hi and cues[k][0] < last:
            toward = first < cues[core[0]][0] if span[k] < lo else last > cues[core[-1]][0]
            if not toward or abs(cues[k][0] - (first if span[k] < lo else last)) < KEEP_PASS:
                return None, "a line it moves would pass a line that keeps its time", []
            move.add(k)
    at = [new[k] if k in move else c[0] for k, c in enumerate(cues)]
    if any(at[k] >= at[k + 1] and cues[k][0] < cues[k + 1][0] for k in range(len(cues) - 1)):   # a moved line passes another line
        return None, "a line it moves would pass a line that keeps its time", []
    moves = [k in move for k in range(len(cues))]
    a, b = moves.index(True), len(moves) - moves[::-1].index(True)
    for name, run in (("first", sorted({ts[span[k]] for k in range(a)})), ("last", sorted({ts[span[k]] for k in range(b, len(cues))}))):
        if len(run) >= SPARSE and bisect.bisect_right(rs, run[-1] - LAYOUT_LEAD + 1) - bisect.bisect_left(rs, run[0] - LAYOUT_LEAD - 1) >= SPARSE:
            got = align(run, rs, 1, LAYOUT_LEAD, REACH, CLEAR)
            if got is not None and abs(got - LAYOUT_LEAD) > TOLERANCE:
                return None, f"the {name} {len(run)} lines, which keep their times, line up with the speech {got - LAYOUT_LEAD:+.2f} s from where they are", []
    keep = ([[None, cues[a][0], a]] if a else []) + ([[cues[b][0], None, len(cues) - b]] if b < len(cues) else [])
    return moves, None, keep


def sat_run(cues, speech, fix):
    """[(end, lines, starts, chance)] of each end of cues, sorted, at fix: the lines from that end up to the last peak
    of the running sum of their stay votes, see stay_votes(), how many of them have a speech start where they sit and
    none at the fix, and the chance of as many: the Poisson tail at the rate of such starts at the NULL offsets."""
    ts, _, here, there, votes = stay_votes(cues, speech, fix)
    rs = [a for a, _ in speech]
    rate = statistics.fmean(statistics.fmean(near_times(ts, rs, LAYOUT_LEAD + d, TOLERANCE)) for d in NULL)
    out = []
    for end, h, th, v in (("first", here, there, votes), ("last", here[::-1], there[::-1], votes[::-1])):
        run = list(itertools.accumulate(v, initial=0.0))
        k = max(range(len(run)), key=lambda i: (run[i], i))
        got = sum(1 for x, y in zip(h[:k], th[:k]) if x and not y)
        term, below = math.exp(-k * rate), 0.0   # the Poisson tail by a running term, as clock() takes it
        for i in range(got):
            below, term = below + term, term * k * rate / (i + 1)
        out.append((end, k, got, max(0.0, 1 - below)))
    return out


def sat_on_speech(cues, speech, fix):
    """Why a fix at a frame-rate ratio would move lines that sit on speech now, or None. A frame-rate error covers the
    whole file, so such a fix moves every line, those with no speech at either place too. But when the run at an end,
    see sat_run(), holds SAT_STARTS or more lines with a speech start where they sit and none at the fix, at a chance
    of SAT_CHANCE at most, that end is in time and the rest is not: two sources. Then nothing moves, and the alert
    says the subtitles seem late."""
    for end, k, got, chance in sat_run(cues, speech, fix):
        if got >= SAT_STARTS and chance <= SAT_CHANCE:
            return f"the {end} {k} lines line up with the speech where they sit, at {got} speech starts, so they would move off it"
    return None


def check_ratio(cues, moves, fix, speech):
    """Check the rule of a fix at a frame-rate ratio, see sat_on_speech() and INVARIANTS: every line moves, the order
    of the lines holds, see ordered(), and no end holds a run that sits on speech now. Raises Broken."""
    new = [moved(c[0] * 1000, fix) / 1000 for c in cues]
    case = {"cues": [c[:2] for c in cues], "moves": moves, "fix": fix}
    if not all(moves):
        broken("every line", cues[moves.index(False)][0], "a fix at a frame-rate ratio keeps this line's time", case)
    ordered([(c[0], c[0], n) for c, n in zip(cues, new)], case)
    for end, k, got, chance in sat_run(cues, speech, fix):
        if got >= SAT_STARTS and chance <= SAT_CHANCE:
            broken("sits on speech", cues[0 if end == "first" else -1][0], f"the {end} {k} lines hold {got} speech starts where they sit, at a chance of "
                   f"{chance:.3f}, and the fix moves them", case)


def kept_at(keep, t):
    """A cue that starts at t lies in a run of keep that keeps its times, see shifted()."""
    return any((lo is None or lo <= t) and (hi is None or t < hi) for lo, hi, _ in keep or ())


def keep_ordered(cues, timing):
    """The new starts of cues [(start, end, ...)] in their file's order, after the fix of timing with its kept runs,
    keep the order of their starts, as subsync.ordered() asks. A line with no text, which the check never read, can
    sit between a kept run and the moved lines."""
    rows = sorted((c[0], c[0] if kept_at(timing.get("keep"), c[0]) else moved(c[0] * 1000, timing["fix"]) / 1000) for c in cues)
    return all(n0 < n1 or s0 == s1 for (s0, n0), (s1, n1) in zip(rows, rows[1:]))


def keep_blocks(timing):
    """The blocks of remux.time_plan() that hold the runs of timing["keep"] where they are against its fix, see
    shifted(): a "kept" block keeps the start and end of each of its cues."""
    return [{"from": -1e12 if lo is None else lo, "to": 1e12 if hi is None else hi, "shift": 0.0, "cues": n, "kept": True} for lo, hi, n in timing.get("keep") or ()]


def check_kept(cues, moves, fix, speech):
    """Check the rules of the moves of shifted() for cues, sorted, see INVARIANTS. moves flags the cues that move by
    fix. Order: no moved cue passes a cue that stays, and no two starts that differed tie, see ordered(). Edges: the
    cues that stay lie at the ends of the file only. Own evidence, at each end of the file: some moved span has a
    speech start where the fix puts it and none where it sits, and the running sum of the stay votes from that end,
    see stay_votes(), has fallen over KEEP_SLACK below its peak at it, see kept_run(). That is the core's edge. Every
    moved cue between it and that end lies where the edge's cue passes it by KEEP_PASS or more. Raises Broken."""
    new = [moved(c[0] * 1000, fix) / 1000 if m else c[0] for c, m in zip(cues, moves)]
    case = {"cues": [c[:2] for c in cues], "moves": moves, "fix": fix}
    ordered([(c[0], c[0], n) for c, n in zip(cues, new)], case)
    a, b = moves.index(True), len(moves) - moves[::-1].index(True)
    if not all(moves[a:b]):
        broken("edges", cues[a + moves[a:b].index(False)][0], "a cue between two cues that move keeps its time", case)
    ts, _, here, there, votes = stay_votes(cues, speech, fix)
    span = [max(0, bisect.bisect_right(ts, c[0]) - 1) for c in cues]
    for start in (True, False):   # each end, one with no kept line too: a cascade there moved every line before the edge
        idx = list(range(a, b)) if start else list(range(b - 1, a - 1, -1))
        run = list(itertools.accumulate(votes if start else votes[::-1], initial=0.0))
        at = lambda s: s + 1 if start else len(ts) - s
        edge = next((k for k in idx if there[span[k]] and not here[span[k]] and max(run[:at(span[k])]) - run[at(span[k])] > KEEP_SLACK), None)
        if edge is None:
            broken("own evidence", cues[idx[0]][0], "no moved line has a speech start at the fix, none where it sits, and the stay votes over KEEP_SLACK "
                   "under their peak", case)
        for k in idx[:idx.index(edge)]:
            if span[k] != span[edge] and (cues[k][0] - new[edge] if start else new[edge] - cues[k][0]) < KEEP_PASS:
                broken("own evidence", cues[k][0], "it moves with no evidence of the core's edge at the fix, and the edge's line does not pass it", case)


def nearer(cues, moved_cues, speech, fix):
    """Check the nearer-its-speech rule on a fix of layout_fix(), see INVARIANTS: the moved cues show over at least as
    much of the speech as the cues did. Raises Broken."""
    before, after = shown(spans(unflashed(sorted(cues))), speech), shown(spans(unflashed(sorted(moved_cues))), speech)
    if after < before:
        broken("nearer its speech", min(c[0] for c in cues), f"the fix {fix} moves the lines off the speech: they show over {after:.1%} "
               f"of it, and {before:.1%} before", {"cues": [c[:2] for c in cues], "speech": speech, "fix": fix})


def onset_parts(duration):
    """[(lo, hi)] of the ONSET_PARTS parts of the file whose onsets layout_onsets() reads: one at the centre of each
    tenth of the file, ONSET_PART seconds long at most, so no two overlap."""
    tenth = duration / ONSET_PARTS
    half = min(ONSET_PART, tenth) / 2
    return [(round((k + 0.5) * tenth - half, 3), round((k + 0.5) * tenth + half, 3)) for k in range(ONSET_PARTS)]


def layout_onsets(cues, timing, onsets, read, duration):
    """timing, a fix of layout_fix(), when the speech onsets confirm it, else no fix and "unfixed". The onsets are a
    second clock: silencedetect marks them, and VAD the spans of speech. onsets are [(time, seconds of silence before
    it)] read in the parts read, see onset_parts(). A cue span start, see spans(), counts where the fix puts it when an
    onset that ends ONSET_QUIET of silence lies within half of TOLERANCE of it less ONSET_LEAD, and likewise where it
    sat. Each half of the file must agree: ONSET_MIN of its starts, and LAYOUT_ONSET_SHARE of them, count where the
    fix puts them, twice as many as where they sat, and as many are rare by chance. Chance is the same count at the
    NULL offsets, or the rate of the counted onsets in the parts, whichever is more, and the chance of as many or more
    must be ONSET_CHANCE at most. A half where as many count where they sat, and no fewer than at the fix, disagrees.
    Else there are too few. A whole track asks LAYOUT_ONSET_SHARE: under a music bed silencedetect marks few onsets, and verified real shifts of about 1 s drew 9% to 19% of their
    starts there, at a chance of 1e-6 or less.
    The lines of timing["keep"], which keep their times, see shifted(), do not count. "onsets" holds the counts of each
    half: starts, where the fix puts them, where they sat, chance."""
    fix = timing["fix"]
    times = [t for t, quiet in onsets if quiet >= ONSET_QUIET]
    hits = lambda ps: sum(bool(times[bisect.bisect_left(times, p - TOLERANCE / 2):bisect.bisect_right(times, p + TOLERANCE / 2)]) for p in ps)
    starts = [a for a, _ in spans(unflashed(sorted(c for c in cues if not kept_at(timing.get("keep"), c[0]))))]
    halves = [[0, 0, 0, 0.0], [0, 0, 0, 0.0]]
    for lo, hi in read:
        new = [p for t in starts if lo <= (p := moved(t * 1000, fix) / 1000 - ONSET_LEAD) <= hi]
        old = [p for t in starts if lo <= (p := t - ONSET_LEAD) <= hi]
        nulls = [hits(q) * len(new) / len(q) for d in NULL if (q := [p + d for p in new if lo <= p + d <= hi])]
        rate = sum(lo <= t <= hi for t in times) / (hi - lo) if hi > lo else 0.0
        h = halves[(lo + hi) / 2 >= duration / 2]
        h[0], h[1], h[2], h[3] = h[0] + len(new), h[1] + hits(new), h[2] + hits(old), h[3] + max(statistics.fmean(nulls) if nulls else 0.0, len(new) * TOLERANCE * rate)

    def judge(n, new, old, chance):
        term, below = math.exp(-chance), 0.0   # the Poisson tail by a running term, as align.vote() takes it
        for i in range(new):
            below, term = below + term, term * chance / (i + 1)
        need = max(ONSET_MIN, LAYOUT_ONSET_SHARE * n)
        return "agree" if new >= need and new >= 2 * old and 1 - below <= ONSET_CHANCE else "disagree" if old >= need and old >= new else "few"
    said = [judge(*h) for h in halves]
    counts = [[n, a, b, round(c, 2)] for n, a, b, c in halves]
    if said == ["agree", "agree"]:
        return dict(timing, onsets=counts)
    why = "the speech onsets put the lines where they sat" if "disagree" in said else "too few speech onsets confirm it"
    return {"fix": None, "unfixed": fix["offset"], "onsets": counts, "why": f'{timing["why"]}, but {why}, so the times stay'}


OVERLAP = 2.5       # seconds two windows of the whole-file hearing share, so a window edge never cuts a line of up to 2.5 s
                    # in both


def merged(parts):
    """[[lo, hi]] of parts [(lo, hi)], in order, with parts that touch merged."""
    out = []
    for lo, hi in sorted(parts):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def whole_grid(duration):
    """[window start] of the whole-file hearing, see subtitles.whole_heard(): windows of WINDOW seconds every
    WINDOW - OVERLAP seconds from 0, and a last one that ends at the file's end. Each start rounds to 0.1 s."""
    last, step = max(0.0, duration - WINDOW), WINDOW - OVERLAP
    return sorted({round(k * step, 1) for k in range(math.ceil(last / step))} | {round(last, 1)})


def whole_starts(duration, have=()):
    """[window start] of whole_grid() that the windows of have, [{"at", "secs"}], do not hold. have holds the windows
    the cache holds, such as those of an earlier run, so no window is heard twice. Only a window on the grid counts,
    so the words are the same wherever the hearing stopped and went on."""
    held = {(round(w["at"], 1), float(w["secs"])) for w in have}
    return [a for a in whole_grid(duration) if (a, WINDOW) not in held]


# The second clock (docs/design.md, "Subtitle match"): speech onsets, where a silence ends. They never use Whisper.
ONSET_MIN = 3       # lines with an onset that the onsets need before they judge a move, see align.vote()
ONSET_QUIET = 0.5   # seconds of the silence an onset must end to time a line. Such onsets are sparse, under one a second,
                    # so they fill few places by chance.
ONSET_CHANCE = 0.001  # the chance at most that the onsets within half of TOLERANCE of the moved lines are as many by chance,
                      # from the same count at the NULL offsets. With real noise a right track that Whisper heard 0.8 s
                      # early drew 4 such onsets of 13 cues, at a chance of 0.003.


def tokens(text):
    """Every word of a cue or of heard speech, lower case, with no tags, no sounds in brackets and no apostrophes: the
    words of words() with the stopwords and the one-letter words kept."""
    return TOKEN.findall(NOISE.sub(" ", text).lower().replace("’", "").replace("'", ""))


# The safety rules of a move (docs/development.md, "Safety self-checks"). AMG_INVARIANTS=1 checks them on every result.
# align.check_plan() checks the moves of the whole-file timing, and remux.time_plan() checks the order. The tests turn
# it on. Off, no check runs.
INVARIANTS = os.environ.get("AMG_INVARIANTS") == "1"
NEAR = 0.01   # seconds of rounding that the nearer-its-speech check allows against each line. A shift keeps ms, and ASS keeps cs.


class Broken(AssertionError):
    """A result that breaks a safety rule. The message names the rule and the cue."""


def broken(rule, cue, why, case):
    """Raise Broken for rule at the cue that starts at cue seconds. When AMG_INVARIANT_DUMP names a folder, the case,
    the inputs of the check, goes there first as one JSON file."""
    folder = os.environ.get("AMG_INVARIANT_DUMP")
    if folder:
        os.makedirs(folder, exist_ok=True)
        fd, path = tempfile.mkstemp(".json", rule.replace(" ", "-") + "-", folder)
        with os.fdopen(fd, "w") as f:
            json.dump({"rule": rule, "cue": cue, "why": why, **case}, f, default=lambda x: sorted(x) if isinstance(x, (set, frozenset)) else str(x))
    raise Broken(f"rule {rule} broken at the cue at {cue} s: {why}")


def mover(blocks, t):
    """The block of blocks, the kept runs of keep_blocks(), that holds a cue that starts at t, or None. remux.time_plan()
    keeps the times of such a cue."""
    return next((b for b in blocks or () if b["from"] <= t < b["to"]), None)


def ordered(rows, case):
    """Check the order rule, see INVARIANTS, on rows [(start, new start with no block, new start)] of one track's
    remux.time_plan(). Two cues in start order keep their order, and two whose starts did not tie never tie. Two cues
    that time_plan() clamps to 0 may tie, because no start lies before 0. A moved cue may still start before the end of
    a cue that stays. Raises Broken."""
    rows = sorted(rows)
    for (s0, a0, n0), (s1, a1, n1) in zip(rows, rows[1:]):
        if s0 < s1 and (n0 > n1 or n0 == n1 > 0 and a0 < a1):
            broken("order", s1, f"it starts at {n1} s, and the cue at {s0} s before it at {n0} s", case)


LIVE_LAG = 0.3       # seconds late live captions sit at least, the lag an alert names when the timing measured none
LABEL = re.compile(r"(^|\n|>>)([ \t]*)[^\W\d_][\w.'’-]*(?: [^\W\d_][\w.'’-]*)?:(?=\s)")   # a speaker's name before a line, never spoken
ROLLUP = 0.5         # the share of a track's cues that repeat lines of the cue before, at or over which it is roll-up captions,
                     # see spoken(). Roll-up tracks sat at 0.69 or more, other tracks at 0.3 or less, paint-on captions among them.


def rolled(cues):
    """The text of each cue of cues [(start, end, text)], in order, less its top lines that repeat the bottom lines of the
    cue before it. Roll-up captions show each new line under the last ones, so those lines were spoken before the cue."""
    out, last = [], []
    for c in cues:
        lines = re.split(r"\n|\\[Nn]", c[2])
        said_by = [tokens(x) for x in lines]
        n = next((n for n in range(min(len(lines) - 1, len(last)), 0, -1) if said_by[:n] == last[-n:] and all(said_by[:n])), 0)
        out.append("\n".join(lines[n:]) if n else c[2])
        last = said_by
    return out


def spoken(cues):
    """cues [(start, end, text)], sorted, with the text that is spoken at each cue. On a roll-up track, ROLLUP of its
    cues or more repeat lines of the cue before, see rolled(). Each of its cues then keeps only its new lines, less a
    speaker's name, see LABEL, so every timing check reads a cue by the words said when it shows. Other tracks keep
    their text: a line said twice in a row there was spoken twice."""
    texts = rolled(cues)
    if sum(x != c[2] for x, c in zip(texts, cues)) < ROLLUP * len(cues):
        return cues
    return [(c[0], c[1], LABEL.sub(r"\1\2", x), *c[3:]) for c, x in zip(cues, texts)]


