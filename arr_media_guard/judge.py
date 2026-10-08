# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The timing outcome of each subtitle (docs/design.md, "Timing outcome").

The subtitle checks add evidence and proposals. The planner picks the moves, and the remux or the sidecar rewrite
writes them. Then one judge reads every piece of evidence at the times the moves put the lines, and gives each
subtitle one outcome. One alert builder words the alert and the change post from that outcome. No other stage
decides whether a subtitle's times post.

The outcome record is the contract between the judge and the alert builder. The decision record keeps one per
subtitle in rec["subjudge"], {track position or sidecar name: outcome}. An outcome is a dict:

- "state": one of STATES.
  - "in time": nothing moved, and no evidence puts a stretch off.
  - "fixed": lines moved, and no evidence puts a stretch off after the moves.
  - "partly fixed": lines moved, and evidence puts a stretch off after the moves.
  - "off": nothing moved, and evidence puts a stretch off.
  - "unknown": no evidence judged the times.
- "fix": the shift of the whole subtitle that the run planned, {"rate", "offset"} as subsync.fit() gives it, or None.
- "moved": the lines that the whole-file timing planned to move to their speech, see subtitles.sub_whole(). 0 when
  it planned none. A shift of the whole subtitle is in "fix".
- "written": what became of the plan, one of WRITTEN, or None when the run planned nothing.
  - "done": the remux or the sidecar rewrite wrote it.
  - "planned": a dry run planned it, and the outcome is the one --apply gives.
  - "failed": the write failed, so nothing moved.
  - "check": SUBTITLES check left the plan out, so nothing moved.
- "off": the stretches still off after the moves that took effect, in time order. Each is a dict:
  - "at": where the stretch starts, in seconds of the file: its earliest line, or the start of the window that heard
    it.
  - "to": where it ends: its latest line, or the end of the window.
  - "lines": the lines of the stretch, or None when its evidence does not count lines, as a word-check window does.
  - "first", "last": True when the stretch starts at the subtitle's first line, or runs to its last line. A stretch
    with both holds the whole subtitle.
  - "late": how late its lines sit against their speech, in seconds. A negative value is early.
  - "clock": the evidence that found it, one of CLOCKS.
- "checked": whether evidence away from the moved lines judged the rest of the file. With nothing moved, it is
  whether any evidence judged the times.
- "why": how the judge decided, for the decision log.
- "seen": only with "off" stretches whose evidence also reads the subtitle as a whole. It holds the keys of the
  timing that the alert words: "offsets" (steps), "unfixed" (one offset no fix lined up), "unconfirmed" (a fix no
  check confirmed), "would" (a speech layout fix that only alerts), "unheard" (the whole-file timing judged nothing),
  "layout" (the speech layout timed it) and "ref" (the reference it was timed against).
- "live": only for live captions, the facts that the alert words, see live_facts(): "lag", "moved", "cues", "left",
  "fixed", and "hearing_stopped" when the hearing stopped part way.

A subtitle whose outcome is "off" or "partly fixed" posts, and the others post nothing. check_outcome() holds these
rules."""
import bisect, statistics

from . import subsync

STATES = ("in time", "fixed", "partly fixed", "off", "unknown")
WRITTEN = ("done", "planned", "failed", "check")
CLOCKS = ("whole", "window", "slices", "layout", "live")   # the whole-file timing, the word check, a reference, the speech
                                                          # layout, live captions
SEEN = ("offsets", "unfixed", "unconfirmed", "would", "unheard", "layout", "ref")
LIVE = ("lag", "moved", "cues", "left", "fixed", "hearing_stopped")


def took(o):
    """Whether the moves of outcome o took effect: the run wrote them, or a dry run planned them."""
    return o["written"] in ("done", "planned") and bool(o["fix"] or o["moved"])


def lines_moved(e):
    """The lines whose start the whole-file timing e moves, see subtitles.sub_whole(). A line whose end alone changes,
    as its next line moved, does not count."""
    return sum(m["why"] != "end" for m in (e or {}).get("moves", {}).values())


def whole_stretches(e, off, clock):
    """The outcome's stretches of off, stretches of align.judged() of the whole-file timing e. No line before a stretch
    that starts at the first anchored line was heard in time, so it starts at the subtitle's first line. One that ends
    at the last anchored line runs to its last line the same way. A stretch counts every line between its ends."""
    segs = e["judge"]["curve"]
    first, last = (segs[0]["first"], segs[-1]["last"]) if segs else (None, None)   # the first and the last anchored line
    out = []
    for x in off:
        a, b = (0 if x["first"] == first else x["first"]), (e["lines"] - 1 if x["last"] == last else x["last"])
        out.append({"at": x["at"], "to": x["to"], "lines": b - a + 1, "late": x["late"], "clock": clock,
                    **({"first": True} if a == 0 else {}), **({"last": True} if b == e["lines"] - 1 else {})})
    return out


def still_off(o):
    """Whether outcome o posts, because lines are still off."""
    return o["state"] in ("off", "partly fixed")


def steps(o):
    """Whether outcome o leaves lines off by different amounts in different parts of the file: its stretches still off
    sit over subsync.TOLERANCE apart, or the word check or a reference heard different offsets."""
    lates = [x["late"] for x in o["off"]]
    return still_off(o) and (bool((o.get("seen") or {}).get("offsets")) or len(lates) > 1 and max(lates) - min(lates) > subsync.TOLERANCE)


def outcome(state, fix=None, moved=0, written=None, off=(), checked=True, why="", seen=None, live=None):
    """An outcome record, see the module's docstring."""
    return {"state": state, "fix": fix, "moved": moved, "written": written, "off": sorted(off, key=lambda x: x["at"]), "checked": checked,
            "why": why, **({"seen": seen} if seen else {}), **({"live": live} if live else {})}


