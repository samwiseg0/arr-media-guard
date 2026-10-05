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
- blocks() times a block of cues that sits off while the rest of the track is in time, as after an edit. The sweep finds
  it, see suspects(), dense hearing hears it in full, see dense(), and only the cues of a block whose heard cues agree
  move.
- layout() checks a subtitle that no word check reads by where its lines show: over the spans of speech that Silero
  VAD found in the audio, see voiced().
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
# The timing. A cue starts when its first words are spoken. The subtitle's author and Whisper's word times both add
# spread, so on a right track some cues sit more than TOLERANCE off, and its windows differ a little.
CUE_LEAD = -0.05  # seconds a right track's cues start after the first heard word, the median over right tracks. A fix keeps it.
TOLERANCE = 0.3   # seconds a fix may leave at the file's start and end, and at a middle window
AGREE = 0.8       # the share of matched cues whose heard words must fall in the fixed span
MIN_CUES = 3      # cues each window needs whose first content word matched, before a fix
MIN_SHIFT = 0.75  # seconds a cue at the file's start or end may sit off before the times need a fix. Right tracks sat closer,
                  # and a 24/23.976 drift of an 18-minute episode sat farther.
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
    singing badly. windows() and dense() pick their windows by them."""
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


def check(heard, cues, lang, duration):
    """Whether the subtitle cues belong to the audio. heard is lid.listen()'s windows, [{"at": start, "words":
    [[seconds from start, word], ...]}]. cues is [(start, end, text)] in seconds, lang the subtitle's 639-2 language,
    whose stopwords drop out, and duration the file's in seconds.

    Returns {"verdict": "match", "mismatch" or "unknown", "why", "windows": [{"at", "words", "overlap", "offset",
    "cues"}], "timing": timing() of a match, else None}. A short phrase said again right after itself counts once, see
    said(). A window inside a longer window heard later is the same audio, and the longer one stands for it. A window
    with under MIN_WORDS heard content words names nothing, and the verdict needs two windows that do. All of those at
    MATCH or more is a match. All at MISMATCH or less is a mismatch. Anything else is unknown. A track under
    MIN_TRACK_CUES cues is unknown too."""
    stop = decide.STOPWORDS.get(lang, frozenset())
    cues = unflashed(sorted(cues))   # the ends of a flash track say nothing, see flash(), so its spans take the new ends
    if len(cues) < MIN_TRACK_CUES:
        return {"verdict": "unknown", "why": f"the track holds {len(cues)} cues, under {MIN_TRACK_CUES}", "windows": [], "timing": None}
    fl = flat(cues, stop)
    index = collections.defaultdict(list)
    for p, x in enumerate(fl):
        index[x[2]].append(p)
    end = lambda w: w["at"] + w.get("secs", WINDOW)
    heard = [w for w in heard if not any(x.get("secs", WINDOW) > w.get("secs", WINDOW) and x["at"] <= w["at"] and end(w) <= end(x) for x in heard)]
    got = [dict(match_window(said(w, stop), fl, index), at=w["at"], secs=w.get("secs", WINDOW)) for w in heard]
    out = {"windows": [{"at": w["at"], "words": g["words"], "overlap": g["overlap"], "offset": g["offset"], "cues": len({x[1] for _, x in g["pairs"]})}
                       for w, g in zip(heard, got)], "timing": None}
    few = sum(g["words"] < MIN_WORDS for g in got)
    got = sorted((g for g in got if g["words"] >= MIN_WORDS), key=lambda g: g["at"])   # in time order, for the ratio
    lap = ", ".join(f'{g["overlap"]:.0%}' for g in got)
    if len(got) < 2:
        return dict(out, verdict="unknown", why=f"{few} of {len(got) + few} windows hold under {MIN_WORDS} heard words")
    if all(g["overlap"] >= MATCH for g in got):
        return dict(out, verdict="match", why=f"the heard words match the cues at {lap}", timing=timing(got, cues, duration))
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
    stop, cues = decide.STOPWORDS.get(lang, frozenset()), unflashed(sorted(cues))
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


def timing(got, cues, duration):
    """fit() of the windows of got with MIN_CUES anchors or more, see anchors(). A window with fewer names no offset.
    When the other windows leave the fit short of evidence, "few" lists the start of each such window, for a longer
    window."""
    ends = [anchors(g["pairs"], cues) for g in got]
    few = [g.get("at") for g, e in zip(got, ends) if len(e) < MIN_CUES]
    r = fit([g for g, e in zip(got, ends) if len(e) >= MIN_CUES], [e for e in ends if len(e) >= MIN_CUES],
            [p for g in got for p in g["pairs"]], cues, duration)
    return dict(r, few=few) if few and not (r["fix"] or r.get("piecewise") or "unfixed" in r or r["why"] == "in time") else r


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
    and a peak away from the search's offset must be clear, see align(). A slice with no clear peak, or with under
    MIN_CUES pairs, gives no fix."""
    rs = [c[0] for c in ref]
    lo, hi = rs[0], rs[-1]
    ends, pairs, kept = [], [], []
    for k in range(SLICES):
        a, b = lo + k * (hi - lo) / SLICES, lo + (k + 1) * (hi - lo) / SLICES
        mine = [t for t in ts if a <= (t - offset) / float(rate) < b]
        if min(len(mine), bisect.bisect_left(rs, b) - bisect.bisect_left(rs, a)) < SPARSE:
            continue
        here = align(mine, rs, rate, offset, REACH, CLEAR)
        if here is None:
            return {"fix": None, "why": f"slice {k + 1} of {SLICES} has no clear offset within {REACH:.0f} s of the fit, so the times stay"}
        got = near(mine, rs, rate, here, PAIR)
        if len(got) < MIN_CUES:
            return {"fix": None, "why": f"slice {k + 1} of {SLICES} holds {len(got)} cues near the reference, under {MIN_CUES}, so the times stay"}
        first = ts.index(mine[0])
        ends.append([(x, t) for x, t, _ in got])
        pairs += [(x, (None, first + i)) for x, _, i in got]
        kept.append(k)
    early = sum(k < SLICES / 2 for k in kept)
    if min(early, len(kept) - early) < HALF:
        return {"fix": None, "why": f"the early half of the reference holds {early} slices with enough cues, and the late half "
                f"{len(kept) - early}, under {HALF}, so the times stay"}
    timing = fit(kept, ends, pairs, cues, duration, lead=lead, unit="slice")
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
                           # A block of clock() asks ONSET_SHARE. A whole track asks none, the owner's choice: under a music
                           # bed silencedetect marks few onsets. Set it to ONSET_SHARE to ask it again.
LAYOUT_FIX = "write"   # what a fix of layout_fix() that the speech onsets confirm does: "alert" only, or "write" the new
                       # times. On planted steps of a review's shapes no right line moved, but no rule makes a right line
                       # safe for sure, and runs of lines at the ends stay where they are, see shifted() and docs/design.md.
LAYOUT_EDGE = 0.08   # the share of the cue spans at each end of the file, MIN_TRACK_CUES at least, that must line up with
                     # the speech best at a fix of layout_fix(), see edges()
LAYOUT_PARTS = 20    # parts of the cue spans, equal by count, none of which may vote for another offset, see edges()


def stepped(offsets):
    """The slices of a piecewise fit, at offsets, show a step: two or more of them lie within TOLERANCE of one offset
    at least 1 s from the offset most slices share. One slice alone off, or a slow trend, may be noise, such as an end
    song a few lines time, or a ratio the search missed."""
    group = lambda u, xs: [v for v in xs if abs(v - u) <= TOLERANCE]
    main = max(offsets, key=lambda u: len(group(u, offsets)))
    off = [u for u in offsets if abs(u - main) >= 1.0]
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
SAT_CHANCE = 0.05    # the chance at most of as many such starts in that run. The owner's choice: on planted subtitles
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
    Else there are too few. clock() asks a block for ONSET_SHARE of its cues. A whole track asks LAYOUT_ONSET_SHARE:
    under a music bed silencedetect marks few onsets, and verified real shifts of about 1 s drew 9% to 19% of their
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
        term, below = math.exp(-chance), 0.0   # the Poisson tail by a running term, as clock() takes it
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


SWEPT = 3         # windows of the sweep in each half of the file that heard MIN_WORDS words and put an offset on the cues,
                  # before a clean sweep makes a match with too few anchors a reference, see clean()


def clean(rows, duration):
    """The sweep() rows show a track in time: in each half of the file SWEPT windows or more heard MIN_WORDS words and
    gave an offset, every window that heard MIN_WORDS words matched at MATCH or more, and every such offset lies under
    MIN_SHIFT."""
    heard = [r for r in rows if r["words"] >= MIN_WORDS]
    timed = [r for r in heard if r["offset"] is not None]
    return all(sum((r["at"] < duration / 2) == early for r in timed) >= SWEPT for early in (True, False)) \
        and all(r["overlap"] >= MATCH for r in heard) and all(abs(r["offset"]) < MIN_SHIFT for r in timed)


def sweep(heard, cues, lang, timing=None):
    """One row per heard window of the sweep of --sub-time, in time order: {"at", "words": its heard content words,
    "overlap", "cues": the matched cues whose first word matched, "offset": their median cue start less the heard time
    and CUE_LEAD, or None, "off": that offset less the fitted line there, or None}. The fitted line is the fix of
    timing, the word check's, else no offset. The rows only report."""
    stop, cues = decide.STOPWORDS.get(lang, frozenset()), sorted(cues)
    fl, index = flat(cues, stop), collections.defaultdict(list)
    for p, x in enumerate(fl):
        index[x[2]].append(p)
    fix = (timing or {}).get("fix")
    rate, offset = (float(Fraction(fix["rate"])), fix["offset"]) if fix else (1.0, 0.0)
    rows = []
    for w in sorted(heard, key=lambda w: w["at"]):
        g = match_window(said(w, stop), fl, index)
        e = anchors(g["pairs"], cues)
        o = round(statistics.median(c - t for t, c in e) - CUE_LEAD, 2) if e else None
        at = statistics.median(t for t, _ in e) if e else w["at"]
        rows.append({"at": w["at"], "words": g["words"], "overlap": g["overlap"], "cues": len(e), "offset": o,
                     "off": None if o is None else round(o - (rate - 1) * (at + CUE_LEAD) - offset, 2)})
    return rows



# The block timing of the sweep (docs/design.md, "Subtitle match"). A part of a track can sit off while the rest is in
# time, as after an edit. The sweep finds such parts, dense hearing hears them in full, and blocks() moves the cues of a
# part only when its heard cues agree.
BLOCK_SHIFT = 0.5   # seconds a block's heard cues sit off the cues around them at least, when the speech onsets agree. On
                    # right tracks the median of 6 heard cues in a row stayed at about 0.30 s or less, see BLOCK_LEAD.
BLOCK_ALONE = 1.0   # seconds a block must sit off to move on Whisper alone, when too few cues have a speech onset. Whisper's
                    # error can run across a stretch of a right track, so 6 heard cues agree off the line while the onsets
                    # show the cues in time. A fuzz with that error moved 6 of 1,414 right tracks at 0.7 s and 1 at 1.0 s.
BLOCK_CUES = 6      # heard cues a block needs, see anchors()
BLOCK_SPREAD = 0.25 # the share of a block's shift within which its heard cues agree, when that is more than TOLERANCE. An
                    # anchor so never sits nearer the line than three quarters of the shift.
BLOCK_HEAR = 360.0  # seconds of audio dense hearing hears per track at most
BLOCK_MARGIN = 120.0   # seconds a part reaches past its suspect rows at most, see suspects()
TRIM_MARGIN = 30.0     # seconds of a part's margin that the cap of BLOCK_HEAR leaves at least, before it trims the rows' stretch
EDIT_CUES = 2       # heard cues at each end of a block within which the mark of an edit puts its edge, see blocks()
FURTHER = 30.0      # seconds a second hearing reaches past the end of a part where dense hearing did not see the edge of a block
OVERLAP = 2.5       # seconds two windows of dense hearing share, so a window edge never cuts a cue of up to 2.5 s in both


