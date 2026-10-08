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
"""The whole-file timing, see arr_media_guard/align.py. Every track here is made up: random made-up words, one line
every few seconds, and Whisper's words heard exactly at the speech. conftest.py turns on the safety rules, so each
run() also passes align.check_plan().

Run: pytest tests/test_align.py
"""
import random
from fractions import Fraction

import pytest

from arr_media_guard import align, subsync

T, LEAD = align.T, align.LEAD
VOCAB = [f"word{k}" for k in range(400)]


def track(n=120, late=lambda i: 0.0, gap=lambda i: 3.0, seed=3):
    """(cues, windows, speech): n lines of 4 made-up words. Line i's speech starts at speech[i], and the line starts
    late(i) seconds after it. Whisper hears each word 0.3 s after the one before, in one window of the whole file."""
    rnd = random.Random(seed)
    cues, heard, speech, t = [], [], [], 5.0
    for i in range(n):
        ws = rnd.sample(VOCAB, 4)
        cues.append((t + late(i), t + late(i) + 2.0, " ".join(ws)))
        heard += [[round(t + 0.3 * k, 2), w] for k, w in enumerate(ws)]
        speech.append(t)
        t += gap(i)
    return cues, [{"at": 0.0, "secs": t + 10.0, "words": heard}], speech


def starts(cues, got):
    return [got["moves"][i]["start"] if i in got["moves"] else c[0] for i, c in enumerate(cues)]


def moved(got):
    """The lines whose start moves. The line before a moved line may change only its end, see align.ends()."""
    return sorted(i for i, m in got["moves"].items() if m["why"] != "end")


def test_a_track_in_time_stays_and_is_judged_in_time():
    cues, ws, _ = track()
    got = align.run(cues, ws)
    assert len(got["anchors"]) == len(cues) and got["moves"] == {} and not got["live"]
    assert got["judge"]["judged"] and got["judge"]["off"] == []


def test_a_jump_moves_the_lines_after_it_onto_their_speech():
    """Lines 60 on sit 2 s late. They move back onto their speech, the lines before stay, and nothing is left off."""
    cues, ws, speech = track(late=lambda i: 2.0 if i >= 60 else 0.0)
    got = align.run(cues, ws)
    new = starts(cues, got)
    assert moved(got) == list(range(60, len(cues))) and all(abs(new[i] - (speech[i] + LEAD)) < 0.01 for i in moved(got))
    assert all(got["moves"][i]["why"] == "curve" and got["moves"][i]["segs"] for i in moved(got))
    rec = got["curve"][got["moves"][60]["segs"][0]]
    assert rec["moves"] and rec["why"] == "whole" and rec["first"] == 60
    assert got["judge"]["off"] == []


def test_a_drift_fits_its_frame_rate_ratio():
    """A track timed for 25 fps on a 24 fps video. One segment of ratio 25/24 moves every line."""
    cues, ws, speech = track(late=lambda i: 0.0)
    cues = [(s * 25 / 24, e * 25 / 24, x) for s, e, x in cues]
    got = align.run(cues, ws)
    assert got["curve"][0]["rate"] == "25/24" and got["curve"][0]["moves"]
    assert all(abs(s - (t + LEAD)) < 0.05 for s, t in zip(starts(cues, got), speech))


def test_live_captions_move_line_by_line():
    """Each line sits 1 to 5 s late by its own amount. The track reads as live captions, and each line moves onto its
    own speech."""
    rnd = random.Random(5)
    lates = [rnd.uniform(1.0, 5.0) for _ in range(120)]
    cues, ws, speech = track(late=lates.__getitem__, gap=lambda i: 4.5)
    got = align.run(cues, ws)
    assert got["live"] and got["scatter"] >= T["live scatter"]
    assert all(abs(s - (t + LEAD)) < 0.01 for s, t in zip(starts(cues, got), speech))
    assert {m["why"] for m in got["moves"].values()} == {"live"}


def test_too_few_anchors_move_nothing_and_judge_nothing():
    cues, ws, _ = track(n=8, late=lambda i: 3.0)
    got = align.run(cues, ws)
    assert got["moves"] == {} and got["curve"] == [] and not got["judge"]["judged"]