def outcome_faults(o):
    """The rules of the outcome record that o breaks, as sentences. [] for a sound record."""
    faults = []
    rule = lambda ok, why: faults.append(why) if not ok else None
    rule(o.get("state") in STATES, f'state {o.get("state")!r} is none of {STATES}')
    rule(o.get("written") in WRITTEN + (None,), f'written {o.get("written")!r} is none of {WRITTEN} or None')
    rule(isinstance(o.get("moved"), int) and o["moved"] >= 0, f'moved {o.get("moved")!r} is no count of lines')
    rule(o.get("fix") is None or {"rate", "offset"} <= set(o["fix"]), f'fix {o.get("fix")!r} is no fix')
    rule(o.get("written") is None or bool(o.get("fix") or o.get("moved")), "written names a plan, but the run planned no move")
    rule(set(o.get("seen") or ()) <= set(SEEN), f'seen holds keys outside {SEEN}')
    rule(set(o.get("live") or ()) <= set(LIVE), f'live holds keys outside {LIVE}')
    if faults:
        return faults
    for x in o["off"]:
        rule(x.get("clock") in CLOCKS, f'a stretch has clock {x.get("clock")!r}, none of {CLOCKS}')
        rule(isinstance(x.get("at"), (int, float)) and isinstance(x.get("to"), (int, float)) and x["at"] <= x["to"], f'a stretch runs from {x.get("at")} to {x.get("to")}')
        rule(x.get("lines") is None or isinstance(x["lines"], int) and x["lines"] > 0, f'a stretch holds {x.get("lines")!r} lines')
        rule(isinstance(x.get("late"), (int, float)) and x["late"] != 0, f'a stretch sits {x.get("late")!r} s off')
    rule([x["at"] for x in o["off"]] == sorted(x["at"] for x in o["off"]), "the stretches are not in time order")
    moved, off = took(o), bool(o["off"])
    rule(o["state"] not in ("fixed", "partly fixed") or moved, f'state {o["state"]} but no move took effect')
    rule(o["state"] not in ("in time", "off") or not moved, f'state {o["state"]} but a move took effect')
    rule(o["state"] not in ("off", "partly fixed") or off, f'state {o["state"]} names no stretch still off')
    rule(o["state"] not in ("in time", "fixed", "unknown") or not off, f'state {o["state"]} names a stretch still off')
    rule(o["state"] != "unknown" or not o["checked"], "state unknown, but evidence judged the times")
    rule(o["state"] not in ("in time", "off") or o["checked"], f'state {o["state"]}, but no evidence judged the times')
    rule("seen" not in o or off, "seen without a stretch still off")
    return faults


# The judge. Every piece of evidence goes through the plan to where the plan puts its lines. The whole-file timing of
# --sub-time and the deep analysis judges every line it anchored, see whole_outcome(). An import has only the windows
# of its word check, see window_outcome().
SNAP = 0.1      # seconds within which a window's cue time names a cue start, as the record rounds both


