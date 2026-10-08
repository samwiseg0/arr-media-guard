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
"""The timing outcome of each subtitle, see arr_media_guard/judge.py.

Run: pytest tests/test_judge.py
"""
import json

import pytest

from arr_media_guard import align, judge

FIX = {"rate": "1/1", "offset": 1.2}
STRETCH = {"at": 823.0, "to": 846.0, "lines": 9, "late": 1.0, "clock": "whole"}
WINDOWED = {"at": 872.0, "to": 882.0, "lines": None, "late": -1.1, "clock": "window"}

# One sound record of each state
SOUND = [judge.outcome("in time", why="every word-check window and the whole-file timing sit in time"),
         judge.outcome("fixed", fix=FIX, written="done"),
         judge.outcome("fixed", moved=59, written="planned"),
         judge.outcome("partly fixed", moved=59, written="done", off=[WINDOWED, STRETCH]),
         judge.outcome("partly fixed", moved=59, written="done", off=[STRETCH], checked=False),
         judge.outcome("off", off=[dict(STRETCH, clock="window", lines=None)], seen={"offsets": [0.0, 1.0, 0.0]}),
         judge.outcome("off", moved=59, written="failed", off=[STRETCH]),
         judge.outcome("off", fix=FIX, written="check", off=[dict(STRETCH, at=0.0, to=1380.0, lines=None, clock="window")], seen={"unfixed": 1.2}),
         judge.outcome("off", off=[dict(STRETCH, lines=40, clock="live")], live={"lag": 2.4, "moved": 0, "cues": 400, "left": 40, "fixed": False}),
         judge.outcome("unknown", checked=False)]


@pytest.mark.parametrize("o", SOUND, ids=[o["state"] for o in SOUND])
def test_a_sound_outcome_breaks_no_rule(o):
    """Each state with the facts it needs passes outcome_faults(), and the record is plain JSON."""
    assert judge.outcome_faults(o) == [], o
    assert json.loads(json.dumps(o)) == o


def test_the_stretches_still_off_come_in_time_order():
    """outcome() sorts the stretches by where they start, as the alert names them."""
    assert [x["at"] for x in judge.outcome("partly fixed", moved=3, written="done", off=[WINDOWED, STRETCH])["off"]] == [823.0, 872.0]


@pytest.mark.parametrize("o, rule", [
    (judge.outcome("late"), "state 'late'"),
    (judge.outcome("fixed", fix=FIX, written="written"), "written 'written'"),
    (judge.outcome("in time", written="done"), "written names a plan"),
    (judge.outcome("fixed", moved=-1, written="done"), "moved -1"),
    (judge.outcome("fixed", fix={"offset": 1.0}, written="done"), "is no fix"),
    (judge.outcome("fixed", fix=FIX), "no move took effect"),
    (judge.outcome("fixed", fix=FIX, written="failed"), "no move took effect"),
    (judge.outcome("fixed", fix=FIX, written="done", off=[STRETCH]), "names a stretch still off"),
    (judge.outcome("partly fixed", moved=5, written="done"), "names no stretch still off"),
    (judge.outcome("in time", fix=FIX, written="done"), "a move took effect"),
    (judge.outcome("in time", off=[STRETCH]), "names a stretch still off"),
    (judge.outcome("off"), "names no stretch still off"),
    (judge.outcome("off", fix=FIX, written="planned", off=[STRETCH]), "a move took effect"),
    (judge.outcome("unknown"), "evidence judged the times"),
    (judge.outcome("unknown", checked=False, off=[STRETCH]), "names a stretch still off"),
    (judge.outcome("in time", checked=False), "no evidence judged the times"),
    (judge.outcome("in time", seen={"unfixed": 1.0}), "seen without a stretch"),
    (judge.outcome("off", off=[STRETCH], seen={"settled": "x"}), "seen holds keys"),
    (judge.outcome("off", off=[STRETCH], live={"scan": {}}), "live holds keys"),
    (judge.outcome("off", off=[dict(STRETCH, clock="ears")]), "clock 'ears'"),
    (judge.outcome("off", off=[dict(STRETCH, to=800.0)]), "runs from 823.0 to 800.0"),
    (judge.outcome("off", off=[dict(STRETCH, lines=0)]), "holds 0 lines"),
    (judge.outcome("off", off=[dict(STRETCH, late=0.0)]), "sits 0.0 s off"),
    (dict(judge.outcome("off", off=[WINDOWED, STRETCH]), off=[WINDOWED, STRETCH]), "not in time order")])
