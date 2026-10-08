# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The whole-file timing of a subtitle (docs/design.md, "Subtitle match"). Pure functions, stdlib only.

Whisper hears the whole file. run() then times every line of a subtitle against those words in one pass.

1. anchors(): one alignment, in order, of the content words of every line to the content words heard in the whole
   file. A line anchors at the heard time of its first spoken word. There is no offset search, so any offset, drift
   or jump aligns.
2. curve(): one offset curve over the anchors. Each segment has a frame-rate ratio and a level, and the curve may
   change at any line. A change costs a fixed amount, so only real jumps start a segment. run() fits it twice: first
   the file's offset, drift and jumps, then local blocks against that.
3. plan(): one gate for every segment. The fewer anchors a segment has, the farther off it must sit to move on
   Whisper alone. The speech onsets can agree or disagree. A segment moves the lines it covers, see owners().
4. The moves keep the screen sound. An anchored line moves toward its own speech and never past it. A line with no
   anchor moves no farther than the anchored lines around it, see short_of_speech(). No line crosses another, see
   apply_order(). Ends keep their length, see ends().
5. Live captions sit off by a different time each line. On such a track each line moves onto its own speech, see
   per_line().
6. judged(): the curve fitted again where the moves put the lines, and the stretches still off.

Every threshold is an entry of subsync.TIMING. With subsync.INVARIANTS, check_plan() checks the safety rules of the
moves (docs/development.md, "Safety self-checks")."""
import bisect
import difflib
import math
import re
import statistics
from fractions import Fraction

from . import decide, subsync

T = subsync.TIMING
LEAD = T["speech lead"]
LYRICS = re.compile(r"[♪♫#¶]")   # a line of song lyrics, as subsync.MUSIC, with the pilcrow some live captions use
EPS = 0.01   # seconds of rounding the safety rules allow. A shift keeps ms, and ASS keeps cs.
DROP = 0.005  # seconds a move must exceed, else it drops


def heard(windows):
    """[(audio time, token)] of every word heard in windows, lid.listen()'s [{"at", "secs", "words": [[seconds from
    at, word]]}], once, in time order. Two windows that overlap split their shared span at its middle."""
    ws = sorted(windows, key=lambda w: w["at"])
    end = lambda w: w["at"] + w.get("secs", subsync.WINDOW)
    out = []
    for k, w in enumerate(ws):
        lo = (end(ws[k - 1]) + w["at"]) / 2 if k else -math.inf
        hi = (end(w) + ws[k + 1]["at"]) / 2 if k + 1 < len(ws) else math.inf
        out += [(round(w["at"] + t, 2), x) for t, x in w["words"] if lo <= w["at"] + t < hi]
    out.sort(key=lambda p: p[0])
    return [(t, v) for t, x in out for v in subsync.tokens(x)]


def anchors(cues, words, stop):
    """{line index: audio time of its first spoken word}. cues are [(start, end, text)] in start order, words the
    heard tokens of heard(), and stop the stopwords of the line's language.

    One alignment, in order, pairs the content words of every line with the heard content words (difflib's matching
    blocks). Only runs of "word run" pairs or more count. A line anchors where its first content word pairs. The anchor
    then steps back over the line's words before it, while each heard word before it is that word and lies under
    "spoken gap" before it. A roll-up line counts by its new text, see subsync.spoken(). A speaker's name is never
    spoken. A line shown under "short line", a line of song lyrics, and two lines in a row with the same content words
    never anchor."""
    texts = [subsync.LABEL.sub(r"\1\2", c[2]) for c in subsync.spoken([c[:3] for c in cues])]
    toks = [subsync.tokens(x) for x in texts]
    said = [subsync.words(x, stop) for x in texts]
    bad = {i for i, (c, x) in enumerate(zip(cues, texts)) if c[1] - c[0] < T["short line"] or LYRICS.search(subsync.NOISE.sub(" ", x))}
    bad |= {k for i in range(len(cues) - 1) if said[i] and said[i] == said[i + 1] for k in (i, i + 1)}
    mine = [(i, p, w) for i, ws in enumerate(said) for p, w in enumerate(ws)]
    theirs = [(k, t, v) for k, (t, v) in enumerate(words) if len(v) > 1 and v not in stop]
    sm = difflib.SequenceMatcher(None, [x[2] for x in mine], [x[2] for x in theirs], autojunk=False)
    out = {}
    for a, b, n in sm.get_matching_blocks():
        if n < T["word run"]:
            continue
        for m in range(n):
            i, p, w = mine[a + m]
            k, t, _ = theirs[b + m]
            if p or i in bad:
                continue
            lead = toks[i][:next((q for q, v in enumerate(toks[i]) if v == w), 0)]
            for v in reversed(lead):
                if k == 0 or words[k - 1][1] != v or t - words[k - 1][0] >= T["spoken gap"]:
                    break
                k, t = k - 1, words[k - 1][0]
            out[i] = t
    return out


def curve(xs, ys, change, rates=subsync.RATES):
    """Segments [(first, last, rate, level)] of points (x, y) in time order, by index: y = rate * x + level in each.
    Here x is a line's anchor and y its start. A Viterbi over a grid of (rate, level) states finds them. Each point
    costs its distance from its segment's line, "fit cap" at most. Each change point costs change times "fit cap",
    and "fit rate" more to enter a ratio other than 1. The level is then the median off the grid."""
    cap, step = T["fit cap"], T["fit step"]
    states = []
    for r in rates:
        fr = float(r)
        states += [(r, fr, v * step) for v in sorted({round((y - fr * x) / step) for x, y in zip(xs, ys)})]
    enter = [0.0 if fr == 1.0 else T["fit rate"] for _, fr, _ in states]
    pen, n = change * cap, len(xs)
    D = [min(abs(ys[0] - fr * xs[0] - v), cap) + e for (_, fr, v), e in zip(states, enter)]
    best, switched = [], []   # per point, the state a change comes from, and which states changed there
    for k in range(1, n):
        j = min(range(len(states)), key=D.__getitem__)
        base, x, y = D[j] + pen, xs[k], ys[k]
        flags, nd = bytearray(len(states)), []
        for s, (_, fr, v) in enumerate(states):
            c = min(abs(y - fr * x - v), cap)
            sw = base + enter[s]
            if D[s] <= sw:
                nd.append(D[s] + c)
            else:
                nd.append(sw + c)
                flags[s] = 1
        D = nd
        best.append(j)
        switched.append(flags)
    s = min(range(len(states)), key=D.__getitem__)
    path = [s]
    for k in range(n - 1, 0, -1):
        if switched[k - 1][s]:
            s = best[k - 1]
        path.append(s)
    path.reverse()
    segs, a = [], 0
    for k in range(1, n + 1):
        if k == n or path[k] != path[a]:
            r = states[path[a]][0]
            segs.append((a, k - 1, r, statistics.median(ys[q] - float(r) * xs[q] for q in range(a, k))))
            a = k
    return segs


def owners(cues, keys, seg_of, sizes):
    """{line index: segment} of every line a segment covers: its anchored lines, and each unanchored line whose
    anchored neighbours both lie in that segment. Both must lie within "line reach" of it, unless the segment has
    "whole anchors" or more. A file edge counts as a near neighbour in the segment."""
    out = {}
    for i, c in enumerate(cues):
        if i in seg_of:
            out[i] = seg_of[i]
            continue
        q = bisect.bisect_left(keys, i)
        p, n = (keys[q - 1] if q else None), (keys[q] if q < len(keys) else None)
        gp, gn = seg_of.get(p), seg_of.get(n)
        if gp is not None and gn is not None and gp != gn:
            continue
        g = gp if gp is not None else gn
        near = (p is None or c[0] - cues[p][0] <= T["line reach"]) and (n is None or cues[n][0] - c[0] <= T["line reach"])
        if sizes[g] >= T["whole anchors"] or near:
            out[i] = g
    return out


def onset_lead(at, onsets):
    """The median of onset less anchor over the anchored lines with exactly one onset of a silence of
    subsync.ONSET_QUIET within "onset around", or None under "onset lines" such lines. It is the offset between
    Whisper's clock and the onsets' clock."""
    ts = [o[0] for o in onsets]
    ds = []
    for a in at.values():
        near = onsets[bisect.bisect_left(ts, a - T["onset around"]):bisect.bisect_right(ts, a + T["onset around"])]
        if len(near) == 1 and near[0][1] >= subsync.ONSET_QUIET:
            ds.append(near[0][0] - a)
    return statistics.median(ds) if len(ds) >= T["onset lines"] else None


def vote(cues, idx, mv, onsets, d_on):
    """("agree" | "disagree" | "few", lines timed where they sit, lines timed where the move puts them): the onsets'
    verdict on moving lines idx by mv[i] each. A line is timed at a place when an onset of a silence of
    subsync.ONSET_QUIET lies within half of subsync.TOLERANCE of where its speech starts there. Only lines that move
    over TOLERANCE count. The onsets agree when enough lines are timed at the new places, twice as many as at the old,
    and as many arise by chance at subsync.ONSET_CHANCE at most. The chance comes from the subsync.NULL offsets."""
    if d_on is None or not onsets:
        return "few", 0, 0
    reach = subsync.TOLERANCE / 2
    ts = [o[0] for o in onsets if o[1] >= subsync.ONSET_QUIET]
    hit = lambda p: bisect.bisect_right(ts, p + reach) > bisect.bisect_left(ts, p - reach)
    idx = [i for i in idx if abs(mv[i]) > 2 * reach]
    old = sum(hit(cues[i][0] - LEAD + d_on) for i in idx)
    new = sum(hit(cues[i][0] + mv[i] - LEAD + d_on) for i in idx)
    places = [cues[i][0] + mv[i] - LEAD + d_on + d for i in idx for d in subsync.NULL]
    chance = len(idx) * sum(map(hit, places)) / len(places) if places else 0.0
    term, below = math.exp(-chance), 0.0
    for k in range(new):   # the Poisson chance of fewer than new hits
        below, term = below + term, term * chance / (k + 1)
    need = max(subsync.ONSET_MIN, T["onset share"] * len(idx))
    if new >= need and new >= 2 * old and 1 - below <= subsync.ONSET_CHANCE:
        return "agree", old, new
    if old >= need and old >= new:
        return "disagree", old, new
    return "few", old, new


def gate(n, far, mid, spread, onsets, most):
    """(moves, why) of a segment of n anchors whose end lines sit far off their speech, the median line mid, and the
    anchors spread from its line at their median. onsets is the verdict of vote(). A segment of "whole anchors" moves
    from "move". A smaller one moves up to most seconds, when the onsets do not disagree, and either agree or
    the segment passes one of the rules on Whisper alone."""
    if n >= T["whole anchors"] and far >= T["move"]:
        return True, "whole"
    if onsets == "disagree":
        return False, "onsets disagree"
    if far > most:
        return False, "too far"
    if onsets == "agree":
        return True, "onsets"
    if n >= T["alone anchors"] and abs(mid) >= T["move alone"]:
        return True, "alone"
    if n >= T["mid anchors"] and abs(mid) >= T["off"]:
        return True, "mid"
    if n >= T["tight anchors"] and abs(mid) >= T["tight move"] and spread <= T["tight spread"]:
        return True, "tight"
    return False, "too little"


def plan(cues, at, segs, keys, onsets, most, step):
    """({line index: move in seconds}, [segment record]) of one pass of the curve. keys[k] is the line of the k-th
    anchor. A segment moves when its first or last anchored line sits TIMING "move" or more off its speech and its
    evidence passes gate(). A line moves with the segment that covers it, see owners(), onto the speech the segment
    puts it at. step names the pass in each record."""
    seg_of = {keys[k]: g for g, (a, b, r, v) in enumerate(segs) for k in range(a, b + 1)}
    sizes = [b - a + 1 for a, b, r, v in segs]
    own = owners(cues, keys, seg_of, sizes)
    d_on = onset_lead(at, onsets)
    news, out = {}, []
    for g, (a, b, r, v) in enumerate(segs):
        f = lambda s: (s - v) / float(r) + LEAD   # where the segment puts the line that starts at s
        far = max(abs(cues[i][0] - f(cues[i][0])) for i in (keys[a], keys[b]))
        rec = {"pass": step, "first": keys[a], "last": keys[b], "anchors": sizes[g], "rate": str(r), "level": round(v, 3),
               "far": round(far, 3), "moves": False, "why": "in line"}
        out.append(rec)
        if far < T["move"]:
            continue
        idx = [i for i, h in own.items() if h == g]
        mv = {i: f(cues[i][0]) - cues[i][0] for i in idx}
        verdict, old, new = vote(cues, idx, mv, onsets, d_on)
        spread = statistics.median(abs(cues[keys[k]][0] - float(r) * at[keys[k]] - v) for k in range(a, b + 1))
        rec["moves"], rec["why"] = gate(sizes[g], far, statistics.median(mv.values()), spread, verdict, most)
        rec.update(lines=len(idx), spread=round(spread, 3), onsets=[verdict, old, new])
        if rec["moves"]:
            news.update({i: (m, len(out) - 1) for i, m in mv.items() if m})
    return news, out


def short_of_speech(cues, at, mv):
    """mv with the move of each anchored line cut so it ends at its own speech at most: toward it, never past it. A line
    with no anchor then moves the smaller of these moves of the anchored lines on each side at most. It stays when one
    of them stays or moves the other way. At a file edge only the one side counts."""
    out = {}
    for i, m in mv.items():
        if i in at:
            d = at[i] + LEAD - cues[i][0]
            m = 0.0 if d * m <= 0 else (m if abs(m) <= abs(d) else d)
        if abs(m) > DROP:
            out[i] = m
    keys = sorted(at)
    for i, m in list(out.items()):
        if i not in at:
            q = bisect.bisect_left(keys, i)
            near = [out.get(keys[k], 0.0) for k in (q - 1, q) if 0 <= k < len(keys)]
            cap = min((abs(x) if x * m > 0 else 0.0 for x in near), default=abs(m))
            if cap > DROP:
                out[i] = math.copysign(min(abs(m), cap), m)
            else:
                del out[i]
    return out


def apply_order(cues, news, pile=True):
    """{line index: new start} of news cut short where a line would cross another, or start under "shown" from it
    (or under the gap the two had). The line that moved toward the other yields, never past its old start. With pile,
    a line cut more than "pile" short of its target stays where it was. Live captions move every line, so there a
    line is cut short instead, as one line that stays would hold back every later line. Moves of DROP or less drop."""
    s0 = [c[0] for c in cues]
    want = [news.get(i, x) for i, x in enumerate(s0)]
    s = list(want)
    for k in range(4 * len(s) + 10):   # forward and backward passes in turn, so a clamp travels the file in one pass
        bad = False
        for i in (range(1, len(s)) if k % 2 == 0 else range(len(s) - 1, 0, -1)):
            g = min(T["shown"], s0[i] - s0[i - 1])
            if s[i] - s[i - 1] >= g - 1e-9:
                continue
            bad = True
            mi, mj = s[i] - s0[i], s[i - 1] - s0[i - 1]
            if mi < 0 and (mj <= 0 or -mi >= mj):
                s[i] = min(s0[i], s[i - 1] + g)
                if pile and abs(s[i] - want[i]) > T["pile"]:
                    s[i] = want[i] = s0[i]
            else:
                s[i - 1] = max(s0[i - 1], s[i] - g) if mj > 0 else s0[i - 1]
                if pile and abs(s[i - 1] - want[i - 1]) > T["pile"]:
                    s[i - 1] = want[i - 1] = s0[i - 1]
        if not bad:
            break
    else:
        raise AssertionError("apply_order() did not settle")
    return {i: x for i, x in enumerate(s) if abs(x - s0[i]) > DROP}


def after(cues, i):
    """The index of the line after line i in cues, or None: the first later line that line i did not show over. Line i
    ended before it starts, or ran up to it within "touch". Lines in between showed together with line i."""
    for k in range(i + 1, len(cues)):
        if cues[k][0] > cues[i][0] and cues[i][1] <= cues[k][0] + T["touch"]:
            return k
    return None


def ends(cues, news):
    """{line index: new end} of every line that moved or whose line after moved, see after(). A line moves its end with
    its start, so it keeps its length. It stops subsync.FRAMES before the new start of the line after, but it shows
    "shown" at least, unless that line starts sooner. A line that ended under "near gap" before the next line in start
    order, its line after, runs up to its new start, less that gap, so a move opens no blank. It then shows "end hold"
    at most, or its old length when that is longer. A line whose line after comes later, past lines shown inside it,
    never grows past "shown" that way."""
    out = {}
    s = [news.get(i, c[0]) for i, c in enumerate(cues)]
    for i, c in enumerate(cues):
        n = after(cues, i)
        if i not in news and (n is None or n not in news):
            continue
        e = c[1] + s[i] - c[0]
        if n is not None:
            gap = max(0.0, cues[n][0] - c[1])
            least = min(s[n], s[i] + T["shown"])   # the end gives way before the line shows under "shown"
            if gap < T["near gap"] and n == i + 1:   # it ran up to the line after, and it still does
                e = max(min(s[n] - gap, s[i] + max(T["end hold"], c[1] - c[0])), min(e, least))
            else:
                e = max(min(e, s[n] - subsync.FRAMES), least)
        out[i] = e
    return out


def per_line(cues, at):
    """{line index: move} of live captions: each anchored line onto its own speech. An unanchored line moves by the
    smaller move of the anchored lines on each side when both move the same way, else it stays."""
    keys = sorted(at)
    mv = {i: at[i] + LEAD - cues[i][0] for i in keys}
    for i in range(len(cues)):
        if i in mv:
            continue
        q = bisect.bisect_left(keys, i)
        if 0 < q < len(keys):
            a, b = mv[keys[q - 1]], mv[keys[q]]
            if a * b > 0:
                mv[i] = a if abs(a) < abs(b) else b
    return mv


def judged(cues, at, news):
    """The judge's view of a track after the moves news {line index: new start}: {"judged", "anchors", "curve", "off"}.
    "judged" is False under "judge anchors" anchored lines, and then nothing else counts. "curve" is the offset curve
    fitted again at the new starts, ratio 1. Each segment is {"first", "last", "anchors", "late"}: its first and last
    anchored line, and how late its lines sit against their speech, in seconds. "off" holds the stretches still off,
    each {"at", "to", "lines", "late", "first", "last"} in the same terms, at and to in seconds of the file. A stretch
    is a segment of TIMING "dense run" anchors or more that sits TIMING "alert" or more off. It is also a segment of
    "tight anchors" or more that sits "tight move" off, with its anchors within "post spread" of its line."""
    keys = sorted(at)
    if len(keys) < T["judge anchors"]:
        return {"judged": False, "anchors": len(keys), "curve": [], "off": []}
    ys = [news.get(i, cues[i][0]) for i in keys]
    segs, off = [], []
    for a, b, r, v in curve([at[i] for i in keys], ys, T["fit change"], rates=(Fraction(1),)):
        late, n = v - LEAD, b - a + 1
        segs.append({"first": keys[a], "last": keys[b], "anchors": n, "late": round(late, 2)})
        spread = statistics.median(abs(ys[k] - at[keys[k]] - v) for k in range(a, b + 1))
        if n >= T["dense run"] and abs(late) >= T["alert"] or n >= T["tight anchors"] and abs(late) >= T["tight move"] and spread <= T["post spread"]:
            off.append({"at": round(min(ys[a:b + 1]), 2), "to": round(max(ys[a:b + 1]), 2), "lines": n, "late": round(late, 2), "first": keys[a], "last": keys[b]})
    return {"judged": True, "anchors": len(keys), "curve": segs, "off": off}


def run(cues, windows, onsets=(), lang="eng"):
    """The whole-file timing of one subtitle. cues are its [(start, end, text)] in start order, windows the whole
    file's heard words in lid.listen()'s format, see heard(), onsets [(time, seconds of silence before)] of
    subtitles.onsets(), and lang the subtitle's 639-2 language, whose stopwords never align. Returns a dict:

    - "anchors": {line index: audio time of its first spoken word}, see anchors().
    - "curve": a record per segment of each pass, see plan(): {"pass": "whole" or "fine", "first", "last": its first
      and last anchored line, "anchors", "rate", "level": line start = rate * speech + level, "far": seconds its end
      lines sit off their speech, "moves", "why": the rule that decided}. A segment that sits TIMING "move" or more
      off also holds "lines", "spread" and "onsets": [verdict, lines timed where they sit, where it puts them].
    - "scatter": the median seconds of the anchors from the first pass's curve, or None under "fit anchors".
    - "live": True when the scatter makes the track live captions, see per_line().
    - "moves": {line index: {"start", "end", "segs", "why"}} of each line whose times change. "segs" are the indices
      in "curve" of the segments that moved it. "why" is "curve" (it moved with its segments), "speech" (it stops at
      its own speech), "order" (it stops short of a line next to it), "live" (onto its own speech), "between" (by
      the smaller move of the anchored lines around it) or "end" (only its end changes, as the next line moved).
    - "judge": the view of judged() at the new starts.

    The first pass fits the curve at every ratio of subsync.RATES and plans its moves. Over "live scatter" each line
    moves on its own instead. Else a second pass fits ratio 1 with "fine change" at the starts the first pass gives,
    and plans local blocks of "block most" at most. The two moves add up."""
    stop = decide.STOPWORDS.get(lang, frozenset())
    at = anchors(cues, heard(windows), stop)
    keys = sorted(at)
    out = {"anchors": at, "curve": [], "scatter": None, "live": False, "moves": {}}
    if len(keys) < T["fit anchors"]:
        return dict(out, judge=judged(cues, at, {}))
    xs = [at[i] for i in keys]
    segs = curve(xs, [cues[i][0] for i in keys], T["fit change"])
    mv, recs = plan(cues, at, segs, keys, onsets, T["block most"], "whole")
    out["scatter"] = scatter = statistics.median(abs(cues[keys[k]][0] - float(r) * xs[k] - v) for a, b, r, v in segs for k in range(a, b + 1))
    if scatter >= T["live scatter"]:
        want = {i: m for i, m in per_line(cues, at).items() if abs(m) > DROP}
        news = apply_order(cues, {i: cues[i][0] + m for i, m in want.items() if cues[i][0] + m >= 0}, pile=False)   # never before the file's start
        why = {i: "live" if i in at else "between" for i in want}
        segs_of = {}
    else:
        moved = [(c[0] + mv[i][0], c[1] + mv[i][0], c[2]) if i in mv else c for i, c in enumerate(cues)]
        mv2, recs2 = plan(moved, at, curve(xs, [moved[i][0] for i in keys], T["fine change"], rates=(Fraction(1),)), keys,
                          onsets, T["block most"], "fine")
        segs_of = {i: [mv[i][1]] if i in mv else [] for i in set(mv) | set(mv2)}
        for i, (m, g) in mv2.items():
            segs_of[i].append(len(recs) + g)
        recs += recs2
        total = {i: mv.get(i, (0.0,))[0] + mv2.get(i, (0.0,))[0] for i in segs_of}
        want = short_of_speech(cues, at, total)
        why = {i: "curve" if abs(m - total[i]) <= 1e-9 else "speech" if i in at else "between" for i, m in want.items()}
        news = apply_order(cues, {i: cues[i][0] + m for i, m in want.items() if cues[i][0] + m >= 0})   # never before the file's start
    out.update(curve=recs, live=scatter >= T["live scatter"])
    for i, e in ends(cues, news).items():
        s = news.get(i, cues[i][0])
        if abs(s - cues[i][0]) <= 0.0005 and abs(e - cues[i][1]) <= 0.0005:
            continue
        w = "end" if i not in news else "order" if abs(news[i] - cues[i][0] - want[i]) > DROP else why[i]
        out["moves"][i] = {"start": s, "end": e, "segs": segs_of.get(i, []) if i in news else [], "why": w}
    out["judge"] = judged(cues, at, news)
    if subsync.INVARIANTS:
        check_plan(cues, at, out["moves"], {"cues": cues, "anchors": at, "moves": out["moves"]})
    return out


def check_plan(cues, at, moves, case):
    """Check the safety rules of the moves of run(), see subsync.INVARIANTS. Each allows EPS of rounding.

    - Order: no line crosses another. Two line starts sit "shown" apart at least, or as far as they did.
    - Start: no line starts before the file's start.
    - Stacked: a line never shows past the start of the line after, see after(). It counts only where the line's end
      or that start changed. Lines keep their order, so no line then shows past a later line it did not show over.
    - Speech: an anchored line moves toward its own speech and never past it.
    - Shown: a line shows "shown" at least, unless it showed less before or the line after starts sooner.
    - Ends: a line keeps its length, unless the line after makes it change. It grows to "shown" at most, or, when it
      ran up to the line after and that is the next line, to "end hold" or its old length. Raises subsync.Broken."""
    new = [(moves[i]["start"], moves[i]["end"]) if i in moves else c[:2] for i, c in enumerate(cues)]
    for i, (c, (s, e)) in enumerate(zip(cues, new)):
        nxt, n = cues[i + 1] if i + 1 < len(cues) else None, after(cues, i)
        if s < -EPS:
            subsync.broken("start", c[0], f"it starts at {s:.3f} s, before the file's start", case)
        if nxt and new[i + 1][0] - s < min(T["shown"], nxt[0] - c[0]) - EPS:
            subsync.broken("order", c[0], f"the next line starts {new[i + 1][0] - s:.3f} s after it, from {nxt[0] - c[0]:.3f} s", case)
        if n is not None and abs(e - c[1]) + abs(new[n][0] - cues[n][0]) > 0.0005 and e > new[n][0] + EPS:
            subsync.broken("stacked", c[0], f"it now shows {e - new[n][0]:.3f} s past the start of the line after", case)
        if i not in moves:
            continue
        if i in at:
            d, m = at[i] + LEAD - c[0], s - c[0]
            if abs(m) > EPS and (d * m < 0 or abs(m) > abs(d) + EPS):
                subsync.broken("speech", c[0], f"it moves {m:+.3f} s, and its speech lies {d:+.3f} s away", case)
        old, length = c[1] - c[0], e - s
        if length < min(old, T["shown"], new[n][0] - s if n is not None else math.inf) - EPS:
            subsync.broken("shown", c[0], f"it shows {length:.3f} s, from {old:.3f} s", case)
        if abs(length - old) <= EPS:
            continue
        if n is None:
            subsync.broken("ends", c[0], f"its length changes from {old:.3f} s to {length:.3f} s, and no line after makes it", case)
        if length > max(old, T["shown"]) + EPS and (n != i + 1 or cues[n][0] - c[1] >= T["near gap"] or length > max(T["end hold"], old) + EPS):
            subsync.broken("ends", c[0], f"it grows from {old:.3f} s to {length:.3f} s", case)