def merged(parts):
    """[[lo, hi]] of parts [(lo, hi)], in order, with parts that touch merged."""
    out = []
    for lo, hi in sorted(parts):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def trusted(rows):
    """(the sweep() rows that count, in time order, the track's lean: their median off). A row counts when it heard
    MIN_WORDS words, matched them at MATCH or more and matched 2 cues or more by their first word. A row that matched
    less may be another track's speech. A right track leans a little off its fitted line, so rows are judged against
    the lean."""
    rows = sorted((r for r in rows if r["words"] >= MIN_WORDS and r["overlap"] >= MATCH and r["cues"] >= 2 and r["off"] is not None),
                  key=lambda r: r["at"])
    return rows, (statistics.median(r["off"] for r in rows) if rows else 0.0)


def suspect_rows(rows):
    """{row time: seconds off the track's lean} of the suspect rows of the sweep() rows of one track: a row that
    counts, see trusted(), and sits BLOCK_SHIFT or more off the lean. subtitles.heard_parts() takes them as suspects()
    does."""
    rows, lean = trusted(rows)
    return {r["at"]: abs(r["off"] - lean) for r in rows if abs(r["off"] - lean) >= BLOCK_SHIFT}


def centred(lo, hi, sus, left):
    """(lo, hi) of the stretch of left seconds inside the part (lo, hi), around its row farthest off of sus, see
    suspect_rows(). That row ranked the part, so it is heard. With under WINDOW seconds left, the stretch has no length
    and lies at that row."""
    f = max((t for t in sus if lo <= t < hi), key=sus.get, default=lo) + WINDOW / 2   # the middle of the row farthest off
    a = min(max(lo, f - left / 2), hi - left) if left >= WINDOW else f - WINDOW / 2
    return a, (a + left if left >= WINDOW else a)


def suspects(rows, duration):
    """The parts of the audio, [(lo, hi)] in seconds, that dense hearing hears for blocks(). rows are the sweep() rows
    of one track. A suspect is a row of suspect_rows(). A single row triggers, so no block is missed. The gates of
    blocks() decide every move, and right-track noise only costs a hearing. A part reaches BLOCK_MARGIN seconds before
    and after its suspect rows, or to the nearest row that counts and sits within TOLERANCE of the lean when that is
    nearer. Dense hearing finds the edges in it. Parts that touch merge. Parts that cover over half the file give [],
    because then the whole track is off, and fit() judges that.

    The parts hold BLOCK_HEAR seconds at most, and the parts with the rows farthest off come first. A part that does
    not fit in what is left keeps its suspect rows, and its margins shrink to one length on both sides, down to
    TRIM_MARGIN or the margin it had. When that still does not fit, the part is the stretch that fits inside it,
    centred on its row farthest off, see centred(). A part with under WINDOW seconds left is never dropped: it gives
    (lo, lo), which dense() does not hear, and blocks() names the cap as its why."""
    rows, lean = trusted(rows)
    near = [r["at"] for r in rows if abs(r["off"] - lean) <= TOLERANCE]
    sus = suspect_rows(rows)
    parts = merged((max(t - BLOCK_MARGIN, max((a for a in near if a < t), default=0.0)),
                    min(t + WINDOW + BLOCK_MARGIN, min((a + WINDOW for a in near if a > t), default=duration), duration)) for t in sus)
    if sum(hi - lo for lo, hi in parts) > duration / 2:
        return []
    kept, left = [], BLOCK_HEAR
    for lo, hi in sorted(parts, key=lambda p: -max(o for t, o in sus.items() if p[0] <= t < p[1])):
        a, b = min(t for t in sus if lo <= t < hi), max(t for t in sus if lo <= t < hi) + WINDOW   # its suspect rows
        if hi - lo > left:
            small, room = sorted((a - lo, hi - b)), left - (b - a)
            x = room - small[0] if 2 * small[0] <= room else room / 2   # one margin length: min(margin, x) on each side
            lo, hi = (a - min(a - lo, x), b + min(hi - b, x)) if x >= min(TRIM_MARGIN, small[1]) else centred(lo, hi, sus, left)
        kept.append((round(lo, 1), round(hi, 1)))
        left -= hi - lo
    return sorted(kept)


def dense(cues, parts, duration, stop=frozenset()):
    """[window start] of dense hearing: windows of WINDOW seconds that hear each part of parts [(lo, hi)] in full. Two
    windows in a row share OVERLAP seconds or more, so each cue starts at least OVERLAP seconds before the end of one
    window. As in windows(), a window with no cue content word, song lyrics left out, within PAD seconds is not heard.
    cues are [(start, end, text)] in audio time: the caller moves each track's cues by its fix first, see moved().
    With the small model on one thread, this hearing costs about 45 CPU seconds a minute of audio."""
    ts = word_times(cues, stop)
    out = []
    for lo, hi in merged(p for p in parts if p[1] > p[0]):   # a part of no length is one the cap left unheard
        lo, hi = max(0.0, lo), min(duration, hi)
        span = max(0.0, hi - lo - WINDOW)
        n = math.ceil(span / (WINDOW - OVERLAP))
        out += [a for a in (round(lo + k * span / n, 1) if n else round(lo, 1) for k in range(n + 1))
                if bisect.bisect_left(ts, a + WINDOW + PAD) > bisect.bisect_left(ts, a - PAD)]
    return out


def spread(shift):
    """Seconds within which the heard cues of a block shift seconds off agree: TOLERANCE, or BLOCK_SPREAD of the shift
    when that is more. A block 2 s off agrees within 0.5 s, and its anchors still lie 1.5 s or more off the line."""
    return max(TOLERANCE, BLOCK_SPREAD * abs(shift))


def agreeing(late, base):
    """Whether anchors in a row, given by how late each sits, form the core of a block off base, see blocks()."""
    m = statistics.median(late)
    if abs(m - base) < BLOCK_SHIFT:
        return False
    ok = [abs(x - m) <= spread(m - base) for x in late]
    clean = all(abs(x - m) < abs(x - base) for x in late)   # no anchor nearer the line inside: a cue in time never joins
    return clean and ok[0] and ok[-1] and sum(ok) >= AGREE * len(ok) and all(any(ok[k:k + MIN_CUES]) for k in range(len(ok) - MIN_CUES + 1))


def widest(late, base, a, b):
    """(i, j) of the longest run late[i:j] inside late[a:b] that agreeing() takes, the earliest of equal runs, or None."""
    for n in range(b - a, BLOCK_CUES - 1, -1):
        for i in range(a, b - n + 1):
            if agreeing(late[i:i + n], base):
                return i, i + n
    return None


# The anchors of dense hearing. The quick stage anchors a cue at its first content word, see anchors(). Dense hearing
# anchors it at its first spoken word, because a cue's leading stopwords put the first content word late. On right
# tracks that took the largest median of 6 heard cues in a row from 0.63 s to 0.30 s, and the bias from -0.21 s to 0 s.
BLOCK_LEAD = -0.03  # seconds a right track's cue starts after its first spoken word, as dense hearing anchors it: the
                    # median on right tracks
SPOKEN_GAP = 1.0    # seconds between two heard words under which the anchor steps back over a cue's leading stopword
SHORT_CUE = 0.05    # seconds a cue shows at least to anchor. A cue of 1 ms that repeats a neighbour's text paired with
                    # that neighbour's speech, 3 s off.
IN_LINE = 2 * BLOCK_CUES   # heard cues a part needs before dense hearing calls it in time, see blocks()
IN_LINE_SHARE = 0.7        # the share of a part's cues with words that must anchor before dense hearing calls it in time
IN_LINE_OFF = 0.3          # seconds 3 heard cues in a row may each sit off the same way in a part that is in time
LONG_QUIET = 2.5           # seconds with no cue before a cue that never anchors: such a cue read 0.37 s late at its median,
                           # as Whisper times the first word after a silence early
# The second clock (docs/design.md, "Subtitle match"): speech onsets, where a silence ends. They never use Whisper.
ONSET_MIN = 3       # cues with an onset that a block needs inside and outside it before the onsets judge it
ONSET_QUIET = 0.5   # seconds of the silence an onset must end to time a cue
ONSET_REACH = 1.0   # seconds around the start of a cue around a block in which its onset must be the only one
ONSET_TWICE = 0.1   # seconds within which two onsets are one, found by two reads that overlap. A read that starts in a
                    # silence counts it from its own start. Onsets that each end a silence of 0.3 s lie farther apart.
ONSET_SHARE = 0.2   # the share of a block's cues with an onset where the block puts them, at least. Stray onsets are
                    # sparse, under one a second, so they fill few such places by chance.
ONSET_CHANCE = 0.001  # the chance at most that the onsets within half of TOLERANCE of a block's cues are as many by chance,
                      # from the same count at the NULL offsets. With real noise a right track that Whisper heard 0.8 s
                      # early drew 4 such onsets of 13 cues, at a chance of 0.003.


def tokens(text):
    """Every word of a cue or of heard speech, lower case, with no tags, no sounds in brackets and no apostrophes: the
    words of words() with the stopwords and the one-letter words kept."""
    return TOKEN.findall(NOISE.sub(" ", text).lower().replace("’", "").replace("'", ""))


def clipped(cues):
    """cues with each end at most the next later start. At the end of a late block its last cue shows over the start
    of the next cue, and flat() then puts their words out of order. In that order the search drops the first word of
    the next cue, so that cue never anchors. Speech says the cues in their order."""
    later = sorted({c[0] for c in cues}) + [float("inf")]
    return [(c[0], min(c[1], later[bisect.bisect_right(later, c[0])]), *c[2:]) for c in cues]


def heard_anchors(heard, cues, stop):
    """({cue index: audio time of its first spoken word}, {cue index: {place of a word in the cue: audio times it was
    heard}}) of dense hearing. heard is lid.listen()'s windows, and cues [(start, end, text)] sorted. Only a window that heard MIN_WORDS
    words and matched at MATCH or more counts.

    A cue anchors where its first content word matched, as in anchors(). The anchor then steps back over the cue's
    words before it, while each heard word before it is that word and lies under SPOKEN_GAP before it. Four rules drop
    an anchor that would mislead. A cue that shows under SHORT_CUE seconds never anchors, and neither do two cues in a
    row with the same content words. A heard word that anchors two cues anchors neither. Two windows within TOLERANCE
    of each other in time hear one word. The windows overlap, so a cue can anchor in two of them. When they time it
    more than TOLERANCE apart it anchors in neither, else it keeps the anchor heard farthest from its window's edges.
    The heard words of a cue that never anchors by the first two rules are left out too."""
    fl, index = flat(clipped(unflashed(cues)), stop), collections.defaultdict(list)   # a flash track's spans take its new ends
    for p, x in enumerate(fl):
        index[x[2]].append(p)
    said_by = [words(c[2], stop) for c in cues]
    bad = {i for i, (s, e, _) in enumerate(cues) if e - s < SHORT_CUE}
    bad |= {k for i in range(len(cues) - 1) if said_by[i] and said_by[i] == said_by[i + 1] for k in (i, i + 1)}
    # claims: (cue, heard time of its first content word, that word, spoken time, distance from the window's edges)
    claims, said_at = [], collections.defaultdict(lambda: collections.defaultdict(list))
    for w in heard:
        g = match_window(said(w, stop), fl, index)
        if g["words"] < MIN_WORDS or g["overlap"] < MATCH:
            continue
        end, toks = w["at"] + w.get("secs", WINDOW), [(w["at"] + s, x) for s, word in w["words"] for x in tokens(word)]
        for t, x in g["pairs"]:
            if x[1] in bad:
                continue
            said_at[x[1]][x[3]].append(t)
            if x[3]:
                continue
            ws = tokens(cues[x[1]][2])
            lead = ws[:next((k for k, v in enumerate(ws) if v == x[2]), 0)]   # the cue's words before its first content word
            q = next((k for k, (s, v) in enumerate(toks) if s == t and v == x[2]), 0)
            spoken = t
            for v in reversed(lead):
                if q == 0 or toks[q - 1][1] != v or spoken - toks[q - 1][0] >= SPOKEN_GAP:
                    break
                q, spoken = q - 1, toks[q - 1][0]
            claims.append((x[1], t, x[2], spoken, min(t - w["at"], end - t)))
    shared = {k for k, a in enumerate(claims) for b in claims if a[2] == b[2] and a[0] != b[0] and abs(a[1] - b[1]) <= TOLERANCE}
    by = collections.defaultdict(list)
    for k, a in enumerate(claims):
        if k not in shared:
            by[a[0]].append(a)
    return {i: max(xs, key=lambda a: a[4])[3] for i, xs in by.items() if max(a[3] for a in xs) - min(a[3] for a in xs) <= TOLERANCE}, said_at