def snap(c, starts):
    """The cue start of starts, sorted, nearest c and within SNAP of it, else c."""
    i = bisect.bisect_left(starts, c)
    near = min(starts[max(0, i - 1):i + 1], key=lambda s: abs(s - c), default=None)
    return near if near is not None and abs(near - c) <= SNAP else c


def window_late(at, late, starts, fix):
    """(the cue start nearest the window's lines, seconds late after the plan) of the lines a word-check window heard at
    audio time at, which sat late seconds late as they were. The plan's fix moves them as remux.time_plan() moves a
    line at their own time."""
    c = at + late + subsync.CUE_LEAD
    return snap(c, starts), round(late + (subsync.moved(c * 1000, fix) / 1000 if fix else c) - c, 2)


def window_outcome(r, starts, fix):
    """(the stretches still off, whether any window judged the times, why, the late of each window after the plan) of a
    subtitle the word check read, after the plan's fix. r is its sync entry and starts its sorted cue starts. A window
    of subsync.MIN_WORDS heard words or more judges its lines, and one subsync.ALERT or more off is a stretch."""
    off, lates = [], []
    for w in sorted(r.get("windows") or (), key=lambda w: w["at"]) if r.get("verdict") == "match" else ():   # in time order, as the alert names them
        if w.get("late") is None or w["words"] < subsync.MIN_WORDS:
            continue
        _, late = window_late(w["at"] + w.get("secs", subsync.WINDOW) / 2, w["late"], starts, fix)
        lates.append(late)
        if abs(late) >= subsync.ALERT:
            off.append({"at": w["at"], "to": round(w["at"] + w.get("secs", subsync.WINDOW), 1), "lines": None, "late": late, "clock": "window"})
    why = "; ".join(f'{x["clock"]} {x["late"]:+.2f} s at {x["at"]:.0f} s' for x in off) or ("no evidence sits off after the plan" if lates else "no window judged")
    return off, bool(lates), why, lates


def heard_again(rec, k, lo, hi):
    """Whether the run of rec heard the times of subtitle k from lo to hi seconds: the whole-file timing judged it and
    its hearing holds that audio, see subtitles.sub_whole(), or a reference or the speech layout timed it, or a
    word-check window that timed its lines lies there."""
    e = (rec.get("whole") or {}).get(k)
    if e and e["judge"]["judged"] and any(a <= hi and lo <= b for a, b in e["heard"]):
        return True
    if k in (rec.get("subtime") or {}):   # a reference or the speech layout times the whole subtitle again
        r = rec["subtime"][k]
        return r.get("verdict") == "fit" or (r.get("layout") or {}).get("verdict") == "fit"
    r = (rec.get("subcheck") or {}).get(k) or {}
    return any(w.get("late") is not None and w["at"] < hi and lo < w["at"] + w.get("secs", subsync.WINDOW) for w in r.get("windows") or ())


def plan_of(rec, k, t, flags_off):
    """(fix, moved lines, written) of the plan of the run of rec for subtitle k, whose timing is t. The whole-file
    timing of rec["whole"] moves lines, see subtitles.sub_whole(). A track's plan is in rec["subremux"], a sidecar's in
    rec["sidecars"]. At SUBTITLES check the run plans nothing, and the fix the check found is the plan that "check"
    left out."""
    rm, apply = rec.get("subremux") or {}, rec.get("apply", True)
    side = next((e for e in rec.get("sidecars") or () if e["name"] == k and e["action"] == "retime"), None)
    moved = lines_moved((rec.get("whole") or {}).get(k))
    if not flags_off:
        return (t.get("fix"), moved, "check") if t.get("fix") or moved else (None, 0, None)
    if side:
        fix, result = t.get("fix"), side.get("result")
        written = "done" if result == "retimed" else "planned" if result == "dry run" else "failed"
    else:
        fix, moved = t.get("fix") if k in (rm.get("fixed") or ()) else None, moved if k in (rm.get("timed") or ()) else 0
        written = "done" if rm.get("done") else "planned" if not apply else "failed"
    return (fix, moved, written) if fix or moved else (None, 0, None)


def plan_stretch(fix, dur, clock):
    """The stretch a plan that did not take effect leaves off: the whole file at its fix."""
    end = subsync.moved(dur * 1000, fix) / 1000 - dur if fix and dur else 0.0
    return [{"at": 0.0, "to": round(dur, 1), "lines": None, "late": round(fix["offset"] if fix and fix["offset"] else end or subsync.ALERT, 2), "clock": clock}]