def test_a_block_too_far_off_stays_and_is_judged_off():
    """Lines 50 to 69 sit 12 s late, over "block most". With 20 anchors the block stays, and the judge names it."""
    cues, ws, _ = track(late=lambda i: 12.0 if 50 <= i < 70 else 0.0, gap=lambda i: 18.0 if i == 69 else 3.0)
    got = align.run(cues, ws)
    assert got["moves"] == {}
    assert any(r["first"] == 50 and r["why"] == "too far" for r in got["curve"])
    (off,) = got["judge"]["off"]
    assert (off["first"], off["last"], off["lines"]) == (50, 69, 20) and abs(off["late"] - 12.0) < 0.05


@pytest.mark.parametrize("onsets", [False, True])
def test_a_small_block_moves_only_when_the_onsets_agree(onsets):
    """Lines 50 to 57 sit 0.6 s late, too little to move on Whisper alone. Speech onsets, where a silence ends, at
    every line's speech agree with the move."""
    rnd = random.Random(7)
    gaps = [rnd.uniform(2.5, 3.5) for _ in range(120)]
    cues, ws, speech = track(late=lambda i: 0.6 if 50 <= i < 58 else 0.0, gap=gaps.__getitem__)
    got = align.run(cues, ws, [(t, 1.0) for t in speech] if onsets else ())
    (rec,) = [r for r in got["curve"] if r["pass"] == "fine" and r["first"] == 50]
    assert rec["moves"] == onsets and rec["why"] == ("onsets" if onsets else "too little")
    assert moved(got) == (list(range(50, 58)) if onsets else [])


def test_an_anchored_line_moves_no_farther_than_its_own_speech():
    """Lines 60 on sit 2 s late, but line 80 sits in time. It moves with its segment only as far as its speech."""
    cues, ws, speech = track(late=lambda i: 2.0 if i >= 60 and i != 80 else 0.0)
    got = align.run(cues, ws)
    assert got["moves"][80]["why"] == "speech" and abs(got["moves"][80]["start"] - (speech[80] + LEAD)) < 0.01


@pytest.mark.parametrize("left, right, got", [(-0.5, -0.7, -0.5), (-0.9, -1.2, -0.8), (0.3, -0.7, None), (-0.5, None, -0.5)])
def test_a_line_with_no_anchor_moves_no_farther_than_the_anchored_lines_around_it(left, right, got):
    """Lines 0 to 2 move -0.8 s with their segment, and only lines 0 and 2 are anchored. Their speech lies left and right
    seconds from their starts. Line 1 then moves the smaller of their moves at most, and stays when one of them moves the
    other way. With right None line 2 is no line, so line 1 sits at the file's end and only line 0 counts."""
    cues = [(10.0, 11.0, ""), (13.0, 14.0, ""), (16.0, 17.0, "")][:3 if right is not None else 2]
    at = {0: 10.0 + left - LEAD, **({2: 16.0 + right - LEAD} if right is not None else {})}
    out = align.short_of_speech(cues, at, {i: -0.8 for i in range(len(cues))})
    assert out.get(1) == (pytest.approx(got) if got is not None else None), out


@pytest.mark.parametrize("late, why", [(0.0, "between"), (1.5, "between"), (2.5, "curve")])
def test_a_line_with_no_anchor_moves_no_farther_than_an_anchored_line_that_stops_at_its_speech(late, why):
    """Lines 60 on sit 2 s late, but lines 80 and 81 sit late seconds off. Line 81 is a song line, so it has no anchor.
    Line 80 moves onto its own speech, or with the segment when that is the smaller move. Line 81 moves as far."""
    cues, ws, _ = track(late=lambda i: late if i in (80, 81) else 2.0 if i >= 60 else 0.0)
    cues[81] = (cues[81][0], cues[81][1], "♪ " + cues[81][2])
    got = align.run(cues, ws)
    assert [round(got["moves"][i]["start"] - cues[i][0], 3) for i in (80, 81, 82)] == [round(LEAD - min(late, 2.0), 3)] * 2 + [round(LEAD - 2.0, 3)]
    assert got["moves"][81]["why"] == why