def placed(start, ts, line, late):
    """The side a cue lies on, "line" or "block", by ts, the audio times of its matched heard words, one per word. start is
    the cue's start in audio time, and line and late are how late the cues on the line and the cues of the block sit.
    A cue's speech starts at its start less how late it sits, so a word heard over TOLERANCE before that rules the side
    out. Two distinct heard words must rule it out, so one chance pair never decides. A word heard late proves nothing,
    because speech can run past the end of its cue. When only one side stays, the cue lies there. Else it is "unsure",
    and None with no heard word."""
    if not ts:
        return None
    fits = lambda d: sum(t < start - BLOCK_LEAD - d - TOLERANCE for t in ts) < 2
    a, b = fits(line), fits(late)
    return "line" if a and not b else "block" if b and not a else "unsure"


def split(sides, left):
    """(e, n) of an edge of a block among the cues between the block and the cues on the line, or the file's edge.
    sides holds placed() of each of those cues in order. With left, the block starts at cue e, else it ends before cue
    e. A cue placed in the block moves. Every other cue stays: one on the line, an unsure one and one with no heard
    word. No gap between cue starts decides, because a block can start mid-scene beside a scene cut, and the gap rule
    moved cues in time there. n counts the cues that stay and are not placed on the line, so the report can name them.
    When the sides disagree, every cue that must stay stays."""
    stays = [k for k, x in enumerate(sides) if x != "block"]
    e = (max(stays) + 1 if stays else 0) if left else (min(stays) if stays else len(sides))
    return e, sum(x != "line" for x in (sides[:e] if left else sides[e:]))


def clock(onsets, at, inside, outside, shift, read=None):
    """The speech onsets' judgement of a block: {"verdict", "inside", "original", "outside", "shift"}. onsets are sorted
    (time, seconds of the silence it ends), at maps a cue to its start in audio time, inside and outside are the cues
    of the block and the cues of its part around it, and shift is Whisper's. An onset counts only when it ends a silence
    of ONSET_QUIET or more.

    The lead is the median of cue start less onset over the cues around the block, each timed by the only onset within
    ONSET_REACH of its start. A cue of the block counts where the block puts it when an onset lies within reach of its
    start less the lead and the shift, and where it sat when one lies within reach of its start less the lead. The
    reach is TOLERANCE, and half the shift at most, so the two places never share an onset. Stray onsets land at both
    places alike, so the verdict weighs the two counts against ONSET_MIN and ONSET_SHARE of the block's cues. It is
    "agree" when that many count where the block puts them, twice as many as where they sat, and the median of their
    cue start less the lead and onset lies within half of TOLERANCE of Whisper's shift and sits BLOCK_SHIFT off the same
    way. The search reaches twice that far, so onsets that cluster off Whisper's shift disagree. That many must also lie
    within half of TOLERANCE of the place, or the onsets are too few: stray onsets scatter over the reach. And that
    count must be rare by chance. The same count at the NULL offsets from those places, over the places within read,
    (lo, hi) of the audio whose onsets were read, gives the mean count by chance, in "chance", and the rate of all
    counted onsets in read gives at least as much. The chance of as many or more must be ONSET_CHANCE at most. Cues an
    author set 0.4 s late are no block. It is "disagree" when that many count where they sat, at least as many as where
    the block puts them, or when the onsets count clearly and put the block elsewhere. Else it is "few": under
    ONSET_MIN cues around the block have an onset, or too few cues count at either place. "inside", "original" and
    "outside" count those cues, and "shift" is the median of cue start less the lead and its onset over the cues that
    count where the block puts them, or None. "kept" is the set of cues of
    the block with an onset where they sat, and "moved" the set with an onset within half of TOLERANCE of where the
    block puts them, the only onset within ONSET_REACH of it, and none where they sat. "stayed" is the same for where
    they sat. blocks() takes them for the edges and for the evidence of each cue. A cue in time that an author set
    0.4 s late has its onset between the two places."""
    times = [o[0] for o in onsets]
    near = lambda t, reach: onsets[bisect.bisect_left(times, t - reach):bisect.bisect_right(times, t + reach)]   # of any silence
    span = lambda t, reach: [o for o in near(t, reach) if o[1] >= ONSET_QUIET]
    alone = [at[k] - o[0][0] for k in outside if len(o := near(at[k], ONSET_REACH)) == 1 and o[0][1] >= ONSET_QUIET]
    if len(alone) < ONSET_MIN:
        return {"verdict": "few", "inside": 0, "original": 0, "outside": len(alone), "shift": None, "moved": set(), "kept": set(), "stayed": set()}
    lead, reach = statistics.median(alone), min(TOLERANCE, abs(shift) / 2)
    moved = {k: at[k] - lead - min(o, key=lambda x: abs(at[k] - lead - shift - x[0]))[0] for k in inside if (o := span(at[k] - lead - shift, reach))}
    kept = {k for k in inside if span(at[k] - lead, reach)}
    new, old = list(moved.values()), len(kept)
    # An onset close to the new place, alone within ONSET_REACH, and none where the cue sat: an edge needs that much.
    sure = {k for k, d in moved.items() if abs(d - shift) <= TOLERANCE / 2 and k not in kept and len(near(at[k] - lead - d, ONSET_REACH)) == 1}
    # The same for the place where the cue sat: its own onset there, alone, and none where the block puts it.
    stayed = {k for k in kept if k not in moved and len(o := near(at[k] - lead, ONSET_REACH)) == 1 and abs(at[k] - lead - o[0][0]) <= TOLERANCE / 2}
    out = {"inside": len(new), "original": old, "outside": len(alone), "shift": round(statistics.median(new), 3) if new else None,
           "moved": sure, "kept": kept, "stayed": stayed}
    need = max(ONSET_MIN, ONSET_SHARE * len(inside))
    clear = len(new) >= need and len(new) >= 2 * old
    there = clear and abs(out["shift"] - shift) <= TOLERANCE / 2 and abs(out["shift"]) >= BLOCK_SHIFT and out["shift"] * shift > 0
    # Stray onsets scatter over the reach, and a block's own onsets cluster where it puts its cues. So that many must lie
    # within half of TOLERANCE of the place: on a right track Whisper heard 0.86 s early, 4 strays of 16 cues agreed.
    tight = sum(abs(d - shift) <= TOLERANCE / 2 for d in new)
    # Chance gives the same count at the NULL offsets from where the block puts its cues, as lift() takes it, over the
    # places in read, the audio whose onsets were read. The count where the block puts them must be rare at that rate.
    lo, hi = read or ((onsets[0][0], onsets[-1][0]) if onsets else (0.0, 0.0))
    places = [p for k in inside for d in NULL if lo <= (p := at[k] - lead - shift + d) <= hi]
    chance = len(inside) * sum(bool(span(p, TOLERANCE / 2)) for p in places) / len(places) if places else 0.0
    # Where lines start on a regular pitch, the NULL offsets can all fall between their onsets and count none. Chance
    # is then at least the rate of all counted onsets in read over those places.
    if hi > lo:
        chance = max(chance, len(inside) * TOLERANCE * sum(lo <= o[0] <= hi and o[1] >= ONSET_QUIET for o in onsets) / (hi - lo))
    # The Poisson tail by a running term: chance ** i / i! overflows a float at i = 171.
    term, below = math.exp(-chance), 0.0
    for i in range(tight):
        below, term = below + term, term * chance / (i + 1)
    rare = 1 - below <= ONSET_CHANCE
    out["chance"] = round(chance, 2)
    agree = there and tight >= need and tight >= 2 * old and rare
    against = (clear and not there) or (old >= need and old >= len(new))   # the cues kept their onsets where they sat
    return dict(out, verdict="agree" if agree else "disagree" if against else "few")


def evidence(late, hushed_late, words, onset_new, onset_old, on, at_block, hushed, conflict=False):
    """"line", "block" or None: where a cue's own evidence puts it. late is its anchor, how late it sits, and hushed_late
    the anchor of a cue after a long silence, which only ever puts it on the line, see blocks(). words is placed() of its
    heard words, and the heard words of such a cue never put it in the block. onset_new tells that an onset lies where
    the block puts it and none where it sits, and onset_old that one lies where it sits. on and at_block tell whether an
    anchor lies nearer the line than the block, and at the block's offset. Any evidence for the line wins. An onset
    puts a cue in the block only with its own heard words placed there, and only the cue after a long silence needs it.
    Beside a block, placed() reads a cue in time as unsure, and a lone onset where the block would put it is another
    sound as often as the cue's speech. conflict tells that its anchor sits over TOLERANCE off the block, though nearer
    it than the line. Its words then put it in the block only with an onset there: between two blocks, a cue in time
    whose words read like the block once moved on them while its own anchor said otherwise."""
    if late is not None and on(late) or hushed_late is not None and on(hushed_late) or words == "line" or onset_old:
        return "line"
    if late is not None and at_block(late) or words == "block" and (not (hushed or conflict) or onset_new):
        return "block"
    return None


# The safety rules of a block move (docs/development.md, "Safety self-checks"). AMG_INVARIANTS=1 checks them on every
# result. blocks() checks own evidence and nearer its speech at its end, and remux.time_plan() checks the order. The
# tests turn it on. Off, no check runs.
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
    """The block of blocks that moves a cue that starts at t, as remux.time_plan() moves it, or None: the first whose
    [from, to) holds t and whose "keep" does not name it."""
    return next((b for b in blocks or () if b["from"] <= t < b["to"] and round(t, 3) not in b.get("keep", ())), None)


def shift_of(block, t):
    """The shift by which block moves its cue that starts at t: its own shift in "shifts" on a live-captioned track,
    see live_moves(), else the shift its "clamp" names for that start, else the block's. See clamped()."""
    if "shifts" in block:
        return block["shifts"][round(t, 3)]
    return next((v for c, v in block.get("clamp") or () if c == round(t, 3)), block["shift"])