def test_a_broken_outcome_names_its_rule(o, rule):
    """outcome_faults() names each rule a record breaks, so the judge can never hand the alert builder a state that its
    facts contradict: a move without a plan, a fixed subtitle with lines still off, or lines off with no stretch."""
    faults = judge.outcome_faults(o)
    assert any(rule in f for f in faults), (rule, faults)


@pytest.mark.parametrize("o, posts", [(o, o["state"] in ("off", "partly fixed")) for o in SOUND])
def test_only_lines_still_off_post(o, posts):
    """An outcome posts when lines are still off: "off" or "partly fixed". In time, fixed and unknown post nothing."""
    assert judge.still_off(o) is posts


# --- the judge ----------------------------------------------------------------------------------------------------

STARTS = [10.0 + 2.5 * k for k in range(200)]   # a line every 2.5 s from 10 s on
WINDOW = {"at": 290.0, "secs": 20.0, "words": 20, "overlap": 0.9, "offset": -1.1, "cues": 6, "late": -1.1}


def whole(late=lambda k: 0.0, heard=range(200), moves=True):
    """The whole-file timing of 200 made-up lines, one every 2.5 s, as subtitles.sub_whole() records it. Line k sits
    late(k) seconds late, and Whisper hears the lines of heard at their speech. With moves False, the plan moved
    nothing, as when it never ran."""
    cues = [(a + late(k), a + late(k) + 2.0, f"word{k} wordx{k} wordy{k}") for k, a in enumerate(STARTS)]
    words = [[STARTS[k] - align.LEAD + 0.3 * n, w] for k in heard for n, w in enumerate(cues[k][2].split())]
    r = align.run(cues, [{"at": 0.0, "secs": 600.0, "words": words}])
    at = r.pop("anchors")
    return dict(r, moves=r["moves"] if moves else {}, anchors=len(at), lines=len(cues), heard=[[0.0, 600.0]], before=align.judged(cues, at, {})["off"],
                lag=None), [c[0] for c in cues]


def outcome_of(e, starts, rec=None, timing=None):
    rec = dict(rec or {}, file_duration=600.0, whole={"s1": e}, subcheck={"s1": {"verdict": "match", "windows": [], "timing": timing or {"fix": None, "why": "x"}}})
    return judge.outcomes(rec, rec["subcheck"], {"s1": sorted(starts)})["s1"]


def test_a_track_in_time_is_in_time():
    e, starts = whole()
    assert outcome_of(e, starts)["state"] == "in time"


def test_a_stretch_too_far_to_move_posts_with_every_line_to_the_end():
    """Lines 150 on sit 12 s late, over "block most", so nothing moves them. The judge names one stretch of 50 lines
    that runs to the last line, as no line after it was heard in time."""
    e, starts = whole(lambda k: 12.0 if k >= 150 else 0.0)
    o = outcome_of(e, starts)
    assert o["state"] == "off" and [(x["lines"], x["late"], x.get("first"), x.get("last")) for x in o["off"]] == [(50, 12.0, None, True)], o
    assert (o["off"][0]["at"], o["off"][0]["to"]) == (starts[150], starts[199]), o


def test_a_stretch_whose_lines_were_not_heard_still_runs_to_the_first_line():
    """Lines 0 to 29 sit 12 s late, and Whisper heard none of lines 0 to 4. The stretch starts at the first anchored line,
    and it holds the subtitle's first line, as no line before it was heard in time."""
    e, starts = whole(lambda k: 12.0 if k < 30 else 0.0, heard=range(5, 200))
    (x,) = outcome_of(e, starts)["off"]
    assert (x["lines"], x.get("first"), x.get("last")) == (30, True, None), x


@pytest.mark.parametrize("subremux, state", [({"done": True, "timed": ["s1"]}, "fixed"), ({"done": False, "timed": ["s1"]}, "off"), ({}, "off")])
def test_moved_lines_are_judged_where_the_plan_puts_them(subremux, state):
    """Lines 100 to 149 sit 2 s late, and the timing moves them. Written, they are fixed. A failed write leaves the
    stretch the timing found before the moves. A plan the remux never took, as for a WebVTT track, leaves it too."""
    e, starts = whole(lambda k: 2.0 if 100 <= k < 150 else 0.0)
    o = outcome_of(e, starts, {"subremux": subremux})
    assert o["state"] == state and o["moved"] == (50 if subremux else 0), o
    if state == "off":
        assert [(x["lines"], x["late"], x["clock"]) for x in o["off"]] == [(50, 2.0, "whole")], o