def test_a_line_whose_move_would_start_before_the_file_keeps_its_start():
    """The track sits 3 s late, and a sound and a song before its first speech hold no words. They would move with the
    track to 2.5 s and 1.4 s before the file's start, so they keep their starts. Every other line moves onto its
    speech."""
    cues, ws, speech = track(late=lambda i: 3.0)
    cues = [(0.5, 1.0, "[MUSIC]"), (1.6, 2.6, "♪ ♪")] + cues
    got = align.run(cues, ws)
    assert moved(got) == list(range(2, len(cues))) and all(s >= 0 for s in starts(cues, got)), got["moves"].get(0)
    assert all(abs(s - (t + LEAD)) < 0.01 for s, t in zip(starts(cues, got)[2:], speech))


def test_a_line_stops_short_of_a_line_that_stays():
    """Line 60's speech starts 1.2 s after line 59's. Line 59 sits 0.9 s late on its own and stays. Line 60 sits 2 s
    late with the lines after it, and it stops "shown" after line 59 instead of on its speech."""
    cues, ws, speech = track(late=lambda i: 2.0 if i >= 60 else 0.9 if i == 59 else 0.0, gap=lambda i: 1.2 if i == 59 else 3.0)
    got = align.run(cues, ws)
    assert got["moves"][60]["why"] == "order" and abs(got["moves"][60]["start"] - (cues[59][0] + T["shown"])) < 0.01


def test_the_end_before_a_moved_line_keeps_its_gap():
    """A line that ran up to the next one keeps running up to it when the next one moves back."""
    cues, ws, _ = track(late=lambda i: 2.0 if i >= 60 else 0.0)
    cues[59] = (cues[59][0], cues[60][0], cues[59][2])
    got = align.run(cues, ws)
    assert got["moves"][59] == {"start": cues[59][0], "end": got["moves"][60]["start"], "segs": [], "why": "end"}


def test_a_line_before_a_moved_line_keeps_its_length():
    """Lines 60 on sit 2 s late and move. Line 60's speech starts 1.2 s after line 59, which shows 2 s. Line 59 then
    stops subsync.FRAMES before line 60's new start. A line that moves keeps its length."""
    cues, ws, _ = track(late=lambda i: 2.0 if i >= 60 else 0.0, gap=lambda i: 1.2 if i == 59 else 3.0)
    got = align.run(cues, ws)
    new = got["moves"][60]["start"]
    assert got["moves"][59] == {"start": cues[59][0], "end": new - subsync.FRAMES, "segs": [], "why": "end"}, (got["moves"][59], new)
    assert got["moves"][61]["end"] - got["moves"][61]["start"] == pytest.approx(cues[61][1] - cues[61][0])


def test_heard_splits_two_windows_at_the_middle_of_their_overlap():
    ws = [{"at": 0.0, "secs": 10.0, "words": [[1.0, "a"], [8.0, "b"], [9.5, "c"]]},
          {"at": 7.5, "secs": 10.0, "words": [[0.6, "b"], [2.0, "c"], [5.0, "d e"]]}]
    assert align.heard(ws) == [(1.0, "a"), (8.0, "b"), (9.5, "c"), (12.5, "d"), (12.5, "e")]


def test_an_anchor_steps_back_over_the_leading_stopwords():
    """The anchor of "So the word1 word2" is the heard "so", while each word lies under "spoken gap" before the next.
    A pause of "spoken gap" stops it."""
    cues = [(10.0, 12.0, "So the word1 word2"), (20.0, 22.0, "The word3 word4")]
    words = [(9.5, "so"), (9.8, "the"), (10.0, "word1"), (10.3, "word2"), (18.0, "the"), (20.0, "word3"), (20.3, "word4")]
    assert align.anchors(cues, words, subsync.decide.STOPWORDS["eng"]) == {0: 9.5, 1: 20.0}


def test_lyrics_short_lines_and_repeats_never_anchor():
    cues = [(1.0, 3.0, "♪ word1 word2 ♪"), (4.0, 4.01, "word3 word4"), (5.0, 6.0, "word5 word6"), (7.0, 8.0, "word5 word6"),
            (9.0, 10.0, "word7 word8")]
    words = [(t, w) for t, w in zip((1.0, 1.3, 4.0, 4.3, 5.0, 5.3, 7.0, 7.3, 9.0, 9.3), "word1 word2 word3 word4 word5 word6 word5 word6 word7 word8".split())]
    assert align.anchors(cues, words, frozenset()) == {4: 9.0}