def check_blocks(cues, blocks, proved, case):
    """Check two safety rules on the blocks of blocks(), see INVARIANTS. cues are the track's cues in start order. proved
    holds one entry for each block that moves. The entry gives the block, its median anchor "mid", its spread "tol", the
    run's median "m", the part's line "base" and the line its move uses "ref". Its "cues" maps each cue the block moves,
    by index, to how late its anchor sits or None, whether it follows a long silence, placed() of its words or None, its
    own onset where it sat, and its own onset where the block puts it. Its "between" maps each cue that stays between
    two cues the block moves to the first four of those. Raises Broken.

    Own evidence: a cue that a block moves is one the block proved, by the rules of evidence(). Its anchor sits at the
    block's offset, or its words lie in the block. A cue after a long silence, or one whose anchor sits over TOLERANCE
    off the block, moves on its words only with its own onset where the block puts it. An anchor nearer the part's line
    than the run, or words placed on the line, keep the cue where it is. Such evidence, or the cue's own onset where it
    sat, also cuts the block, so no such cue stays between two cues the block moves. A cue in "keep", or outside every
    block, moves only by the whole-track fix, see mover().

    Nearer its speech: a moved cue never ends up farther from its speech than it was. Its anchor gives its speech. The
    rule breaks only when the cue ends farther from both the line the move uses and the part's line, each by more than
    NEAR. The line the move uses is the line beside the block, or the track's lean when the move takes that. So the real
    margin is NEAR plus the distance between the two lines, up to about 0.15 s beside a block and 0.3 s against the lean.
    The rule catches an overshoot, and the harnesses measure a wrong move against the planted truth. The first cue after
    a long silence never anchors here, see blocks(). A cue with its own onset where it sat stays, because a shift of
    BLOCK_SHIFT takes it farther from that onset."""
    lined = lambda p, late, said, onset: late is not None and abs(late - p["base"]) <= abs(late - p["m"]) or said == "line" or onset   # as evidence() takes it
    for p in proved:
        for k, (late, _, said, onset) in p.get("between", {}).items():
            if lined(p, late, said, onset):
                broken("own evidence", cues[k][0], "its own evidence puts it on the line, so the block must not move the cues on both sides of it", case)
    for k, c in enumerate(cues):
        b = mover(blocks, c[0])
        if b is None:
            continue
        p = next((p for p in proved if p["block"] is b), None)
        if p is None or k not in p["cues"]:
            broken("own evidence", c[0], "a block moves it, but the block never proved it", case)
        late, hushed, said, onset, onset_new = p["cues"][k]
        on = lambda x: abs(x - p["base"]) <= abs(x - p["m"])   # nearer the part's line than the run, as blocks() takes it
        own = None if hushed else late   # the anchor of a cue after a long silence only ever keeps it on the line
        conflict = own is not None and abs(own - p["mid"]) > TOLERANCE
        at_block = own is not None and abs(own - p["mid"]) <= p["tol"] and not on(own)
        if late is not None and on(late) or said == "line" or not (at_block or said == "block" and (not (hushed or conflict) or onset_new)):
            anchor = "no anchor" if late is None else f"an anchor {late:+.3f} s late" + (" after a long silence" if hushed else "")
            broken("own evidence", c[0], f"it has {anchor}, its words place it {said or 'nowhere'}, and {'an' if onset_new else 'no'} onset of its "
                   f"own lies where the block puts it, against the block at {p['mid']:+.3f} s and the part's line at {p['base']:+.3f} s", case)
        moved_by = shift_of(b, c[0])   # a clamped cue moves part of the way, see clamped()
        farther = lambda line: abs(late - moved_by - line) > abs(late - line) + NEAR
        if own is not None and farther(p["ref"]) and farther(p["base"]):
            broken("nearer its speech", c[0], f"its anchor sits {late - p['ref']:+.3f} s off the line the move uses and {late - p['base']:+.3f} s off the part's "
                   f"line, and {late - moved_by - p['ref']:+.3f} s and {late - moved_by - p['base']:+.3f} s after the move", case)
        if onset:
            broken("nearer its speech", c[0], "its own onset lies where it sat", case)


def ordered(rows, case):
    """Check the order rule, see INVARIANTS, on rows [(start, new start with no block, new start)] of one track's
    remux.time_plan(). Two cues in start order keep their order, and two whose starts did not tie never tie. Two cues
    that time_plan() clamps to 0 may tie, because no start lies before 0. A moved cue may still start before the end of
    a cue that stays. Raises Broken."""
    rows = sorted(rows)
    for (s0, a0, n0), (s1, a1, n1) in zip(rows, rows[1:]):
        if s0 < s1 and (n0 > n1 or n0 == n1 > 0 and a0 < a1):
            broken("order", s1, f"it starts at {n1} s, and the cue at {s0} s before it at {n0} s", case)


def fixed_ms(t, fix):
    """The time in ms where fix puts a cue time t in seconds, before a block moves it, as remux.time_plan() takes it."""
    return moved(t * 1000, fix) if fix else t * 1000


def written(t, shift, fix):
    """The start in centiseconds that remux.time_plan() writes for a cue that starts at t in its own time and moves by
    shift. ASS keeps centiseconds, so a test on them never lets two starts tie."""
    return round((fixed_ms(t, fix) - shift * 1000) / 10)


def crossing(starts, moving, first, last, shift, fix):
    """(k, j) of a cue k of moving, indices into the sorted cue starts, whose move by shift would put it at, past or on
    the same centisecond as j, a cue next to it that stays, from first - 1 to last + 1, or None. Cues that move together
    keep their order, so a pair of a cue that moves and one that stays is all a crossing needs."""
    new = lambda k: written(starts[k], shift if k in moving else 0.0, fix)
    return next(((k, k + 1) if k in moving else (k + 1, k) for k in range(max(0, first - 1), min(len(starts) - 1, last + 1))
                 if (k in moving) != (k + 1 in moving) and new(k) >= new(k + 1)), None)


def clamped(starts, moving, first, last, shift, fix, may=lambda k: True):
    """{k: shift} of each cue k of moving, indices into the sorted cue starts, whose move by shift would put it at,
    past or on the same centisecond as the cue next to it on the side it moves toward. That cue stays, or moves less
    by this clamp. The shift puts the cue one centisecond short of that cue instead, so it moves only part of the way
    to its speech. It is 0 when no part of the move is left, or when may(k) is false: such a cue stays, as it did
    before the clamp. The cues lie from first - 1 to last + 1. A block moves its cues early when shift is over 0, so the
    cue before each one bounds it, and late when under 0. A cue next to a block keeps its place, as in crossing(). Two
    cues that start at once keep one start, as remux.time_plan() keeps a start."""
    d, new, out = (1 if shift > 0 else -1), {}, {}
    for k in (range(first, last + 1) if shift > 0 else range(last, first - 1, -1)):
        if k not in moving:
            continue
        j = k - d   # the cue next to it on the side it moves toward
        if j in new and round(starts[j], 3) == round(starts[k], 3):   # two lines shown at once move as one
            new[k] = new[j]
            if j in out:
                out[k] = out[j]
            continue
        full = written(starts[k], shift, fix)
        bound = new[j] if j in new else written(starts[j], 0.0, fix) if 0 <= j < len(starts) else None
        if bound is None or (full - bound) * d > 0:
            new[k] = full
            continue
        target = bound + d
        if not may(k) or (written(starts[k], 0.0, fix) - target) * d <= 0:   # not allowed, or no room left: it stays
            new[k], out[k] = written(starts[k], 0.0, fix), 0.0
        else:
            new[k], out[k] = target, round((fixed_ms(starts[k], fix) - target * 10) / 1000, 6)
    return out