def test_a_track_the_timing_did_not_judge_alerts_with_the_word_check_s_finding():
    """Whisper heard 10 lines, under "judge anchors". With no finding of the word check the times are unknown. With a fix
    the timing did not confirm, the track is off as the word check found it."""
    e, starts = whole(heard=range(10))
    assert outcome_of(e, starts)["state"] == "unknown"
    o = outcome_of(e, starts, timing={"fix": None, "unconfirmed": FIX, "unheard": True, "why": "x"})
    assert (o["state"], o["off"][0]["late"], o["seen"]) == ("off", 1.2, {"unconfirmed": FIX, "unheard": True}), o


@pytest.mark.parametrize("subremux, apply, flags_off, written, state", [
    ({"done": True, "fixed": ["s1"]}, True, True, "done", "fixed"),
    ({"done": False, "fixed": ["s1"]}, False, True, "planned", "fixed"),
    ({"done": False, "fixed": ["s1"]}, True, True, "failed", "off"),
    ({}, True, False, "check", "off")])
def test_a_plan_takes_effect_only_when_written_or_planned(subremux, apply, flags_off, written, state):
    """The word check fixed the track by 1.2 s, and its windows sat 1.2 s late. A written or planned fix leaves them in
    time. A failed write, or SUBTITLES check, leaves the plan's own stretch off."""
    fix = {"rate": "1/1", "offset": 1.2}
    windows = [dict(WINDOW, at=a, late=1.2) for a in (100.0, 500.0)]
    rec = {"file_duration": 600.0, "apply": apply, "subremux": subremux,
           "subcheck": {"s1": {"verdict": "match", "windows": windows, "timing": {"fix": fix, "why": "x"}}}}
    o = judge.outcomes(rec, rec["subcheck"], {"s1": STARTS}, flags_off)["s1"]
    assert (o["written"], o["state"], o["fix"]) == (written, state, fix), o
    if state == "off":
        assert o["off"] == [{"at": 0.0, "to": 600.0, "lines": None, "late": 1.2, "clock": "window"}], o


def test_no_evidence_gives_unknown_and_a_repair_first_waits():
    """A match whose word check timed nothing, with no whole-file timing, is unknown. So is a track whose garbled text a
    repair rewrites first. A track that does not match gets no outcome."""
    rec = {"subcheck": {"s1": {"verdict": "match", "windows": [], "timing": {"fix": None, "few": [100.0], "why": "x"}},
                        "s2": {"verdict": "match", "windows": [dict(WINDOW, late=1.2)], "timing": {"fix": None, "unfixed": 1.2, "why": "x"}},
                        "s3": {"verdict": "mismatch", "windows": [], "timing": None}},
           "garbled": {"s2": {"later": True}}}
    got = judge.outcomes(rec, rec["subcheck"], {})
    assert {k: o["state"] for k, o in got.items()} == {"s1": "unknown", "s2": "unknown"}, got


def test_live_captions_take_their_own_facts():
    """Each line sits 2 to 6 s late by its own amount, and each moves onto its own speech."""
    e, starts = whole(lambda k: 2.0 + k % 5)
    e["lag"] = 4.0
    o = outcome_of(e, starts, {"subremux": {"done": True, "timed": ["s1"]}})
    assert e["live"] and o["state"] == "fixed" and o["live"] == {"lag": 4.0, "moved": 200, "cues": 200, "left": 0, "fixed": True}, o


@pytest.mark.parametrize("timing, state", [
    ({"fix": None, "piecewise": True, "offsets": [0.0] * 6 + [-0.7] * 4, "why": "x"}, "off"),   # a jump of 0.7 s, see subsync.stepped()
    ({"fix": None, "piecewise": True, "offsets": [0.0] * 9 + [0.9], "why": "x"}, "in time"),   # one slice alone
    ({"fix": None, "unfixed": 1.3, "why": "x"}, "off"),
    ({"fix": None, "why": "in time"}, "in time")])
def test_a_reference_timing_keeps_its_own_rules(timing, state):
    """A subtitle timed by a reference keeps the rules of its fit. Its steps post when subsync.stepped() shows a jump, and
    an offset no fix lined up posts."""
    r = {"verdict": "fit", "why": "x", "reference": "s1", "timing": timing}
    rec = {"file_duration": 600.0, "subtime": {"s2": r}}
    o = judge.outcomes(rec, {"s2": r}, {})["s2"]
    assert o["state"] == state and (o.get("seen") or {}).get("ref") == ("s1" if state == "off" else None), o


@pytest.mark.parametrize("timing, state", [
    ({"fix": None, "offsets": [-0.15] * 9 + [-1.2], "why": "x"}, "in time"),   # one slice alone, an end song: no "piecewise"
    ({"fix": None, "piecewise": True, "offsets": [-0.15] * 6 + [-1.2] * 4, "why": "x"}, "off")])
