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
"""The subtitle match check of arr-media-guard (docs/design.md, "Subtitle match"). Stdlib only.

A text subtitle belongs to the audio when the words Whisper hears in the audio are the words of the cues at the same
time. arr_lid.listen() hears two short windows of the audio. This module compares those words with the cues.

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
"""
import bisect
import collections
import difflib
import re
import statistics
from fractions import Fraction

import arr_decide

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


def windows(cues, duration, stop=frozenset(), have=(), secs=WINDOW, taken=(), parts=PARTS):
    """[a start in seconds per part], or [] when a part of the file holds no cue. parts are shares of the duration,
    PARTS by default: one early and one late window near the ends of the file, for the drift. In each part it takes the
    window of secs that holds the most cue content words. Song lyrics never count, because Whisper hears singing badly.
    have lists (start, seconds) of audio that is decoded already, the language check's samples. A window inside one of
    them wins when it holds 3/4 of the best count, so that audio is not decoded again. taken lists windows heard already.
    A window that overlaps one never comes back, and a part with no other place gives None, so the caller can pick a
    third window in the same part."""
    ts = [x[0] for x in flat([c for c in cues if not MUSIC.search(NOISE.sub(" ", c[2]))], stop)]
    count = lambda s: bisect.bisect_left(ts, s + secs) - bisect.bisect_left(ts, s)
    free = lambda s: all(s + secs <= a or s >= a + WINDOW for a in taken)
    out = []
    for lo, hi in ((a * duration, b * duration) for a, b in parts):
        starts = [s for s in (max(lo, t - 0.5) for t in ts if lo <= t <= hi - secs) if free(s)]   # half a second before a word
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
    """The places in heard, arr_lid.listen()'s windows, of the windows with under MIN_WORDS content words."""
    stop = arr_decide.STOPWORDS.get(lang, frozenset())
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
    """Whether the subtitle cues belong to the audio. heard is arr_lid.listen()'s windows, [{"at": start, "words":
    [[seconds from start, word], ...]}]. cues is [(start, end, text)] in seconds, lang the subtitle's 639-2 language,
    whose stopwords drop out, and duration the file's in seconds.

    Returns {"verdict": "match", "mismatch" or "unknown", "why", "windows": [{"at", "words", "overlap", "offset",
    "cues"}], "timing": timing() of a match, else None}. A short phrase said again right after itself counts once, see
    said(). A window inside a longer window heard later is the same audio, and the longer one stands for it. A window
    with under MIN_WORDS heard content words names nothing, and the verdict needs two windows that do. All of those at
    MATCH or more is a match. All at MISMATCH or less is a mismatch. Anything else is unknown. A track under
    MIN_TRACK_CUES cues is unknown too."""
    stop = arr_decide.STOPWORDS.get(lang, frozenset())
    cues = sorted(cues)
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