def blocks(heard, cues, lang, timing, parts, onsets=None, rows=None):
    """The blocks of one track: runs of cues that sit off the fitted line while the cues around them sit on it, as after
    an edit (docs/design.md, "Subtitle match"). heard is lid.listen()'s windows of dense hearing, see dense(), cues the
    track's [(start, end, text)] in seconds, lang its 639-2 language, timing the word check's timing(), whose fix may be
    None, parts the parts that dense hearing heard, see suspects(), and onsets the speech onsets of those parts in audio
    seconds, sorted. An onset is its time, or (time, seconds of the silence it ends). A bare time counts as ending a
    silence of ONSET_QUIET or more. rows are the track's sweep() rows, or None.

    Returns {"blocks": [{"from", "to", "shift", "cues", "anchors", "spread", "edge_left", "edge_right", "onsets", "keep",
    "unproved"}],
    "parts": [{"lo", "hi", "anchors", "median", "in_line", "why", "onsets", "more"}]}. A block holds every cue whose
    start lies in [from, to), in the track's own cue time before any fix. shift is how late its cues sit against their
    speech after the fix, against the cues around it: the median over its anchors, see heard_anchors(), less the median
    of the BLOCK_CUES nearest anchors on the part's line at each side. So the block keeps the lead of the cues around
    it. With rows, it is the median less the track's lean when that is smaller. A block fix moves each of its cues to moved(start, fix) - shift. "cues" counts the cues of the block and
    "anchors" its heard cues. spread is the distance from the median of its anchors within which AGREE of them lie.
    edge_left and edge_right count the cues at each edge that stay where they are and are not placed on the line, see
    split(). "onsets" holds the onsets' judgement of the block, see clock(). A part's "more" holds the stretches of
    audio, [(lo, hi)], past its ends that a second hearing hears: FURTHER seconds past an end where a block's edge was
    not seen, see further().

    A part gives its count of heard cues and their median, how late they sit. It is in line when IN_LINE heard cues or
    more anchor and so does IN_LINE_SHARE of its cues with words, they sit within TOLERANCE of the line at their
    median, no BLOCK_CUES in a row sit BLOCK_SHIFT off the part's line at theirs, and no MIN_CUES in a row each sit
    IN_LINE_OFF off it the same way. The last rule catches a block of a few cues near BLOCK_SHIFT. Then the sweep rows
    there were noise, see after_blocks(). why says why the part holds no block, else it is None.

    A part of no length is one the cap of suspects() left unheard, and its why says so. The part's line is the median of
    its anchors within TOLERANCE of the fitted line. A block needs BLOCK_CUES anchors in a row. AGREE of them lie within
    spread() of their median, and no MIN_CUES in a row lie farther. The first and the last among them lie within
    spread() of the median, and every one lies nearer it than the part's line. The median sits BLOCK_SHIFT or more off
    the part's line. On each side, the BLOCK_CUES nearest anchors of the part that lie nearer its line bound the block.
    MIN_CUES of them lie within TOLERANCE of the line, or the median of the nearest MIN_CUES or of all of them does.
    Also, no MIN_CUES of them in a row sit IN_LINE_OFF off the line the way of the block before MIN_CUES lie on it. A
    side with under MIN_CUES anchors counts only when the part reaches the speech of the track's first or last cue. The
    edge then lies among the cues from that file edge to the run. Else it lies among the cues from the last anchor
    nearer the part's line to the first anchor of the block. Among those cues, an anchor nearer the part's line stays,
    an anchor within TOLERANCE of the block moves, and placed() puts every other cue on one side by its heard words.
    split() places the edge. Over all its anchors, the block must still agree and sit BLOCK_SHIFT off, both off the cues
    around it and off the fitted line. A part's line can lie between the track's line and a block that fills most of the
    part, and then cues in time read off it. So with rows, the block must also sit BLOCK_SHIFT off the track's lean, and
    the part's line, the line beside the block and the line on
    each side of it must lie within TOLERANCE of the track's lean, the median of the rows that count, see trusted(),
    outside every part. The rows inside the parts give how far the sweep reads from dense hearing. With rows, the block
    moves by the smaller of its shift against the line beside it and its shift against the lean. A block of the other sign beside a long block can fill the part and its side, and the move then
    overshoots. One side can sit off while the line of both sides passes. With onsets that agree, an anchor past the block, farther from the line,
    leaves that share. The longest run wins, and the anchors at each side of it are searched again for more blocks.

    The speech onsets then judge the block, see clock(). An onset that two reads found counts once. When they agree, it
    moves. When too few cues have an onset, it moves only when it sits BLOCK_ALONE off. When they disagree, nothing
    moves, and the part's why names both shifts. A part records the judgement of the block the onsets refused, in
    "onsets", else None.

    The mark of an edit within the first or last EDIT_CUES heard cues moves an edge in to it: two cues in a row whose
    gap the move closes or whose overlap it ends, to FRAMES at most. A block moves toward the cues at one edge, after it
    when late and before it when early. There a cue of the block may sit past a cue in time, which then lies within the
    block's times. So each cue within the shift of the block's outer cue at that edge, as the edges first found it, and
    that outer cue when the cue beyond it lies within the shift, needs an onset where the block puts it. The edge moves
    in past the last that has none.

    Then each cue between the edges needs evidence of its own that it belongs to the block, see evidence(): its anchor
    at the block's offset, or its heard words placed in the block. The cue after a long silence also needs an onset
    where the block puts it and none where it sits. An onset counts as a cue's own only when no cue next to it starts
    within TOLERANCE of it, where either sits or where the block puts it. Evidence of its own that puts a cue on the line
    cuts the block there. A cut inside the run sends each side back to be judged as a block of its own, and a cut nearer
    an edge moves that edge in past it. A cue with no evidence either way stays where it is: the block names its start
    in "keep", and "unproved" counts such cues. A cue that starts with a cue that stays stays too. A cue whose own anchor
    sits over TOLERANCE off the block, though nearer it than the line, moves on its words only with an onset there.
    Accepted residual: a cue in time between two blocks of one sign whose own anchor, words and both windows put it at
    the block's offset, with no onset at either place, moves with the block. Nothing tells it from the block's own cues,
    as nothing tells Whisper's error from a block on Whisper alone, see BLOCK_ALONE. So does such a cue with only its
    words or only its anchor in the block: asking those for a second piece cost many of the cues fixed.
    A cue whose move would put it at, past or on the same centisecond as a cue next to it that stays, stays too, see
    crossing(). So the order of cue starts never changes, and no two starts tie. On Whisper alone, a block with a cue
    that would pass a cue outside its edges is refused. Whisper can read a whole stretch off, the cues on the line
    beside the block among them, and with no onsets the order is the only check left on the shift. "cues" counts the
    cues that move, and the block needs MIN_CUES heard cues that move. A cue outside a block never moves. A moved cue
    may start before the end of a cue that stays. That cue keeps its times, and a player shows both lines while they
    overlap. With INVARIANTS, check_blocks() checks the result.
"""
    stop, raw = decide.STOPWORDS.get(lang, frozenset()), sorted(cues)
    ons = []
    for o in sorted((o, ONSET_QUIET) if isinstance(o, (int, float)) else (o[0], o[1]) for o in onsets or ()):
        if ons and o[0] - ons[-1][0] < ONSET_TWICE:   # two reads of the same audio find one onset twice
            ons[-1] = max(ons[-1], o, key=lambda x: x[1])
        else:
            ons.append(o)
    cues = unflashed(raw)
    fix = (timing or {}).get("fix")
    rate, offset = (float(Fraction(fix["rate"])), fix["offset"]) if fix else (1.0, 0.0)
    audio = lambda c: (c - offset) / rate   # where the fix puts a cue time
    starts, (at, said_at) = [c[0] for c in cues], heard_anchors(heard, raw, stop)
    # The first cue after a long silence reads late, so it never anchors. Its heard words still place it at an edge.
    hushed = {k for k in range(1, len(cues)) if audio(cues[k][0]) - audio(cues[k - 1][1]) >= LONG_QUIET}
    found = [(i, t, audio(cues[i][0]) - t - BLOCK_LEAD) for i, t in sorted(at.items()) if i not in hushed]   # (cue, heard time, how late)
    late_of = {i: x for i, _, x in found}
    worded = [bool(words(c[2], stop)) and not MUSIC.search(NOISE.sub(" ", c[2])) for c in cues]   # cues dense hearing can anchor
    out, proved = {"blocks": [], "parts": []}, []   # proved: the evidence of each cue a block moves, see check_blocks()
    # The track's lean: the median off of its sweep rows that count, outside every part. The sweep pairs a cue's first
    # content word, and dense hearing steps back to the first word spoken, so their leans differ by how the track's lines
    # start. The rows inside the parts, against the anchors of the cues in their windows, measure that.
    counted = trusted(rows or [])[0]
    past = lambda r: all(r["at"] + WINDOW <= a or r["at"] >= b for a, b in parts)
    gaps = [statistics.median(xs) - r["off"] for r in counted if not past(r) and len(xs := [x for _, t, x in found if r["at"] <= t < r["at"] + WINDOW]) >= MIN_CUES]
    outside = [r["off"] for r in counted if past(r)]
    lean = statistics.median(outside or [0.0]) + statistics.median(gaps or [0.0]) if rows is not None else None
    for lo, hi in parts:
        got = [a for a in found if lo <= a[1] <= hi]
        late, todo, whys, made, refused, more = [x for _, _, x in got], [(0, len(got))], [], 0, None, []
        # The part's line: the median of its anchors within TOLERANCE of their median within BLOCK_SHIFT of the fitted line.
        # A window of TOLERANCE alone cuts the spread of a track that leans, and one step never slides onto a block.
        base = statistics.median([x for x in late if abs(x) <= BLOCK_SHIFT] or [0.0])
        base = statistics.median([x for x in late if abs(x - base) <= TOLERANCE] or [base])
        while todo:
            a, b = todo.pop()
            run = widest(late, base, a, b)
            if run is None:
                continue
            i, j = run
            todo += [(j, b), (a, i)]
            m = statistics.median(late[i:j])
            on = lambda x: abs(x - base) <= abs(x - m)   # nearer the part's line than the block
            clear = lambda x: abs(x - m) <= TOLERANCE and abs(x - base) >= BLOCK_SHIFT   # clearly in the block, see agreeing()
            # The BLOCK_CUES nearest anchors on each side that lie nearer the line than the block, so anchors of the block
            # left out of the run never stand for the line. The side sits on the line when MIN_CUES of them lie within
            # TOLERANCE of it, or the median of the nearest MIN_CUES or of all of them does. So one chance pair or one cue
            # Whisper heard far off never hides the line, and a stretch off the line still does.
            nearest = lambda ks: [late[k] for k in ks if on(late[k])][:BLOCK_CUES]
            lined = lambda xs: [x for x in xs if abs(x - base) <= TOLERANCE]
            side = lambda xs: len(lined(xs)) >= MIN_CUES or any(abs(statistics.median(ys) - base) <= TOLERANCE for ys in (xs[:MIN_CUES], xs))

            def stretch(ks):
                """Whether MIN_CUES anchors in a row of those nearer the line, from the run out by ks, each sit IN_LINE_OFF
                off the line the way of the block, before MIN_CUES lie on the line. Then the run is part of a stretch off
                the line, as when Whisper hears the end of a block late, and the side is no line."""
                n = seen = 0
                for k in (k for k in ks if on(late[k])):
                    n = n + 1 if (late[k] - base) * (m - base) > 0 and abs(late[k] - base) >= IN_LINE_OFF else 0
                    seen += abs(late[k] - base) <= TOLERANCE
                    if n >= MIN_CUES or seen >= MIN_CUES:
                        return n >= MIN_CUES
                return False
            before, after = nearest(range(i - 1, -1, -1)), nearest(range(j, len(late)))
            bound = lined(before)[:MIN_CUES] + lined(after)[:MIN_CUES]   # the anchors on the line that bound it
            sided = (lined(late[:i])[-BLOCK_CUES:], lined(late[j:])[:BLOCK_CUES])   # the anchors on the line at each side
            around = sided[0] + sided[1]
            line = statistics.median(around if len(around) >= MIN_CUES else bound or [base])
            # An anchor between stays when it lies nearer the line, and moves only when it lies clearly in the block. Its
            # words judge any other, as a chance pair far off.
            # The words of the first cue after a long silence read early too, so they never put it in the block.
            words_say = lambda k: placed(audio(starts[k]), [statistics.median(v) for v in said_at[k].values()], line, m)   # a time a word
            sides = lambda a, b: ["line" if k in late_of and on(late_of[k]) else "block" if k in late_of and clear(late_of[k]) else
                                  ("unsure" if k in hushed and (x := words_say(k)) == "block" else x if k in hushed else words_say(k))
                                  if k in said_at else None for k in range(a + 1, b)]
            edge = [0, 0]
            if len(before) >= MIN_CUES:
                if not side(before) or stretch(range(i - 1, -1, -1)):
                    whys.append(f"the {len(before)} heard cues before the cues {m:+.2f} s off sit off the line too, so its start is not seen")
                    continue
                p = got[max((k for k in range(i) if on(late[k])), default=i - 1)][0]
                e, edge[0] = split(sides(p, got[i][0]), True)
                start = starts[p + 1 + e]
            elif lo <= audio(cues[0][0]) - m:   # the file's start: its cues are judged as at an edge
                e, edge[0] = split(sides(-1, got[i][0]), True)
                start = starts[e] if e else 0.0
            else:
                whys.append(f"dense hearing heard {len(before)} cues on the line before the cues {m:+.2f} s off, under {MIN_CUES}, so its start is not seen")
                more.append((max(0.0, lo - FURTHER), lo))
                continue
            if len(after) >= MIN_CUES:
                if not side(after) or stretch(range(j, len(late))):
                    whys.append(f"the {len(after)} heard cues after the cues {m:+.2f} s off sit off the line too, so its end is not seen")
                    continue
                q, k = got[j - 1][0], got[min((k for k in range(j, len(late)) if on(late[k])), default=j)][0]
                e, edge[1] = split(sides(q, k), False)
                end = starts[q + 1 + e]
            elif hi >= audio(cues[-1][0]) - m:   # the file's end: its cues are judged as at an edge
                q = got[j - 1][0]
                e, edge[1] = split(sides(q, len(cues)), False)
                end = starts[q + 1 + e] if q + 1 + e < len(starts) else cues[-1][0] + 1.0
            else:
                whys.append(f"dense hearing heard {len(after)} cues on the line after the cues {m:+.2f} s off, under {MIN_CUES}, so its end is not seen")
                more.append((hi, hi + FURTHER))
                continue
            held = [x for c, _, x in got if start <= cues[c][0] < end]
            if not held:
                whys.append(f"the edges of the cues {m:+.2f} s off hold none of them")
                continue
            mid = statistics.median(held)
            shift = round(mid - line, 3)   # against the cues around it, as the onsets measure it too
            # The line beside the block may lie up to TOLERANCE off the track's lean, pulled by a block of the other sign
            # in the part. Of the shift against that line and the shift against the lean, the block moves by the smaller,
            # so a pulled line never carries its cues past their speech. The order test below uses that move.
            move = round(min(shift, mid - lean, key=abs), 3) if lean is not None and shift * (mid - lean) > 0 else shift
            tol = spread(shift)
            first, last = bisect.bisect_left(starts, start), bisect.bisect_left(starts, end) - 1
            said = clock(ons, {k: audio(starts[k]) for k in range(len(starts))}, range(first, last + 1),
                         [k for k in range(len(starts)) if not first <= k <= last and lo <= audio(starts[k]) <= hi], shift, (lo, hi))
            # A heard cue past the block, farther from the line, is no cue in time. With onsets that agree, such cues
            # leave the share that must agree: Whisper hears some cues of a block far off.
            near = sorted(abs(x - mid) for x in held if said["verdict"] != "agree" or (x - mid) * shift <= tol * abs(shift))
            if abs(shift) < BLOCK_SHIFT or sum(d <= tol for d in near) < AGREE * len(near):
                whys.append(f"within its edges {sum(d <= tol for d in near)} of {len(near)} heard cues agree within {tol:.2f} s of "
                            f"{mid:+.2f} s, and they sit {shift:+.2f} s off the cues around them")
            elif abs(mid) < BLOCK_SHIFT or lean is not None and abs(mid - lean) < BLOCK_SHIFT:
                # The part's line can lie between the track's line and a block that fills most of the part. Cues in time
                # then read off that line. So the block's heard cues must also sit BLOCK_SHIFT off the fitted line, and
                # off the track's lean: a block of the other sign in the part can pull its line toward itself.
                off, ref = (mid, "the fitted line") if abs(mid) < BLOCK_SHIFT else (mid - lean, f"the track's lean of {lean:+.2f} s")
                whys.append(f"its heard cues sit {shift:+.2f} s off the cues around them, but {off:+.2f} s off {ref}, under {BLOCK_SHIFT} s")
            elif abs(line - base) > TOLERANCE / 2:
                # A block next to it, of the other sign, can pass as the line beside it, and the move then overshoots.
                whys.append(f"the cues around the cues {m:+.2f} s off sit {line - base:+.2f} s off the part's line, over {TOLERANCE / 2} s, so their line is not seen")
            elif lean is not None and (far := max(abs(x - lean) for x in [base, line] + [statistics.median(xs) for xs in sided if len(xs) >= MIN_CUES])) > TOLERANCE:
                # A long block can fill the part, and a block of the other sign beside it then reads its line there.
                whys.append(f"the line around the cues {m:+.2f} s off sits {far:.2f} s off the track's lean of {lean:+.2f} s outside the part, over {TOLERANCE} s")
            elif said["verdict"] == "disagree":
                refused = refused or {k: v for k, v in said.items() if k not in ("moved", "kept", "stayed")}
                whys.append(f"Whisper puts its cues {shift:+.2f} s off, and a speech onset fits {said['inside']} of them there and "
                            f"{said['original']} where they sit, so they stay")
            elif said["verdict"] == "few" and abs(move) < BLOCK_ALONE:
                refused = refused or {k: v for k, v in said.items() if k not in ("moved", "kept", "stayed")}
                whys.append(f"its cues sit {shift:+.2f} s off, under {BLOCK_ALONE} s, and {said['inside']} cues in it and {said['outside']} around it "
                            f"have a speech onset, under {ONSET_MIN}, so they stay")
            else:
                # A cue in time at a block's edge can read like the block, a few in a row, and a stray onset can confirm
                # one. With onsets that agree, each end of the block moves in to its first heard cue that its anchor puts
                # clearly in the block, within half of TOLERANCE of it, and that one more thing puts there too: an onset
                # where the block puts it, its heard words, see placed(), or an anchor BLOCK_ALONE off the line beside the
                # anchor of the next cue in, which its anchor puts clearly in the block too. A chance pair stands alone
                # past cues with no anchor. On Whisper alone, the outermost heard cue at each end stays, and so does each
                # next one whose anchor sits under BLOCK_ALONE off the line. A file's edge has no cue in time beyond it.
                confirmed, kept, stayed = said.pop("moved"), said.pop("kept"), said.pop("stayed")
                heard_in = [k for k in range(first, last + 1) if k in late_of]
                sure = lambda k: k in late_of and clear(late_of[k]) and abs(late_of[k] - m) <= TOLERANCE / 2
                far = lambda k, inner: abs(late_of[k] - line) >= BLOCK_ALONE and sure(inner)
                ok = lambda k, inner: sure(k) and (k in confirmed or said["verdict"] == "agree" and (words_say(k) == "block" or far(k, inner)))
                a, b = 0, len(heard_in)
                weak = lambda k: said["verdict"] != "agree" and abs(late_of[k] - line) < BLOCK_ALONE   # Whisper alone moves only that far
                while start > 0 and a < b and not ok(heard_in[a], heard_in[a] + 1) and (a == 0 or said["verdict"] == "agree" or weak(heard_in[a])):
                    a += 1
                while end <= cues[-1][0] and b > a and not ok(heard_in[b - 1], heard_in[b - 1] - 1) and (b == len(heard_in) or said["verdict"] == "agree" or weak(heard_in[b - 1])):
                    b -= 1
                f0, l0, e0 = first, last, list(edge)
                first, last = (heard_in[a] if 0 < a < len(heard_in) else f0, heard_in[b - 1] if a < b < len(heard_in) else l0)
                # An edit leaves a mark between the last cue before it and the first cue after it. At the start of a
                # block early by the shift they overlap by about the shift, and at the start of a block late by it they
                # lie about the shift apart. The end mirrors that. The move leaves at most FRAMES of overlap there. Such a
                # pair within the first or last EDIT_CUES heard cues of the block, after the trims above, puts its edge
                # there.
                # So a cue in time beside an edit never joins the block, even when its author set it as far off.
                gap = lambda k: cues[k][0] - cues[k - 1][1]
                mark = lambda k, d: gap(k) * shift * d > 0 and gap(k) - d * shift >= -FRAMES   # d: 1 at the start, -1 at the end
                # The window holds EDIT_CUES heard cues in from each edge. A cue with no anchor gives no sign of its side.
                inner = [k for k in range(first + 1, last + 1) if k in late_of][:EDIT_CUES]
                first = next((k for k in range(inner[-1] if len(inner) == EDIT_CUES else last, first, -1) if mark(k, 1)), first)
                inner = [k for k in range(first, last) if k in late_of][-EDIT_CUES:]
                last = next((k - 1 for k in range(inner[0] + 1 if len(inner) == EDIT_CUES else first + 1, last + 1) if mark(k, -1)), last)
                # A block moves toward the cues at one edge: after it when late, before it when early. There a cue of the
                # block may sit past a cue in time, so that cue lies within the block's times, and Whisper's words pair out
                # of order. Only an onset where the block puts it tells such a cue apart. So a cue within the shift of the
                # outer cue the edges first found must have one, and so must that outer cue when the cue beyond it lies
                # within the shift. The edge moves in past the last cue that has none. Whisper may read the shift up to
                # TOLERANCE short, so the window is that much wider.
                # An onset within TOLERANCE of two cue starts may be either cue's, so it times neither.
                tied = lambda k, j, d=abs(shift) + TOLERANCE: 0 <= j < len(starts) and abs(audio(starts[k]) - audio(starts[j])) < d   # the shift may read short
                timed = lambda k: k in confirmed and not tied(k, k - 1, TOLERANCE) and not tied(k, k + 1, TOLERANCE)
                if shift > 0:
                    last = next((k - 1 for k in range(first, last + 1) if not timed(k) and (k < l0 and tied(k, l0) or k == l0 and tied(k, k + 1))), last)
                else:
                    first = next((k + 1 for k in range(last, first - 1, -1) if not timed(k) and (k > f0 and tied(k, f0) or k == f0 and tied(k, k - 1))), first)
                # Every cue that moves needs evidence of its own that it belongs to the block: its anchor at the block's
                # offset, or its heard words placed in the block, see placed(), with an onset where the block puts it and
                # none where it sits after a long silence. Evidence of its own that puts it on the line cuts the block
                # there: its anchor, the anchor of a cue after a long silence, its heard words, or an onset where it sits.
                # A cut inside the run sends each side back to be judged as a block of its own. A cut nearer an edge moves
                # that edge in past it. A cue with no evidence either way stays where it is, and the block counts it as
                # unproved.
                # An onset is a cue's own only when no cue next to it starts within TOLERANCE of it, where either cue sits
                # or where the block puts it: else it may be the other cue's.
                alone = lambda k: all(abs(audio(starts[k]) - audio(starts[j]) + d) >= TOLERANCE for j in (k - 1, k + 1) if 0 <= j < len(starts)
                                      for d in (0.0, shift, -shift))
                said_of = lambda k: evidence(late_of.get(k), audio(starts[k]) - at[k] - BLOCK_LEAD if k in hushed and k in at else None,
                                             words_say(k) if k in said_at else None, k in confirmed and alone(k), k in stayed and alone(k), on,
                                             lambda x: abs(x - mid) <= tol and not on(x), k in hushed, k in late_of and abs(late_of[k] - mid) > TOLERANCE)
                proof = {k: said_of(k) for k in range(first, last + 1)}
                cuts = [k for k, v in proof.items() if v == "line"]
                inside = [k for k in cuts if got[i][0] < k < got[j - 1][0]]
                if inside:
                    marks = sorted({next(x for x in range(i, j) if got[x][0] >= k) for k in inside})
                    bounds = [i] + marks + [j]
                    todo += [(x + (got[x][0] in inside), y) for x, y in zip(bounds, bounds[1:]) if y - x - (got[x][0] in inside) >= BLOCK_CUES]
                    whys.append(f"{len(inside)} cues inside the cues {m:+.2f} s off sit on the line by their own evidence, so each side is judged alone")
                    continue
                first = max([k + 1 for k in cuts if k <= got[i][0]], default=first)
                last = min([k - 1 for k in cuts if k >= got[j - 1][0]], default=last)
                if first != f0:
                    edge[0], start = e0[0] + first - f0, starts[first]
                if last != l0:
                    edge[1], end = e0[1] + l0 - last, starts[last + 1] if last + 1 < len(starts) else cues[-1][0] + 1.0
                n = min(b - a, sum(k in late_of and proof[k] == "block" for k in range(first, last + 1)))   # b - a: the trims may leave none, and the edges then stay
                moving = {k for k in range(first, last + 1) if proof[k] == "block"}
                # remux.time_plan() keeps a start, not a cue. So a cue that starts with a cue that stays, as two lines shown
                # at once, stays too, and so does the first or the last when a cue outside the edges starts with it.
                still = {starts[k] for k in range(len(starts)) if k not in moving}
                moving = {k for k in moving if starts[k] not in still}
                # A cue whose move would pass a cue that stays, or tie with it, would stay, and so on until none would.
                # The block is judged on the cues that move all the way. A cue that stops short moves part of the way
                # instead, to one centisecond short of the cue next to it, see clamped(), when two pieces of its own
                # evidence put it in the block: its anchor, its heard words, or its own onset where the block puts it.
                # Else it stays: the crossing kept a cue in time whose anchor alone read like the block, between two
                # blocks of one sign.
                passed, whole = [], set(moving)
                while (x := crossing(starts, whole, first, last, move, fix)) is not None:
                    whole.discard(x[0])
                    passed.append(x[1])
                two = lambda k: sum((k in late_of and abs(late_of[k] - mid) <= tol and not on(late_of[k]), k in said_at and words_say(k) == "block",
                                     k in confirmed and alone(k))) >= 2
                part = clamped(starts, moving, first, last, move, fix, two)
                moving -= {k for k, v in part.items() if not v}
                stays = [k for k in range(first, last + 1) if k not in moving]   # time_plan() skips their starts
                n = min(n, sum(k in late_of for k in whole))
                if n < MIN_CUES:
                    whys.append(f"after its edges stay, {n} heard cues are left in it, under {MIN_CUES}")
                elif said["verdict"] != "agree" and any(not first <= k <= last for k in passed):
                    whys.append(f"on Whisper alone, a move of {-move:+.2f} s would put a cue past the cue next to the block")
                elif any(start < x["to"] and x["from"] < end for x in out["blocks"]):
                    whys.append("its cues lie in another block")
                else:
                    made += 1
                    out["blocks"].append({"from": start, "to": end, "shift": move, "cues": len(moving), "anchors": b - a,
                                          "spread": round(near[math.ceil(AGREE * len(near)) - 1], 2), "edge_left": edge[0],
                                          "edge_right": edge[1], "onsets": said, "unproved": sum(proof[k] is None for k in stays),
                                          "keep": [round(starts[k], 3) for k in stays],
                                          "clamp": [[round(starts[k], 3), v] for k, v in sorted(part.items()) if v]})
                    if INVARIANTS:
                        fact = lambda k: (audio(starts[k]) - at[k] - BLOCK_LEAD if k in at else None, k in hushed, words_say(k) if k in said_at else None,
                                          k in stayed and alone(k))
                        proved.append({"block": out["blocks"][-1], "mid": mid, "tol": tol, "m": m, "base": base, "ref": line if move == shift else lean,
                                       "cues": {k: fact(k) + (k in confirmed and alone(k),) for k in moving},
                                       "between": {k: fact(k) for k in range(min(moving), max(moving)) if k not in moving}})
        median = round(statistics.median(late), 2) if late else None
        share = len(late) / max(1, sum(w and lo <= audio(c[0]) <= hi for w, c in zip(worded, cues)))
        in_line = len(late) >= IN_LINE and share >= IN_LINE_SHARE and abs(median) <= TOLERANCE and \
            all(abs(statistics.median(late[k:k + BLOCK_CUES]) - base) < BLOCK_SHIFT for k in range(len(late) - BLOCK_CUES + 1)) and \
            not any(all(sign * (x - base) >= IN_LINE_OFF for x in late[k:k + MIN_CUES]) for sign in (1, -1) for k in range(len(late) - MIN_CUES + 1))
        if hi <= lo:
            why = f"the dense hearing cap of {BLOCK_HEAR:.0f} s was used by parts farther off"
        elif made:
            why = None
        elif whys:
            why = whys[0]
        elif in_line:
            why = f"its {len(late)} heard cues sit {median:+.2f} s off the line at their median, in time"
        elif len(got) < BLOCK_CUES:
            why = f"dense hearing heard {len(got)} cues whose first word matched, under {BLOCK_CUES}"
        else:
            why = f"no {BLOCK_CUES} heard cues in a row agree within {TOLERANCE} s, or {BLOCK_SPREAD:.0%} of their shift, at {BLOCK_SHIFT} s or more off the cues around them"
        out["parts"].append({"lo": lo, "hi": hi, "anchors": len(got), "median": median, "in_line": in_line, "why": why, "whys": whys, "onsets": refused,
                             "more": [tuple(x) for x in merged(more)],
                             "anchored": [round(t, 1) for _, t, _ in got]})
    out["blocks"].sort(key=lambda x: x["from"])
    if INVARIANTS:
        check_blocks(raw, out["blocks"], proved, {"blocks": out["blocks"], "proved": proved, "anchors": at, "fix": fix, "call": {
            "heard": heard, "cues": raw, "lang": lang, "timing": timing, "parts": parts, "onsets": onsets, "rows": rows}})
    return out