def test_the_speech_layout_posts_its_slices_only_when_they_show_a_step(timing, state):
    """subsync.layout_fix() keeps a subtitle's slices as "piecewise" only when two or more of them share another offset.
    One slice alone 1.2 s off, as an end song gives, posts nothing, as in 2.7.0."""
    r = {"verdict": "unknown", "why": "x", "timing": timing, "layout": {"verdict": "fit", "rate": "1/1", "offset": -0.3, "score": 0.8, "why": "y"}}
    rec = {"file_duration": 2600.0, "subtime": {"s3": r}}
    o = judge.outcomes(rec, {"s3": r}, {})["s3"]
    assert o["state"] == state and ("offsets" in (o.get("seen") or {})) == (state == "off"), o


@pytest.mark.parametrize("line", [{"code": "check_flash", "track": "s1", "median": 0.25},
                                  {"code": "sidecar_left", "name": "s1", "why": "x", "left": "x", "action": "lengthen"}])
def test_a_sentence_on_flashing_lines_says_nothing_of_the_times(line):
    """A subtitle in time whose lines flash by gets a sentence on that, at SUBTITLES check or when its fix was left.
    check_outcome() passes it. The same sentence never stands for the times of a subtitle still off."""
    e, starts = whole()
    rec = {"file_duration": 600.0, "whole": {"s1": e}, "subcheck": {"s1": {"verdict": "match", "windows": [], "timing": {"fix": None, "why": "x"}}}}
    rec["subjudge"] = judge.outcomes(rec, rec["subcheck"], {"s1": starts})
    judge.check_outcome(rec, [{"kind": "subtiming", "lines": [line]}])
    rec["subjudge"]["s1"] = judge.outcome("off", off=[STRETCH])
    with pytest.raises(judge.subsync.Broken, match="no sentence names it"):
        judge.check_outcome(rec, [{"kind": "subtiming", "lines": [line]}])


def test_a_failed_sidecar_retime_is_the_timing_sentence_of_a_subtitle_still_off():
    """A sidecar whose retime was left gets the sidecar_left sentence. For a subtitle still off, that sentence names
    its times, so check_outcome() passes it."""
    rec = {"subjudge": {"s1": judge.outcome("off", off=[STRETCH])}}
    judge.check_outcome(rec, [{"kind": "subtiming", "lines": [{"code": "sidecar_left", "name": "s1", "why": "x", "left": "x", "action": "retime"}]}])


def test_a_window_moves_by_the_plan_at_its_own_time():
    """A word-check window 0.8 s late in a file with a fix of 0.1 s. Its lines move by the fix at their own time, to 0.7
    s, under the bar. The nearest cue start, 0.09 s later, only picks the block, so it never adds its distance."""
    rec = {"file_duration": 600.0, "subremux": {"done": True, "fixed": ["s1"]},
           "subcheck": {"s1": {"verdict": "match", "windows": [dict(WINDOW, late=0.8)], "timing": {"fix": {"rate": "1/1", "offset": 0.1}, "why": "x"}}}}
    starts = [a + 0.84 for a in STARTS]   # the window's lines sit 0.09 s before a cue start
    o = judge.outcomes(rec, rec["subcheck"], {"s1": starts})["s1"]
    assert o["state"] == "fixed" and o["fix"], o


def test_a_held_place_counts_as_judged_only_when_heard_again():
    """The import held a timing alert for the lines at 300 to 310 s. The deep analysis judged the track in time, but its
    hearing stopped at 250 s, so the held place was never heard again. A word-check window there judges it, and so
    does a hearing that holds it."""
    e, starts = whole()
    e["heard"] = [[0.0, 250.0]]
    rec = {"whole": {"s1": e}, "subcheck": {"s1": {"verdict": "match", "windows": [], "timing": {"fix": None, "why": "x"}}}}
    assert judge.heard_again(rec, "s1", 300.0, 310.0) is False
    rec["subcheck"]["s1"]["windows"] = [dict(WINDOW, late=0.1)]
    assert judge.heard_again(rec, "s1", 300.0, 310.0)
    e["heard"], rec["subcheck"]["s1"]["windows"] = [[0.0, 600.0]], []
    assert judge.heard_again(rec, "s1", 300.0, 310.0)


def test_steps_are_stretches_off_by_different_amounts():
    """An outcome is in steps when its stretches still off sit apart, or the word check heard different offsets."""
    two = judge.outcome("off", off=[STRETCH, dict(STRETCH, at=900.0, to=920.0, late=-1.1)])
    assert judge.steps(two) and not judge.steps(judge.outcome("off", off=[STRETCH]))
    assert judge.steps(judge.outcome("off", off=[STRETCH], seen={"offsets": [1.0, 0.0]})) and not judge.steps(judge.outcome("in time"))