@pytest.mark.parametrize("move, rule", [
    ({2: {"start": 10.2, "end": 12.2}}, "order"),
    ({0: {"start": 4.0, "end": 12.0}}, "stacked"),
    ({1: {"start": 9.5, "end": 11.5}}, "speech"),
    ({1: {"start": 10.0, "end": 10.2}}, "shown"),
    ({2: {"start": 20.0, "end": 26.0}}, "ends"),
    ({0: {"start": -0.5, "end": 1.5}}, "start"),
])
def test_check_plan_names_each_broken_rule(move, rule):
    cues = [(5.0, 7.0, "a"), (10.0, 12.0, "b"), (20.0, 22.0, "c")]
    with pytest.raises(subsync.Broken, match=f"rule {rule} "):
        align.check_plan(cues, {1: 10.0}, move, {})


def test_an_old_overlap_within_touch_passes_when_only_the_next_end_changes():
    """Line 0 ran 0.01 s into line 1 and keeps its times. Line 1 keeps its start, and its end gives way to line 2's move."""
    cues = [(154.644, 157.948, "a"), (157.938, 160.11, "b"), (160.11, 161.952, "c")]
    align.check_plan(cues, {2: 159.99 - LEAD}, {1: {"start": 157.938, "end": 159.99}, 2: {"start": 159.99, "end": 161.832}}, {})


def test_check_plan_holds_a_line_to_the_first_later_line_it_did_not_show_over():
    """Line 1 shows inside line 0. Line 2 starts after line 0 ends, and moves under its end."""
    cues = [(5.0, 9.0, "a"), (6.0, 7.0, "b"), (9.5, 11.0, "c")]
    with pytest.raises(subsync.Broken, match="rule stacked "):
        align.check_plan(cues, {}, {2: {"start": 8.0, "end": 9.5}}, {})


def test_only_a_line_that_ran_up_to_the_next_line_grows_to_its_new_start():
    """Line 0 ended 0.05 s before the line after it. When that is line 1, line 0 runs up to line 1's new start, for
    "end hold" at most. When line 1 shows inside line 0, the line after is line 2, and line 0 keeps its length.
    check_plan() allows the growth only in the first case."""
    nxt = [(10.0, 14.0, "a"), (14.05, 15.0, "c")]
    assert align.ends(nxt, {1: 20.0})[0] == pytest.approx(10.0 + T["end hold"])
    inside = [(10.0, 14.0, "a"), (11.0, 12.0, "b"), (14.05, 15.0, "c")]
    assert align.ends(inside, {2: 20.0})[0] == pytest.approx(14.0)
    align.check_plan(nxt, {}, {0: {"start": 10.0, "end": 17.0}, 1: {"start": 20.0, "end": 20.95}}, {})
    with pytest.raises(subsync.Broken, match="rule ends "):
        align.check_plan(inside, {}, {0: {"start": 10.0, "end": 17.0}, 2: {"start": 20.0, "end": 20.95}}, {})


def test_a_long_line_ends_before_the_line_after_the_one_shown_inside_it():
    """The track sits 2 s late. Line 40 shows 4.2 s, and its speech lies only 0.5 s back, so it stops there. Line 41,
    song lyrics with no anchor, shows inside line 40. Line 42 starts 0.3 s after line 40 ends and moves the full 2 s.
    Line 40 then ends before line 42's new start, and no line shows over a later line it did not show over before."""
    rnd = random.Random(5)
    cues, heard, t = [], [], 10.0
    for i in range(80):
        ws, s = rnd.sample(VOCAB, 3), t + 2.0
        e, said = s + 1.5, t
        if i == 40:
            e, said = s + 4.2, s - 0.5
        elif i == 41:
            ws, s, e, said = ["♪", "la", "♪"], cues[40][0] + 1.0, cues[40][0] + 2.0, None
        elif i == 42:
            s = cues[40][1] + 0.3
            e, said = s + 1.5, s - 2.0
        cues.append((s, e, " ".join(ws)))
        heard += [[round(said + 0.3 * k, 2), w] for k, w in enumerate(ws)] if said is not None else []
        t = max(t + 4.0, s + 2.0)
    got = align.run(cues, [{"at": 0.0, "secs": t + 10.0, "words": heard}])
    new = [(got["moves"][i]["start"], got["moves"][i]["end"]) if i in got["moves"] else c[:2] for i, c in enumerate(cues)]
    assert 42 in moved(got) and got["moves"][40]["why"] == "speech" and new[40][1] < new[42][0], (new[40], new[42])
    assert not [(i, k) for i in range(len(cues)) for k in range(i + 1, len(cues))
                if cues[k][0] > cues[i][0] and cues[i][1] <= cues[k][0] and new[i][1] > new[k][0] + 0.01]