def unheard(spans, union):
    """[(lo, hi)] of spans [(lo, hi)] less every part of union [(lo, hi)], in order."""
    out = []
    for lo, hi in merged(spans):
        for a, b in merged(union):
            if a < hi and lo < b:
                if lo < a:
                    out.append((lo, a))
                lo = max(lo, b)
        if lo < hi:
            out.append((lo, hi))
    return out


def further(results, union, duration):
    """{key: [(lo, hi)]} of the stretches a second dense hearing hears, for results {key: blocks() result} of the tracks
    of one audio track. union is the parts the first hearing heard, see subtitles.heard_parts(). Each part's "more"
    counts against what BLOCK_HEAR leaves after union, in order, and a stretch cut short keeps its end at the part. A
    stretch under WINDOW seconds, or one outside the file's duration, is not heard. Only its pieces outside union count,
    see unheard(), each as WINDOW seconds at least, as dense() hears it: another track's part may have heard the rest.
    The caller hears only those pieces, adds their onsets, and calls blocks() again for each key with its parts and its
    whole stretches. blocks() takes an onset that two reads found once."""
    left, out = BLOCK_HEAR - sum(hi - lo for lo, hi in union), {}
    for key, r in results.items():
        for p in r["parts"]:
            for lo, hi in p.get("more") or ():
                lo, hi = max(0.0, lo), min(duration, hi)
                cost = lambda a, b: sum(max(WINDOW, y - x) for x, y in unheard([(a, b)], union))   # dense() hears a short piece whole
                n = hi - lo if cost(lo, hi) <= left else min(hi - lo, left)
                if n >= WINDOW:
                    span = (hi - n, hi) if hi <= p["lo"] else (lo, lo + n)
                    out.setdefault(key, []).append(span)
                    left -= cost(*span)
    return out