def live_facts(e, off):
    """The "live" facts of an outcome of live captions that the whole-file timing e timed, with the stretches off still."""
    return {"lag": e["lag"] or subsync.LIVE_LAG, "moved": lines_moved(e), "cues": e["lines"], "left": sum(x["lines"] for x in off), "fixed": not off,
            **({"hearing_stopped": True} if e.get("stopped") else {})}


def whole_outcome(e, t, plan, dur):
    """The outcome of a subtitle that the whole-file timing e judged, see subtitles.sub_whole(), with the timing t of
    sync and plan, the plan's fix, moved lines and written. Its judge reads the stretches still off where the moves
    put the lines, see align.judged(), or where they sit when no move took effect. A track the timing did not judge
    alerts with what the word check found, a fix no check confirmed or an offset none fixed, see off_facts()."""
    done = took(plan)
    clock = "live" if e["live"] else "whole"
    off = whole_stretches(e, e["judge"]["off"] if done else e["before"], clock)
    checked, seen = e["judge"]["judged"], None
    if not checked and (t.get("unconfirmed") or t.get("unfixed") is not None or t.get("piecewise")):   # the word check's own finding
        steps = bool(t.get("piecewise")) and len(t.get("offsets") or ()) > 1
        late = max(t["offsets"], key=abs) if steps else t["unfixed"] if t.get("unfixed") is not None else t["unconfirmed"]["offset"]
        off = [{"at": 0.0, "to": round(dur, 1), "lines": None, "late": round(late or subsync.ALERT, 2), "clock": "window"}]
        seen = {**({"offsets": t["offsets"]} if steps else {"unfixed": t["unfixed"]} if t.get("unfixed") is not None else {}),
                **{x: t[x] for x in ("unconfirmed", "unheard") if t.get(x)}}
    state = ("partly fixed" if off else "fixed") if done else "off" if off else "in time" if checked else "unknown"
    return outcome(state, **plan, off=off, checked=checked or bool(off), seen=seen, why=t.get("why") or "",
                   live=live_facts(e, off) if e["live"] and checked else None)


def outcomes(rec, sync, starts, flags_off=True):
    """{subtitle: its outcome} of the run of rec, see the module's docstring. sync holds the word check and the reference
    timing of each subtitle, {position or sidecar name: entry}, and starts {subtitle: its sorted cue starts in seconds}.
    flags_off is False at SUBTITLES check, see subtitles.sub_fixes(). A subtitle that does not match the audio gets no
    outcome: its alert says so. Nor does one a remux removed or took out as garbled.

    A plan that failed or that SUBTITLES check left out leaves its own stretches off. A track whose garbled text a
    repair rewrites first gets "unknown", as a later run judges its times. A subtitle the whole-file timing timed goes
    to whole_outcome(). A subtitle timed by a reference or by the speech layout takes its fit: steps from
    subsync.ALERT_ROWS, or when subsync.stepped() shows a jump, and an offset no fix lined up. Any other subtitle goes to
    window_outcome()."""
    rm, out = rec.get("subremux") or {}, {}
    gone = set(rm.get("remove") or ()) | set(rm.get("stripped") or ())
    dur = rec.get("file_duration") or 0.0
    for k, r in sorted(sync.items()):
        if r.get("verdict") == "mismatch" or k in gone:
            continue
        t, e = r.get("timing") or {}, (rec.get("whole") or {}).get(k)
        fix, moved, written = plan_of(rec, k, t, flags_off)
        plan = {"fix": fix, "moved": moved, "written": written}
        done = written in ("done", "planned")
        clock = "layout" if (r.get("layout") or {}).get("verdict") == "fit" else "slices" if k in (rec.get("subtime") or {}) else "window"
        if ((rec.get("garbled") or {}).get(k) or {}).get("later"):
            out[k] = outcome("unknown", checked=False, why="a repair of its text runs first, and a later run judges its times")
        elif written in ("failed", "check") and e:
            off = whole_stretches(e, e["before"], "live" if e["live"] else "whole") or plan_stretch(fix, dur, "whole")
            out[k] = outcome("off", **plan, off=off, live=live_facts(e, off) if e["live"] else None,
                             why="the plan did not take effect" if written == "failed" else "SUBTITLES check left the plan out")
        elif written in ("failed", "check"):
            out[k] = outcome("off", **plan, off=plan_stretch(fix, dur, clock),
                             why="the plan did not take effect" if written == "failed" else "SUBTITLES check left the plan out")
        elif e:
            out[k] = whole_outcome(e, t, plan, dur)
        elif k in (rec.get("subtime") or {}):   # the fit's own measures: its slices, an offset it found, a fix only the layout alerts
            offsets, unfixed, would = t.get("offsets") or [], t.get("unfixed"), t.get("would")
            # Slices no ratio explains are "piecewise". The speech layout drops that mark when stepped() reads no jump in
            # them, see subsync.layout_fix(), and such a subtitle stays in time.
            steps = bool(t.get("piecewise")) and (round(max(offsets) - min(offsets), 2) >= subsync.ALERT_ROWS or subsync.stepped(offsets))
            seen = {"offsets": offsets} if steps else {x: v for x, v in (("unfixed", unfixed), ("would", would)) if v is not None}
            if clock == "layout":
                seen["layout"] = True
            elif r.get("reference"):
                seen["ref"] = r["reference"]
            judged = r.get("verdict") == "fit" or (r.get("layout") or {}).get("verdict") == "fit"
            if done:
                out[k] = outcome("fixed", **plan, why=t.get("why") or "")
            elif steps or unfixed is not None:
                late = max(offsets, key=abs) if steps else unfixed
                out[k] = outcome("off", **plan, off=[{"at": 0.0, "to": round(dur, 1), "lines": None, "late": round(late or subsync.ALERT, 2), "clock": clock}],
                                 seen=seen, why=t.get("why") or "")
            else:
                out[k] = outcome("in time" if judged else "unknown", **plan, checked=judged, why=t.get("why") or r.get("why") or "")
        else:
            off, checked, why, lates = window_outcome(r, starts.get(k) or [], fix if done else None)
            seen = window_words(off, lates, t) if not done else None
            state = ("partly fixed" if off else "fixed") if done and (fix or moved) else "off" if off else "in time" if checked else "unknown"
            out[k] = outcome(state, **plan, off=off, checked=checked, seen=seen, why=why)
    return out