def test_the_gate_takes_each_rule_in_turn():
    most = T["block most"]
    assert align.gate(T["whole anchors"], T["move"], 0.5, 0.1, "disagree", most) == (True, "whole")
    assert align.gate(10, 1.5, 1.5, 0.1, "disagree", most) == (False, "onsets disagree")
    assert align.gate(10, most + 1, most + 1, 0.1, "agree", most) == (False, "too far")
    assert align.gate(4, 0.6, 0.6, 0.1, "agree", most) == (True, "onsets")
    assert align.gate(T["alone anchors"], 1.0, T["move alone"], 0.1, "few", most) == (True, "alone")
    assert align.gate(T["mid anchors"], 0.8, T["off"], 0.1, "few", most) == (True, "mid")
    assert align.gate(T["tight anchors"], 2.0, T["tight move"], T["tight spread"], "few", most) == (True, "tight")
    assert align.gate(T["tight anchors"], 2.0, T["tight move"], 0.5, "few", most) == (False, "too little")


# --- each setting at its boundary, with the numbers written here ------------------------------------------------------

@pytest.mark.parametrize("spread, live", [(0.5, False), (0.7, True)])
def test_live_captions_start_at_a_scatter_of_0_6_s(spread, live):
    """Lines sit 3 s late, every other one spread seconds more and the rest spread less. The scatter is spread. At
    0.5 s the curve moves the lines, and at 0.7 s each line moves on its own."""
    cues, ws, _ = track(late=lambda i: 3.0 + (spread if i % 2 else -spread))
    got = align.run(cues, ws)
    assert got["scatter"] == pytest.approx(spread, abs=0.02) and got["live"] is live


@pytest.mark.parametrize("n, moves", [(30, True), (29, False)])
def test_a_segment_of_30_anchors_moves_from_0_5_s_on_whisper_alone(n, moves):
    cues, ws, _ = track(late=lambda i: 0.55 if 40 <= i < 40 + n else 0.0)
    got = align.run(cues, ws)
    assert (moved(got) == list(range(40, 40 + n))) is moves, moved(got)


@pytest.mark.parametrize("nxt, end", [(0.8, 0.8 - subsync.FRAMES), (0.55, 0.5), (0.3, 0.3)])
def test_a_line_shows_0_5_s_at_least_unless_the_next_starts_sooner(nxt, end):
    """Line 1 moves to nxt seconds after line 0 starts. Line 0 stops two frames before it, but shows 0.5 s at least,
    or up to line 1 when that starts sooner."""
    got = align.ends([(0.0, 2.0, ""), (3.0, 5.0, "")], {1: nxt})
    assert got[0] == pytest.approx(end) and got[1] == pytest.approx(nxt + 2.0)


def test_an_outlier_costs_one_second_at_most():
    """Three anchors 4 s off cost 3 at the cap of 1 s each, under the 5 of a change point, so the curve stays one segment.
    With no cap they would cost 12."""
    xs = [10.0 * k for k in range(60)]
    ys = [x + (4.0 if 30 <= k < 33 else 0.0) for k, x in enumerate(xs)]
    assert [s[:2] for s in align.curve(xs, ys, 5.0)] == [(0, 59)]


def test_a_frame_rate_ratio_costs_2_to_enter():
    """A drift of 1001/1000 over 100 s leaves 41 anchors 0.025 s off at ratio 1 on average, about 1 in all, under the cost
    of 2, so the curve keeps ratio 1. Over 3000 s they sit 0.75 s off, and the curve takes the ratio."""
    for span, rate in ((100, Fraction(1)), (3000, Fraction(1001, 1000))):
        xs = [span * k / 40 for k in range(41)]
        (seg,) = align.curve(xs, [x * 1.001 for x in xs], 5.0)
        assert seg[2] == rate, (span, seg)