def after_blocks(rows, blocks, timing, parts=()):
    """The sweep() rows after dense hearing, so the sweep alert never fires on a part that dense hearing judged. A row in
    a block of blocks() that moved gets "off" less the block's shift. A row sits in a block when the cue times of its
    window, rate * (t + CUE_LEAD) + offset + off from its start to its end, lie in the block's [from, to) and no cue the
    block keeps starts there. A row whose window crosses an edge, or holds a cue that stays or moves only part of the
    way, see clamped(), keeps its "off": cues there still sit off. A row in a part of parts, blocks()'s, that is in line gets the part's median as its "off": the heard
    cues there showed the row was noise. A part off the line whose cues do not agree keeps its rows. rows and parts use
    audio time, and blocks cue time. The block of a live-captioned track moves each cue by its own shift, so a row in it
    takes the median shift of the cues that start in its window."""
    fix = (timing or {}).get("fix")
    rate, offset = (float(Fraction(fix["rate"])), fix["offset"]) if fix else (1.0, 0.0)
    out = []
    for r in rows:
        c = None if r["off"] is None else rate * (r["at"] + WINDOW / 2 + CUE_LEAD) + offset + r["off"]
        lo, hi = (None, None) if c is None else (c - rate * WINDOW / 2, c + rate * WINDOW / 2)
        b = next((b for b in blocks if c is not None and b["from"] <= lo and hi < b["to"]
                  and not any(lo <= x <= hi for x in [*b.get("keep", ()), *(x for x, _ in b.get("clamp") or ())])), None)
        p = next((p for p in parts if c is not None and p.get("in_line") and p["lo"] <= r["at"] + WINDOW / 2 <= p["hi"]
                  and ("anchored" not in p or sum(r["at"] <= a <= r["at"] + WINDOW for a in p["anchored"]) >= 2)), None)
        shift = b and (statistics.median([v for t, v in b["shifts"].items() if lo <= t <= hi] or [b["shift"]]) if "shifts" in b else b["shift"])
        out.append(dict(r, off=round(r["off"] - rate * shift, 2)) if b else dict(r, off=round(rate * p["median"], 2)) if p else dict(r))
    return out


# Live captions (docs/design.md, "Subtitle match"). A captioner who types along with a broadcast shows each line some
# seconds after it is spoken, by a different time each line. One shift cannot fix that, and neither can a block. So on a
# track whose sweep looks live-captioned, dense hearing hears the whole track, and each cue moves to its own speech.
# Per-cue moves on other tracks stay out: there an author's jitter is the size of Whisper's noise.
LIVE_ROWS = 10       # sweep rows that count a track needs before the sweep can call it live-captioned, see live()
LIVE_FEW = 5         # sweep rows that count that are enough when each sits LIVE_FAR or more late. Roll-up captions, which
                     # repeat the last line, let few rows match 2 cues.
LIVE_FAR = 3.0       # seconds late each of those rows sits at least. No row of a verified right track sat 2.5 s late.
LIVE_LAG = 0.3       # seconds late its rows sit at their median at least: live captions trail the speech
LIVE_SCATTER = 0.4   # seconds its rows sit from their median, at the median, at least. Logged tracks that were
                     # not live captions sat at 0.26 s or less, and the live ones at 0.49 s or more.
LIVE_AGREE = 2.0     # seconds the two cues around a cue with no anchor sit off at least before it takes their shift on their word
                     # alone. With each anchor hidden in turn, the place between moving neighbours 1 s off was farther than the
                     # old start for about a third of the cues of a test track, and at 2 s for almost none.
LIVE_AROUND = 2      # anchored cues on each side of a cue whose median must sit off the same way before the cue moves by its own anchor
LIVE_LEFT = 0.2      # the share of a track's cues that may stay off before the track counts as not fixed, see live_moves()
LIVE_STEPS = 8       # steps per cue at most of the loop that keeps the cue order, see live_moves(). It needs 5 at most.
LIVE_TWICE = 3.0     # seconds after a cue's anchor within which its first content word heard again may be its speech, see unsure()
LABEL = re.compile(r"(^|\n|>>)([ \t]*)[^\W\d_][\w.'’-]*(?: [^\W\d_][\w.'’-]*)?:(?=\s)")   # a speaker's name before a line, never spoken


def live(rows):
    """{"lag", "off", "scatter", "rows"} of a track whose sweep() rows look live-captioned, else None. The rows that
    count, see trusted(), give it. lag is their median offset against the audio, off their median "off" against the
    fitted line, and scatter the median distance of their "off" from off. A track looks live-captioned when LIVE_ROWS
    rows count, lag is LIVE_LAG or more and scatter is LIVE_SCATTER or more. LIVE_FEW rows are enough when each of
    them sits LIVE_FAR or more late. A fix for the whole track leaves the scatter. A right track, a track off by one
    shift and a block leave little."""
    rows = trusted(rows)[0]
    if len(rows) < LIVE_FEW or len(rows) < LIVE_ROWS and min(r["offset"] for r in rows) < LIVE_FAR:
        return None
    lag, off = statistics.median(r["offset"] for r in rows), statistics.median(r["off"] for r in rows)
    scatter = statistics.median(abs(r["off"] - off) for r in rows)
    return {"lag": round(lag, 2), "off": round(off, 2), "scatter": round(scatter, 2), "rows": len(rows)} \
        if lag >= LIVE_LAG and scatter >= LIVE_SCATTER else None


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


def unsure(heard, at, texts, stop):
    """{cue: d} of the anchors at {cue: audio time} that may pair the wrong word: a window that holds the anchor heard
    the cue's first content word again up to d seconds later, within LIVE_TWICE, as often as the cue holds it or more.
    texts are the cue texts that anchored. Two "yeah" a second apart, or an "I'm" said before a line the captioner left
    out, sent cues the wrong way: the cue's speech was the later word. A word said twice in a row counts twice here,
    though said() keeps one of them for the match."""
    spans = [(w["at"], w["at"] + w.get("secs", WINDOW), [(w["at"] + t, v) for t, x in w["words"] for v in words(x, stop)])
             for w in sorted(heard, key=lambda w: w["at"])]   # every heard word, a repeat that said() drops too
    starts, longest, out = [a for a, _, _ in spans], max((b - a for a, b, _ in spans), default=0.0), {}
    for k, t in at.items():
        mine = words(texts[k], stop)
        first = (mine or [None])[0]
        for a, b, ws in spans[bisect.bisect_left(starts, t - longest):bisect.bisect_right(starts, t)]:
            ts = [x for x, v in ws if v == first]
            t0 = min((x for x in ts if x >= t - TOLERANCE), default=None)   # the anchor's word, as this window times it
            later = [x - t0 for x in ts if t0 < x <= t0 + LIVE_TWICE] if t < b and t0 is not None else []
            if later and len(later) >= mine.count(first):
                out[k] = max(out.get(k, 0.0), *later)
    return out


def rising(pairs):
    """The keys of the longest run of pairs [(key, time)], in their order, whose times rise strictly."""
    tops, ends, back = [], [], []   # the least last time of a run of each length, the pair that ends it, each pair's one before
    for n, (_, t) in enumerate(pairs):
        k = bisect.bisect_left(tops, t)
        back.append(ends[k - 1] if k else None)
        tops[k:k + 1], ends[k:k + 1] = [t], [n]
    out, n = set(), ends[-1] if ends else None
    while n is not None:
        out.add(pairs[n][0])
        n = back[n]
    return out