def window_words(off, lates, t):
    """The facts that word the stretches off as the word check heard them, when a word-check window sits off. lates are
    the late of each word-check window after the plan, as window_outcome() gives them, and t the timing. Lates that
    differ are "offsets", and one late for every window is "unfixed". "unconfirmed" holds a fix the plan refused, and
    "unheard" says the whole-file timing judged nothing."""
    if not any(x["clock"] == "window" for x in off):
        return None
    words = {"offsets": lates} if max(lates) - min(lates) > subsync.TOLERANCE else {"unfixed": round(statistics.median(lates), 2)}
    return {**words, **{x: t[x] for x in ("unconfirmed", "unheard") if t.get(x)}}


def check_outcome(rec, findings=()):
    """With subsync.INVARIANTS, raise subsync.Broken when the outcomes of rec["subjudge"] and the subtitle findings break
    a rule (docs/development.md, "Safety self-checks"):
    - Each outcome is a sound record, see outcome_faults().
    - A subtitle whose outcome posts nothing gets no timing sentence, and one whose lines are still off gets one. A plan
      that failed leaves that to the not_retimed or sidecar_left sentence. A sentence on lines that flash by, see
      flashes(), says nothing of the times.
    - No sentence of a subtitle whose lines moved says they were left as they are.
    The judge reads the evidence, the plan and its write. Of an earlier stage it reads only the measures that came with
    the evidence, such as a reference fit's slices, so no flag outlives its facts."""
    if not subsync.INVARIANTS:
        return
    from . import report   # report imports this module
    faults, outs = [], rec.get("subjudge") or {}
    lines = [x for f in findings if f["kind"] == "subtiming" for x in f["lines"] if not flashes(x)]
    named = lambda x: {x["track"]} if "track" in x else set(x.get("tracks") or ()) | {k for k, *_ in x.get("far") or ()} | ({x["name"]} if "name" in x else set())
    for k, o in outs.items():
        faults += [f"{k}: {f}" for f in outcome_faults(o)]
        says = [x for x in lines if k in named(x)]
        if not still_off(o) and any(x["code"] != "not_retimed" for x in says):
            faults.append(f'{k}: state {o["state"]}, but a sentence names it: {[x["code"] for x in says]}')
        if still_off(o) and not says:
            faults.append(f'{k}: state {o["state"]}, but no sentence names it')
        if took(o) and any("left as they are" in report.sub_line(x, report.tense_of(rec)) for x in says if x["code"] != "not_retimed"):
            faults.append(f"{k}: its lines moved, but a sentence says they were left as they are")
    if faults:
        raise subsync.Broken("the timing outcome breaks a rule: " + "; ".join(faults))