def fit(got, ends, pairs, cues, duration):
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
    after the fix."""
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
    d = [round(statistics.median(c - t for t, c in e) - CUE_LEAD, 2) for e in ends]
    if max(abs(a0 - CUE_LEAD), abs(a1 - CUE_LEAD), *(abs(x) for x in d)) < MIN_SHIFT:
        return {"fix": None, "why": "in time", "offset": round((a0 + a1) / 2 - CUE_LEAD, 3)}
    fits = {}
    for r in RATES:   # every window within TOLERANCE of one offset and of the line through the others, and the ends under MIN_SHIFT
        m, b0, b1, miss = line(r)
        mid, half = (max(m) + min(m)) / 2, (max(m) - min(m)) / 2
        if half <= TOLERANCE and miss <= TOLERANCE and max(abs(b0 - mid), abs(b1 - mid)) < MIN_SHIFT:
            fits[r] = (round(mid - CUE_LEAD * float(r), 3), max(abs(b0 - mid), abs(b1 - mid)))
    span = "".join(f", {x:+.2f} s" for x in d[1:-1]) + f" and {d[-1]:+.2f} s late"
    if not fits:
        return {"fix": None, "piecewise": True, "offsets": d,
                "why": f"the cues are off by {d[0]:+.2f} s early{span}, which no frame-rate ratio explains"}
    rate = min(fits, key=lambda r: fits[r][1])   # the least error at the file's ends, and a plain offset on a near tie
    rate = Fraction(1) if Fraction(1) in fits and fits[Fraction(1)][1] <= fits[rate][1] + TOLERANCE / 2 else rate
    to = lambda r, t: (t - fits[r][0]) / float(r)   # where a cue at t moves
    offset, name = fits[rate][0], f"{rate.numerator}/{rate.denominator}"
    what = f"{offset:+.2f} s" + ("" if rate == 1 else f" and the ratio {name}")
    if rate != 1 and not any(abs((a - at[0]) / (at[-1] - at[0]) - 0.5) <= MIDDLE for a in at[1:-1]):   # no middle window yet
        return {"fix": None, "confirm": {"rate": name, "offset": offset, "ends": [round(at[0], 1), round(at[-1], 1)]},
                "why": f"a fix of {what} waits for a middle window to confirm the ratio"}
    near = [r for r in fits if fits[r][1] <= fits[rate][1] + TOLERANCE / 2]   # the fits as good as the best, within the noise
    if any(abs(to(r, t) - to(rate, t)) > TOLERANCE for r in near for t in (0, duration)):   # a plain offset too
        return {"fix": None, "unfixed": fits[rate][0], "why": "the ratios " + ", ".join(f"{r.numerator}/{r.denominator}" for r in near)
                + " fit the windows and move the cues apart, so the ratio is not certain"}
    share = agree(pairs, cues, rate, offset + CUE_LEAD * float(rate))
    if share < AGREE:
        return {"fix": None, "unfixed": offset, "why": f"after a fix of {what}, {share:.0%} of the matched cues fall in their spans, under {AGREE:.0%}"}
    return {"fix": {"rate": name, "offset": offset},
            "why": f"a fix of {what} puts every window within {TOLERANCE} s and {share:.0%} of the matched cues in their spans"}


def middle(confirm, duration):
    """The part of the file, as windows() takes it, of the middle window that confirms a ratio. confirm is timing()'s:
    the fix, and "ends", the audio times of the first and the last window. The part is MIDDLE of their span at each
    side of its centre, in cue time, where the fix puts the cues of that audio. At a third of the span, a cut of a second
    moves the middle window only a third of a second off the line, and the spread of a right track can hide that."""
    a, b = confirm["ends"]
    rate = float(Fraction(confirm["rate"]))
    return (tuple((rate * ((a + b) / 2 + k * MIDDLE * (b - a)) + confirm["offset"]) / duration for k in (-1, 1)),)


FAR = (tuple(r for r in RATES if r > 1.01), tuple(r for r in RATES if r < 0.99))   # the ratios far from 1: faster, slower


def drift(starts, hint=None):
    """[window start] of the windows that hear the speech of dense cues when the track drifts far from 1. starts are the
    cue times where the windows of the first hearing start. At 25/23.976 the speech of a cue at 20 minutes is 50 seconds
    earlier in the audio, so a window at cue time can hear silence. For each start and each group of FAR, the window
    lies where that ratio puts the speech. hint is (audio time, offset) of a window whose words matched, the offset cue
    time minus audio time, so the fix goes through it. Else the cues start with the audio. The ratios of a group lie
    within 0.1 percent of each other, so one window hears them all."""
    t, d = hint or (0.0, 0.0)
    return [round(max(0.0, statistics.fmean(t + (s - t - d) / float(r) for r in g)), 1) for s in starts for g in FAR]


def moved(ms, fix):
    """A cue time in ms after fix: (time - offset) / rate."""
    rate = Fraction(fix["rate"])
    return round((ms / 1000 - fix["offset"]) / float(rate) * 1000)