def live_moves(heard, cues, lang, timing, scan=None):
    """The moves of a live-captioned track: each cue to its own speech (docs/design.md, "Subtitle match"). heard is
    lid.listen()'s windows of dense hearing over the whole track, cues the track's [(start, end, text)] in seconds, lang
    its 639-2 language, timing the word check's timing(), whose fix may be None, and scan its live() reading.

    A cue's speech starts at its first spoken word, its anchor, see heard_anchors(). A speaker's name before a cue's
    text, as ">> Reporter:", is never spoken, so it leaves the text first, and so do the top lines that roll-up
    captions repeat from the cue before, see rolled(). Live captions keep the order of the speech, so only the longest
    run of anchors that rises with the cues counts, see rising(). An anchor out of that order paired a word said
    elsewhere. A cue then moves in one of three ways.

    - By its own anchor, when it sits BLOCK_ALONE or more off it, and the median of the LIVE_AROUND anchored cues on
      each side sits BLOCK_SHIFT or more off the same way. A lone anchor off is Whisper's error or an author's slip.
      Whisper times the first word after a silence early. On a test track that sent a few cues 0.5 to 1 s off the wrong
      way, so a move on Whisper alone needs BLOCK_ALONE, as a block does. Its new start is the anchor less BLOCK_LEAD.
      An anchor whose first word was heard again up to d seconds later may be the wrong one, see unsure(). Its cue
      moves by it only when that is nearer the later word too: an early cue, or one late by 2 d or more. Else it
      moves as a cue with no anchor. As a bound for the cues around it, such an anchor reaches d seconds later.
    - Between the anchors of the cues around it, when it has no anchor that counts and the span proves the move. Its
      speech lies between theirs, TOLERANCE wider for Whisper's error. Its new place sits at the same share of the way
      between their anchors as its start sits between their starts. At the first or the last anchor, it takes the lag
      of the nearest. The move stands when the new place is nearer every point of the span than the old start. So a
      late cue's old start lies past the span's end by the distance from the new place to that end or more.
    - Between the anchors of the cues around it, when both of them move by their own anchors the same way, LIVE_AGREE
      or more, and the new place lies inside the heard windows. At the first or the last anchor, the two nearest. It
      takes their shift, as above. A stay costs more here than on a block. On captions late by more than their spacing,
      a cue that stays blocks every later move that would pass it. A place outside the heard windows, as past a hearing
      that stopped part way, is no guess either, so such a cue stays and counts as left.

    A cue that starts with another cue stays, as two lines shown at once. A cue whose new start would lie before the
    file's start starts at 0, and two such cues may tie there. Every other cue stays. A move that would put
    a cue at, past or on the same centisecond as the cue next to it, see written(), changes. When the cue that stays
    has no anchor, it takes the nearest place to its guess between anchors that is nearer every speech up to the bound
    the next cue gives: its anchor, TOLERANCE wider, or the end of its span. When the cue that moves has its own anchor,
    it stops a centisecond after the cue that stays, when that is still nearer its anchor. Else that move stays too.
    So the order of cue starts never changes, and no two starts tie that did not tie before. Two cues that both stay
    may share a centisecond, as the file has them. A loop past LIVE_STEPS steps a cue moves nothing.

    Returns {"blocks": [one block over the track with "shifts", {start: how late it sat}, and "keep", the starts that
    stay, or none], "parts": [{"lo", "hi", "anchors", "median", "in_line": False, "why", "whys", "onsets": None, "more":
    [], "anchored": []}], "live": {"cues", "anchored", "moved", "own", "between", "agree", "stayed", "left", "lags",
    "fixed", "scan", "stopped"}}. A block holds every cue whose start lies in [from, to). "own", "between" and "agree"
    count the cues that move each way, "stopped" the cues that stopped after a cue that stays, "stayed" the cues that
    stay, "left" those of them BLOCK_ALONE or more off their anchor or their place between anchors, or with no anchor
    around them, "lags" the 10th, 50th and 90th percentile of how late the moved cues sat, and "fixed" whether "left"
    is LIVE_LEFT of the cues or less. With INVARIANTS, check_live() checks the result."""
    stop, raw = decide.STOPWORDS.get(lang, frozenset()), sorted(cues)
    fix = (timing or {}).get("fix")
    rate, offset = (float(Fraction(fix["rate"])), fix["offset"]) if fix else (1.0, 0.0)
    starts = [c[0] for c in raw]
    said = [(c[0] - offset) / rate - BLOCK_LEAD for c in raw]   # where each cue puts its first spoken word on the audio
    texts = [LABEL.sub(r"\1\2", x) for x in rolled(raw)]
    at = heard_anchors(heard, [(c[0], c[1], x) for c, x in zip(raw, texts)], stop)[0]
    doubt = unsure(heard, at, texts, stop)   # {cue: seconds its speech may start after its anchor}
    cover = merged((w["at"], w["at"] + w.get("secs", WINDOW)) for w in heard)
    known = lambda t: any(lo - NEAR <= t <= hi + NEAR for lo, hi in cover)   # a place inside the heard windows
    twice = {t for t, n in collections.Counter(starts).items() if n > 1}   # two lines shown at once
    own = rising([(k, at[k]) for k in sorted(at) if starts[k] not in twice])
    keys, new, how, span, guess, stopped = sorted(own), {}, {}, {}, {}, set()   # guess: the place between anchors of a cue that did not move
    late = lambda j: said[j] - at[j]
    refused = {k for k, d in doubt.items() if k in own and 0 <= late(k) < 2 * d}   # a move to its anchor may pass its speech
    sure = own - refused
    bounds = [k for k in keys if k in sure]   # the anchors that bound the speech of the cues between them
    for i, k in enumerate(keys):   # by its own anchor, when the anchors around it sit off the same way
        around = statistics.median([late(j) for j in keys[max(0, i - LIVE_AROUND):i] + keys[i + 1:i + 1 + LIVE_AROUND]] or [0.0])
        if k not in refused and abs(late(k)) >= BLOCK_ALONE and abs(around) >= BLOCK_SHIFT and around * late(k) > 0:
            new[k], how[k] = at[k], "own"
    for k, c in enumerate(raw):   # between the anchors around it
        if starts[k] in twice or k in sure:
            continue
        i = bisect.bisect_left(bounds, k)
        p, n = bounds[i - 1] if i else None, bounds[i] if i < len(bounds) else None
        if p is None and n is None:
            continue
        lo, hi = at[p] - TOLERANCE if p is not None else -math.inf, at[n] + TOLERANCE + doubt.get(n, 0.0) if n is not None else math.inf
        x = at[p] + (said[k] - said[p]) / (said[n] - said[p]) * (at[n] - at[p]) if p is not None and n is not None else \
            said[k] - (said[n] - at[n] if n is not None else said[p] - at[p])   # at an end of the anchors, the lag of the nearest
        # Nearer every speech in [lo, hi]: past hi by at least the distance from x up to hi, or before lo by that from lo.
        proof = said[k] > hi and 2 * hi - said[k] <= x or said[k] < lo and x <= 2 * lo - said[k]
        pair = (p, n) if p is not None and n is not None else bounds[:2] if n is not None else bounds[-2:]
        two = [late(j) for j in pair]
        agree = known(x) and len(two) == 2 and all(how.get(j) == "own" for j in pair) and two[0] * two[1] > 0 and min(map(abs, two)) >= LIVE_AGREE
        if (proof or agree) and abs(said[k] - x) >= BLOCK_SHIFT:
            new[k], how[k], span[k] = x, "between" if proof else "agree", (lo, hi, *two)
        elif known(x):   # a place past what the hearing heard is no guess: such a cue counts as left
            guess[k] = x
    shift = {k: round(said[k] - x, 3) for k, x in new.items()}
    for k in [k for k, v in shift.items() if written(starts[k], v, fix) < 0]:   # a speech before the file's start: the cue starts at 0
        shift[k] = (moved(starts[k] * 1000, fix) if fix else round(starts[k] * 1000)) / 1000
        new[k] = said[k] - shift[k]
    w = lambda k: written(starts[k], shift.get(k, 0.0), fix)
    k, steps = 0, 0
    while k < len(raw) - 1:   # a move that would pass the cue next to it, or tie with it, stays, and so on until none would
        steps += 1
        if steps > LIVE_STEPS * len(raw):   # a second guard: each step moves on or drops a move, so the loop always ends
            for d in (shift, new, how, span):
                d.clear()
            break
        # Two starts at 0 may tie, and so may two cues that both stay, as the file has them on one centisecond
        if starts[k] < starts[k + 1] and (k in shift or k + 1 in shift) and w(k) >= w(k + 1) and w(k + 1) > 0:
            j = k + 1
            # A cue with no anchor that stays bounds no speech, but the cue after it does: its speech comes first. So it
            # takes the nearest place to its own guess that is nearer every speech up to that bound, when the place
            # fits between its neighbours. Else the move after it stays.
            top = at[j] + TOLERANCE + doubt.get(j, 0.0) if how.get(j) == "own" else span[j][1] if j in span else math.inf
            x = max(guess.get(k, -math.inf), 2 * top - said[k])
            fits = k in guess and said[k] > top and x <= top and written(starts[k], round(said[k] - x, 3), fix) < w(j) \
                and (k == 0 or written(starts[k], round(said[k] - x, 3), fix) > w(k - 1))
            if fits:
                new[k], how[k], span[k], shift[k] = x, "between", (-math.inf, top), round(said[k] - x, 3)
                guess.pop(k)
                k += 1
                continue
            # A cue that moves by its own anchor stops a centisecond after the cue that stays, when that is still nearer
            # its anchor. The cues after it then rarely meet the cue that stays.
            ms = (moved(starts[j] * 1000, fix) if fix else round(starts[j] * 1000)) - (w(k) + 1) * 10   # the shift to that centisecond, in ms
            if how.get(j) == "own" and k not in shift and abs(said[j] - ms / 1000 - at[j]) < abs(said[j] - at[j]) - NEAR:
                shift[j], new[j] = ms / 1000, said[j] - ms / 1000
                stopped.add(j)
                k += 1
                continue
            drop = j if j in shift else k
            for d in (shift, new, how, span):
                d.pop(drop, None)
            k = max(0, k - 1)
            continue
        k += 1
    stays = [k for k in range(len(raw)) if k not in shift]
    # left: off by its own anchor, or by its place between anchors, or with no anchor around it
    left = sum(abs(said[k] - (at[k] if k in sure else guess.get(k, math.inf))) >= BLOCK_ALONE for k in stays)
    lags = sorted(shift.values())
    facts = {"cues": len(raw), "anchored": len(at), "moved": len(shift), **{x: sum(v == x for v in how.values()) for x in ("own", "between", "agree")},
             "stopped": len(stopped & set(shift)), "stayed": len(stays), "left": left, "lags": [lags[int(q * (len(lags) - 1))] for q in (0.1, 0.5, 0.9)] if lags else [],
             "fixed": bool(raw) and left <= LIVE_LEFT * len(raw), **({"scan": scan} if scan else {})}
    blocks = [{"from": starts[0], "to": starts[-1] + 1.0, "shift": lags[len(lags) // 2], "cues": len(shift), "anchors": facts["own"],
               "shifts": {round(starts[k], 3): v for k, v in shift.items()}, "keep": [round(starts[k], 3) for k in range(len(raw)) if k not in shift],
               "live": True}] if shift else []
    why = None if shift else "the moves did not settle in the order of the cues, so none moved" if steps > LIVE_STEPS * len(raw) else \
        f"dense hearing anchored {len(own)} of {len(raw)} cues in the order of the speech, and none moved {BLOCK_SHIFT} s or more"
    part = {"lo": 0.0, "hi": max((w["at"] + w.get("secs", WINDOW) for w in heard), default=0.0), "anchors": len(own),
            "median": facts["lags"][1] if lags else None, "in_line": False, "why": why, "whys": [why] if why else [], "onsets": None, "more": [], "anchored": []}
    if INVARIANTS:
        check_live(raw, blocks, {"said": said, "at": at, "own": sure, "how": how, "span": span, "heard": cover, "doubt": doubt}, {"blocks": blocks, "fix": fix, "call": {
            "heard": heard, "cues": raw, "lang": lang, "timing": timing}})
    return {"blocks": blocks, "parts": [part], "live": facts}


def check_live(cues, blocks, proved, case):
    """Check two safety rules, see INVARIANTS, on the block of live_moves(). proved holds "said", where each cue puts
    its speech on the audio, "at", the anchors, "own", the anchors that rise in order, "how" each move and "span" the
    span of a move between anchors. Raises Broken.

    Own evidence: a cue the block moves has its own anchor among those that rise, or no anchor that counts and a span
    between two such anchors. When the span did not prove the move, the cues around it sat LIVE_AGREE or more off the
    same way, and the cue ends inside the span and inside the heard windows, proved "heard". Nearer its speech: a cue
    moved by its anchor ends nearer it, and nearer the later word that proved "doubt" names, and a cue whose span proved
    the move ends nearer every point of the span than it sat, each by NEAR for rounding."""
    for k, c in enumerate(cues):
        b = mover(blocks, c[0])
        if b is None:
            continue
        new, how = proved["said"][k] - b["shifts"][round(c[0], 3)], proved["how"].get(k)
        if how == "own" and k in proved["own"]:
            t, d = proved["at"][k], proved.get("doubt", {}).get(k, 0.0)
            if any(abs(new - x) > abs(proved["said"][k] - x) + NEAR for x in (t, t + d)):
                broken("nearer its speech", c[0], f"it moves to {new:.3f} s, and its anchor sits at {t:.3f} s, or {d:.3f} s later", case)
        elif how == "between" and k not in proved["own"]:
            lo, hi = proved["span"][k][:2]
            if any(abs(new - t) > abs(proved["said"][k] - t) + NEAR for t in (lo, hi, min(max(proved["said"][k], lo), hi)) if math.isfinite(t)):
                broken("nearer its speech", c[0], f"it moves to {new:.3f} s, farther from a point of its span {lo:.3f} to {hi:.3f} s", case)
        elif how == "agree" and k not in proved["own"]:
            lo, hi, a, z = proved["span"][k]
            heard = any(x - NEAR <= new <= y + NEAR for x, y in proved["heard"])
            if not lo - NEAR <= new <= hi + NEAR or not heard or a * z <= 0 or min(abs(a), abs(z)) < LIVE_AGREE:
                broken("own evidence", c[0], f"it moves to {new:.3f} s, with its span {lo:.3f} to {hi:.3f} s, the lags {a:+.3f} and {z:+.3f} s "
                       f"around it, {'inside' if heard else 'outside'} the heard windows", case)
        else:
            broken("own evidence", c[0], "the live block moves it, but neither its anchor nor a span proved it", case)