def flashes(x):
    """Whether the alert sentence x is about lines that flash by too fast to read, and not about their times."""
    return x["code"] == "check_flash" or x["code"] == "sidecar_left" and x.get("action") == "lengthen"


# The alert builder. It words the subtiming alert from the outcomes, see report.SUB_LINES.
SEEN_TEXT = ("offsets", "unfixed", "unconfirmed", "would")   # the facts that word a subtitle as a whole


def off_facts(k, r, o, dur):
    """The facts of the "off" sentence of subtitle k, see report.off_line(), from its outcome o. r is its sync entry and
    dur {"duration": the file's seconds} or {}. A plan that took effect, or stretches that the word check does not read
    as a whole, give what moved, "edges", the stretches that count their lines, and "still", the others. Else the word
    check's facts word it. "edges" holds the stretches as the outcome has them."""
    t, laid = r.get("timing") or {}, (r.get("layout") or {}).get("verdict") == "fit"
    base = {"code": "off", "track": k, "ref": None if laid else r.get("reference"), "why": t.get("why") or r.get("why")}
    seen = o.get("seen") or {}
    if took(o) or not set(SEEN_TEXT) & set(seen):
        return {**base, "moved": o["moved"], **({"fix": o["fix"], **dur} if took(o) and o["fix"] else {}),
                "edges": [x for x in o["off"] if x["lines"]], "still": [[x["at"], x["late"]] for x in o["off"] if not x["lines"]],
                **({"unchecked": True} if took(o) and not o["checked"] else {})}
    return {**base, "offsets": seen.get("offsets"), "unfixed": seen.get("unfixed"), **({"layout": True} if seen.get("layout") else {}),
            **({"would": seen["would"], **dur} if seen.get("would") else {}), **({"unconfirmed": seen["unconfirmed"], **dur} if seen.get("unconfirmed") else {}),
            **({"unheard": True} if seen.get("unheard") else {})}


def alert_lines(rec, sync, flags_off, plan):
    """The sentences of the subtiming alert of the run of rec, from the outcome of each subtitle in rec["subjudge"]. sync
    holds the word check and the reference timing, flags_off is False at SUBTITLES check, and plan is what --apply
    would do in a dry run, see subtitles.remux_block(). Only an outcome that still_off() passes speaks.

    A plan that failed leaves its words to the not_retimed sentence, or to the sidecar_left sentence of a sidecar.
    Live captions get the live sentence. A fix that SUBTITLES check left out gets check_times. Every other one gets the
    off sentence. A dry run's planned moves that wait for --apply get not_retimed, unless their own sentence says so."""
    rm, outs = rec.get("subremux") or {}, rec.get("subjudge") or {}
    dur = {"duration": rec["file_duration"]} if rec.get("file_duration") else {}
    redo = set() if rm.get("done") else {*(rm.get("fixed") or []), *(rm.get("ended") or []), *(rm.get("timed") or [])}
    live, off, check, said = [], [], [], set()
    for k, o in sorted(outs.items()):
        r = sync.get(k) or {}
        if not still_off(o) or o["written"] == "failed":
            continue
        if "live" in o:
            said.add(k)
            f = o["live"]
            live.append({"code": "live", "track": k, "lag": f["lag"], "moved": f["moved"], "cues": f["cues"], "left": f["left"], "flags_off": flags_off,
                         **({"block": plan.get("block")} if k in redo else {}), **({"hearing_stopped": True} if f.get("hearing_stopped") else {})})
        elif o["written"] == "check":
            check.append({"code": "check_times", "track": k, "why": (r.get("timing") or {}).get("why"), "fix": o["fix"], **dur,
                          **({"kept": r["timing"]["kept"]} if (r.get("timing") or {}).get("kept") else {})})
        else:
            off.append(off_facts(k, r, o, dur))
    nr = [{"code": "not_retimed", "tracks": sorted(redo - said), "result": rm.get("result"), "block": plan.get("block")}] if redo - said else []
    flash = [] if flags_off else [{"code": "check_flash", "track": p, "median": f["median"]} for p, f in sorted((rec.get("flash") or {}).items())]
    return live + off + nr + check + flash