def test_the_level_is_the_median_off_the_grid():
    xs = [10.0 * k for k in range(9)]
    ys = [x + d for x, d in zip(xs, (0.0, 0.0, 0.03, 0.03, 0.013, 0.0, 0.03, 0.0, 0.03))]
    (seg,) = align.curve(xs, ys, 5.0)
    assert seg[3] == pytest.approx(0.013)


@pytest.mark.parametrize("reach, size, owned", [(14.0, 10, True), (16.0, 10, False), (16.0, 30, True)])
def test_an_unanchored_line_moves_with_its_segment_within_15_s(reach, size, owned):
    """Line 1 has no anchor. Its anchored neighbours lie reach seconds away. A segment under 30 anchors takes it within
    15 s only."""
    cues = [(0.0, 1.0, ""), (reach, reach + 1.0, ""), (2 * reach, 2 * reach + 1.0, "")]
    assert (1 in align.owners(cues, [0, 2], {0: 0, 2: 0}, [size])) is owned


@pytest.mark.parametrize("new, verdict", [(6, "agree"), (5, "few")])
def test_the_onsets_agree_with_twice_as_many_lines_at_the_new_places(new, verdict):
    """20 lines move 1 s late to early. 3 have an onset where they sit, and new where the move puts them. The onsets agree
    only with twice as many lines at the new places."""
    cues = [(5.0 + 3.7 * i, 6.0 + 3.7 * i, "") for i in range(20)]
    mv = {i: -1.0 for i in range(20)}
    onsets = sorted([(cues[i][0] - LEAD, 1.0) for i in range(3)] + [(cues[i][0] - 1.0 - LEAD, 1.0) for i in range(10, 10 + new)])
    assert align.vote(cues, list(range(20)), mv, onsets, 0.0)[0] == verdict


def test_a_live_line_with_no_anchor_takes_the_smaller_move_around_it():
    cues = [(10.0, 11.0, ""), (12.0, 13.0, ""), (14.0, 15.0, "")]
    assert align.per_line(cues, {0: 8.0 - LEAD, 2: 11.0 - LEAD})[1] == pytest.approx(-2.0)
    assert 1 not in align.per_line(cues, {0: 8.0 - LEAD, 2: 15.0 - LEAD})   # the two move different ways


@pytest.mark.parametrize("want, pile, got", [(9.6, True, {2: 10.5}), (9.4, True, {}), (9.4, False, {2: 10.5})])
def test_a_line_cut_over_1_s_short_stays(want, pile, got):
    """Line 2 moves from 20 s to want, and line 1 at 10 s stays. It stops 0.5 s after line 1, 0.9 or 1.1 s short."""
    cues = [(0.0, 1.0, ""), (10.0, 11.0, ""), (20.0, 21.0, "")]
    assert align.apply_order(cues, {2: want}, pile) == got


@pytest.mark.parametrize("n, off", [(10, True), (9, False)])
def test_the_judge_posts_10_lines_in_a_row_0_75_s_off(n, off):
    """The last n lines sit 0.8 s late, and nothing moves."""
    cues, ws, _ = track(late=lambda i: 0.8 if i >= 120 - n else 0.0)
    j = align.judged(cues, align.anchors(cues, align.heard(ws), frozenset()), {})
    assert [(x["first"], x["last"]) for x in j["off"]] == ([(120 - n, 119)] if off else [])


def test_two_stretches_side_by_side_of_opposite_sign_keep_every_line():
    """Lines from 50 to 72 s sit 1 s late, and from 74 to 96 s 1 s early. The judge names both, each with every one of
    its lines. The line at 73 s between them sits in time, and it may join one of them."""
    cues, ws, speech = track(gap=lambda i: 2.0)
    late = lambda t: 1.0 if 50 <= t <= 72 else -1.0 if 74 <= t <= 96 else 0.0
    cues = [(a + late(t), b + late(t), x) for (a, b, x), t in zip(cues, speech)]
    j = align.judged(cues, align.anchors(cues, align.heard(ws), frozenset()), {})
    want = [[i for i, t in enumerate(speech) if lo <= t <= hi] for lo, hi in ((50, 72), (74, 96))]
    assert len(j["off"]) == 2 and all(x["first"] <= w[0] and w[-1] <= x["last"] and x["lines"] <= len(w) + 1 and x["late"] * d > 0
                                      for x, w, d in zip(j["off"], want, (1, -1))), j["off"]
