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
"""Unit tests for subsync.py, the subtitle match check.

The dialogue is written for these tests from a word list. Whisper is faked: a window hears the words of the lines
spoken in it, at the times the audio holds them. A line is spoken LEAD seconds after its cue starts in a right track.

Run: pytest tests/test_arr_subsync.py
"""
import json
import math
import os
import random
import sys
from fractions import Fraction

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arr_media_guard import remux, subsync as s  # noqa: E402

NAMES = ["mira", "tobin", "juna", "pell"]   # the names both scripts of one show share
WORDS = ("garden window bicycle pancake lantern river mountain rocket pillow marble ladder violin carpet thunder biscuit "
         "helmet puzzle orange shovel candle blanket kitten meadow pencil trumpet wagon basket feather hammer island "
         "jacket kettle lemon mirror noodle pepper quilt saddle tunnel umbrella velvet walnut yogurt zipper anchor "
         "button castle dragon engine forest glacier harbor igloo jungle koala lizard magnet nectar oyster parrot "
         "raccoon spinach tomato unicorn vulture whistle yarn acorn bamboo cactus donkey eclipse falcon goblin hedge "
         "iceberg jigsaw kayak lobster mango nugget otter penguin quiver rhubarb sparrow tulip urchin volcano waffle "
         "admire borrow carry dance escape follow gather hurry invent juggle knock listen measure notice offer paint "
         "quarrel repair search travel unpack visit wander yawn climb dream float giggle hide imagine jump kick "
         "laugh mumble nibble open pretend question rescue shout tickle whisper").split()
STOP = "the a to and you it is of that we".split()
LEAD = 0.05      # seconds from a cue's start to its first heard word on a right track, the median CUE_LEAD pins
STEP = 0.3       # seconds between two spoken words
FIRST, GAP, SHOW, LINES = 60.0, 2.5, 2.2, 400   # the first cue, the cue pitch, how long a cue shows, the cue count
DURATION = FIRST + GAP * LINES + 60


def script(seed):
    """LINES lines of dialogue: a name, then content words with a stopword between some of them."""
    r, out = random.Random(seed), []
    for _ in range(LINES):
        ws = [r.choice(NAMES)]
        for _ in range(r.randint(3, 5)):
            ws += ([r.choice(STOP)] if r.random() < 0.4 else []) + [r.choice(WORDS)]
        out.append(" ".join(ws).capitalize() + ".")
    return out


RIGHT = script(1)
OTHER = script(2)   # another episode of the same show: the same names, other lines


def cues(lines, rate=1, offset=0.0, where=lambda i: 0.0):
    """The cues of lines as a track holds them: cue i starts at FIRST + GAP * i on the audio, moved to rate * t +
    offset. where(i) adds seconds to one cue, for a mistimed cue or a second cut."""
    out = []
    for i, text in enumerate(lines):
        t = FIRST + GAP * i + where(i)
        out.append((float(rate) * t + offset, float(rate) * (t + SHOW) + offset, text))
    return out


def heard(starts, lines=RIGHT, secs=s.WINDOW, keep=lambda i: True, say=lambda i, k, w: w):
    """What Whisper hears in each window: every word of lines spoken in it, as [seconds from the window start, word].
    Line i is spoken from FIRST + GAP * i + LEAD on the audio. keep(i) drops a line, for a window with little speech.
    say(i, k, w) is what Whisper makes of word k of line i."""
    out = []
    for a in starts:
        ws = []
        for i, text in enumerate(lines):
            if keep(i):
                for k, w in enumerate(text.rstrip(".").split()):
                    t = FIRST + GAP * i + LEAD + STEP * k
                    if a <= t < a + secs:
                        ws.append([round(t - a, 2), say(i, k, w)])
        out.append({"at": a, "words": ws})
    return out


EARLY, MIDDLE, LATE = 150.0, 520.0, 950.0   # the windows the tests hear: near the start, in the middle, near the end


def run(track, starts=(EARLY, LATE), **kw):
    return s.check(heard(starts, **kw), track, "eng", DURATION)


def back(track, fix):
    """The largest distance, in seconds, of a fixed cue start from where the right track starts it."""
    return max(abs(max(0, s.moved(round(b * 1000), fix)) / 1000 - a) for (a, _, _), (b, _, _) in zip(cues(RIGHT), track))


def test_a_right_track_matches_and_is_in_time():
    r = run(cues(RIGHT))
    assert r["verdict"] == "match" and all(w["overlap"] >= 0.9 for w in r["windows"]), r
    assert r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r["timing"]


def test_a_wrong_track_does_not_match():
    r = run(cues(OTHER))
    assert r["verdict"] == "mismatch" and all(w["overlap"] <= s.MISMATCH for w in r["windows"]), r


def test_a_wrong_track_of_the_same_show_with_shared_names_does_not_match():
    """Near miss: every line of both scripts starts with one of four names, so each window shares names with the wrong
    track. The names alone stay under MISMATCH."""
    named = sum(w.lower() in NAMES for x in heard([EARLY, LATE]) for _, w in x["words"])
    assert named >= 8   # the windows hear the shared names
    assert run(cues(OTHER))["verdict"] == "mismatch"


def test_a_right_track_two_seconds_late_gets_its_old_times_back():
    """The fix keeps CUE_LEAD after the first word, as the right track had it, so the cues come back where they were."""
    track = cues(RIGHT, offset=2.0)
    r = run(track)
    fix = r["timing"]["fix"]
    assert r["verdict"] == "match" and fix["rate"] == "1/1" and abs(fix["offset"] - 2.0) < 0.05 and back(track, fix) < 0.05, r["timing"]


def test_a_ratio_waits_for_a_middle_window_then_fixes():
    """A PAL speed-up: every cue time is 25/23.976 of its time on the audio. Two windows cannot tell it from a cut
    between them, so the fix waits for a middle window. With it, the cues come back where they were."""
    rate = Fraction(25025, 24000)
    track = cues(RIGHT, rate=rate)
    two = run(track)["timing"]
    assert two["fix"] is None and Fraction(two["confirm"]["rate"]) == rate, two
    fix = run(track, starts=(EARLY, MIDDLE, LATE))["timing"]["fix"]
    assert Fraction(fix["rate"]) == rate and back(track, fix) < 0.05, fix


def test_a_right_track_timed_for_24_fps_gets_the_ratio_1001():
    """24 against 23.976 over this file leaves the last cue 1.3 s late. The error at the file's end decides, so the
    track is not in time, and the middle window confirms the ratio."""
    rate = Fraction(1001, 1000)
    track = cues(RIGHT, rate=rate)
    fix = run(track, starts=(EARLY, MIDDLE, LATE))["timing"]["fix"]
    assert Fraction(fix["rate"]) == rate and back(track, fix) < 0.05, fix


def test_a_cut_with_two_offsets_alerts_and_keeps_its_times():
    """The late half of the track is 8 seconds late, a scene the audio lacks. No frame-rate ratio explains it."""
    r = run(cues(RIGHT, where=lambda i: 8.0 if FIRST + GAP * i > DURATION / 2 else 0.0))
    assert r["verdict"] == "match" and r["timing"]["fix"] is None and r["timing"]["piecewise"], r["timing"]


@pytest.mark.parametrize("cut", [1.4, 2.1])
def test_a_small_cut_between_the_windows_is_never_in_time(cut):
    """A cut of 1.4 to 2.1 s leaves the cues at the file's end more than MIN_SHIFT off. It alerts and changes nothing."""
    r = run(cues(RIGHT, where=lambda i: cut if FIRST + GAP * i > DURATION / 2 else 0.0), starts=(EARLY, MIDDLE, LATE))
    assert r["timing"]["fix"] is None and r["timing"].get("piecewise"), r["timing"]


def test_a_cut_that_looks_like_25_24_is_caught_by_the_middle_window():
    """Windows 720 s apart and a 30 s cut between them: the line through them has the slope of 25/24. The middle window
    before the cut lies far off that line, so no ratio fits and the times stay."""
    track = cues(RIGHT, where=lambda i: 30.0 if FIRST + GAP * i > 560 else 0.0)
    two = run(track, starts=(EARLY, 870.0))["timing"]
    assert two["fix"] is None and two.get("confirm"), two
    three = run(track, starts=(EARLY, 400.0, 870.0))["timing"]
    assert three["fix"] is None and three["piecewise"], three


def test_too_few_heard_words_give_unknown():
    """A window of music: one line in five holds speech."""
    r = run(cues(OTHER), keep=lambda i: i % 5 == 0)
    assert r["verdict"] == "unknown" and "under" in r["why"], r


def test_a_track_with_no_cues_gives_unknown():
    """A track the read could not take gives no verdict, never a mismatch."""
    assert run([])["verdict"] == "unknown"
    assert run(cues(RIGHT)[:s.MIN_TRACK_CUES - 1])["verdict"] == "unknown"


def test_both_windows_must_agree():
    """The early half is the right episode and the late half another: no verdict."""
    mixed = cues(RIGHT)[:LINES // 2] + cues(OTHER)[LINES // 2:]
    r = run(mixed)
    assert [w["overlap"] >= s.MATCH for w in r["windows"]] == [True, False] and r["verdict"] == "unknown", r


def edge(matched, total, n=2):
    """n windows of total heard content words, the first matched of them in the cues and in order, the rest words no
    cue holds. The cues hold one content word each."""
    track = [(10.0 * i, 10.0 * i + 2, f"cueword{i}") for i in range(120)]
    out = []
    for k in range(n):
        a = 200.0 + 600 * k
        base = int(a // 10)
        ws = [[10.0 * j + 0.1, f"cueword{base + j}"] for j in range(matched)] + [[10.0 * j + 5, f"stray{k}x{j}"] for j in range(total - matched)]
        out.append({"at": a, "words": sorted([[t - a, w] for t, w in ws])})
    return s.check(out, track, "eng", 1200)


def test_the_match_and_mismatch_edges():
    assert edge(5, 10)["verdict"] == "match" and edge(5, 10)["windows"][0]["overlap"] == s.MATCH
    assert edge(4, 10)["verdict"] == "unknown"
    assert edge(3, 10)["verdict"] == "mismatch" and edge(3, 10)["windows"][0]["overlap"] == s.MISMATCH
    assert edge(4, 12)["verdict"] == "unknown"   # 33 percent


def test_the_min_words_edge():
    """A window needs 8 heard content words."""
    assert edge(8, 8)["verdict"] == "match"
    assert edge(7, 7)["verdict"] == "unknown"


def test_a_whisper_loop_never_reads_as_a_mismatch():
    """Whisper can repeat one word for a whole segment. A run of one word counts once, so the right track still matches."""
    w = heard([EARLY, LATE])
    for x in w:
        x["words"] += [[s.WINDOW - 2 + k * 0.01, "Hey!"] for k in range(108)]
    r = s.check(w, cues(RIGHT), "eng", DURATION)
    assert r["verdict"] == "match", r
    assert s.said({"at": 0, "words": [[0, "hey"], [0.1, "Hey."], [0.2, "you"], [0.3, "hey"]]}, frozenset()) == [(0, "hey"), (0.2, "you"), (0.3, "hey")]


def in_window(a):
    return sorted(i for i in range(LINES) if a <= FIRST + GAP * i + LEAD < a + s.WINDOW)


def test_a_right_track_with_a_few_mistimed_cues_is_not_moved():
    """Near miss: one cue in each window shows 2 seconds late. The median keeps the track in time."""
    off = {in_window(EARLY)[1], in_window(LATE)[2]}
    r = run(cues(RIGHT, where=lambda i: 2.0 if i in off else 0.0))
    assert r["verdict"] == "match" and r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r["timing"]


def test_a_late_track_with_mistimed_cues_is_not_moved():
    """AGREE of the matched cues must fall in their spans after the fix. Two cues in each window that stay off keep the
    track as it is."""
    off = set(in_window(EARLY)[1:3] + in_window(LATE)[2:4])
    r = run(cues(RIGHT, offset=2.0, where=lambda i: -2.0 if i in off else 0.0))
    assert r["verdict"] == "match" and r["timing"]["fix"] is None and "unfixed" in r["timing"], r["timing"]


@pytest.mark.parametrize("offset, fixed", [(0.7, False), (0.8, True)])
def test_min_shift_decides_between_in_time_and_a_fix(offset, fixed):
    """A right track sits a little off the heard words. Under MIN_SHIFT, 0.75 s, it is in time. Over it, the times get a
    fix, and the fixed cues come back where they were: CUE_LEAD keeps the lead of a right track."""
    track = cues(RIGHT, offset=offset)
    t = run(track)["timing"]
    assert (t["fix"] is not None) == fixed and (fixed or t["why"] == "in time"), t
    assert not fixed or back(track, t["fix"]) < 0.02, t


def test_a_fix_needs_enough_cues_in_each_window():
    """Whisper mishears the first two words of all but two lines of the early window. Two anchors cannot carry a fix."""
    early = in_window(EARLY)
    r = run(cues(RIGHT, offset=2.0), say=lambda i, k, w: "zzz" if k <= 2 and i in early[2:] else w)
    first = [w for w in r["windows"] if w["at"] == EARLY][0]
    assert first["words"] >= s.MIN_WORDS and r["verdict"] == "match", r
    assert r["timing"]["fix"] is None and "under" in r["timing"]["why"], r["timing"]


def test_the_search_covers_the_whole_cue_list():
    """A track 40 seconds late still matches: the search takes offsets far past PAD."""
    r = run(cues(RIGHT, offset=40.0))
    assert r["verdict"] == "match" and abs(r["timing"]["fix"]["offset"] - 40) < 0.05, r


def test_the_search_compares_more_offsets_than_the_one_with_the_most_words():
    """A scene 100 s later holds the window's words three times each, out of order in groups of three. That offset holds
    the most shared words, but only a third of them in order. The search also compares the next offsets, so it finds
    the words in order where they are."""
    ws = WORDS[:12]
    place = lambda k: 3 * (k // 3) + 2 - k % 3   # the order of the words in the later scene
    track = sorted([(500 + 0.1 * k, 500.02 + 0.1 * k, w) for k, w in enumerate(ws)]
                   + [(600 + 0.1 * place(k) + 0.001 * n, 600.02 + 0.1 * place(k) + 0.001 * n, w) for k, w in enumerate(ws) for n in range(3)])
    fl = s.flat(track)
    index = {}
    for p, x in enumerate(fl):
        index.setdefault(x[2], []).append(p)
    got = s.match_window([(500 + 0.1 * k, w) for k, w in enumerate(ws)], fl, index)
    assert got["overlap"] == 1.0 and abs(got["offset"]) < 0.1, got


def test_a_plain_offset_wins_in_a_short_file():
    """In a short file 1001/1000 fits beside a plain 2 s offset, here even a little better. The plain offset wins, so
    the shift is fixed at once, with no middle window."""
    track = cues(RIGHT, offset=2.0, where=lambda i: 0.18 if FIRST + GAP * i > 250 else 0.0)
    got = s.check(heard([100.0, 400.0]), track, "eng", 500)["timing"]
    assert got["fix"] and got["fix"]["rate"] == "1/1", got


def test_two_ratios_that_both_fit_give_no_fix():
    """A track halfway between 25/23.976 and 25/24 in a short file: both ratios fit, and they move the end of the file
    apart by more than TOLERANCE."""
    track = cues(RIGHT, rate=(Fraction(25025, 24000) + Fraction(25, 24)) / 2)
    got = s.check(heard([100.0, 350.0, 600.0]), track, "eng", 700)["timing"]
    assert got["fix"] is None and "not certain" in got["why"], got


def test_a_right_track_whose_windows_differ_a_little_is_in_time():
    """Near miss: the late half shows 0.5 s later than the early half, as a right track can. The cues at the file's
    end stay under MIN_SHIFT, so the track is in time with no alert and no 1001/1000 fix."""
    r = run(cues(RIGHT, where=lambda i: 0.5 if FIRST + GAP * i > DURATION / 2 else 0.0))
    assert r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r["timing"]


def test_a_ratio_near_1_in_a_short_file_is_in_time():
    """At 1001/1000 a file of 700 seconds drifts 0.7 s, under MIN_SHIFT, so the times stay."""
    got = s.check(heard([EARLY, 400.0, 600.0]), cues(RIGHT, rate=Fraction(1001, 1000), offset=-0.2), "eng", 700)
    assert got["timing"]["fix"] is None and got["timing"]["why"] == "in time", got["timing"]


def test_the_windows_lie_near_the_ends_away_from_the_edge():
    """Dense cues at the very start and end are the intro and the credits. The windows stay inside EDGE, and near the
    ends, so the line through them covers most of the file."""
    dense = [(t, t + 1, "garden window bicycle pancake") for t in range(0, 40)] + cues(RIGHT)
    got = s.windows(dense, DURATION)
    assert len(got) == 2 and got[0] >= s.EDGE * DURATION and got[1] <= (1 - s.EDGE) * DURATION - s.WINDOW
    assert got[0] <= 0.25 * DURATION - s.WINDOW and got[1] >= 0.75 * DURATION


def test_the_middle_window_lies_near_the_centre_between_the_others():
    """From 40 to 60 percent of the span between the first and the last window, in the audio. In cue time, the part is
    where the fix to confirm puts the cues of that audio."""
    rate = Fraction(25025, 24000)
    confirm = {"rate": "25025/24000", "offset": 2.0, "ends": [155.0, 955.0]}
    (lo, hi), = s.middle(confirm, DURATION)
    assert [(x * DURATION - 2.0) / float(rate) for x in (lo, hi)] == pytest.approx([475.0, 635.0])
    mid = s.windows(cues(RIGHT, rate=rate, offset=2.0), DURATION, parts=s.middle(confirm, DURATION))
    assert 475.0 <= s.moved(mid[0] * 1000, confirm) / 1000 <= 635.0 - s.WINDOW, mid


def test_the_windows_skip_song_lyrics_but_not_a_font_colour():
    """A song holds the densest cues of the early part. Singing is hard to hear, so the window goes elsewhere. A # in a
    font colour tag is no song."""
    track = [c for c in cues(RIGHT) if not 140 <= c[0] <= 190]   # the song replaces the talk there
    song = [(150 + k * 0.5, 150.4 + k * 0.5, "♪ garden window bicycle pancake lantern ♪") for k in range(60)]
    got = s.windows(sorted(song + track), DURATION)
    assert not 140 <= got[0] <= 180, got
    tagged = [(a, b, '<font color="#ffff00">garden window bicycle pancake lantern</font>') for a, b, _ in song]
    got = s.windows(sorted(tagged + track), DURATION)
    assert 140 <= got[0] <= 180, got


def test_the_windows_take_the_language_check_audio_when_it_holds_enough_cues():
    track = cues(RIGHT)
    free = s.windows(track, DURATION)
    kept = s.windows(track, DURATION, have=[(120, 30), (900, 30)])
    assert 120 <= kept[0] <= 120 + 30 - s.WINDOW and 900 <= kept[1] <= 900 + 30 - s.WINDOW and kept != free


def test_words_drop_tags_sounds_and_stopwords():
    stop = frozenset(STOP)
    assert s.words("<i>[music] Mira, don’t open the WINDOW!</i> {\\an8}(laughs)", stop) == ["mira", "dont", "open", "window"]


def test_moved_inverts_the_map():
    fix = {"rate": "25025/24000", "offset": 1.5}
    t = 600.0
    assert s.moved(round((float(Fraction(fix["rate"])) * t + 1.5) * 1000), fix) == 600000


@pytest.mark.parametrize("lang", ["jpn", "chi"])
def test_text_with_no_spaces_gives_unknown(lang):
    """Japanese and Chinese write no spaces, so a cue reads as one long word. The check names nothing."""
    track = [(FIRST + GAP * i, FIRST + GAP * i + SHOW, "今日は天気がいいですね") for i in range(LINES)]
    w = [{"at": a, "words": [[k * 0.3, "今日は天気がいいですね"[k]] for k in range(11)]} for a in (EARLY, LATE)]
    assert s.check(w, track, lang, DURATION)["verdict"] == "unknown"


def test_a_third_window_is_picked_in_the_same_part_away_from_the_first():
    track = cues(RIGHT)
    first = s.windows(track, DURATION)
    more = s.windows(track, DURATION, taken=first)
    assert all(m is not None and abs(m - f) >= s.WINDOW for m, f in zip(more, first))
    assert more[0] <= 0.25 * DURATION and more[1] >= 0.75 * DURATION
    # a part with no other place gives None there, and the other part still answers
    only = [c for c in track if first[0] <= c[0] < first[0] + s.WINDOW] + [c for c in track if c[0] > DURATION / 2]
    got = s.windows(only, DURATION, taken=s.windows(only, DURATION))
    assert got[0] is None and got[1] is not None, got


def test_short_names_the_windows_with_too_few_words():
    w = heard([EARLY, LATE], keep=lambda i: FIRST + GAP * i > DURATION / 2)
    assert s.short(w, "eng") == [0]
    eight = {"at": 0.0, "words": [[k, w] for k, w in enumerate(WORDS[:8])]}   # 8 words are enough, 7 are not
    assert s.short([eight, dict(eight, words=eight["words"][:7])], "eng") == [1]


def test_a_third_window_gives_the_verdict_a_short_window_could_not():
    """Three windows, one of them short: the two others decide."""
    starts = [EARLY, LATE, EARLY + 100]
    quiet = lambda i: not EARLY - 1 <= FIRST + GAP * i < EARLY + s.WINDOW
    r = s.check(heard(starts, keep=quiet), cues(OTHER), "eng", DURATION)
    assert r["verdict"] == "mismatch" and [w["words"] < s.MIN_WORDS for w in r["windows"]] == [True, False, False], r
    r = s.check(heard(starts[:2], keep=quiet), cues(OTHER), "eng", DURATION)
    assert r["verdict"] == "unknown"
    r = s.check(heard(starts, keep=quiet), cues(RIGHT, offset=2.0), "eng", DURATION)
    assert r["verdict"] == "match" and r["timing"]["fix"]["rate"] == "1/1", r["timing"]


def test_a_window_with_more_anchors_does_not_pull_the_offset():
    """The late cues sit 0.2 s later than the early ones, and the late window holds fewer anchors. The offset lies
    halfway between the two window medians, so the line fits and the track gets its fix."""
    late = in_window(LATE)
    r = run(cues(RIGHT, offset=2.0, where=lambda i: 0.2 if FIRST + GAP * i > DURATION / 2 else 0.0),
            say=lambda i, k, w: "zzz" if k <= 2 and i == late[0] else w)
    fix = r["timing"]["fix"]
    assert fix and 2.05 < fix["offset"] < 2.15, r["timing"]


def test_a_plain_offset_and_a_ratio_that_both_fit_give_no_fix():
    """A track 2 s late, its late half 0.4 s later still. A plain offset and 24/23.976 both fit the two windows, and
    they move the cues at the file's ends apart by more than TOLERANCE. Two windows cannot tell them apart, so the times
    stay and it alerts."""
    r = run(cues(RIGHT, offset=2.0, where=lambda i: 0.4 if FIRST + GAP * i > DURATION / 2 else 0.0))
    assert r["timing"]["fix"] is None and "not certain" in r["timing"]["why"] and "1/1" in r["timing"]["why"], r["timing"]


def test_a_middle_window_far_off_is_never_in_time():
    """The start and the end agree, and the middle sits 1.5 s late, a scene added and removed again. The track is not
    in time, and no ratio explains it."""
    r = run(cues(RIGHT, where=lambda i: 1.5 if 480 <= FIRST + GAP * i < 580 else 0.0), starts=(EARLY, MIDDLE, LATE))
    assert r["timing"]["fix"] is None and r["timing"].get("piecewise"), r["timing"]


def test_only_a_cue_whose_first_word_was_heard_anchors_the_fix():
    """Whisper mishears the first word of every line. A later word of the cue would put each anchor a word late, so the
    track gets no fix rather than one that is off by that word."""
    r = run(cues(RIGHT, offset=2.0), say=lambda i, k, w: "zzz" if k == 0 else w)
    assert r["verdict"] == "match" and r["timing"]["fix"] is None and "under" in r["timing"]["why"], r["timing"]


def test_a_drift_between_close_windows_is_judged_at_the_file_ends():
    """At 1001/1000 two windows near the middle both sit under MIN_SHIFT, but the drift puts the file's end over a
    second late. The error at the ends decides, so the track is not in time."""
    r = run(cues(RIGHT, rate=Fraction(1001, 1000)), starts=(400.0, 700.0))
    assert r["timing"]["why"] != "in time" and r["timing"].get("confirm"), r["timing"]


def test_a_fit_must_hold_at_the_file_ends():
    """Two windows 100 s apart, a 2 s offset, and 0.5 s more between them. A plain offset fits both windows, but the line
    through them runs seconds off at the file's ends, so there is no fix."""
    r = s.check(heard([150.0, 250.0]), cues(RIGHT, offset=2.0, where=lambda i: 0.5 if FIRST + GAP * i > 200 else 0.0), "eng", 450)
    assert r["timing"]["fix"] is None and r["timing"].get("piecewise"), r["timing"]
    r = run(cues(RIGHT, offset=2.0), starts=(150.0, 250.0))   # windows in one half of the file give no fix
    assert r["timing"]["fix"] is None and "one half" in r["timing"]["why"], r["timing"]


@pytest.mark.parametrize("rate", [Fraction(24000, 25025), Fraction(24, 25), Fraction(1000, 1001)])
def test_the_inverse_ratios_get_their_fix(rate):
    """A subtitle timed for a faster video: 23.976/25, 24/25 and 23.976/24. Each cue time is that ratio of its time on
    the audio, and the middle window confirms it."""
    track = cues(RIGHT, rate=rate)
    fix = run(track, starts=(EARLY, MIDDLE, LATE))["timing"]["fix"]
    assert Fraction(fix["rate"]) == rate and back(track, fix) < 0.05, fix


def test_a_cut_of_a_second_never_gets_a_ratio_fix():
    """A cut of 1.02 s between the middle and the late window. 1001/1000 puts all three windows within TOLERANCE of one
    offset, but the middle window sits 0.5 s off the line through the others. So no ratio fits, and it alerts."""
    r = run(cues(RIGHT, where=lambda i: 1.02 if FIRST + GAP * i > 700 else 0.0), starts=(EARLY, MIDDLE, LATE))
    assert r["timing"]["fix"] is None and r["timing"]["piecewise"], r["timing"]


def test_a_looping_phrase_never_reads_as_a_mismatch():
    """Whisper can loop on a short line. "I'm sorry." four times is 8 content words that no cue holds, and its text
    compresses too little to drop. A phrase of up to 4 words said again right after itself counts once. So each window
    hears 2 words, too few for a verdict. After the dialogue of a window, a long loop leaves the right track a match."""
    loop = [{"at": a, "words": [[1 + 0.4 * k, w] for k, w in enumerate(["I'm", "sorry."] * 4)]} for a in (EARLY, LATE)]
    assert s.check(loop, cues(RIGHT), "eng", DURATION)["verdict"] == "unknown"
    w = heard([EARLY, LATE])
    for x in w:
        x["words"] += [[s.WINDOW - 3 + k * 0.05, t] for k in range(40) for t in ("I'm", "sorry.")]
    assert s.check(w, cues(RIGHT), "eng", DURATION)["verdict"] == "match"
    four = {"at": 0, "words": [[k, w] for k, w in enumerate("pancake lantern river rocket".split() * 3)]}
    assert [w for _, w in s.said(four, frozenset())] == ["pancake", "lantern", "river", "rocket"]


def test_a_chant_the_subtitle_holds_still_matches():
    """Near miss: a real chant, a line of two content words said four times, in the audio and in the cues. The heard
    side counts it once and the cues keep it four times. Each heard word still matches, so the right track matches and
    stays in time. The chant's words point to many offsets, and bins of BIN seconds keep them from outvoting the right
    one."""
    lines = [("Tobin rocket! Tobin rocket! Tobin rocket! Tobin rocket!" if i % 3 == 0 else x) for i, x in enumerate(RIGHT)]
    w = heard([EARLY, LATE], lines)
    stop = s.decide.STOPWORDS["eng"]
    assert all(len(s.said(x, stop)) < len(s.words(" ".join(t for _, t in x["words"]), stop)) for x in w)   # the chant counts once
    r = s.check(w, cues(lines), "eng", DURATION)
    assert r["verdict"] == "match" and min(x["overlap"] for x in r["windows"]) >= 0.9 and r["timing"]["why"] == "in time", r


def test_min_track_cues_is_the_floor_of_a_verdict():
    """A track needs 20 cues."""
    track = cues(RIGHT)[:20]
    w = heard([FIRST, FIRST + 25])
    assert s.check(w, track, "eng", 200)["verdict"] == "match"
    assert s.check(w, track[:-1], "eng", 200)["verdict"] == "unknown"


def test_three_anchors_in_each_window_carry_a_fix():
    """MIN_CUES is 3: Whisper mishears the first word of all but three lines of the early window, and the fix still comes."""
    early = in_window(EARLY)
    r = run(cues(RIGHT, offset=2.0), say=lambda i, k, w: "zzz" if k == 0 and i in early[3:] else w)
    assert r["timing"]["fix"] and r["timing"]["fix"]["rate"] == "1/1", r["timing"]


@pytest.mark.parametrize("off, fixed", [(2, True), (3, False)])
def test_agree_is_the_floor_of_a_fix(off, fixed):
    """Ten matched cues, their first words 2 s early: a plain offset fits. off of them hold later words far outside
    their spans. 8 of 10 in their spans is AGREE and gets the fix, 7 of 10 does not."""
    track = [(100.0 + 3 * i if i < 5 else 800.0 + 3 * (i - 5), 0.0, f"word{i} later{i} more{i}") for i in range(10)]
    track = [(a, a + 2.0, t) for a, _, t in track]
    got = []
    for part in (range(5), range(5, 10)):
        pairs = []
        for i in part:
            t = track[i][0] - 2.0 - s.CUE_LEAD
            pairs.append((t, (track[i][0], i, f"word{i}", 0)))
            if i in (1, 6, 3)[:off]:   # two words far past the span pull the cue's median out
                pairs += [(t + 6, (track[i][0], i, f"later{i}", 1)), (t + 6.2, (track[i][0], i, f"more{i}", 2))]
        got.append({"pairs": pairs})
    fix = s.timing(got, track, 1000)
    assert (fix["fix"] is not None) == fixed, fix
    assert fixed or "under 80%" in fix["why"], fix


def test_drift_windows_lie_where_each_far_ratio_puts_the_speech():
    """A window at a cue time of 900 s hears other speech when the track drifts at 23.976/25. The drift windows lie
    where the faster and the slower ratios put the speech of those cues. A hint, a window whose cues sit 3 s after its
    audio at 200 s, moves them by that offset."""
    rate = Fraction(24000, 25025)
    fast, slow = s.drift([900.0])
    assert (fast, slow) == (round(900 * (24 / 25 + 24000 / 25025) / 2, 1), round(900 * (25 / 24 + 25025 / 24000) / 2, 1))
    track = cues(RIGHT, rate=rate)
    r = s.check(heard([EARLY, slow]), track, "eng", DURATION)
    assert r["verdict"] == "match" and min(w["overlap"] for w in r["windows"]) >= 0.9, r
    assert abs(s.drift([900.0], (200.0, 3.0))[1] - (200 + 697 / float(rate))) < 1.0


def test_a_window_with_too_few_matched_cues_gives_way_to_a_longer_one():
    """Whisper mishears the first words of all but two lines of the early window, so it names that window in "few".
    A window of THIRD seconds around it holds more cues whose first words it heard. It stands for the short window,
    and the fix comes."""
    early = in_window(EARLY)
    track = cues(RIGHT, offset=2.0)
    w = heard([EARLY, LATE], say=lambda i, k, w: "zzz" if k <= 2 and i in early[2:] else w)
    assert s.check(w, track, "eng", DURATION)["timing"]["few"] == [EARLY]
    longer = dict(heard([EARLY - 7], secs=s.THIRD)[0], secs=s.THIRD)
    r = s.check(w + [longer], track, "eng", DURATION)
    assert [x["at"] for x in r["windows"]] == [LATE, EARLY - 7] and r["timing"]["fix"]["rate"] == "1/1", r


def test_a_ratio_needs_a_window_near_the_centre():
    """Four windows, two near each end, cannot tell a drift from a cut between them. The fix waits for a window near
    the centre."""
    track = cues(RIGHT, rate=Fraction(25025, 24000))
    r = run(track, starts=(EARLY, EARLY + 60, LATE - 60, LATE))
    assert r["timing"]["fix"] is None and r["timing"]["confirm"], r["timing"]
    assert run(track, starts=(EARLY, 400.0, LATE))["timing"]["confirm"]   # at a third of the span it is no middle window
    assert run(track, starts=(EARLY, EARLY + 60, MIDDLE, LATE - 60, LATE))["timing"]["fix"]


def test_a_window_with_too_few_matched_cues_drops_out_of_the_fit():
    """The middle window holds only two cues whose first words Whisper heard. It names no offset, and the early and the
    late window still carry the fix of a track 2 s late."""
    middle = in_window(MIDDLE)
    r = run(cues(RIGHT, offset=2.0), starts=(EARLY, MIDDLE, LATE), say=lambda i, k, w: "zzz" if k <= 2 and i in middle[2:] else w)
    assert r["timing"]["fix"] and r["timing"]["fix"]["rate"] == "1/1" and "few" not in r["timing"], r["timing"]


def test_jitter_under_min_shift_with_mixed_signs_gets_no_fix():
    """A right track whose cue starts each sit between 0.23 s early and 0.48 s late of the speech, with mixed signs, as a
    person timed them. That is per-cue jitter, and no global offset or ratio fixes it, so the times stay."""
    jitter = (-0.23, 0.48, 0.07, -0.1, 0.3, -0.2, 0.41, 0.0)
    r = run(cues(RIGHT, where=lambda i: jitter[i % len(jitter)]), starts=(EARLY, MIDDLE, LATE))
    assert r["verdict"] == "match" and r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r["timing"]
    rs = s.reference([(a + jitter[k % len(jitter)], b, t) for k, (a, b, t) in enumerate(TALK)], {"s1": TALK}, LONG)
    assert rs["verdict"] == "fit" and rs["timing"]["why"] == "in time", rs


# --- the reference timing of --sub-time (docs/design.md, "Subtitle match") ---------------------------------------

def show(seed, n=400, first=60.0):
    """n cues timed like dialogue from seed: each shows 1 to 4 s, and 0.3 to 6 s pass before the next. Cues then
    cover about 44 percent of the time."""
    r, t, out = random.Random(seed), first, []
    for i in range(n):
        d = r.uniform(1.0, 4.0)
        out.append((round(t, 3), round(t + d, 3), f"line {i} of show {seed}"))
        t += d + r.uniform(0.3, 6.0)
    return out


TALK = show(11)                   # the reference, in audio time
LONG = TALK[-1][1] + 60           # the file's duration


def moved_to(cs, rate=1, offset=0.0, cut=None):
    """cs timed for another video: each time t becomes rate * t + offset, and from cut[0] on cut[1] more seconds."""
    step = lambda t: float(rate) * t + offset + (cut[1] if cut and t >= cut[0] else 0.0)
    return [(round(step(a), 3), round(step(b), 3), x) for a, b, x in cs]


def back_to(cs, fix):
    """The largest distance, in seconds, of a fixed cue start from the reference's start of the same cue."""
    return max(abs(s.moved(a * 1000, fix) / 1000 - b) for (a, _, _), (b, _, _) in zip(cs, TALK))


def test_reference_fixes_a_track_two_seconds_late():
    track = moved_to(TALK, offset=2.0)
    r = s.reference(track, {"s1": TALK}, LONG)
    assert r["verdict"] == "fit" and r["reference"] == "s1" and r["score"] >= 0.9, r
    assert r["timing"]["fix"]["rate"] == "1/1" and back_to(track, r["timing"]["fix"]) <= 0.01, r["timing"]


@pytest.mark.parametrize("rate", [Fraction(25025, 24000), Fraction(24000, 25025)])
def test_reference_fixes_a_track_timed_for_another_frame_rate(rate):
    track = moved_to(TALK, rate=rate, offset=0.4)
    r = s.reference(track, {"s1": TALK}, LONG)
    assert Fraction(r["timing"]["fix"]["rate"]) == rate and back_to(track, r["timing"]["fix"]) <= 0.01, r["timing"]


def test_reference_leaves_a_track_in_time():
    r = s.reference(moved_to(TALK, offset=0.3), {"s1": TALK}, LONG)
    assert r["verdict"] == "fit" and r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r


@pytest.mark.parametrize("offset, fixed", [(0.7, False), (0.8, True)])
def test_reference_needs_min_shift_before_a_fix(offset, fixed):
    r = s.reference(moved_to(TALK, offset=offset), {"s1": TALK}, LONG)
    assert bool(r["timing"]["fix"]) == fixed, r["timing"]


def test_another_episode_is_a_weak_fit_that_only_reports():
    """Another episode's track has its own rhythm of lines and pauses. Some offset always overlaps a little, so the
    lift over chance decides, and it stays far under FIT."""
    r = s.reference(show(12), {"s1": TALK}, LONG)
    assert r["verdict"] == "weak" and r["score"] < s.FIT and r["timing"] is None and "another episode" in r["why"], r


def test_a_track_mostly_of_another_episode_is_still_weak():
    """Near miss: 30 percent of the lines are this file's, the rest another episode's. The lift reaches 0.24, still
    under FIT, so the track only reports."""
    r, other = random.Random(5), show(12)
    mix = sorted([c for c in TALK if r.random() < 0.3] + [c for c in other if r.random() >= 0.3])
    got = s.reference(moved_to(mix, offset=2.0), {"s1": TALK}, LONG)
    assert got["verdict"] == "weak" and 0.2 < got["score"] < s.FIT, got


@pytest.mark.parametrize("step", [1.0, 3.0])
def test_a_cut_gives_parts_at_different_offsets_and_no_fix(step):
    """From the middle on, the track runs step seconds later: another cut. The early parts fit at 2 s, the late ones
    at 2 s plus the step. No ratio explains that, so the times stay."""
    r = s.reference(moved_to(TALK, offset=2.0, cut=(LONG / 2, step)), {"s1": TALK}, LONG)
    t = r["timing"]
    assert r["verdict"] == "fit" and t["fix"] is None and t["piecewise"] and round(max(t["offsets"]) - min(t["offsets"]), 1) == step, t


def test_merged_and_split_lines_still_fit():
    """A translation from another source joins some short lines and splits long ones, and it runs 2 s late. Its starts
    miss many reference starts, but where the cues show still fits."""
    cs = []
    for k, (a, b, x) in enumerate(TALK):
        if cs and k % 3 == 0:
            cs[-1] = (cs[-1][0], b, cs[-1][2] + " " + x)   # joined with the line before
        elif b - a > 3:
            cs += [(a, (a + b) / 2, x), ((a + b) / 2 + 0.05, b, x)]
        else:
            cs.append((a, b, x))
    r = s.reference(moved_to(cs, offset=2.0), {"s1": TALK}, LONG)
    assert r["verdict"] == "fit" and r["timing"]["fix"]["rate"] == "1/1" and abs(r["timing"]["fix"]["offset"] - 2.0) <= 0.01, r


def test_a_pgs_cue_list_gets_its_fix():
    """A picture track gives times only, with no text, and picture_cues() caps each cue at SPAN."""
    track = [(a, min(b, a + s.SPAN), "") for a, b, _ in moved_to(TALK, offset=-2.0)]
    r = s.reference(track, {"s1": TALK}, LONG)
    assert r["timing"]["fix"]["rate"] == "1/1" and abs(r["timing"]["fix"]["offset"] + 2.0) <= 0.01, r


def test_too_few_cues_and_no_reference_give_unknown():
    few = s.reference(TALK[:s.MIN_TRACK_CUES - 1], {"s1": TALK}, LONG)
    assert few["verdict"] == "unknown" and f"under {s.MIN_TRACK_CUES}" in few["why"]
    assert s.reference(TALK[:s.MIN_TRACK_CUES], {"s1": TALK}, LONG)["verdict"] == "fit"
    none = s.reference(TALK, {}, LONG)
    assert none["verdict"] == "unknown" and none["why"].startswith("no track or sidecar of the file matched")
    assert s.reference(TALK, {"s1": TALK[:s.MIN_TRACK_CUES - 1]}, LONG)["verdict"] == "unknown"


def test_the_best_reference_wins():
    """An SDH track and a full track match the audio. The one whose cues this track fits best times it."""
    r = s.reference(moved_to(TALK, offset=2.0), {"s1": show(13), "s2": TALK}, LONG)
    assert r["reference"] == "s2" and r["timing"]["fix"], r


def tenth(c, k, offset=0.0):
    """Whether the cue c starts in the tenth k of TALK's span, from 0, once offset is taken off."""
    lo, hi = TALK[0][0], TALK[-1][0]
    return lo + k * (hi - lo) / 10 <= c[0] - offset < lo + (k + 1) * (hi - lo) / 10


@pytest.mark.parametrize("left", [0, 1, s.SPARSE - 1])
def test_a_slice_the_track_leaves_sparse_is_left_out(left):
    """The track keeps only a few lines in the last tenth of the reference's span, as when the reference times the
    credits and the track does not. That slice is left out, and the fix holds."""
    late = moved_to(TALK, offset=2.0)
    track = [c for c in late if not tenth(c, 9, 2.0)] + [c for c in late if tenth(c, 9, 2.0)][:left]
    assert s.reference(track, {"s1": TALK}, LONG)["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}


@pytest.mark.parametrize("n, fixed", [(5, True), (6, False)])
def test_sparse_is_the_floor_of_a_slice_on_the_track_side(n, fixed):
    """The track holds n lines in the fifth tenth, each 10 s off its reference line. Under SPARSE, 6, lines the slice
    is left out. At 6 it counts, and its offset 10 s off the others gives no fix."""
    late = moved_to(TALK, offset=2.0)
    fifth = [c for c in late if tenth(c, 4, 2.0)]
    track = [c for c in late if not tenth(c, 4, 2.0)] + [(a + 10, b + 10, x) for a, b, x in fifth[:n]]
    r = s.reference(track, {"s1": TALK}, LONG)
    assert (r["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}) if fixed else r["timing"]["offsets"][4] == 12.0, r


@pytest.mark.parametrize("n, fixed", [(5, True), (6, False)])
def test_sparse_is_the_floor_of_a_slice_on_the_reference_side(n, fixed):
    """The reference keeps n lines in the fifth tenth, and the track holds song lines there that the reference leaves
    out, as a Japanese track with captions can. Under SPARSE, 6, reference lines the slice is left out. At 6 it counts,
    and chance pairs give it no clear offset."""
    ref = [c for c in TALK if not tenth(c, 4)] + [c for c in TALK if tenth(c, 4)][:n]
    track = [c for c in moved_to(TALK, offset=2.0) if not tenth(c, 4, 2.0)] + moved_to([c for c in show(12) if tenth(c, 4)], offset=2.0)
    r = s.reference(track, {"s1": ref}, LONG)
    assert (r["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}) if fixed else "slice 5 of 10 has no clear offset" in r["timing"]["why"], r


@pytest.mark.parametrize("drop, fixed", [((6, 7), True), ((6, 7, 8), False), ((1, 2), True), ((1, 2, 3), False)])
def test_each_half_needs_half_slices(drop, fixed):
    """The track holds no line in the tenths drop. Each half of the span keeps HALF slices or more, or no fix."""
    track = [c for c in moved_to(TALK, offset=2.0) if not any(tenth(c, k, 2.0) for k in drop)]
    r = s.reference(track, {"s1": TALK}, LONG)
    assert (r["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}) if fixed else f"under {s.HALF}, so the times stay" in r["timing"]["why"], r


def test_a_slice_with_under_min_cues_pairs_gets_no_fix():
    """The fifth tenth of both sides holds six quiet lines 31 s apart. Two of the track's lines pair with the reference,
    and the rest sit 4 s or more off. The slice's peak lies at the fit, but MIN_CUES pairs carry a slice."""
    lo, hi = TALK[0][0], TALK[-1][0]
    w, quiet = (hi - lo) / 10, lambda t: lo + 4 * (hi - lo) / 10 - 35 <= t < lo + 5 * (hi - lo) / 10 + 35
    six = [(lo + 4.5 * w - 77.5 + 31 * i, lo + 4.5 * w - 75.5 + 31 * i, f"quiet line {i}") for i in range(6)]
    ref = sorted([c for c in TALK if not quiet(c[0])] + six)
    for off, fixed in (((0, 0, 4, 8, 12, 16), False), ((0, 0, 0, 4, 8, 12), True)):
        track = sorted([c for c in moved_to(TALK, offset=2.0) if not quiet(c[0] - 2)] + [(a + 2 + d, b + 2 + d, x) for (a, b, x), d in zip(six, off)])
        r = s.reference(track, {"s1": ref}, LONG)
        assert (r["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}) if fixed else "slice 5 of 10 holds 2 cues near the reference, under 3" in r["timing"]["why"], r


def test_a_slice_searches_within_reach_of_the_fit():
    """A cut of 50 s at the middle, under REACH, shows as slices at two offsets, which alert. A cut of 70 s finds no
    clear offset in the slices after it, so no fix and no offsets to alert with."""
    r = s.reference(moved_to(TALK, offset=2.0, cut=(LONG / 2, 50.0)), {"s1": TALK}, LONG)
    assert r["timing"]["piecewise"] and max(r["timing"]["offsets"]) == 52.0, r["timing"]
    r = s.reference(moved_to(TALK, offset=2.0, cut=(LONG / 2, 70.0)), {"s1": TALK}, LONG)
    assert r["timing"]["fix"] is None and "has no clear offset" in r["timing"]["why"], r["timing"]


def test_a_second_reference_that_cannot_judge_a_slice_keeps_the_times():
    """s2 holds another episode's lines in its fifth tenth. The track's lines there pair with none of them, so s2 finds
    no clear offset in that slice, and the times stay. The reason reads once."""
    s2 = sorted([c for c in TALK if not tenth(c, 4)] + [c for c in show(12) if tenth(c, 4)])
    r = s.reference(moved_to(TALK, offset=2.0), {"s1": TALK, "s2": s2}, LONG)
    assert r["timing"] == {"fix": None, "unfixed": 2.0, "why": "a fix of +2.00 s fits s1, but s2 says slice 5 of 10 has no clear offset "
                           f"within {s.REACH:.0f} s of the fit, so the times stay"}, r["timing"]


def test_a_peak_away_from_the_fit_must_be_clear():
    """Reference starts lie 100 s apart, so each cue start votes once. Five starts point 10 s from around and two
    point 10 s the other way. The peak needs over CLEAR times the votes of the other offset, unless it lies near around."""
    rs, most = [100.0 * i for i in range(10)], int(2 * s.CLEAR)
    ts = lambda n: [100.0 * i + 10 for i in range(n)] + [100.0 * i - 10 for i in range(6, 8)]
    assert s.align(ts(most + 1), rs, 1, 0.0, s.REACH, s.CLEAR) == 10.0
    assert s.align(ts(most), rs, 1, 0.0, s.REACH, s.CLEAR) is None
    assert s.align(ts(most), rs, 1, 8.0, s.REACH, s.CLEAR) is None   # 2 s from around is away
    assert s.align(ts(most), rs, 1, 10.0, s.REACH, s.CLEAR) == 10.0


def test_a_clear_peak_counts_its_own_spread_as_its_own():
    """Four starts point 10 s from around, three point 10.6 s, and two point -10 s. The starts within 1 s of the peak
    are its spread, so the peak is clear. Three starts at 12 s are another offset, and then it is not."""
    rs = [100.0 * i for i in range(10)]
    at = lambda d, ks: [100.0 * i + d for i in ks]
    assert s.align(at(10, range(4)) + at(10.6, range(4, 7)) + at(-10, (7, 8)), rs, 1, 0.0, s.REACH, s.CLEAR) == 10.0
    assert s.align(at(10, range(4)) + at(12, range(4, 7)) + at(-10, (7, 8)), rs, 1, 0.0, s.REACH, s.CLEAR) is None


def test_a_tie_goes_to_the_peak_nearest_the_fit():
    """Three starts point -10 s and three +10 s. With the whole track at +10 s, the peak there wins the tie."""
    rs = [100.0 * i for i in range(10)]
    assert s.align([100.0 * i - 10 for i in (1, 2, 3)] + [100.0 * i + 10 for i in (5, 6, 7)], rs, 1, 10.0, s.REACH, s.CLEAR) == 10.0


def test_a_second_reference_must_agree_at_the_file_end_too():
    """s2 runs at 1001/1000 of s1. A track 2 s late fits s1 with +2 s, and s2 with +2 s and the ratio 1000/1001. The two
    fixes put the cues within 0.01 s of each other at the file's start and over 2 s apart at its end, so no fix."""
    r = s.reference(moved_to(TALK, offset=2.0), {"s1": TALK, "s2": moved_to(TALK, rate=Fraction(1001, 1000))}, LONG)
    assert r["timing"]["fix"] is None and "but s2 needs a fix of +2.00 s and the ratio 1000/1001" in r["timing"]["why"], r["timing"]


def test_a_second_reference_must_agree_at_the_file_start_too():
    """s2 runs at 1001/1000 of s1 and meets it at the file's end. The two fixes of a track 2 s late then agree at the
    end and lie over 2 s apart at the start, so no fix."""
    s2 = moved_to(TALK, rate=Fraction(1001, 1000), offset=-LONG / 1000)
    r = s.reference(moved_to(TALK, offset=2.0), {"s1": TALK, "s2": s2}, LONG)
    assert r["timing"]["fix"] is None and "but s2 needs a fix of" in r["timing"]["why"], r["timing"]


def test_picture_cues_that_flash_get_their_ends_before_the_fit():
    """A PGS or VobSub cue has no text, and a sidecar's cues reach the fit as times only. Both count as visible, so a
    picture track whose cues flash gets its new ends."""
    pics = [(a, a + 0.1, "") for a, _, _ in TALK]
    assert s.unflashed(pics)[0][1] == s.unflashed([(a, b) for a, b, _ in pics])[0][1] == 63.0


def test_flash_cues_get_their_ends_before_the_fit():
    """A reference whose cues each show 0.1 s, as a flash track does, still times a track 2 s late."""
    flashing = [(a, a + 0.1, x) for a, _, x in TALK]
    r = s.reference(moved_to(TALK, offset=2.0), {"s1": flashing}, LONG)
    assert r["timing"]["fix"] and abs(r["timing"]["fix"]["offset"] - 2.0) <= 0.01, r
    assert s.unflashed(flashing)[0][1] > flashing[0][1] and s.unflashed(TALK) == TALK


def test_spans_cap_each_cue_and_merge_overlaps():
    assert s.spans([(0.0, 60.0), (5.0, 7.0), (30.0, 31.0)]) == [[0.0, s.SPAN], [30.0, 31.0]]


def test_lift_is_one_for_a_perfect_fit_and_near_zero_by_chance():
    a = s.spans(TALK)
    assert s.lift(a, a, 1, 0.0) == pytest.approx(1.0) and abs(s.lift(s.spans(show(12)), a, 1, 0.0)) < 0.2


def test_a_right_track_half_a_second_off_still_lifts_well_over_fit():
    """Chance comes from offsets far from the peak. Offsets near it would count the pair's own overlap as chance, and a
    right track 0.5 s off would lift under 0.6."""
    assert s.lift(s.spans([(a + 0.5, b + 0.5) for a, b, _ in TALK]), s.spans(TALK), 1, 0.0) >= 0.6


# --- the speech layout (docs/design.md, "Incorrect subtitle identification") ------------------------------------------

def voice(cs, lead=0.15, hold=0.8):
    """The spans of speech of the lines of cs: each line is spoken from lead after its cue starts to hold before it ends."""
    return s.spans([(a + lead, max(a + lead + 0.3, b - hold)) for a, b, _ in cs])


def test_voiced_starts_at_on_ends_under_off_and_joins_short_gaps():
    """A span starts at VAD_ON and holds while the probability stays at VAD_OFF or more. A silence of 15 frames, under
    VAD_GAP, joins two spans, and one of 18 frames does not. The last span ends with the audio."""
    on, mid, off = s.VAD_ON, (s.VAD_ON + s.VAD_OFF) / 2, s.VAD_OFF - 0.01
    probs = [0.0, on, mid, mid, off] + [0.0] * 14 + [on, off] + [0.0] * 17 + [on]
    assert s.voiced(probs) == [[0.032, 0.64], [1.216, 1.248]]


def test_layout_fits_a_track_to_its_speech_and_not_another_episode():
    speech = voice(TALK)
    right = s.layout(TALK, speech, LONG)
    assert right["verdict"] == "fit" and right["score"] >= 0.6, right
    wrong = s.layout(show(12), speech, LONG)
    assert wrong["verdict"] == "mismatch" and wrong["score"] < s.LAYOUT and "another episode" in wrong["why"], wrong


@pytest.mark.parametrize("rate, offset", [(1, 45.0), (Fraction(25025, 24000), 0.4), (Fraction(24000, 25025), -3.0)])
def test_layout_fits_a_track_with_a_whole_track_offset_or_another_frame_rate(rate, offset):
    """The search finds the ratio and the offset, so a track the timing fix would move still fits."""
    r = s.layout(moved_to(TALK, rate=rate, offset=offset), voice(TALK), LONG)
    assert r["verdict"] == "fit" and Fraction(r["rate"]) == rate and abs(r["offset"] - offset) < 0.3, r


def test_layout_tries_more_than_the_top_vote_peak():
    """Every other line is spoken 2 s after its cue starts, and every line has a short sound 0.5 s before it. The
    sounds outvote the speech, so the one peak of align() lies at +0.5 s, where the lines miss the speech. The second
    peak at -2 s lifts most, so the track fits there."""
    speech = s.spans([(a + 2.0, b + 2.0, x) for a, b, x in TALK[::2]] + [(a - 0.5, a - 0.3, x) for a, b, x in TALK])
    assert s.align([c[0] for c in TALK], [a for a, _ in speech], 1) == pytest.approx(0.5, abs=0.05)
    r = s.layout(TALK, speech, LONG)
    assert r["verdict"] == "fit" and r["rate"] == "1/1" and abs(r["offset"] + 2.0) < 0.05, r


def test_a_karaoke_song_outvotes_no_dialogue():
    """300 lines on the speech, then an ending song typeset as 4,000 short events over two minutes with no speech
    under them, as karaoke or an ASS drawing is. Each span of cues that overlap votes once, so the dialogue still gives
    the offset, in layout() and in the search of the reference timing. When each event voted, layout() never tried
    the right offset, and the track read as another episode's at a lift of 0.09."""
    r, speech, cues, t = random.Random(1), [], [], 90.0
    while t < 1320:   # a span of speech holds one or two lines, and a cue starts near its speech
        a = t
        for _ in range(r.choice((1, 1, 2))):
            d = r.uniform(1.0, 3.5)
            cues.append((round(t + r.gauss(-0.15, 0.25), 3), round(t + d + 0.5, 3), "line"))
            t += d + r.uniform(0.1, 0.4)
        speech.append([round(a, 3), round(t, 3)])
        t += r.uniform(1.0, 8.0)
    song = [(round(1330 + 0.03 * k, 3), round(1332 + 0.03 * k, 3), "m 30 23 b 24 0") for k in range(4000)]
    got = s.layout(cues + song, speech, 1440)
    assert got["verdict"] == "fit" and got["rate"] == "1/1" and abs(got["offset"]) < 0.3, got
    lift, rate, offset = s.searched(s.spans(moved_to(cues + song, offset=2.0)), s.spans(cues + song))
    assert rate == 1 and abs(offset - 2.0) < 0.3 and lift > s.FIT, (lift, rate, offset)


def test_layout_needs_a_long_enough_file_and_enough_cues_and_speech():
    assert s.layout(TALK, voice(TALK), s.LAYOUT_MIN - 1)["verdict"] == "unknown"
    few = s.layout(TALK[:s.MIN_TRACK_CUES - 1], voice(TALK), LONG)
    assert few["verdict"] == "unknown" and f"under {s.MIN_TRACK_CUES}" in few["why"], few
    quiet = s.layout(TALK, voice(TALK)[:5], LONG)
    assert quiet["verdict"] == "unknown" and "of speech" in quiet["why"], quiet


def test_layout_does_not_judge_lines_that_do_not_follow_the_speech():
    """A track of sound captions shows a line now and then, for far less time than people speak. Live captions roll on
    with no gap, so they cover the speech at any offset. Neither tells where the lines belong."""
    captions = [(a, a + 2.0, "DOOR OPENS") for a, _, _ in TALK[::10]]
    sparse = s.layout(captions, voice(TALK), LONG)
    assert sparse["verdict"] == "unknown" and "do not follow the speech" in sparse["why"], sparse
    rolling = [(60.0 + 3 * k, 63.0 + 3 * k, f"line {k}") for k in range(int((LONG - 120) / 3))]
    live = s.layout(rolling, voice(TALK), LONG)
    assert live["verdict"] == "unknown" and "at any offset" in live["why"], live


@pytest.mark.parametrize("rate, offset", [(1, 30.0), (1, 200.0), (Fraction(25, 24), 2.0)])
def test_layout_fix_moves_a_track_off_by_one_shift_back_to_its_speech(rate, offset):
    """layout() finds the shift within LAYOUT_SEARCH, every slice agrees, and the moved lines fit at ratio 1. The fix
    keeps LAYOUT_LEAD. voice() speaks 0.15 s after each cue starts, so the cues land 0.05 s from where they belong."""
    cs, speech = moved_to(TALK, rate=rate, offset=offset), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    assert Fraction(t["fix"]["rate"]) == rate and back_to(cs, t["fix"]) < 0.1, t


def test_layout_fix_leaves_a_track_in_time_and_times_no_mismatch():
    speech = voice(TALK)
    t = s.layout_fix(TALK, speech, LONG, s.layout(TALK, speech, LONG))
    assert t["fix"] is None and t["why"] == "in time", t
    assert s.layout_fix(show(12), speech, LONG, s.layout(show(12), speech, LONG)) is None


def test_layout_fix_moves_nothing_when_the_parts_sit_at_different_offsets():
    """From the 200th cue on, the lines sit 8 s later than before, as in another cut. The slices show the step."""
    cs, speech = moved_to(TALK, offset=5.0, cut=(TALK[200][0], 8.0)), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    assert t["fix"] is None and t["piecewise"] and s.stepped(t["offsets"]), t


@pytest.mark.parametrize("at, step, unfixed, why", [(385, 8.0, 5.05, "the last 16 lines, which keep their times, line up with the speech +13.05 s"),
                                                   (15, -6.0, -0.95, "the first 17 lines, which keep their times, line up with the speech +5.05 s"),
                                                   (390, 8.0, 5.05, "the last 12 lines, which keep their times, line up with the speech +13.05 s")])
def test_layout_fix_refuses_an_end_off_by_its_own_offset(at, step, unfixed, why):
    """The track sits 5 s late, and from cue at on step seconds later. The lines at the end the step leaves have no
    speech start at the fix, so they keep their times. They line up with the speech at their own offset, so nothing
    moves."""
    cs, speech = moved_to(TALK, offset=5.0, cut=(TALK[at][0], step)), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    assert t["fix"] is None and t["unfixed"] == unfixed and why in t["why"], t


def kept_judged(cs, t):
    """(right lines the fix t moves off, the largest distance of a moved line from TALK, lines kept) of cs, one cue
    for each cue of TALK. A right line sits within TOLERANCE of TALK."""
    new = [a if s.kept_at(t.get("keep"), a) else s.moved(a * 1000, t["fix"]) / 1000 for a, _, _ in cs]
    right = [abs(a - b) <= s.TOLERANCE for (a, _, _), (b, _, _) in zip(cs, TALK)]
    return (sum(r and abs(n - b) > s.TOLERANCE for r, n, (b, _, _) in zip(right, new, TALK)),
            max(abs(n - b) for (a, _, _), n, (b, _, _) in zip(cs, new, TALK) if not s.kept_at(t.get("keep"), a)),
            sum(s.kept_at(t.get("keep"), a) for a, _, _ in cs))


@pytest.mark.parametrize("step, kept", [(40.0, 17), (6.0, 19)])
def test_a_cold_open_in_time_keeps_its_times_and_the_rest_moves(step, kept):
    """The first 15 lines are in time, and the rest sits step seconds late, as on a subtitle of a cut with a longer
    opening. The fix moves the rest, and the first lines keep their times. The move goes away from the file's end, so
    its last 2 lines, with no line of the fix to pass them, keep their times too. At 6 s two body lines after the cold
    open sit within the slack of the stay votes and keep their times."""
    cs, speech = moved_to(TALK, cut=(TALK[15][0], step)), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    right_moved, off, n = kept_judged(cs, t)
    assert t["fix"]["offset"] == pytest.approx(step, abs=0.1) and n == t["kept"] == kept and t["keep"][0][0] is None, t
    assert right_moved == 0 and off < 0.1 and t["keep"][-1][2] == 2, (right_moved, off, t["keep"])
    assert "keep their times" in t["why"], t["why"]


@pytest.mark.parametrize("at, g, kept", [(385, -20.0, 17), (380, -40.0, 22)])
def test_an_end_in_time_keeps_its_times_and_the_rest_moves(at, g, kept):
    """The track sits g seconds early, and from cue at on it is in time. The rest moves later, toward the end, and the
    last lines keep their times. The first 2 lines, which the move leaves behind, keep their times too."""
    cs, speech = moved_to(TALK, offset=g, cut=(TALK[at][0], -g)), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    assert t["fix"]["offset"] == pytest.approx(g + 0.05) and kept_judged(cs, t)[:2] == (0, pytest.approx(0.05, abs=0.01)) and t["kept"] == kept, t


def test_a_whole_track_shift_moves_the_lines_the_move_passes():
    """The track sits 30 s late, and no one speaks the first two lines. They have no speech start at the fix, but the
    first moved line with its own evidence passes where they sit, so they move. The last 2 lines, which the move goes
    away from, keep their times: the slack of two chance speech starts keeps them."""
    cs, speech = moved_to(TALK, offset=30.0), voice(TALK[2:])
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    assert t["fix"]["offset"] == pytest.approx(30.05) and t["keep"] == [[cs[-2][0], None, 2]] and kept_judged(cs, t)[1] < 0.1, t


def test_lines_no_one_speaks_at_the_start_keep_their_times():
    """The first two lines are in time where no one speaks, as signs are, and the rest sits 30 s late. Nothing says
    they belong at the fix, and no moved line passes them, so they keep their times."""
    cs = TALK[:2] + moved_to(TALK[2:], offset=30.0)
    t = s.layout_fix(cs, voice(TALK[2:]), LONG, s.layout(cs, voice(TALK[2:]), LONG))
    assert t["keep"][0] == [None, cs[2][0], 2] and kept_judged(cs, t)[:2] == (0, pytest.approx(0.05, abs=0.01)), t


def test_a_short_block_beside_the_cold_open_moves_nothing():
    """The review's cascade: the first 10 lines are in time, the next 6 sit 29 s late, and the rest 35 s late. The fix
    of 35 s passes the block, and the block, moved, would pass the cold open. Only the core's own edge line may move a
    line it passes, so the cold open never moves: the times stay."""
    cs = [c if k < 10 else (c[0] + 29, c[1] + 29, c[2]) if k < 16 else (c[0] + 35, c[1] + 35, c[2]) for k, c in enumerate(TALK)]
    t = s.layout_fix(cs, voice(TALK), LONG, s.layout(cs, voice(TALK), LONG))
    assert t["fix"] is None and "would pass a line that keeps its time" in t["why"], t


def test_a_frame_rate_fix_moves_every_line():
    """The track was timed for 25 fps on a 24 fps video. A frame-rate error covers the whole file, so the fix moves
    every line, the first and last lines too, which the move passes by too little to move them at ratio 1."""
    cs = moved_to(TALK, rate=Fraction(25, 24), offset=2.0)
    t = s.layout_fix(cs, voice(TALK), LONG, s.layout(cs, voice(TALK), LONG))
    assert t["fix"]["rate"] == "25/24" and "keep" not in t and kept_judged(cs, t)[:2] == (0, pytest.approx(0.05, abs=0.01)), t


def test_a_frame_rate_fix_moves_lines_with_no_speech_at_either_place():
    """The first 15 lines are in time, as a sign or a song with no speech under them, and the rest was timed for 25 fps.
    Nothing says the first lines sit right, so they move with the rest. This is the known limit of the two-source
    shape, see docs/design.md."""
    cs = TALK[:15] + moved_to(TALK[15:], rate=Fraction(25, 24), offset=2.0)
    t = s.layout_fix(cs, voice(TALK[15:]), LONG, s.layout(cs, voice(TALK[15:]), LONG))
    assert t["fix"]["rate"] == "25/24" and "keep" not in t and kept_judged(cs, t)[0] == 15, t


def test_a_frame_rate_fix_whose_end_sits_on_speech_moves_nothing():
    """The first 15 lines are in time and spoken, and the rest was timed for 25 fps. The run at the start holds speech
    starts where its lines sit and none at the fix, far over chance. The two sources cannot share one fix, so nothing
    moves, and the offset is kept for the alert."""
    cs = TALK[:15] + moved_to(TALK[15:], rate=Fraction(25, 24), offset=2.0)
    t = s.layout_fix(cs, voice(TALK), LONG, s.layout(cs, voice(TALK), LONG))
    assert t["fix"] is None and "unfixed" in t and "line up with the speech where they sit" in t["why"], t


def test_the_frame_rate_rule_breaks_on_a_kept_line_or_an_end_on_speech():
    """check_ratio() breaks when a fix at a ratio keeps a line, and when it moves an end that sits on speech."""
    cs = TALK[:15] + moved_to(TALK[15:], rate=Fraction(25, 24), offset=2.0)
    fix = {"rate": "25/24", "offset": 2.052}
    with pytest.raises(s.Broken, match="every line"):
        s.check_ratio(cs, [k >= 1 for k in range(len(cs))], fix, voice(TALK))
    with pytest.raises(s.Broken, match="sits on speech"):
        s.check_ratio(cs, [True] * len(cs), fix, voice(TALK))


def test_kept_runs_need_a_plan_of_blocks_that_holds_them_in_place():
    """keep_blocks() marks each kept run, so remux.time_plan() writes the times of its cues as they were."""
    for cs in (moved_to(TALK, cut=(TALK[15][0], 40.0)), moved_to(TALK, cut=(TALK[15][0], -40.0))):
        t = s.layout_fix(cs, voice(TALK), LONG, s.layout(cs, voice(TALK), LONG))
        plan = remux.time_plan(cs, t["fix"], s.keep_blocks(t))
        assert [(a, n) for _, _, _, a, n in plan[:15]] == [(a, b) for a, b, _ in cs[:15]], plan[:15]
        assert remux.blocks_moved(plan, t["fix"]) == f'{t["kept"]} cues kept their times'


def test_a_line_with_no_text_between_a_kept_run_and_the_moved_lines_stops_the_plan():
    """keep_ordered() takes every cue of the file, a line with no text too, which the check never read. Here such a line
    sits just after the cold open, and the moved lines would land before it."""
    cs = moved_to(TALK, cut=(TALK[15][0], 40.0))
    t = s.layout_fix(cs, voice(TALK), LONG, s.layout(cs, voice(TALK), LONG))
    assert s.keep_ordered(cs, t)
    blank = (round(cs[15][0] - 1.0, 3), round(cs[15][0] - 0.5, 3), "")
    assert not s.keep_ordered(sorted(cs + [blank]), t)


def rules_case(move=()):
    """(cues, moves, fix, speech) of a partial shift for check_kept(): five right lines 50 s apart, then the dialogue of
    TALK from 300 s on, 40 s late. The fix moves the dialogue back, and the right lines at the places in move move too."""
    right = [(10.0 + 50 * k, 12.0 + 50 * k, f"right {k}") for k in range(5)]
    body = [c for c in TALK if c[0] >= 300]
    cs = right + moved_to(body, offset=40.0)
    return cs, [k >= 5 or k in move for k in range(len(cs))], {"rate": "1/1", "offset": 40.05}, voice(right + body)


@pytest.mark.parametrize("move", [(2, 3, 4), (4,), ()])
def test_the_rules_of_a_partial_shift_catch_a_right_line_the_edge_never_passes(move):
    """check_kept() breaks when a line moves at the start that the core's edge line never passes: a cascade of passes
    moved the review's cold open that way, and a weak edge run moved one right line at its end. The order holds in
    both, so only the own evidence rule sees them. The moves shifted() makes pass."""
    cs, moves, fix, speech = rules_case(move)
    if move:
        with pytest.raises(s.Broken, match="own evidence"):
            s.check_kept(cs, moves, fix, speech)
    else:
        t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
        assert t["keep"][0] == [None, cs[5][0], 5], t
        s.check_kept(cs, [not s.kept_at(t["keep"], a) for a, _, _ in cs], t["fix"], speech)


def test_a_hole_of_kept_lines_breaks_the_edges_rule():
    cs = moved_to(TALK, offset=0.5)
    moves = [k != 200 for k in range(len(cs))]
    with pytest.raises(s.Broken, match="edges"):
        s.check_kept(cs, moves, {"rate": "1/1", "offset": 0.5}, voice(TALK))


def test_the_onsets_judge_only_the_lines_that_move():
    """The first 15 lines keep their times. Onsets lie only where the moved lines' speech starts, so both halves agree,
    and the kept lines count in neither."""
    cs, speech = moved_to(TALK, cut=(TALK[15][0], 40.0)), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    got = s.layout_onsets(cs, t, [(a, 1.0) for a, _ in voice(TALK[15:])], s.onset_parts(LONG), LONG)
    assert got["fix"] == t["fix"] and got["keep"] == t["keep"], got


@pytest.mark.parametrize("offsets, step", [([0.0] * 9 + [32.1], False), ([0.1 * k for k in range(10)], False), ([1.0 * k for k in range(10)], False),
                                           ([0.0] * 8 + [8.0, 8.1], True), ([5.0] * 5 + [13.0] * 5, True)])
def test_a_step_needs_two_slices_at_another_offset(offsets, step):
    """One slice alone off, as an end song that a few lines time, or a slow trend, as a ratio the search missed, is
    no step."""
    assert s.stepped(offsets) is step


def test_one_slice_off_alerts_nothing(monkeypatch):
    """A piecewise fit whose step stepped() does not show loses "piecewise", so it never alerts. The times stay."""
    monkeypatch.setattr(s, "sliced", lambda *a: {"fix": None, "piecewise": True, "offsets": [0.0] * 9 + [32.1], "why": "the cues are off"})
    t = s.layout_fix(TALK, voice(TALK), LONG, s.layout(TALK, voice(TALK), LONG))
    assert t["fix"] is None and "piecewise" not in t and "unfixed" not in t and "no two slices" in t["why"], t


def test_layout_fix_needs_the_moved_lines_to_fit_at_ratio_one(monkeypatch):
    """The check fits the moved lines again. Here it finds them 2 s off, so the times stay and the offset is kept for
    the alert."""
    cs, speech = moved_to(TALK, offset=30.0), voice(TALK)
    lay = s.layout(cs, speech, LONG)
    monkeypatch.setattr(s, "layout", lambda cues, sp, duration: dict(lay, rate="1/1", offset=s.LAYOUT_LEAD + 2.0))
    t = s.layout_fix(cs, speech, LONG, lay)
    assert t["fix"] is None and t["unfixed"] == pytest.approx(30.05, abs=0.05) and "do not line up" in t["why"], t


def test_the_nearer_its_speech_rule_breaks_on_a_fix_that_moves_the_lines_off_the_speech():
    with pytest.raises(s.Broken, match="nearer its speech"):
        s.nearer(TALK, moved_to(TALK, offset=7.0), voice(TALK), {"rate": "1/1", "offset": -7.0})


def test_onset_parts_spread_over_the_file_and_never_overlap():
    assert s.onset_parts(3600.0) == [(120.0 + 360.0 * k, 240.0 + 360.0 * k) for k in range(10)]
    assert s.onset_parts(1000.0) == [(100.0 * k, 100.0 * (k + 1)) for k in range(10)]


def test_the_speech_onsets_confirm_a_layout_fix():
    """Onsets where the speech starts confirm the fix in both halves. Onsets where the cues sat, none, onsets in one
    half only, and onsets after silences under ONSET_QUIET leave no fix, with the offset kept for the alert."""
    cs, speech = moved_to(TALK, offset=30.0), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    judge = lambda onsets: s.layout_onsets(cs, t, onsets, s.onset_parts(LONG), LONG)
    agree = judge([(a, 1.0) for a, _ in speech])
    assert agree["fix"] == t["fix"] and all(new > 2 * old for _, new, old, _ in agree["onsets"]), agree
    sat = judge([(a + 0.1, 1.0) for a, _, _ in cs])
    assert sat["fix"] is None and sat["unfixed"] == t["fix"]["offset"] and "where they sat" in sat["why"], sat
    for onsets in ([], [(a, 1.0) for a, _ in speech if a < LONG / 2], [(a, s.ONSET_QUIET - 0.1) for a, _ in speech]):
        few = judge(onsets)
        assert few["fix"] is None and "too few speech onsets" in few["why"], few


def test_a_music_bed_with_few_onsets_still_confirms_a_layout_fix():
    """Under a music bed silencedetect marks few onsets: here one at every eighth speech start, 14% of the starts in a
    half, under the 20% that clock() asks of a block. They lie where the fix puts the lines, none where the lines sat,
    and they are far over chance, so the fix stands."""
    cs, speech = moved_to(TALK, offset=30.0), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    got = s.layout_onsets(cs, t, [(a, 1.0) for a, _ in speech[::8]], s.onset_parts(LONG), LONG)
    assert got["fix"] == t["fix"] and all(new < s.ONSET_SHARE * n and new >= 10 * chance for n, new, _, chance in got["onsets"]), got


@pytest.mark.parametrize("case", ["under ONSET_MIN", "under the chance limit"])
def test_a_layout_fix_still_needs_onset_min_and_the_chance_limit(case):
    """Two onsets in each half lie under ONSET_MIN. A track whose parts sound every half second draws as many onsets by
    chance as the music bed gives, so its count is not rare. Neither confirms the fix."""
    cs, speech = moved_to(TALK, offset=30.0), voice(TALK)
    t = s.layout_fix(cs, speech, LONG, s.layout(cs, speech, LONG))
    parts = s.onset_parts(LONG)
    if case == "under ONSET_MIN":
        onsets = [(a, 1.0) for half in (False, True) for a in [a for a, _ in speech if any(lo <= a <= hi for lo, hi in parts) and (a > LONG / 2) == half][:2]]
    else:
        places = [a - s.ONSET_LEAD for a, _, _ in TALK + cs]
        noise = [(round(lo + 0.5 * k, 3), 1.0) for lo, hi in parts for k in range(int((hi - lo) / 0.5))]
        onsets = [(a, 1.0) for a, _ in speech[::8]] + [o for o in noise if all(abs(o[0] - p) > 0.2 for p in places)]
    got = s.layout_onsets(cs, t, sorted(onsets), parts, LONG)
    assert got["fix"] is None and "too few speech onsets" in got["why"], got


def test_align_refines_the_offset_past_its_vote_bins():
    """The votes give 2.0 s in bins of 0.1 s. The median of the pairs near the peak gives 2.037 s."""
    assert s.align([a for a, _, _ in moved_to(TALK, offset=2.037)], [a for a, _, _ in TALK], 1) == 2.037


def test_a_slice_pairs_cue_starts_up_to_pair_seconds_off():
    """A track 2 s late whose cue starts in the fifth tenth sit 0.4 s early and late in turn. Each pairs with its
    reference cue within PAIR, the slice's median sits on the line, and the fix holds."""
    lo, hi = TALK[0][0], TALK[-1][0]
    fifth = [c for c in TALK if 0.4 <= (c[0] - lo) / (hi - lo) < 0.5]
    fifth = fifth[:len(fifth) // 2 * 2]
    jitter = {c: 0.4 if k % 2 else -0.4 for k, c in enumerate(fifth)}
    track = [(a + 2.0 + jitter.get((a, b, x), 0.0), b + 2.0, x) for a, b, x in TALK if (a, b, x) in jitter or not 0.4 <= (a - lo) / (hi - lo) < 0.5]
    assert s.reference(track, {"s1": TALK}, LONG)["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}


def test_the_search_covers_offsets_up_to_search_seconds():
    assert s.reference(moved_to(TALK, offset=100.0), {"s1": TALK}, LONG + 100)["timing"]["fix"]["offset"] == pytest.approx(100.0, abs=0.01)
    assert s.reference(moved_to(TALK, offset=s.SEARCH + 30), {"s1": TALK}, LONG + 200)["verdict"] == "weak"


def test_a_ratio_whose_middle_slices_lie_off_the_centre_gets_no_fix():
    """The cues of the two middle slices sit only at their outer edges. A drift then cannot be told from a cut, so no fix."""
    lo, hi = TALK[0][0], TALK[-1][0]
    at = lambda a: (a - lo) / (hi - lo)
    ref = [c for c in TALK if not 0.4 <= at(c[0]) < 0.6 or at(c[0]) < 0.412 or at(c[0]) >= 0.588]
    r = s.reference(moved_to(ref, rate=Fraction(25025, 24000)), {"s1": ref}, LONG)
    assert r["timing"]["fix"] is None and "too far from the centre" in r["timing"]["why"], r["timing"]


# --- flash cues (docs/design.md, "Subtitle match") ----------------------------------------------------------------

FAR = [(1000.0 + 10 * i, 1000.1 + 10 * i, "a far line") for i in range(20)]   # flash cues long after, so a short list reaches MIN_TRACK_CUES


def test_flash_gives_each_cue_the_next_start_less_two_frames_or_its_hold():
    """Six cues that each show 0.138 s, as a real WEB release stored them. A new end is the next start less FRAMES, at
    most the start plus max(3 s, twice the reading time), 7 s at most. The last cue takes its hold."""
    starts = (9.24, 11.0, 12.45, 13.66, 15.72, 18.76, 60.0)
    texts = ("Are we there yet?", "No. [engine]", "Now?", "Sit down, please.", "La la...", "Here we go.", "x" * 200)
    ends = s.flash([(a, a + 0.138, t) for a, t in zip(starts, texts)] + FAR)
    assert [round(e - a, 2) for a, e in zip(starts, ends)] == [1.68, 1.37, 1.13, 1.98, 2.96, 3.0, 7.0], ends
    assert ends[5] == round(18.76 + 3.0, 3)   # 9 visible characters read in half a second: the hold of 3 s wins over the next start
    # the hold counts visible characters, with no tags and no spaces: 40 of them read in 2.35 s at 17 a second, and hold twice that
    tagged, plain = (s.flash([(0.0, 0.1, t)] + FAR)[0] for t in ("<i>" + "ab " * 20 + "</i>{\\an8}\\N", "ab \u200b" * 20))
    assert tagged == plain == 4.706


@pytest.mark.parametrize("blank", ["\u200b", " \u2060\ufeff ", "<i></i>", "{\\an8}\\N"])
def test_flash_counts_only_cues_with_a_visible_character(blank):
    """A cue of a zero-width space, or of tags alone, shows nothing. It never makes a track flash, and it keeps its end."""
    cs = [(10.0 * i, 10.0 * i + 0.1, "a line") for i in range(19)] + [(500.0, 500.333, blank)]
    assert s.flash(cs) is None and s.flash([(0.0, 0.333, blank)] * 30) is None
    ends = s.flash(cs + [(600.0, 600.1, "one more")])
    assert ends[19] == 500.333 and ends[0] == 3.0 and ends[20] == 603.0, ends


@pytest.mark.parametrize("apart, flagged", [(8, True), (7, False)])
def test_flash_needs_half_its_short_cues_to_end_over_two_frames_before_the_next(apart, flagged):
    """16 cues of 0.1 s and 14 of 2 s. Of the short cues, apart end 1 s before the next cue, and the rest 0.05 s
    before it, under two frames, as karaoke syllables and sign frames do. The long cues end 1 s before the next cue,
    and they never count toward GAP."""
    cs, t = [], 0.0
    for k, (d, gap) in enumerate([(0.1, 1.0)] * apart + [(0.1, 0.05)] * (16 - apart) + [(2.0, 1.0)] * 14):
        cs.append((round(t, 3), round(t + d, 3), f"line {k}"))
        t += d + gap
    assert (s.flash(cs) is not None) == flagged


def chain(parts):
    """Cues laid one after another from 10 s: each part is (seconds shown, seconds to the next cue, text)."""
    out, t = [], 10.0
    for d, gap, text in parts:
        out.append((round(t, 3), round(t + d, 3), text))
        t += d + gap
    return out


def test_a_cue_of_exactly_flash_seconds_is_no_short_cue():
    """26 cues of 0.1 s, 14 of them with a gap after them, and 25 cues of 0.5 s that run into the next. The 0.5 s cues
    are not short, so 14 of 26 short cues end apart, and the track flashes. They keep their ends."""
    parts = [(0.1, 1.0, f"a {k}") for k in range(14)] + [(0.1, 0.05, f"b {k}") for k in range(12)] + [(0.5, 0.05, f"c {k}") for k in range(25)]
    cs = chain(parts)
    ends = s.flash(cs)
    assert ends and ends[26:] == [e for _, e, _ in cs[26:]], ends


def test_blank_events_never_count_toward_the_gap_of_the_short_cues():
    """Signs drawn in three steps, 12 frames each, run into their next frame. 40 blank events with gaps between them lie
    elsewhere in the track. Only the signs count, none of them ends apart, so the track does not flash."""
    signs = [p for step in range(3) for p in [(0.1, 0.02, "{\\pos(10,20)}EXIT")] * 11 + [(0.1, 30.0, "{\\pos(10,20)}EXIT")]]
    blanks = [(0.1, 2.0, "{\\an8}")] * 40
    assert s.flash(chain(signs + blanks)) is None


def test_a_blank_event_right_after_a_line_counts_as_its_next_cue():
    """Each line of 0.2 s is followed 0.05 s later by a blank event, as a clear. The line does not end apart from the
    next cue, so the track does not flash, and no line is lengthened across the blank event."""
    assert s.flash(chain([p for k in range(30) for p in ((0.2, 0.05, f"line {k}"), (0.1, 3.0, "{\\an8}"))])) is None


def test_a_plain_shift_under_min_shift_is_in_time_even_when_the_line_through_two_windows_runs_off():
    """A right track with a slow trend: its two windows sit 0.1 s and 0.65 s early. The line through them runs 0.79 s off
    at the file's end, but a plain shift of 0.38 s is all a fix could do, and it would move the early part off."""
    e1 = [(155.0 + k, 155.0 + k - 0.15) for k in range(4)]
    e2 = [(2078.0 + k, 2078.0 + k - 0.70) for k in range(4)]
    r = s.fit([0, 1], [e1, e2], [], [(c, c + 2.0, "x") for _, c in e1 + e2], 2550.0)
    assert r == {"fix": None, "why": "in time", "offset": -0.375}, r


def test_needs_line_judges_the_move_at_both_file_ends():
    """+1.8 s at the start and none at the end, and none at the start and 1.8 s at the end, both move the cues 1.5 s or
    more at one end, so they keep the rules of fit()."""
    assert not s.needs_line({"rate": "1001/1000", "offset": -1.8}, 1800) and not s.needs_line({"rate": "1001/1000", "offset": 0.0}, 1800)
    assert s.needs_line({"rate": "1/1", "offset": 1.49}, 1800) and not s.needs_line({"rate": "1/1", "offset": 1.5}, 1800)


def test_a_plain_shift_of_exactly_min_shift_is_fixed():
    e1 = [(155.0 + k, 155.0 + k + 0.70) for k in range(4)]
    e2 = [(2078.0 + k, 2078.0 + k + 0.70) for k in range(4)]
    pairs = [(t, (None, i)) for i, (t, _) in enumerate(e1 + e2)]
    r = s.fit([0, 1], [e1, e2], pairs, [(c, c + 2.0, "x") for _, c in e1 + e2], 2550.0)
    assert r["fix"] == {"rate": "1/1", "offset": 0.75}, r


def test_a_fix_that_moves_the_cues_under_line_shift_at_both_ends_needs_the_windows_on_its_line():
    """A shift of 1.2 s, and +0.87 s with 1000/1001 over 30 minutes, which ends 0.93 s the other way, need them. A shift
    of 1.5 s, and a drift of 25/23.976 or 1001/1000 over 45 minutes, keep the rules of fit()."""
    assert s.needs_line({"rate": "1/1", "offset": 1.2}, 2700) and s.needs_line({"rate": "1000/1001", "offset": 0.87}, 1800)
    assert not s.needs_line({"rate": "1/1", "offset": 1.5}, 2700) and not s.needs_line({"rate": "1/1", "offset": -2.0}, 2700)
    assert not s.needs_line({"rate": "25025/24000", "offset": 0.0}, 2700) and not s.needs_line({"rate": "1001/1000", "offset": 0.3}, 2700)
    assert not s.needs_line(None, 2700)


def line_windows(track, fix, thin=None):
    """Heard windows at a third and two thirds of the file, of an in-time hearing of RIGHT, and their parts for
    on_line(). thin keeps only that many lines of the second window, as a window that hears little."""
    w = [370.0, 750.0]
    inside = lambda i: w[1] <= FIRST + GAP * i + LEAD <= w[1] + s.WINDOW
    kept = [i for i in range(LINES) if inside(i)][:thin] if thin is not None else None
    return heard(w[:1]) + heard(w[1:], keep=lambda i: kept is None or i in kept), [(a, None) for a in w]


WANDA = {"rate": "1000/1001", "offset": 0.17}   # the line of a false fix: 0.2 s off a right track at a third, 0.58 s at two thirds


@pytest.mark.parametrize("thin", [None, 0, 2, 3])
def test_a_false_fix_is_never_confirmed_by_a_window_too_thin_to_judge(thin):
    """A right track and the line of a false fix of +0.17 s and 1000/1001. The window at a third sits within 0.3 s of the
    line. The window at two thirds sits 0.58 s off it, or hears 0, 2 or 3 lines. No case confirms the fix."""
    got, parts = line_windows(cues(RIGHT), WANDA, thin)
    assert s.on_line(got, cues(RIGHT), "eng", WANDA, parts) is False


def test_a_window_on_the_line_with_under_min_cues_anchors_confirms_nothing():
    """A track 1.2 s late. The window at two thirds hears 2 lines, both on the line: too thin to judge."""
    track, fix = cues(RIGHT, offset=1.2), {"rate": "1/1", "offset": 1.2}
    inside = lambda i: 751.2 <= FIRST + GAP * i + LEAD <= 751.2 + s.WINDOW
    kept = [i for i in range(LINES) if inside(i)][:2]
    got = heard([371.2]) + heard([751.2], keep=lambda i: i in kept)
    assert s.on_line(got, track, "eng", fix, [(371.2, None), (751.2, None)]) is False


def test_both_windows_on_the_line_confirm_a_fix_and_a_start_no_window_has_does_not():
    track, fix = cues(RIGHT, offset=1.2), {"rate": "1/1", "offset": 1.2}
    got, parts = line_windows(track, fix)
    assert s.on_line([dict(w, at=w["at"] + 1.2) for w in got], track, "eng", fix, [(a + 1.2, b) for a, b in parts]) is False   # windows at no heard start
    assert s.on_line(heard([371.2, 751.2]), track, "eng", fix, [(371.2, None), (751.2, None)])


def test_a_window_with_enough_anchors_off_the_line_is_never_rescued_by_its_longer_window():
    """The window at two thirds holds enough anchors, off the line. Its longer window would sit on the line, but it
    counts only for a window that heard too little."""
    track, fix = cues(RIGHT, offset=1.2), {"rate": "1/1", "offset": 1.2}
    off = cues(RIGHT, offset=1.2, where=lambda i: -0.6 if 751.2 <= FIRST + GAP * i + 1.2 <= 761.2 + 1.2 else 0.0)
    got = heard([371.2, 751.2]) + heard([800.0], secs=s.THIRD)
    assert s.on_line(got, off, "eng", fix, [(371.2, None), (751.2, 800.0)]) is False
    assert s.on_line(got, track, "eng", fix, [(371.2, None), (751.2, 800.0)])


def test_flash_lengthens_only_the_short_cues():
    """Short cues with gaps among dialogue lines of 2 s. The track flashes, and each line keeps its 2 s."""
    cs = [(5.0 * i, 5.0 * i + 0.2, "la") for i in range(20)] + [(200.0 + 5 * i, 202.0 + 5 * i, "a line of dialogue") for i in range(10)]
    ends = s.flash(cs)
    assert ends[:2] == [3.0, 8.0] and ends[20:] == [e for _, e, _ in cs[20:]], ends


@pytest.mark.parametrize("length, flagged", [(0.49, True), (0.5, False)])
def test_flash_needs_a_median_under_flash(length, flagged):
    cs = [(10.0 * i, 10.0 * i + length, "some words") for i in range(30)]
    assert (s.flash(cs) is not None) == flagged


def test_flash_leaves_signs_that_run_into_the_next_event():
    """A typesetting track draws a sign frame by frame: each event of 0.04 s runs straight into the next one."""
    signs = [(10 + 0.04 * i, 10 + 0.04 * (i + 1), "{\\pos(10,20)}EXIT") for i in range(200)]
    assert s.flash(signs) is None
    with_gaps = [(10 + 2 * i, 10 + 2 * i + 0.04, "EXIT") for i in range(200)]
    assert s.flash(with_gaps) is not None


def test_flash_only_lengthens():
    """A cue that already shows past its new end keeps its end, as does a cue whose next line starts right after it."""
    cs = [(0.0, 0.1, "a"), (1.0, 5.0, "b"), (5.02, 5.1, "c"), (5.12, 5.2, "d"), (20.0, 20.1, "e")]
    assert s.flash(cs + FAR)[:5] == [0.917, 5.0, 5.1, 8.12, 23.0]


# --- the sweep of --sub-time ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("rate", [Fraction(1), Fraction(25025, 24000)])
def test_the_sweep_measures_each_window_against_the_fitted_line(rate):
    """A track 2 s late, or drifting, with the fix of the word check: every window sits on the fitted line. With no fix
    the rows say how far each part of the file is off."""
    track = cues(RIGHT, rate=rate, offset=2.0)
    fix = {"rate": f"{rate.numerator}/{rate.denominator}", "offset": 2.0}
    rows = s.sweep(heard([EARLY, MIDDLE, LATE]), track, "eng", {"fix": fix})
    assert [r["at"] for r in rows] == [EARLY, MIDDLE, LATE] and all(r["overlap"] >= 0.9 and r["cues"] >= s.MIN_CUES for r in rows), rows
    assert all(abs(r["off"]) <= 0.05 for r in rows), rows
    plain = s.sweep(heard([EARLY, LATE]), cues(RIGHT, offset=2.0), "eng", {"fix": None, "why": "in time"})
    assert [round(r["off"], 1) for r in plain] == [2.0, 2.0], plain


def test_a_sweep_window_that_heard_nothing_has_no_offset():
    rows = s.sweep([{"at": 50.0, "words": []}], cues(RIGHT), "eng")
    assert rows == [{"at": 50.0, "words": 0, "overlap": 0.0, "cues": 0, "offset": None, "off": None}]


def sweep_rows(offsets, overlap=0.9, words=15, cues=4):
    """Sweep rows, one a minute from 60 s, with these offsets."""
    return [{"at": 60.0 * (k + 1), "words": words, "overlap": overlap, "cues": cues, "offset": o, "off": o} for k, o in enumerate(offsets)]


def test_a_clean_sweep_is_in_time_everywhere():
    """Ten windows across a file of 11 minutes, every one matched and under MIN_SHIFT."""
    ok = [0.08, -0.49, 0.24, -0.38, 0.05, 0.12, 0.14, 0.04, 0.26, -0.24]
    assert s.clean(sweep_rows(ok), 660)
    assert not s.clean(sweep_rows(ok[:5] + [1.0] + ok[6:]), 660)   # one window 1 s off
    assert not s.clean(sweep_rows(ok[:5] + [-0.75] + ok[6:]), 660)   # MIN_SHIFT itself is off
    rows = sweep_rows(ok)
    rows[3]["overlap"] = 0.4   # a window that heard enough and matched too little
    assert not s.clean(rows, 660)
    rows = sweep_rows(ok) + [{"at": 600.0, "words": 3, "overlap": 0.33, "cues": 1, "offset": -2.05, "off": -2.05}]
    assert s.clean(rows, 660)   # a window that heard 3 words says nothing
    assert not s.clean(sweep_rows(ok[:7]), 660)   # the late half holds 2 windows, under SWEPT
    assert s.clean(sweep_rows(ok[:8]), 660)


def decile_steps(steps):
    """TALK with each tenth of its span moved by its own step, as a track cut ten ways."""
    lo, hi = TALK[0][0], TALK[-1][0]
    step = lambda a: steps[min(9, int((a - lo) / (hi - lo) * 10))]
    return [(a + step(a), b + step(a), x) for a, b, x in TALK]


@pytest.mark.parametrize("steps", [(0.2, 0.5, 0.9, 1.2, 1.6, 1.9, 2.2, 2.4, 2.8, 3.0), (0.2, 0.2, 0.9, 0.9, 1.6, 1.6, 2.3, 2.3, 3.0, 3.0)])
def test_a_cut_in_steps_never_passes_as_a_ratio(steps):
    """Each tenth of the track sits at its own offset, from +0.2 s to +3.0 s. A ratio near 1 could draw a line through
    five parts of it, but every slice must sit within TOLERANCE of the fix."""
    r = s.reference(decile_steps(steps), {"s1": TALK}, LONG)
    assert r["verdict"] == "fit" and r["timing"]["fix"] is None and r["timing"]["piecewise"], r


def test_a_reference_with_a_cut_of_its_own_leaves_a_right_track_in_time():
    """The reference hides a cut of 0.6 s at the middle, too small for the word check to see. A right track then
    sits 0.6 s apart from it in half the file, and it keeps its times."""
    r = s.reference(TALK, {"s1": moved_to(TALK, cut=(LONG / 2, 0.6))}, LONG)
    assert r["timing"]["fix"] is None and r["timing"]["why"] == "in time", r["timing"]


def test_a_fix_must_agree_with_a_second_reference():
    """A track 2 s late fits s1 with a fix of +2 s. Against s2, which has a cut, it reads as two offsets, so no fix.
    Against a second reference 0.1 s off the first, the same fix holds."""
    late = moved_to(TALK, offset=2.0)
    r = s.reference(late, {"s1": TALK, "s2": moved_to(TALK, cut=(LONG / 2, 1.0))}, LONG)
    assert r["timing"]["fix"] is None and r["timing"]["unfixed"] == 2.0 and "a fix of +2.00 s fits s1, but s2 says the cues are off" in r["timing"]["why"], r
    r = s.reference(late, {"s1": TALK, "s2": moved_to(TALK, offset=0.1)}, LONG)
    assert r["timing"]["fix"] == {"rate": "1/1", "offset": 2.0}, r["timing"]
    r = s.reference(late, {"s1": TALK, "s2": moved_to(TALK, offset=0.5)}, LONG)
    assert r["timing"]["fix"] is None and "but s2 needs a fix of +1.50 s" in r["timing"]["why"], r["timing"]


# --- the block timing of the sweep ------------------------------------------------------------------------------------

SCENE, PAUSE = 12, 5.0   # lines of one scene, and seconds of silence between two scenes
AT = [FIRST + GAP * i + PAUSE * (i // SCENE) for i in range(LINES)]   # where line i starts on the audio of a right track
LENGTH = AT[-1] + 60     # the file's duration: the credits hold no speech


def scenes(a, b):
    """The lines of scenes a to b - 1."""
    return range(a * SCENE, min(b * SCENE, LINES))


def track(late=lambda i: 0.0, rate=1, offset=0.0, show=lambda i: SHOW, at=AT):
    """The cues of RIGHT, cue i late(i) seconds after the speech of line i at at[i], moved to rate * t + offset."""
    return [(float(rate) * (at[i] + late(i)) + offset, float(rate) * (at[i] + late(i) + show(i)) + offset, x) for i, x in enumerate(RIGHT)]


def hear(starts, noise=0.2, at=AT, secs=s.WINDOW, mute=(), lost=(), extra=(), delay={}):
    """What Whisper hears in each window: line i from at[i] + LEAD, each line moved by its own error of up to noise
    seconds either way, as Whisper's word times are. Whisper hears no word of the lines in mute, and mishears the first
    word of the lines in lost. extra holds (audio time, word) heard by chance, and delay {line: seconds} hears a line
    later."""
    r = random.Random(3)
    err = [r.uniform(-noise, noise) for _ in RIGHT]
    said = sorted([(at[i] + LEAD + err[i] + delay.get(i, 0.0) + STEP * k, "zzz" if k == 0 and i in lost else w) for i, x in enumerate(RIGHT) if i not in mute
                   for k, w in enumerate(x.rstrip(".").split())] + list(extra))
    return [{"at": a, "secs": secs, "words": [[round(t - a, 2), w] for t, w in said if a <= t < a + secs]} for a in starts]


def swept(trk, fix=None, noise=0.2, mute=()):
    """The sweep() rows of trk, one window a minute, as --sub-time hears them."""
    there = [(s.moved(round(a * 1000), fix) / 1000, s.moved(round(b * 1000), fix) / 1000, x) for a, b, x in trk] if fix else trk
    minutes = [w for m in range(int(LENGTH // 60) + 1)
               for w in s.windows(there, LENGTH, parts=((m * 60 / LENGTH, min(1.0, (m + 1) * 60 / LENGTH)),))]
    return s.sweep(hear(minutes, noise, mute=mute), trk, "eng", {"fix": fix})


def timed(trk, fix=None, noise=0.2, mute=(), clock=True):
    """The block timing of trk as --sub-time runs it. The sweep hears one window a minute, suspects() names the parts,
    dense() hears them, and blocks() judges them, with the speech onsets of the audio unless clock is False. Returns
    (sweep rows, parts, blocks())."""
    timing = {"fix": fix}
    there = [(s.moved(round(a * 1000), fix) / 1000, s.moved(round(b * 1000), fix) / 1000, x) for a, b, x in trk] if fix else trk
    rows = swept(trk, fix, noise, mute)
    parts = s.suspects(rows, LENGTH)
    return rows, parts, s.blocks(hear(s.dense(there, parts, LENGTH), noise, mute=mute), trk, "eng", timing, parts, onsets(every=True) if clock else None, rows)


def placed(trk, fix, got):
    """Where each cue of trk starts on the audio after the fix and the block fixes, and whether a block moved it."""
    out = []
    for a, _, _ in trk:
        b = next((b for b in got["blocks"] if b["from"] <= a < b["to"] and round(a, 3) not in b.get("keep", ())), None)
        out.append(((s.moved(round(a * 1000), fix) / 1000 if fix else a) - (s.shift_of(b, a) if b else 0.0), b is not None))
    return out


def lands(trk, fix, got, block, at=AT, exact=False):
    """Only cues of block move, at least half of them, each to within 0.2 s of its speech. With exact, every cue of block
    moves. blocks() leaves a cue at a block's edge where it is unless both clocks put it clearly in the block, see the
    edges of blocks()."""
    moved = [i for i, (t, m) in enumerate(placed(trk, fix, got)) if m]
    assert set(moved) <= set(block) and 2 * len(moved) >= len(block), (sorted(set(moved) - set(block)), len(moved), len(block), got)
    assert not exact or set(moved) == set(block), (sorted(set(block) - set(moved)), got)
    for i in moved:
        t = placed(trk, fix, got)[i][0]
        assert abs(t - at[i]) <= 0.2, (i, t, at[i], got)


@pytest.mark.parametrize("late, where", [(0.85, scenes(10, 15)), (1.5, scenes(10, 13)), (-2.0, scenes(10, 12))])
def test_a_block_of_one_to_three_minutes_moves_to_its_speech(late, where):
    """A block of 1 to 3 minutes sits late or early after an edit, and the cues around it are in time. The sweep finds it,
    dense hearing hears it, and only its cues move. Its edges lie at the scene cuts."""
    trk = track(late=lambda i: late if i in where else 0.0)
    rows, parts, got = timed(trk)
    assert any(abs(r["off"] or 0) >= s.BLOCK_SHIFT for r in rows) and parts, rows
    assert len(got["blocks"]) == 1, got
    b = got["blocks"][0]
    assert trk[where[0]][0] <= b["from"] and b["to"] <= trk[where[-1] + 1][0], b
    assert abs(b["shift"] - late) <= 0.1 and b["anchors"] >= s.BLOCK_CUES and b["spread"] <= s.TOLERANCE, b
    assert all(p["why"] is None for p in got["parts"]), got
    lands(trk, None, got, where)


def test_a_block_at_the_file_end_runs_past_the_last_cue():
    """The last scenes sit 2 s late up to the end of the file. No cue after them sits on the line, so the block runs
    past the last cue. A block from the first cue starts at 0."""
    where = scenes(30, 34)
    trk = track(late=lambda i: 2.0 if i in where else 0.0)
    _, parts, got = timed(trk)
    assert parts[-1][1] == round(LENGTH, 1) and len(got["blocks"]) == 1, (parts, got)
    assert got["blocks"][0]["from"] >= trk[where[0]][0] and got["blocks"][0]["to"] == trk[-1][0] + 1.0, got
    lands(trk, None, got, where)
    where = scenes(0, 3)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    _, parts, got = timed(trk)
    assert parts[0][0] == 0.0 and [b["from"] for b in got["blocks"]] == [0.0], (parts, got)
    lands(trk, None, got, where)


def test_cues_with_no_heard_word_at_an_edge_stay_where_they_are():
    """Whisper hears no word of the last line before the block and the first two lines of it. A scene cut lies between
    them, but a block can also start mid-scene beside one, so no gap decides. The three lines stay, and the block counts
    them at its start."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    _, _, got = timed(trk, mute={119, 120, 121})
    assert got["blocks"][0]["from"] >= trk[122][0] and got["blocks"][0]["edge_left"] >= 3, got
    lands(trk, None, got, [i for i in where if i not in (120, 121)])


def test_two_blocks_each_move_by_their_own_shift():
    """Two blocks in one part, with cues on the line between them, and two blocks in two parts. Each moves by its own
    shift. In one track the sweep finds blocks of two minutes or more, and two of them exceed BLOCK_HEAR, so the
    parts here are given."""
    one, two = scenes(5, 7), scenes(10, 12)
    trk = track(late=lambda i: 1.5 if i in one else -1.0 if i in two else 0.0)
    for parts in ([(AT[50], AT[155])], [(AT[50], AT[96]), (AT[108], AT[155])]):
        got = s.blocks(hear(s.dense(trk, parts, LENGTH)), trk, "eng", {"fix": None}, parts, onsets(every=True))
        assert [round(b["shift"], 1) for b in got["blocks"]] == [1.5, -1.0], (parts, got)
        lands(trk, None, got, set(one) | set(two))


def test_a_ratio_fix_and_a_block_on_one_track():
    """A track timed for 25 fps, 3 s late, holds a block 1.5 s later still. The block's shift counts after the fix."""
    rate, where = Fraction(25025, 24000), scenes(15, 18)
    fix = {"rate": "25025/24000", "offset": 3.0}
    trk = track(late=lambda i: 1.5 if i in where else 0.0, rate=rate, offset=3.0)
    _, _, got = timed(trk, fix)
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 1.5) <= 0.1, got
    lands(trk, fix, got, where)


def test_the_jitter_of_a_right_track_never_becomes_a_block():
    """A right track whose author starts each cue from 0.23 s early to 0.48 s late, signs mixed, with Whisper's own
    error on top. Even when dense hearing hears three minutes of it, no cue moves."""
    r = random.Random(11)
    jit = [r.uniform(-0.23, 0.48) for _ in RIGHT]
    trk = track(late=lambda i: jit[i])
    rows, parts, got = timed(trk)
    assert got["blocks"] == [], (parts, got)
    forced = [(300.0, 480.0), (700.0, 880.0)]
    got = s.blocks(hear(s.dense(trk, forced, LENGTH)), trk, "eng", {"fix": None}, forced)
    assert got["blocks"] == [] and all(p["why"] and p["anchors"] >= 40 for p in got["parts"]), got


def test_a_few_slipped_cues_are_no_block():
    """Three cues in a row slipped 0.9 s, as one sweep row can show. Dense hearing hears too few to move them."""
    trk = track(late=lambda i: 0.9 if 200 <= i < 203 else 0.0)
    part = [(AT[190], AT[212])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH)), trk, "eng", {"fix": None}, part)
    assert got["blocks"] == [] and not got["parts"][0]["in_line"] and got["parts"][0]["why"].startswith("no 6 heard cues in a row agree"), got


def test_the_suspects_reach_the_rows_on_the_line_around_them():
    """Two rows a minute apart sit +1.65 and +0.61 s off, with 2 cues each, and both are suspects. The row after them
    holds a single cue, so the part reaches BLOCK_MARGIN past the suspects, or the next row on the line with 2 cues when
    that is nearer."""
    rows = [{"at": a, "words": w, "overlap": 0.9, "cues": c, "offset": o, "off": o}
            for a, o, c, w in [(841.7, 0.12, 3, 15), (901.7, -0.16, 3, 14), (985.7, 1.65, 2, 19), (1028.8, 0.61, 2, 13), (1084.4, -0.23, 1, 10),
                               (1141.0, 0.05, 4, 16)]] + CALM
    assert s.suspects(rows, 2600) == [(901.7, 1151.0)]
    assert s.suspects(rows[:4] + [dict(r, at=r["at"] - 2000) for r in CALM], 1100) == [(901.7, 1100)]   # no row on the line after
    early = [dict(r, at=r["at"] - 900) for r in rows[2:6]] + CALM
    assert s.suspects(early, 2600) == [(0.0, 251.0)]       # no row on the line before: from the file's start
    far = [dict(r, cues=1) if r["at"] in (901.7, 1141.0) else r for r in rows]
    assert s.suspects(far, 2600) == [(985.7 - s.BLOCK_MARGIN, 1028.8 + s.WINDOW + s.BLOCK_MARGIN)]   # never out to a far row
    half = sweep_rows([0.0, 2.0, 2.0, 2.0, 2.0, 0.0, 0.0, 0.0, 0.0])
    assert s.suspects(half, 640) == [(60.0, 370.0)]
    assert s.suspects(half, 600) == []                      # over half the file: the whole track is off


def row(at, off, words=15, cues=3, overlap=0.9):
    return {"at": at, "words": words, "overlap": overlap, "cues": cues, "offset": off, "off": off}


CALM = [{"at": 2200.0 + 20 * k, "words": 15, "overlap": 0.9, "cues": 3, "offset": 0.0, "off": 0.0} for k in range(15)]   # rows in time


def test_a_single_row_triggers_and_blocks_refuses_right_track_noise():
    """One row 0.75 s off with in-time rows around it triggers dense hearing, so no block is missed. There dense
    hearing hears right-track noise, a run of 6 heard cues seen on a real track, and blocks() moves nothing. A row that
    heard 4 words gave -50.59 s by a chance pair, and a row with one cue proves little: neither triggers."""
    rows = lambda o, words=12, cues=2: [row(1500.0, 0.1), row(1631.1, o, words, cues), row(1751.1, -0.1)]
    assert s.suspects(rows(0.75), 6000) == [(1511.1, 1761.1)]   # BLOCK_MARGIN is nearer than the rows on the line
    assert s.suspects(rows(-50.59), 6000) == [(1511.1, 1761.1)]
    assert s.suspects(rows(s.BLOCK_SHIFT - 0.01), 6000) == []
    assert s.suspects(rows(-50.59, words=4), 6000) == []
    assert s.suspects(rows(-50.59, cues=1), 6000) == []
    assert s.suspects(sweep_rows([-0.3, 0.46, 0.2, -0.31, 0.12, 0.44, -0.05]), 600) == []   # all under BLOCK_SHIFT off their lean
    noise = [-0.81, -0.03, -0.45, -0.25, -0.81, -1.77]
    r = random.Random(4)
    jit = [r.uniform(-0.15, 0.15) for _ in RIGHT]
    got = forced(lambda i: noise[i - 200] if 200 <= i < 206 else jit[i], 185, 220)
    assert got["blocks"] == [], got   # its 6 cues sit 0.63 s off at their median, so the part is not called in time either


def test_a_block_of_one_minute_with_one_sweep_row_moves():
    where = scenes(10, 12)
    trk = track(late=lambda i: -2.0 if i in where else 0.0)
    rows, parts, got = timed(trk)
    assert sum(abs(r["off"] or 0) >= s.BLOCK_SHIFT for r in rows) == 1 and len(parts) == 1, rows
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


def test_the_cap_trims_a_part_and_never_drops_one():
    """Three suspect rows with full margins make one part of 370 s. Its margins shrink to fit BLOCK_HEAR."""
    three = [row(300.0, 0.0), row(480.0, 0.0, cues=1), row(600.0, 1.0), row(660.0, 1.0), row(720.0, 1.0), row(780.0, 0.0, cues=1),
             row(1100.0, 0.0)] + CALM
    assert s.suspects(three, 3600) == [(485.0, 845.0)]
    # The part 2 s off takes 310 s. The part 1 s off has 50 s left, under 2 * TRIM_MARGIN of margins, so it hears the
    # 50 s around its row. The part 0.8 s off has no audio left: it gives a part of no length.
    offs = [0.0] * 60
    offs[9:13], offs[29], offs[49] = [2.0] * 4, 1.0, 0.8
    parts = s.suspects(sweep_rows(offs), 3600)
    assert parts == [(540.0, 850.0), (1780.0, 1830.0), (3000.0, 3000.0)], parts
    got = s.blocks([], track(), "eng", {"fix": None}, parts)
    assert got["parts"][2]["why"] == f"the dense hearing cap of {s.BLOCK_HEAR:.0f} s was used by parts farther off", got
    assert s.dense(track(), [(600.0, 600.0)], LENGTH) == []   # dense hearing never hears a part of no length
    # A margin of 20 s stays whole, and the other margin takes the rest of the 230 s left.
    first = [row(100.0, 0.0), row(160.0, 2.0), row(220.0, 0.0)]
    second = [row(580.0, 0.0), row(600.0, 1.0), row(660.0, 1.0), row(720.0, 1.0), row(780.0, 0.0, cues=1), row(1200.0, 0.0)]
    assert s.suspects(first + second + CALM, 3600) == [(100.0, 230.0), (580.0, 810.0)]
    # With 45 s left, the margins of 10 and 60 s cannot both keep TRIM_MARGIN, so the part is the 45 s around the row,
    # moved to lie inside the part it had.
    first = [row(100.0, 0.0)] + [row(a, 2.0) for a in (160.0, 220.0, 280.0, 340.0)] + [row(405.0, 0.0)]
    second = [row(1990.0, 0.0), row(2000.0, 1.0), row(2060.0, 0.0)]
    assert s.suspects(first + second + CALM, 3600) == [(100.0, 415.0), (1990.0, 2035.0)]
    two = [row(100.0, 0.0), row(130.0, 0.9), row(160.0, 0.0), row(190.0, 0.9), row(220.0, 0.0)]
    assert s.suspects(two, 3600) == [(100.0, 230.0)]   # parts that touch merge
    assert s.suspects([row(500.0, 0.8), row(2000.0, 2.0)] + CALM, 3600) == [(450.0, 560.0), (1880.0, 2130.0)]   # the later part is farther off


@pytest.mark.parametrize("second, found", [(scenes(24, 27), True), (scenes(22, 25), False)])
def test_two_blocks_of_two_minutes_are_both_heard(second, found):
    """The first block's part takes most of BLOCK_HEAR, and the second's part is trimmed. It still moves when its
    trimmed part holds MIN_CUES heard cues on the line at each side. Else it gives why, and none of its cues moves."""
    first = scenes(5, 9)
    trk = track(late=lambda i: 1.5 if i in first else -1.0 if i in second else 0.0)
    _, parts, got = timed(trk)
    assert len(parts) == 2 and round(sum(b - a for a, b in parts)) == s.BLOCK_HEAR and all(p["anchors"] >= 20 for p in got["parts"]), got
    if found:
        assert len(got["blocks"]) == 2 and all(abs(b["shift"] - x) <= 0.1 for b, x in zip(got["blocks"], (1.5, -1.0))), got
        lands(trk, None, got, set(first) | set(second))
    else:
        assert len(got["blocks"]) == 1 and "is not seen" in got["parts"][1]["why"], got
        lands(trk, None, got, first)


def test_dense_windows_overlap_and_skip_silence_and_songs():
    trk = track()
    got = s.dense(trk, [(300.0, 480.0)], LENGTH)
    assert got[0] == 300.0 and got[-1] == 470.0, got
    assert all(0 < b - a <= 7.5 for a, b in zip(got, got[1:])), got   # 10 s windows that share 2.5 s
    gap = [c for c in trk if not 350 <= c[0] <= 420]   # a minute with no cue
    song = [(c[0], c[1], "♪ " + c[2]) if 350 <= c[0] <= 420 else c for c in trk]
    for cs in (gap, song):
        got = s.dense(cs, [(300.0, 480.0)], LENGTH)
        assert got and not any(350 + s.PAD < a and a + s.WINDOW < 420 - s.PAD for a in got), got
    assert s.dense(trk, [(300.0, 400.0), (380.0, 480.0)], LENGTH) == s.dense(trk, [(300.0, 480.0)], LENGTH)   # parts that overlap merge


def test_a_cue_before_a_block_keeps_its_end_when_the_block_moves_over_it():
    """The last cue before a block shows until 0.5 s after the block's first line is spoken. The block moves 1.5 s
    earlier, so its first cue starts while that cue shows. The block still moves, and that cue keeps its times."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0, show=lambda i: AT[120] + 0.5 - AT[119] if i == 119 else SHOW)
    _, _, got = timed(trk)
    assert len(got["blocks"]) == 1 and got["blocks"][0]["from"] == trk[120][0], got   # every line has an onset: the edge holds
    assert trk[119][1] > placed(trk, None, got)[120][0]   # they overlap
    lands(trk, None, got, where)


def test_a_block_move_never_changes_the_order_of_cue_starts():
    """Line 119 is spoken right after the block's first line, and its cue sits on the line before the block's first
    cue, which is 2.5 s late. A move of the block would put that cue first. The onsets judge too few cues, so on Whisper
    alone the block gets no move."""
    at = [a + (2.0 if i > 120 else 0.0) for i, a in enumerate(AT)]
    at[119] = AT[120] + 1.9
    where = scenes(10, 14)
    trk = sorted(track(late=lambda i: 2.5 if i in where else 0.0, at=at))
    starts = [AT[120] - 20, AT[120] + 1.6] + [AT[120] + 11.6 + 7 * k for k in range(20)] + [AT[100] + 7 * k for k in range(5)]
    heard = hear([a for a in starts if a != AT[120] - 20], at=at, noise=0.03) + hear([AT[120] - 20], at=at, secs=21.0, noise=0.03)
    got = s.blocks(heard, trk, "eng", {"fix": None}, [(AT[100], AT[175])], [(a + LEAD, 1.0) for a in at])
    assert got["blocks"] == [] and got["parts"][0]["whys"] == ["on Whisper alone, a move of -2.50 s would put a cue past the cue next to the block"], got


def test_with_the_onsets_agreeing_a_cue_that_would_pass_the_cue_before_the_block_moves_part_of_the_way():
    """A block 3 s late from line 120, the first line of a scene, whose lines start 2.5 s apart. Line 120 follows a long
    silence, so it never joins the block. Line 121 would move past it, so it moves only to one centisecond after it.
    The lines after it then have room, and they move all the way. The onsets agree, so the shift is sure."""
    trk = track(late=lambda i: 3.0 if 120 <= i <= 140 else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True))
    lines, at = moved_lines(trk, got), placed(trk, None, got)
    assert [b["onsets"]["verdict"] for b in got["blocks"]] == ["agree"] and set(range(121, 140)) <= lines <= set(range(121, 141)), got
    assert s.written(at[121][0], 0.0, None) == s.written(trk[120][0], 0.0, None) + 1 and got["blocks"][0]["clamp"] == [[round(trk[121][0], 3), 2.49]]
    assert all(abs(at[i][0] - AT[i]) <= 0.2 for i in lines - {121}) and keeps_order(trk, got)


def test_on_whisper_alone_a_cue_that_would_pass_an_unproved_cue_in_the_block_moves_part_of_the_way():
    """A block 2 s late from line 122 to 155, on Whisper alone. Line 136 has no heard words, so it stays. Line 137 is
    spoken 1 s after it, so its move would pass it, and line 137 moves only to one centisecond after it. The cue it
    would pass lies in the block, so the rest of the block moves all the way."""
    at = list(AT)
    at[137] = AT[136] + 1.0
    trk = sorted(track(late=lambda i: 2.0 if 122 <= i <= 155 else 0.0, at=at))
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, at=at, mute={136}), trk, "eng", {"fix": None}, part)
    lines, new = moved_lines(trk, got), placed(trk, None, got)
    assert [b["onsets"]["verdict"] for b in got["blocks"]] == ["few"] and 136 not in lines and 137 in lines, got
    assert s.written(new[137][0], 0.0, None) == s.written(trk[136][0], 0.0, None) + 1 and [c for c, _ in got["blocks"][0]["clamp"]] == [round(trk[137][0], 3)]
    assert set(range(123, 132)) | set(range(138, 144)) <= lines and keeps_order(trk, got), sorted(lines)


def test_a_cue_in_time_whose_anchor_alone_reads_like_the_block_never_moves_part_of_the_way():
    """Two blocks 2 s late, lines 122 to 136 and 138 to 155, on Whisper alone. Line 137 between them is in time. Its
    speech starts 0.35 s after the late cue of line 136, which has no heard words and stays. Whisper hears the first
    word of line 137 2 s early, so its anchor alone reads like the block. Its move would pass line 136. A part move
    needs two pieces of a cue's own evidence, so line 137 stays where it is."""
    at = list(AT)
    at[137] = AT[136] + 2.35
    trk = sorted(track(late=lambda i: 2.0 if 122 <= i <= 155 and i != 137 else 0.0, at=at))
    part = [(AT[100], AT[175])]
    first = RIGHT[137].split()[0]
    heard = hear(s.dense(trk, part, LENGTH), 0.03, at=at, mute={136}, lost={137}, extra=[(at[137] + LEAD - 2.0, first)])
    got = s.blocks(heard, trk, "eng", {"fix": None}, part)
    lines = moved_lines(trk, got)
    assert 136 not in lines and 137 not in lines and set(range(123, 132)) | set(range(138, 144)) <= lines and keeps_order(trk, got), got


def test_a_cue_whose_anchor_sits_off_the_block_never_moves_on_its_words():
    """Blocks 1.5 s late on lines 120 to 129 and 131 to 143. Line 130, in time between them, is heard 1.0 s early, so its
    anchor sits 0.5 s off the block, over TOLERANCE, and its words read like the block. Its own anchor says otherwise,
    so its words alone never move it."""
    blk = set(range(120, 130)) | set(range(131, 144))
    trk = track(late=lambda i: 1.5 if i in blk else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={130: -1.0}), trk, "eng", {"fix": None}, part)
    lines = moved_lines(trk, got)
    assert 130 not in lines and set(range(122, 128)) | set(range(133, 142)) <= lines, sorted(lines)


def test_an_onset_needs_the_cue_s_own_words_in_the_block():
    """Blocks 1.5 s late on lines 120 to 139 and 141 to 164. Line 140, in time between them, has its first word
    misheard, so its words fit both sides, and no onset of its own. A lone onset lies where the block would put it.
    The onset alone never moves it."""
    blk = set(range(120, 140)) | set(range(141, 165))
    trk = track(late=lambda i: 1.5 if i in blk else 0.0)
    ons = sorted([o for o in onsets(every=True) if abs(o[0] - AT[140] - LEAD) > 0.01] + [(AT[140] + LEAD - 1.45, 1.0)])
    part = [(AT[100], AT[185])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, lost={140}), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert 140 not in lines and set(range(121, 139)) - HUSHED <= lines, sorted(lines)


def test_a_cue_that_starts_with_a_cue_that_stays_stays_too():
    """Lines 123 and 124 show at once in a block 1.5 s late, and Whisper hears nothing of line 123, so it stays.
    remux.time_plan() keeps a start, not a cue, so line 124 stays with it, and the block counts only the cues that
    move."""
    at = list(AT)
    at[123] = at[124]
    trk = track(late=lambda i: 1.5 if 122 <= i <= 155 else 0.0, at=at)
    got = quiet(trk, [(AT[100], AT[175])], at=at, mute={123})
    lines = moved_lines(trk, got)
    assert not {123, 124} & lines and [b["cues"] for b in got["blocks"]] == [len(lines)] and len(lines) >= 20, (sorted(lines), got["blocks"])


def test_a_side_off_the_line_hides_the_edge():
    """Dense hearing heard only the block and the cues after it. Its start is not seen, so no cue moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[where[0]] + 1.0, AT[where[-1]] + 40)]
    got = s.blocks(hear(s.dense(trk, part, LENGTH)), trk, "eng", {"fix": None}, part)
    assert got["blocks"] == [] and "so its start is not seen" in got["parts"][0]["why"], got
    assert got["parts"][0]["more"] == [(part[0][0] - s.FURTHER, part[0][0])], got["parts"][0]["more"]
    part = [(AT[100], AT[where[-1]] - 5)]   # and only the cues before it and the block: its end is not seen
    got = s.blocks(hear(s.dense(trk, part, LENGTH)), trk, "eng", {"fix": None}, part)
    assert got["blocks"] == [] and "so its end is not seen" in got["parts"][0]["why"], got
    assert got["parts"][0]["more"] == [(part[0][1], part[0][1] + s.FURTHER)], got["parts"][0]["more"]


def test_a_second_hearing_past_an_unseen_end_finds_the_block():
    """Dense hearing heard the cues before a block 1.5 s late and the block but its last line, so its end is not seen.
    The part asks for FURTHER seconds past its end, and blocks() over the part with that stretch finds the block. Line
    120 starts the scene, and its words never put it in the block."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[100], AT[where[-1]] - 5)]
    got = s.blocks(hear(s.dense(trk, part, LENGTH)), trk, "eng", {"fix": None}, part, onsets(every=True))
    more = s.further({"s1": got}, part, LENGTH)["s1"]
    assert got["blocks"] == [] and more == [(part[0][1], part[0][1] + s.FURTHER)], (got, more)
    both = [tuple(p) for p in s.merged(part + more)]
    got = s.blocks(hear(s.dense(trk, both, LENGTH)), trk, "eng", {"fix": None}, both, onsets(every=True))
    assert set(where[1:]) <= moved_lines(trk, got) <= set(where), sorted(moved_lines(trk, got))   # line 120 starts the scene


def test_further_hears_within_what_the_cap_leaves():
    """The stretches of further() count against what BLOCK_HEAR leaves after the first hearing, in order. Only their
    seconds that the first hearing did not hear count. A stretch cut short keeps its end at its part, one under WINDOW
    seconds is not heard, and none reaches past the file."""
    part = lambda lo, hi, more: {"lo": lo, "hi": hi, "more": more}
    results = {"s1": {"parts": [part(100.0, 300.0, [(70.0, 100.0), (300.0, 330.0)])]}, "s2": {"parts": [part(500.0, 590.0, [(590.0, 620.0)])]}}
    heard = lambda n: [(100.0, 300.0), (400.0, 400.0 + n), (500.0, 590.0)]   # 290 + n seconds heard first
    assert s.BLOCK_HEAR == 360.0
    assert s.further(results, [(100.0, 300.0)], 610.0) == {"s1": [(70.0, 100.0), (300.0, 330.0)], "s2": [(590.0, 610.0)]}
    assert s.further(results, heard(0.0), 610.0) == {"s1": [(70.0, 100.0), (300.0, 330.0)], "s2": [(590.0, 600.0)]}
    assert s.further(results, heard(45.0), 610.0) == {"s1": [(75.0, 100.0)]}
    assert s.further(results, heard(55.0), 610.0) == {"s1": [(85.0, 100.0)]}
    assert s.further(results, heard(65.0), 610.0) == {}
    assert s.further(results, [(60.0, 300.0), (500.0, 590.0)], 610.0) == {"s1": [(70.0, 100.0), (300.0, 330.0)]}   # 70 to 100 was heard


def test_unheard_leaves_out_what_was_heard():
    assert s.unheard([(0.0, 100.0)], [(10.0, 20.0), (50.0, 120.0)]) == [(0.0, 10.0), (20.0, 50.0)]
    assert s.unheard([(0.0, 10.0), (30.0, 40.0)], [(5.0, 35.0)]) == [(0.0, 5.0), (35.0, 40.0)]
    assert s.unheard([(0.0, 10.0)], []) == [(0.0, 10.0)] and s.unheard([(0.0, 10.0)], [(0.0, 10.0)]) == []


def test_after_blocks_takes_the_shift_off_the_rows_of_a_block():
    where = scenes(15, 18)
    fix = {"rate": "25025/24000", "offset": 3.0}
    trk = track(late=lambda i: 1.5 if i in where else 0.0, rate=Fraction(25025, 24000), offset=3.0)
    rows, _, got = timed(trk, fix)
    after = s.after_blocks(rows, got["blocks"], {"fix": fix})
    assert len(got["blocks"]) == 1 and sum(abs(r["off"]) >= 1.0 for r in rows if r["off"] is not None) >= 2, rows
    assert all(abs(r["off"]) < s.BLOCK_SHIFT for r in after if r["off"] is not None), after
    assert all(a == b for a, b in zip(rows, after) if a["off"] is not None and abs(a["off"]) < 0.5), (rows, after)   # rows on the line stay
    assert s.after_blocks([{"at": 50.0, "off": None}], got["blocks"], {"fix": fix}) == [{"at": 50.0, "off": None}]


def test_after_blocks_fixes_a_row_only_where_its_cues_moved():
    """A block 1 s late from 100 s to 200 s keeps the cue at 150 s. A row inside it sits on the line after the move. A
    row that holds the kept cue, and a row whose window crosses an edge, keep their off: cues there still sit off."""
    blocks = [{"from": 100.0, "to": 200.0, "shift": 1.0, "keep": [150.0]}]
    rows = [{"at": at, "off": off} for at, off in ((110.0, 1.0), (145.0, 1.0), (95.0, 1.0), (195.0, 1.0), (300.0, 0.1))]
    assert [r["off"] for r in s.after_blocks(rows, blocks, None)] == [0.0, 1.0, 1.0, 1.0, 0.1]


def test_cues_on_the_line_between_two_blocks_never_move():
    """Two blocks 1.5 s late with three cues on the line between them. Together they agree at over AGREE, but three
    heard cues in a row off the median split them, and the three cues keep their times."""
    where = [i for i in scenes(10, 14) if not 144 <= i < 147]
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    _, _, got = timed(trk)
    assert len(got["blocks"]) == 2 and got["blocks"][0]["to"] <= trk[144][0] and got["blocks"][1]["from"] >= trk[147][0], got
    lands(trk, None, got, where)


def forced(late, a=110, b=180, ons=None):
    """blocks() of a track with these late times, heard in full from line a to line b, with these speech onsets."""
    trk = track(late=late)
    part = [(AT[a], AT[b])]
    return s.blocks(hear(s.dense(trk, part, LENGTH)), trk, "eng", {"fix": None}, part, ons)


def test_a_block_whose_heard_cues_disagree_does_not_move():
    """Every third cue of the block sits 1 s later than the rest, so under AGREE of its heard cues agree."""
    got = forced(lambda i: (2.0 if i % 3 == 2 else 1.0) if i in scenes(10, 13) else 0.0)
    assert got["blocks"] == [] and got["parts"][0]["why"].startswith("no 6 heard cues in a row agree"), got


def test_a_block_under_block_shift_does_not_move():
    """With no speech onset, a block moves only when it sits BLOCK_ALONE off."""
    assert forced(lambda i: 0.4 if i in scenes(10, 13) else 0.0)["blocks"] == []
    assert forced(lambda i: 0.9 if i in scenes(10, 13) else 0.0)["blocks"] == []
    assert len(forced(lambda i: 1.15 if i in scenes(10, 13) else 0.0)["blocks"]) == 1


def test_two_blocks_side_by_side_never_move_each_others_cues():
    """A block 2.5 s late runs straight into one 1.2 s late. The 1.2 s block's sides are the nearest cues on the line,
    so it can move. The words of the 2.5 s block's cues fit neither side of it, so they stay."""
    trk = track(late=lambda i: 2.5 if i in scenes(10, 12) else 1.2 if i in scenes(12, 14) else 0.0)
    got = forced(lambda i: 2.5 if i in scenes(10, 12) else 1.2 if i in scenes(12, 14) else 0.0)
    assert moved_lines(trk, got) <= set(scenes(12, 14)), got


def test_a_window_that_heard_under_min_words_names_no_anchor():
    """A window in the part heard the first words of the two lines before the block, by chance far from their speech.
    It heard under MIN_WORDS words, so its pairs name no anchor, and the block still moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[100], AT[175])]
    chance = [[4.9 + 0.1 * k, w] for k, w in enumerate(RIGHT[117].split()[:3] + RIGHT[118].split()[:3])]
    heard_ = hear(s.dense(trk, part, LENGTH), mute={117, 118}) + [{"at": AT[105], "words": chance}]   # only the chance window hears them
    got = s.blocks(heard_, trk, "eng", {"fix": None}, part)
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


def test_a_block_keeps_the_lead_of_the_cues_around_it():
    """Every cue of the track starts 0.2 s before its speech, as right tracks often do, and a block sits 1.2 s later
    than the rest. Its shift counts against the cues around it, so its cues move to the same lead."""
    where = scenes(10, 13)
    trk = track(late=lambda i: (1.0 if i in where else -0.2))
    _, _, got = timed(trk)
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 1.2) <= 0.1, got
    lands(trk, None, got, where, at=[a - 0.2 for a in AT])
    got = forced(lambda i: 0.6 if i in where else -0.2, ons=onsets())   # 0.8 s off the cues around it, 0.6 s off the fitted line
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 0.8) <= 0.1, got



# --- the anchors and the edges of dense hearing ------------------------------------------------------------------------

def spoken(lines=RIGHT, moved=lambda i, k: 0.0, drop=lambda i, k: False):
    """(audio time, word) of every word of lines, line i from FIRST + GAP * i + LEAD, as heard() says them."""
    return sorted((FIRST + GAP * i + LEAD + STEP * k + moved(i, k), w) for i, x in enumerate(lines)
                  for k, w in enumerate(x.rstrip(".").split()) if not drop(i, k))


def window(a, said, secs=s.WINDOW):
    return {"at": a, "secs": secs, "words": [[round(t - a, 3), w] for t, w in said if a <= t < a + secs]}


STOPS = s.decide.STOPWORDS["eng"]


def test_dense_hearing_anchors_a_cue_at_its_first_spoken_word():
    """Each line starts with two stopwords. The anchor steps back from the first content word to "and", the first word
    spoken, while each word lies under SPOKEN_GAP before the next. A pause of 1.2 s before "the" stops it there."""
    lines = [" ".join(["And", "the"] + x.rstrip(".").split()[:3]) + "." for x in RIGHT]   # 5 words, said in 1.5 s
    track, starts = cues(lines), [500.0 + 5 * k for k in range(8)]
    at, _ = s.heard_anchors([window(a, spoken(lines)) for a in starts], track, STOPS)
    assert len(at) >= 10 and all(abs(t - (track[i][0] + LEAD)) <= 0.01 for i, t in at.items()), at
    pause = spoken(lines, moved=lambda i, k: -1.2 if k == 0 else 0.0)
    at, _ = s.heard_anchors([window(a, pause) for a in starts], track, STOPS)
    assert len(at) >= 10 and all(abs(t - (track[i][0] + LEAD + STEP)) <= 0.01 for i, t in at.items()), at


def test_a_cue_of_under_short_cue_or_that_repeats_its_neighbour_never_anchors():
    track = cues(RIGHT)
    track[200] = (track[200][0], track[200][0] + 0.001, track[200][2])   # shows 1 ms
    starts = [FIRST + GAP * 196 + 5 * k for k in range(4)]
    at, words = s.heard_anchors([window(a, spoken()) for a in starts], track, STOPS)
    assert 200 not in at and 200 not in words and {199, 201} <= set(at), at
    lines = list(RIGHT)
    lines[201] = lines[200]   # a cue that repeats the one before it. Its line is never spoken, so one speech fits both.
    at, _ = s.heard_anchors([window(FIRST + GAP * 198 + 1.0, spoken(lines, drop=lambda i, k: i == 201), 14.0)], cues(lines), STOPS)
    assert not {200, 201} & set(at) and {199, 202} <= set(at), at


def test_one_heard_word_anchors_one_cue_at_most():
    """Two lines in a row start with the same name. One window hears both. Another hears only the name of the first
    line and the second line without its name, so its name pairs with the second cue, 2.5 s early. That heard word
    then anchors two cues and anchors neither. The second cue keeps its anchor from the first window."""
    i = next(k for k in range(200, 300) if RIGHT[k].split()[0] == RIGHT[k + 1].split()[0])
    track, a = cues(RIGHT), FIRST + GAP * i - 3.0
    one = window(a, spoken())
    two = window(a, spoken(drop=lambda li, k: (li == i and k > 0) or (li == i + 1 and k == 0)))
    at, _ = s.heard_anchors([one, two], track, STOPS)
    assert i not in at and abs(at[i + 1] - (track[i + 1][0] + LEAD)) <= 0.01, (i, at)


@pytest.mark.parametrize("apart, kept", [(0.5, False), (0.2, True)])
def test_two_windows_that_time_a_cue_apart_drop_it(apart, kept):
    """One window hears a line 0.5 s later than the other, as after a song. The cue anchors in neither. Within
    TOLERANCE it keeps the anchor heard farthest from its window's edges."""
    i, track = 250, cues(RIGHT)
    a = FIRST + GAP * i - 2.0
    late = window(a - 3.0, spoken(moved=lambda li, k: apart if li == i else 0.0))
    at, _ = s.heard_anchors([window(a, spoken()), late], track, STOPS)
    assert (i in at) == kept and {i - 1, i + 1} <= set(at), at
    if kept:
        assert abs(at[i] - (track[i][0] + LEAD + apart)) <= 0.01, at[i]   # 5.05 s from the start of the later window


def test_placed_puts_a_cue_on_the_side_its_heard_words_allow():
    """A cue from 100 s. Two words heard 1.15 s and 0.5 s before it rule out the line, so the cue lies in a block 1.5 s
    late. One early word may be a chance pair, and a word heard late proves nothing, so those cues are unsure."""
    assert s.placed(100.0, [98.85, 99.5], 0.0, 1.5) == "block"
    assert s.placed(100.0, [98.85, 100.4], 0.0, 1.5) == "unsure"
    assert s.placed(100.0, [100.35, 101.0, 101.6], 0.0, 1.5) == "unsure"
    assert s.placed(100.0, [100.35, 100.65], 0.0, -1.0) == "line"   # a block 1 s early would say them from 101 s
    assert s.placed(100.0, [], 0.0, 1.5) is None


def test_split_moves_only_cues_placed_in_the_block():
    assert s.split([None], False) == (0, 1)
    assert s.split([None, None, None], True) == (3, 3)
    assert s.split(["line", "block"], True) == (1, 0)
    assert s.split(["block", "block"], True) == (0, 0)
    assert s.split([None, "block"], True) == (1, 1)
    assert s.split(["unsure", "block"], True) == (1, 1)
    assert s.split(["block", "line"], True) == (2, 1)       # sides that disagree: everything stays
    assert s.split(["block", None, "line"], False) == (1, 1)
    assert s.split(["block", "unsure"], False) == (1, 1)
    assert s.split(["line", "block"], False) == (0, 1)


def even_block(say=lambda i, k, w: w, keep=lambda i: True):
    """blocks() of cues 2.5 s apart with no scene pause, lines 160 to 219 1.5 s late, heard in full from line 140 to 240."""
    track = cues(RIGHT, where=lambda i: 1.5 if 160 <= i < 220 else 0.0)
    part = [(FIRST + GAP * 140, FIRST + GAP * 240)]
    return track, s.blocks(heard(s.dense(track, part, DURATION), say=say, keep=keep), track, "eng", {"fix": None}, part)


@pytest.mark.parametrize("case", ["heard", "first word lost", "unheard"])
def test_an_edge_on_even_cues_never_moves_a_cue_outside_the_block(case):
    """Cue 220 follows the block and sits on the line. Its first word may go unheard. Its other heard words then put it
    on the line. With no word heard, the largest gap lies at cue 220, and it is not clear in both readings, so cue 220
    stays and counts at the block's end. Cue 160 starts the block. Unheard, it stays too and counts at its start. With
    only its later words heard, cue 220 is unsure, because its speech fits both sides, and it stays too."""
    say = (lambda i, k, w: "zzz" if i == 220 and k == 0 else w) if case == "first word lost" else (lambda i, k, w: w)
    keep = (lambda i: i not in (160, 220)) if case == "unheard" else (lambda i: True)
    track, got = even_block(say, keep)
    (b,) = got["blocks"]
    moved = {i for i, c in enumerate(track) if b["from"] <= c[0] < b["to"] and round(c[0], 3) not in b["keep"]}
    assert moved <= set(range(160, 220)) and len(moved) >= 50, (sorted(moved)[:3], sorted(moved)[-3:], b)   # cue 220 never moves
    assert abs(b["shift"] - 1.5) <= 0.1, b


def test_a_part_in_line_takes_its_sweep_rows_to_its_median():
    """The sweep put two rows of a right track 0.8 s off by chance. Dense hearing hears that part in line, so the rows
    take its median and no alert fires. A part whose cues sit off but disagree keeps its rows."""
    trk = track()
    rows = [row(a, o) for a, o in ((300.0, 0.1), (360.0, 0.8), (420.0, 0.85), (480.0, -0.1), (540.0, 0.05))]
    parts = s.suspects(rows, LENGTH)
    got = s.blocks(hear(s.dense(trk, parts, LENGTH)), trk, "eng", {"fix": None}, parts)
    (p,) = got["parts"]
    assert got["blocks"] == [] and p["in_line"] and p["anchors"] >= s.IN_LINE and abs(p["median"]) <= 0.1, p
    after = s.after_blocks(rows, got["blocks"], {"fix": None}, got["parts"])
    # The row at the part's start holds under 2 anchors in its window, so it keeps its time. So does the row after the part.
    assert [r["off"] for r in after] == [0.1] + [p["median"]] * 3 + [0.05], after
    off = forced(lambda i: (2.0 if i % 3 == 2 else 1.0) if i in scenes(10, 13) else 0.0)
    assert not off["parts"][0]["in_line"] and s.after_blocks(rows, [], {"fix": None}, off["parts"]) == rows, off
    even = forced(lambda i: 0.5)   # every cue 0.5 s late: no run sits BLOCK_SHIFT off, but the median sits over TOLERANCE
    assert not even["parts"][0]["in_line"] and even["blocks"] == [], even



# --- shapes a fuzz of blocks() found -----------------------------------------------------------------------------------

def test_a_block_near_the_file_start_never_takes_the_cues_in_time_before_it():
    trk = track(late=lambda i: 1.5 if 5 <= i < 60 else 0.0)
    part = [(0.0, AT[80])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), lost=range(5)), trk, "eng", {"fix": None}, part)
    assert len(got["blocks"]) == 1 and got["blocks"][0]["from"] >= trk[5][0] and got["blocks"][0]["edge_left"] >= 5, got
    lands(trk, None, got, range(5, 60))


def test_a_block_is_measured_against_the_anchors_on_the_line_around_it():
    """Whisper hears the three lines at each side of a block 0.2 s late. The block's shift counts against the
    BLOCK_CUES nearest anchors on the line at each side, so those six never pull it 0.2 s off."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[90], AT[185])]
    slow = {i: 0.2 for i in (117, 118, 119, 156, 157, 158)}
    got = s.blocks(hear(s.dense(trk, part, LENGTH), delay=slow), trk, "eng", {"fix": None}, part)
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 1.5) <= 0.1, got
    lands(trk, None, got, where)


def test_a_block_at_the_file_end_never_takes_the_cues_in_time_after_it():
    """A block 1.5 s late on lines 340 to 394. Lines 395 to 399 are in time, and Whisper mishears only their first
    words, so they have no anchor. Their later words fit both sides, so they stay. The block reaches the file's end
    only when its own heard cues do."""
    trk = track(late=lambda i: 1.5 if 340 <= i < 395 else 0.0)
    part = [(AT[310], LENGTH)]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), lost=range(395, 400)), trk, "eng", {"fix": None}, part)
    assert len(got["blocks"]) == 1 and got["blocks"][0]["to"] <= trk[395][0] and got["blocks"][0]["edge_right"] >= 5, got
    lands(trk, None, got, range(340, 395))


@pytest.mark.parametrize("unheard", [False, True])
def test_a_chance_anchor_next_to_a_block_is_judged_by_its_words(unheard):
    """A block 1.95 s early starts a scene. Line 131 before it is unheard. The first two words of the line before
    them, or of line 131 itself, are heard by chance 4.9 s after its cue starts. That anchor sits nearer the block than
    the line, but not within TOLERANCE of the block, so its words judge the cue: they fit both sides, and it stays."""
    k = 130 if unheard else 131
    trk = track(late=lambda i: -1.95 if i in scenes(11, 14) else 0.0)
    chance = [(AT[k] + 4.92 + STEP * n, w) for n, w in enumerate(RIGHT[k].rstrip(".").split()[:2])]
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), mute={k} | ({131} if unheard else set()), extra=chance), trk, "eng", {"fix": None}, part)
    at, _ = s.heard_anchors(hear(s.dense(trk, part, LENGTH), mute={k} | ({131} if unheard else set()), extra=chance), sorted(trk), STOPS)
    assert abs(trk[k][0] - at[k] + 4.92) <= 0.05, at.get(k)   # the chance anchor, 4.92 s after the cue starts
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, scenes(11, 14))


def test_the_cue_after_a_late_block_keeps_its_anchor_where_the_block_overlaps_it():
    """A block 1.5 s late ends mid-scene at line 214. Its last cue shows over the start of line 215, which is in time.
    The words of the two cues then lie out of order by time, and the search would drop the first word of line 215.
    Clipped at the next start, line 215 keeps its anchor and stays."""
    trk = track(late=lambda i: 1.5 if 180 <= i < 215 else 0.0)
    part = [(AT[170], AT[235])]
    heard_ = hear(s.dense(trk, part, LENGTH))
    at, _ = s.heard_anchors(heard_, sorted(trk), STOPS)
    assert trk[214][1] > trk[215][0] and abs(trk[215][0] - at[215] - (-LEAD)) <= 0.25, at.get(215)
    got = s.blocks(heard_, trk, "eng", {"fix": None}, part)
    assert len(got["blocks"]) == 1 and got["blocks"][0]["to"] <= trk[215][0], got
    lands(trk, None, got, range(180, 215))


def test_a_part_with_too_few_heard_cues_or_three_off_in_a_row_is_never_in_line():
    """A block of 8 cues 0.8 s late, with every fourth line unheard. Under IN_LINE_SHARE of the part's cues anchor, or
    three heard cues in a row sit BLOCK_SHIFT less TOLERANCE off, so the part is not in line and its rows keep their
    times. The same part with every line heard and no block is in line."""
    trk = track(late=lambda i: 0.8 if 200 <= i < 208 else 0.0)
    part = [(AT[185], AT[225])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), mute=set(range(185, 225, 4))), trk, "eng", {"fix": None}, part)
    assert got["blocks"] == [] and not got["parts"][0]["in_line"], got
    right = track()
    sparse = s.blocks(hear(s.dense(right, part, LENGTH), mute=set(range(185, 225, 3))), right, "eng", {"fix": None}, part)
    full = s.blocks(hear(s.dense(right, part, LENGTH)), right, "eng", {"fix": None}, part)
    assert not sparse["parts"][0]["in_line"] and full["parts"][0]["in_line"], (sparse, full)   # a third unheard: under the share



# --- the second clock: speech onsets ------------------------------------------------------------------------------------

def onsets(keep=lambda i: True, every=False):
    """(time, seconds of the silence before it) of the start of each line's speech, from when the line before ends.
    With every, each line starts after a silence long enough to time it, as a clean recording would give."""
    out = []
    for i in range(LINES):
        end = AT[i - 1] + LEAD + STEP * len(RIGHT[i - 1].split()) if i else 0.0
        if keep(i):
            out.append((AT[i] + LEAD, max(AT[i] + LEAD - end, s.ONSET_QUIET) if every else AT[i] + LEAD - end))
    return out


def judged(late, delay={}, ons=None):
    """blocks() of a track with these late times, heard with Whisper's times moved by delay, and these onsets."""
    trk = track(late=late)
    part = [(AT[100], AT[175])]
    return s.blocks(hear(s.dense(trk, part, LENGTH), delay=delay), trk, "eng", {"fix": None}, part, ons)


def test_a_block_the_onsets_agree_with_moves_at_block_shift():
    where = scenes(10, 13)
    got = judged(lambda i: 0.55 if i in where else 0.0, ons=onsets())
    (b,) = got["blocks"]
    assert b["onsets"]["verdict"] == "agree" and abs(b["onsets"]["shift"] - 0.55) <= 0.05 and min(b["onsets"]["inside"], b["onsets"]["outside"]) >= s.ONSET_MIN, b
    assert abs(b["shift"] - 0.55) <= 0.1 and b["from"] >= track(late=lambda i: 0.55 if i in where else 0.0)[120][0], b


@pytest.mark.parametrize("error", [0.34, 0.75])
def test_whisper_early_across_a_stretch_never_moves_a_right_track(error):
    """Whisper hears three scenes of a right track early, so their cues seem late. The onsets show the subtitle at its
    usual lead. At 0.34 s the cues sit under BLOCK_SHIFT. At 0.75 s the onsets disagree, and nothing moves."""
    got = judged(lambda i: 0.0, delay={i: -error for i in scenes(10, 13)}, ons=onsets())
    assert got["blocks"] == [], got
    if error > 0.5:
        assert got["parts"][0]["onsets"]["verdict"] == "disagree", got
        assert got["parts"][0]["why"].startswith("Whisper puts its cues +0.") and got["parts"][0]["why"].endswith("so they stay"), got


@pytest.mark.parametrize("ons", [None, "two"])
def test_with_few_onsets_a_block_moves_only_at_block_alone(ons):
    where = scenes(10, 13)
    few = None if ons is None else onsets(keep=lambda i: i in (121, 130))
    got = judged(lambda i: 0.9 if i in where else 0.0, ons=few)
    assert got["blocks"] == [] and got["parts"][0]["onsets"]["verdict"] == "few", got
    assert " under 1.0 s, and " in got["parts"][0]["why"], got
    got = judged(lambda i: 1.1 if i in where else 0.0, ons=few)
    assert len(got["blocks"]) == 1 and got["blocks"][0]["onsets"]["verdict"] == "few", got


def test_an_onset_counts_only_after_a_silence():
    """The cues around the block are timed by onsets 0.05 s after their starts. Cue 0 has an onset where a block 1.5 s
    late puts it, cue 1 one that ends a short silence, cue 2 one where it sat, and cue 3 none. Under ONSET_MIN cues count,
    so the onsets are few."""
    at = {k: 10.0 * k for k in range(7)}
    ons = [(8.55, 0.8), (18.55, 0.3), (20.05 + 10.0, 0.8), (40.05, 0.8), (50.05, 0.8), (60.05, 0.8)]
    got = s.clock(sorted(ons), at, [0, 1, 2, 3], [4, 5, 6], 1.5)
    assert (got["inside"], got["original"], got["outside"], got["verdict"]) == (1, 1, 3, "few"), got


def test_onsets_that_put_a_stretch_under_block_shift_never_move_it():
    """An author set three scenes 0.38 s late, and Whisper hears them 0.17 s early, so they seem 0.55 s late. The onsets
    put them 0.38 s late, within TOLERANCE of Whisper but under BLOCK_SHIFT, so they disagree and nothing moves."""
    where = scenes(10, 13)
    got = judged(lambda i: 0.38 if i in where else 0.0, delay={i: -0.17 for i in where}, ons=onsets())
    assert got["blocks"] == [] and got["parts"][0]["onsets"]["verdict"] == "disagree", got
    assert abs(got["parts"][0]["onsets"]["shift"] - 0.38) <= 0.05, got


def test_the_onsets_time_a_cue_of_a_block_where_the_block_puts_it():
    """A block 1.5 s late: each cue's speech starts 1.5 s before its start, outside ONSET_REACH of it. The onset is
    sought where the block's shift puts the speech, so the onsets agree."""
    at = {k: 10.0 * k + (1.5 if k < 4 else 0.0) for k in range(8)}
    ons = [(10.0 * k + 0.05, 0.8) for k in range(8)]
    got = s.clock(ons, at, range(4), range(4, 8), 1.5)
    assert {k: got[k] for k in ("verdict", "inside", "original", "outside", "shift")} == {"verdict": "agree", "inside": 4, "original": 0, "outside": 4, "shift": 1.5}


def test_an_anchor_nearer_the_line_never_ends_the_run_of_a_small_block():
    """A run 0.55 s off its part's line. An anchor at 0.26 s lies within TOLERANCE of the run, but nearer the line, as
    the first in-time cue after a small block can. The run never ends on it, so that cue never moves with the block."""
    run = [0.5, 0.55, 0.6, 0.55, 0.6, 0.52]
    assert s.agreeing(run, 0.0) and not s.agreeing(run + [0.26], 0.0) and not s.agreeing([0.26] + run, 0.0)
    assert s.widest(run + [0.26], 0.0, 0, 7) == (0, 6)



# --- the gates of blocks(), one test for each ----------------------------------------------------------------------------

def moved_lines(trk, got):
    return {i for i, c in enumerate(trk) for b in got["blocks"] if b["from"] <= c[0] < b["to"] and round(c[0], 3) not in b.get("keep", ())}


HUSHED = set(range(0, LINES, SCENE))   # the first line of each scene: it never anchors, so it stays unproved in a block


def keeps_order(trk, got, fix=None):
    """Whether the cue starts of trk, sorted, still rise after the block moves, each on its own centisecond."""
    shift = lambda c: next((s.shift_of(b, c) for b in got["blocks"] if b["from"] <= c < b["to"] and round(c, 3) not in b.get("keep", ())), 0.0)
    new = [s.written(c, shift(c), fix) for c, _, _ in sorted(trk)]
    return all(a < b for a, b in zip(new, new[1:]))


def quiet(trk, part, noise=0.03, **kw):
    """blocks() of trk heard in full over part, with Whisper's error up to noise seconds."""
    return s.blocks(hear(s.dense(trk, part, LENGTH), noise, **kw), trk, "eng", {"fix": None}, part)


@pytest.mark.parametrize("end", [False, True])
def test_a_block_at_a_file_edge_keeps_the_heard_cues_on_the_line_beside_it(end):
    """Two lines in time lie between the file's edge and a block 1.5 s late. Their anchors sit nearer the line, so they
    stay, and they count as placed on the line."""
    n = len(RIGHT)
    where = range(n - 36, n - 2) if end else range(2, 36)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[n - 50], LENGTH)] if end else [(0.0, AT[50])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True))
    lands(trk, None, got, where)
    assert got["blocks"][0]["edge_right" if end else "edge_left"] == 0, got   # on the line, so not counted


def test_a_block_with_cues_at_its_edges_that_disagree_never_moves():
    """Six cues sit 1.5 s late, and the cue at each side of them 0.95 s. Those two lie in the block by their words,
    and then under AGREE of its heard cues agree. Nothing moves."""
    trk = track(late=lambda i: 1.5 if 120 <= i < 126 else 0.95 if i in (119, 126) else 0.0)
    got = quiet(trk, [(AT[100], AT[150])])
    assert got["blocks"] == [], got
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.95 if 110 <= i < 120 else 0.0)
    assert moved_lines(trk, quiet(trk, [(AT[90], AT[175])])) <= set(scenes(10, 13))   # the cues 0.95 s late never move with it


def test_a_block_under_block_shift_against_the_cues_around_it_never_moves():
    """A block 0.75 s late on a line, with the six cues at each side of it 0.28 s late. Against those cues it sits
    0.47 s off, under BLOCK_SHIFT, though the onsets agree that it sits 0.75 s off the rest. Nothing moves."""
    where, near = scenes(10, 13), set(range(114, 120)) | set(range(156, 162))
    trk = track(late=lambda i: 0.75 if i in where else 0.28 if i in near else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets())
    assert got["blocks"] == [] and " sit +0.4" in got["parts"][0]["why"] and "off the cues around them" in got["parts"][0]["why"], got


def test_a_block_move_never_puts_its_last_cue_past_the_cue_after_it():
    """The block of lines 120 to 143 sits 2.5 s early. Line 144 sits on the line and is spoken right before line 143,
    so its cue starts just after the cue of line 143. A move of the block would put line 143 after it."""
    at = [a - (2.0 if i < 143 else 0.0) for i, a in enumerate(AT)]
    at[144] = AT[143] - 1.9
    trk = sorted(track(late=lambda i: -2.5 if 120 <= i <= 143 else 0.0, at=at))
    part, c = [(at[100], at[168])], AT[143]
    grid = [a for a in s.dense(trk, part, LENGTH) if a + s.WINDOW <= c - 12 or a >= c + 20]
    heard_ = hear(grid, at=at, noise=0.03) + hear([c - 12], at=at, secs=11.9, noise=0.03) + hear([c], at=at, secs=20.0, noise=0.03)   # one window hears each
    got = s.blocks(heard_, trk, "eng", {"fix": None}, part, [(a + LEAD, 1.0) for a in at])
    lines = moved_lines(trk, got)
    assert keeps_order(trk, got) and lines <= set(range(len(trk))) - {k for k, c in enumerate(trk) if c[0] >= AT[143] - 1.0}, (sorted(lines)[-3:], got)


def test_a_window_under_match_names_no_anchor():
    """A window heard the first words of the two lines before the block among words the cues never hold. It matched
    under MATCH, so it names no anchor, and the block still moves."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.0)
    words = RIGHT[117].rstrip(".").split()[:2] + RIGHT[118].rstrip(".").split()[:2] + [f"qx{k}" for k in range(8)]
    chance = {"at": AT[105], "words": [[1.0 + 0.3 * k, w] for k, w in enumerate(words)]}
    got = s.blocks(hear(s.dense(trk, [(AT[100], AT[175])], LENGTH), mute={117, 118}) + [chance], trk, "eng", {"fix": None}, [(AT[100], AT[175])])
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, scenes(10, 13))


@pytest.mark.parametrize("late, off", [(0.42, range(115, 120)), (-0.42, range(115, 120)), (-0.42, range(116, 120))])
def test_cues_off_the_line_before_a_block_hide_its_start(late, off):
    """Four or five lines before a block 1.5 s late sit 0.42 s off the line, more than TOLERANCE. Of the six nearest
    anchors on that side, only one or two then lie on the line, and the median of the nearest three and of all six lies
    0.42 s off. So the start of the block is not seen. Late, the lines also make a stretch off the line the way of the
    block, see stretch(). Early, they do not, and the six nearest anchors alone decide."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else late if i in off else 0.0)
    got = quiet(trk, [(AT[100], AT[175])])
    assert got["blocks"] == [] and "before the cues" in got["parts"][0]["why"] and "sit off the line too" in got["parts"][0]["why"], got


@pytest.mark.parametrize("off, side", [((116, 117, 118), "before"), ((158, 159, 160), "after")])
def test_a_stretch_off_the_line_beside_a_block_hides_its_edge(off, side):
    """Beside a block 1.5 s late, the nearest line with an anchor sits on the line, the next three sit 0.45 s late, and
    the three after them on the line again. Three of the six nearest anchors lie on the line, but the three in a row
    between sit IN_LINE_OFF off it the way of the block, as when Whisper hears the end of a block late. So that edge is
    not seen. Line 156 starts a scene and never anchors."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.45 if i in off else 0.0)
    got = quiet(trk, [(AT[100], AT[175])])
    assert got["blocks"] == [] and f"{side} the cues" in got["parts"][0]["why"] and "sit off the line too" in got["parts"][0]["why"], got


@pytest.mark.parametrize("late", [{117: 0.35, 118: -1.6, 119: 0.42}, {116: -0.7, 117: -0.7, 119: -0.7},
                                  {114: -0.6, 115: -0.6, 116: -0.6, 117: -0.6}, {114: 0.4, 117: 0.35, 118: -1.6, 119: 0.42}],
                         ids=["a chance pair", "three of six on the line", "the three nearest on the line", "the median of six on the line"])
def test_the_lines_before_a_block_sit_on_the_line_despite_a_few_far_anchors(late):
    """Some of the six lines before a block 1.5 s late sit off the line, as a chance pair or a mishearing reads. The
    start is still seen when three of the six nearest anchors on that side lie within TOLERANCE of the line, or the
    median of the nearest three, or of all six, does. Each case holds by one of those alone, but the first. The lines
    off the line sit early, against the way of the block, so they make no stretch off the line. The block moves, and
    those lines stay."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else late.get(i, 0.0))
    got = moved_lines(trk, quiet(trk, [(AT[100], AT[175])]))
    assert got and not set(late) & got and got <= set(scenes(10, 13)), sorted(got)


def test_a_cue_short_of_the_block_at_its_edge_stays():
    """The cue at each side of a block 1.5 s late sits 1.1 s late, 0.4 s short of the block. Neither clock puts it
    clearly in the block, so it stays, and the block moves."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 1.1 if i in (119, 156) else 0.25)
    got = moved_lines(trk, quiet(trk, [(AT[100], AT[175])], 0.02))
    assert got and not {119, 156} & got and got <= set(scenes(10, 13)), got


@pytest.mark.parametrize("k", [119, 156])
def test_an_anchor_nearer_the_part_s_line_stays_at_each_edge(k):
    """The track sits 0.25 s late, and a block 1.5 s late. The cue at one side of the block sits 0.8 s late: nearer the
    part's line, 0.25 s, than the block. Against 0 s it would sit nearer the block. It stays, as an anchor on the
    line."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.80 if i == k else 0.25)
    got = s.blocks(hear(s.dense(trk, [(AT[100], AT[175])], LENGTH), 0.02), trk, "eng", {"fix": None}, [(AT[100], AT[175])], onsets(every=True))
    lines = moved_lines(trk, got)
    assert k not in lines and lines and lines <= set(scenes(10, 13)), got
    # It bounds the block as an anchor on the line, so only the first cue of the block's scene stays and counts.
    assert got["blocks"][0]["edge_left" if k == 119 else "edge_right"] == 1, got


def test_a_part_needs_in_line_heard_cues_to_be_in_line():
    trk = track()
    got = quiet(trk, [(AT[200], AT[209])], 0.2)   # about 8 heard cues, under IN_LINE
    assert 6 <= got["parts"][0]["anchors"] < 12 and not got["parts"][0]["in_line"], got


def test_the_anchor_steps_back_only_over_the_same_word():
    """The cue starts "And the", and Whisper hears "but the". The anchor steps back to "the" and stops there."""
    lines = [" ".join(["And", "the"] + x.rstrip(".").split()[:3]) + "." for x in RIGHT]
    track_, starts = cues(lines), [500.0 + 5 * k for k in range(8)]
    said_ = [(a, "but" if w == "And" else w) for a, w in spoken(lines)]
    at, _ = s.heard_anchors([window(a, said_) for a in starts], track_, STOPS)
    assert len(at) >= 10 and all(abs(x - (track_[i][0] + LEAD + STEP)) <= 0.01 for i, x in at.items()), at


def test_a_part_inside_another_merges_whole():
    assert s.merged([(100.0, 400.0), (150.0, 200.0)]) == [[100.0, 400.0]]


@pytest.mark.parametrize("shape", ["three in a row", "two of three"])
def test_each_rule_of_a_part_in_line_holds_alone(shape):
    """Three cues 0.6 s late in a row: no 6 in a row sit BLOCK_SHIFT off at their median, but 3 in a row sit
    IN_LINE_OFF off. Twelve cues 0.6 s late but every third in time: no 3 in a row sit off, but 6 in a row do at their
    median, and they never agree as a block does. Neither part is in line, and nothing moves."""
    if shape == "three in a row":
        late = lambda i: 0.6 if 200 <= i < 203 else 0.0
    else:
        late = lambda i: 0.0 if not 200 <= i < 212 or (i - 200) % 3 == 2 else 0.6
    trk = track(late=late)
    got = quiet(trk, [(AT[190], AT[222])])
    assert got["blocks"] == [] and not got["parts"][0]["in_line"], got
    assert quiet(track(), [(AT[190], AT[222])])["parts"][0]["in_line"]



def test_a_part_cut_to_the_cap_keeps_its_row_farthest_off():
    """Nine suspect rows from 5:00 to 13:00. The first sits 2 s off, the rest 0.8 s. The part holds 730 s, over
    BLOCK_HEAR, and the rows alone hold 490 s. The 360 s it keeps lie around the row farthest off, the row that ranked
    it, and never around the middle of the rows, which would leave that row unheard."""
    rows = [row(180.0, 0.0), row(300.0, 2.0)] + [row(360.0 + 60 * k, 0.8) for k in range(8)] + [row(900.0, 0.0)] + CALM
    assert s.suspects(rows, 3600) == [(180.0, 540.0)]
    late = [row(180.0, 0.0)] + [row(300.0 + 60 * k, 0.8) for k in range(8)] + [row(780.0, 2.0), row(900.0, 0.0)] + CALM
    assert s.suspects(late, 3600) == [(550.0, 910.0)]   # the row farthest off at the end: the stretch stays inside the part


def test_a_row_that_matched_under_match_is_no_suspect():
    """A row 1.5 s off that matched 24% of its heard words may hear other speech than the track's. It starts no
    dense hearing. The same row at MATCH does."""
    rows = lambda overlap: [row(1500.0, 0.1), row(1631.1, 1.5, 12, 2, overlap), row(1751.1, -0.1)]
    assert s.suspects(rows(0.24), 6000) == [] and s.suspects(rows(0.5), 6000) == [(1511.1, 1761.1)]


# --- shapes from the reviews of the edges, the onset clock and the trigger ----------------------------------------------

def test_a_cue_in_time_that_reads_like_the_block_at_its_edge_never_moves():
    """A block 0.7 s early runs mid-scene from line 125 to 150. Whisper hears the line on each side of it, both in time,
    0.45 s late, so their anchors read like the block. An end of a run sits BLOCK_SHIFT off the line itself, and their
    words fit both sides, so both lines stay."""
    where = range(125, 151)
    trk = track(late=lambda i: -0.7 if i in where else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={124: 0.45, 151: 0.45}), trk, "eng", {"fix": None}, part, onsets(every=True))
    lands(trk, None, got, where)


def strays(rate=0.2, seed=7):
    """(time, seconds of silence) of onsets that are no speech of a cue: other sounds that end a silence."""
    r = random.Random(seed)
    return [(r.uniform(0, LENGTH), r.uniform(0.5, 2.0)) for _ in range(int(LENGTH * rate))]


@pytest.mark.parametrize("real", [False, True])
def test_the_onsets_count_at_both_places_so_stray_onsets_never_carry_a_move(real):
    """Whisper hears three scenes of a right track 1.2 s early, so they seem a block 1.2 s late. Stray onsets land where
    that block would put the cues as often as anywhere, and the cues keep their own onsets where they sit, so the onsets
    disagree and nothing moves. A real block 1.2 s late has its onsets where the block puts its cues, so it moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.2 if real and i in where else 0.0)
    part = [(AT[100], AT[175])]
    delay = {} if real else {i: -1.2 for i in where}
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay=delay), trk, "eng", {"fix": None}, part, sorted(onsets(every=True) + strays()))
    if real:
        assert got["blocks"] and got["blocks"][0]["onsets"]["verdict"] == "agree", got
        lands(trk, None, got, where)
    else:
        assert got["blocks"] == [] and got["parts"][0]["onsets"]["verdict"] == "disagree", got


def test_a_row_over_a_stretch_no_heard_cue_covers_keeps_its_time():
    """A part in line holds a block of 6 cues 1.2 s early that dense hearing anchored none of. The sweep row over it
    keeps its time, so its alert rule still holds. A row whose window holds 2 heard cues takes the part's median."""
    part = {"lo": 300.0, "hi": 600.0, "in_line": True, "median": 0.05, "anchored": [302.0 + 7.5 * k for k in range(40) if not 395 <= 302.0 + 7.5 * k <= 420]}
    rows = [row(400.0, -1.2), row(450.0, 0.7)]
    assert [r["off"] for r in s.after_blocks(rows, [], {"fix": None}, [part])] == [-1.2, 0.05]


def test_one_word_that_two_windows_hear_counts_once():
    """Two windows that overlap hear the same words of a line. placed() takes one time a word, however many windows
    heard it, so one chance word never counts twice."""
    i, track_ = 250, cues(RIGHT)
    a = FIRST + GAP * i - 2.0
    _, words_at = s.heard_anchors([window(a, spoken()), window(a - 3.0, spoken())], track_, STOPS)
    assert words_at[i] and all(len(ts) == 2 for ts in words_at[i].values()), words_at[i]
    assert sorted(words_at[i]) == list(range(len(words_at[i]))), words_at[i]   # one entry a word of the cue, by its place


def test_suspect_rows_and_centred_serve_heard_parts():
    rows = [row(300.0, -0.2), row(360.0, 0.4), row(420.0, -0.2)] + [dict(r, off=-0.2, offset=-0.2) for r in CALM]
    assert s.suspect_rows(rows) == {360.0: pytest.approx(0.6)}   # 0.6 s off the track's lean of -0.2 s
    assert s.centred(100.0, 900.0, {300.0: 0.8, 700.0: 2.0}, 200.0) == (605.0, 805.0)   # around the row farthest off
    assert s.centred(100.0, 900.0, {300.0: 0.8}, 5.0) == (300.0, 300.0)   # under WINDOW left: no length, at that row


def test_a_row_is_judged_against_the_track_s_lean():
    """The rows of a track lean 0.2 s early. A row 0.35 s late sits 0.55 s off that lean, so it is a suspect, though it
    sits under BLOCK_SHIFT off the fitted line. A row 0.25 s early sits on the lean."""
    calm = [dict(r, off=-0.2, offset=-0.2) for r in CALM]
    assert s.suspects([row(1631.1, 0.35)] + calm, 6000) == [(1511.1, 1761.1)]
    assert s.suspects([row(1631.1, -0.25)] + calm, 6000) == []


def test_anchors_of_the_block_left_out_of_its_run_never_stand_for_the_line():
    """A block of 3 minutes sits 1.2 s late, and Whisper hears three of its cues near its end 0.5 s further off, so the
    run leaves them out. The sides of the block are the nearest anchors on the line beyond them, so the block moves."""
    where = scenes(10, 15)
    trk = track(late=lambda i: 1.2 if i in where else 0.0)
    part = [(AT[100], AT[195])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={i: -0.5 for i in (174, 175, 176)}), trk, "eng", {"fix": None}, part,
                   onsets(every=True))
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


def test_the_first_cue_after_a_scene_cut_never_stands_for_the_line():
    """A block 2 s early ends at a scene cut. The three cues after the cut read +0.41, +0.01 and +0.35 s, as the first
    cue after a long silence reads late. That cue never anchors, so the side after the block is the next three, on the
    line, and the block moves."""
    where = scenes(10, 12)
    after = {144: 0.41, 145: 0.01, 146: 0.35}
    trk = track(late=lambda i: -2.0 if i in where else after.get(i, 0.0))
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True))
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


def test_on_whisper_alone_the_outermost_heard_cue_at_each_end_stays():
    """With no onset, a block 1.5 s late moves on Whisper alone. A cue in time at its edge can read like it, so the
    outermost heard cue at each end stays. Line 120 starts the scene: its words never put it in the block."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    got = moved_lines(trk, forced(lambda i: 1.5 if i in where else 0.0))
    assert not {120, 121, 155} & got and {122, 154} <= got, sorted(got)


@pytest.mark.parametrize("timed", [(125, 130, 135), range(123, 149, 5), range(123, 150, 3)])
def test_onsets_on_too_few_cues_of_a_block_are_few(timed):
    """A block 0.8 s late, with an onset on 3, 6 or 9 of its 36 cues. 3 and 6 lie under ONSET_SHARE of them, so the
    onsets are few, and a block under BLOCK_ALONE stays. 9 is enough, so the onsets agree, and the block moves."""
    where = scenes(10, 13)
    ons = onsets(keep=lambda i: i not in where or i in timed, every=True)
    got = forced(lambda i: 0.8 if i in where else 0.0, 100, 175, ons)
    if len(timed) < 9:
        assert got["blocks"] == [] and got["parts"][0]["onsets"]["verdict"] == "few", got
    else:
        assert [b["onsets"]["verdict"] for b in got["blocks"]] == ["agree"], got


@pytest.mark.parametrize("case", ["clean", "onset 0.25 s off", "a second onset near", "anchor 0.2 s off", "two without onsets",
                                  "early, two without onsets", "early, the last two without onsets", "early 0.8 s, two without onsets"])
def test_an_edge_of_a_block_needs_both_clocks_on_it(case):
    """A block 1.2 s late, timed by onsets that agree. Line 120 starts the scene and stays. The block starts at line 121
    when its anchor lies within half of TOLERANCE of the block, and one more thing puts it there: an onset within half
    of TOLERANCE of where the block puts it, alone within ONSET_REACH, two heard words too early for the line, see
    placed(), or an anchor BLOCK_ALONE off the line beside the anchor of line 122 in the block. Else the edge moves in to
    the next such cue. The end at line 155 is judged the same way. A block early has no such words, because a word
    heard late proves nothing. So a block 0.8 s early needs the onset at its edge."""
    where = scenes(10, 13)
    trk = track(late=lambda i: (-0.8 if "0.8" in case else -1.2 if case.startswith("early") else 1.2) if i in where else 0.0)
    ons = onsets(every=True)
    near = lambda i: AT[i] + LEAD
    if case == "onset 0.25 s off":
        ons = [(t + 0.25, q) if abs(t - near(121)) < 0.01 else (t, q) for t, q in ons]
    if case == "a second onset near":
        ons = sorted(ons + [(near(121) + 0.6, 0.8)])
    if case.endswith("two without onsets"):
        ons = [(t, q) for t, q in ons if abs(t - near(121)) > 0.01 and abs(t - near(122)) > 0.01]
    if case.endswith("the last two without onsets"):
        ons = [(t, q) for t, q in ons if abs(t - near(154)) > 0.01 and abs(t - near(155)) > 0.01]
    delay = {121: 0.2} if case == "anchor 0.2 s off" else {}
    part = [(AT[100], AT[175])]
    got = moved_lines(trk, s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay=delay), trk, "eng", {"fix": None}, part, ons))
    stays = {"anchor 0.2 s off": {121}, "early 0.8 s, two without onsets": {121, 122}}.get(case, set())
    assert 120 not in got and not stays & got and (set(range(121, 125)) - stays) <= got and {154, 155} <= got, (case, sorted(got)[:5])


def test_a_chance_pair_past_unheard_lines_never_joins_a_block():
    """A block 1.2 s early ends at line 155. Whisper hears none of lines 156 to 160, and hears line 161 1.2 s late, as a
    chance pair reads. So the anchor of line 161 lies in the block, BLOCK_ALONE off the line, and its onset lies where it
    sits. No anchor of the next line in stands beside it, so it stays, with the unheard lines before it."""
    where = scenes(10, 13)
    trk = track(late=lambda i: -1.2 if i in where else 0.0)
    part = [(AT[100], AT[175])]
    heard = hear(s.dense(trk, part, LENGTH), 0.03, mute=range(156, 161), delay={161: 1.2})
    got = moved_lines(trk, s.blocks(heard, trk, "eng", {"fix": None}, part, onsets(every=True)))
    assert got and not set(range(156, 162)) & got and got <= set(where), sorted(got)[-5:]


def test_a_track_that_leans_late_keeps_its_line():
    """Every cue of the track sits 0.4 s late, more than TOLERANCE off the fitted line, and a block 1.5 s later still.
    The part's line follows the lean, and the sides of the block are judged against it, so the block moves to the lean."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 0.4 + (1.5 if i in where else 0.0))
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True), swept(trk, noise=0.03))
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 1.5) <= 0.1, got
    lands(trk, None, got, where, at=[a + 0.4 for a in AT])


@pytest.mark.parametrize("late, moves", [(0.6, False), (0.75, True)])
def test_a_block_sits_off_the_track_s_lean_too(late, moves):
    """The track leans 0.2 s late, and lines 95 to 179 sit on the fitted line, within TOLERANCE of that lean. A block
    0.6 s late there sits 0.4 s off the lean, as cues an author set late can, so it stays. One 0.75 s late moves, by
    its shift against the lean, the smaller of that and its shift against the line beside it."""
    where = scenes(10, 11)
    trk = track(late=lambda i: (late if i in where else 0.0) if 95 <= i < 180 else 0.2)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True), swept(trk, noise=0.03))
    assert bool(got["blocks"]) == moves and (moves or "off the track's lean of" in got["parts"][0]["why"]), got   # the onsets agree on 0.75 s
    assert not moves or abs(got["blocks"][0]["shift"] - (late - 0.2)) <= 0.1, got["blocks"]


def test_the_sweep_s_own_lead_never_moves_the_track_s_lean():
    """The sweep pairs a cue's first content word, so on a track whose lines open with short words its rows read 0.35 s
    early everywhere. The rows inside the part measure that against dense hearing, so the block still moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    part = [(AT[100], AT[175])]
    rows = [dict(r, off=None if r["off"] is None else r["off"] - 0.35) for r in swept(trk, noise=0.03)]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True), rows)
    assert len(got["blocks"]) == 1 and abs(got["blocks"][0]["shift"] - 1.5) <= 0.1, got


def test_a_part_whose_line_sits_off_the_track_s_lean_moves_nothing():
    """Lines 95 to 179 sit 0.45 s late, a stretch that fills the part, and a block inside it 1.5 s later still. The sweep
    rows outside the part put the track's lean on its fitted line. The part's line sits 0.45 s off that lean, so the cues
    around the block are no line to measure it from, and nothing moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: (0.45 if 95 <= i < 180 else 0.0) + (1.5 if i in where else 0.0))
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True), swept(trk, noise=0.03))
    assert got["blocks"] == [] and "off the track's lean of" in got["parts"][0]["why"], got


def test_a_side_off_the_track_s_lean_moves_nothing():
    """Lines 104 to 119 sit 0.4 s early, and a block 0.8 s late follows on lines 120 to 135. The part's line falls
    between that stretch and the cues in time after the block, so the line of both sides passes. The side before the
    block sits 0.4 s off the track's lean, so nothing moves."""
    trk = track(late=lambda i: 0.8 if 120 <= i < 136 else -0.4 if 104 <= i < 120 else 0.0)
    part = [(AT[104] - 1, AT[150])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.1), trk, "eng", {"fix": None}, part, onsets(every=True), swept(trk, noise=0.03))
    assert got["blocks"] == [] and "off the track's lean of" in got["parts"][0]["why"], got


def test_a_block_never_takes_in_the_cues_in_time_before_it():
    """A live shape: a block 0.7 s late on lines 86 to 135. Whisper hears line 82, in time, 0.42 s late, within
    TOLERANCE of the block, and lines 83 and 84 at 0.14 s and 0 s. An end of a run sits BLOCK_SHIFT off the line itself,
    and no anchor nearer the line lies inside a run, so lines 82 to 85 stay."""
    where = range(86, 136)
    trk = track(late=lambda i: 0.7 if i in where else 0.0)
    part = [(AT[70], AT[150])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={82: -0.42, 83: -0.14}), trk, "eng", {"fix": None}, part, onsets(every=True))
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


def test_the_side_of_a_block_looks_past_its_own_noisy_anchors():
    """A live shape: a block 1.3 s early, whose last heard cues read -1.77, -0.19 and -0.80 s. The run ends before them.
    The side after it takes the nearest anchors that sit nearer the line than the block, past those, so the block moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: -1.3 if i in where else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={151: 0.47, 152: -1.11, 153: -0.5}), trk, "eng", {"fix": None}, part,
                   onsets(every=True))
    assert len(got["blocks"]) == 1, got
    lands(trk, None, got, where)


@pytest.mark.parametrize("shift, moves", [(2.0, True), (1.2, False)])
def test_the_heard_cues_of_a_block_agree_within_a_share_of_its_shift(shift, moves):
    """Whisper hears each line up to 0.5 s early or late. A block 2 s late agrees within BLOCK_SPREAD of its shift,
    0.5 s, and moves. A block 1.2 s late agrees within TOLERANCE only, so under AGREE of its heard cues agree, and it
    stays."""
    where = scenes(10, 13)
    trk = track(late=lambda i: shift if i in where else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.5), trk, "eng", {"fix": None}, part)
    lines = moved_lines(trk, got)
    assert (len(lines) > len(where) / 2 and lines <= set(where)) if moves else got["blocks"] == [] and "agree within" in got["parts"][0]["why"], got


@pytest.mark.parametrize("timed", [True, False])
def test_heard_cues_past_a_block_leave_its_share_when_the_onsets_agree(timed):
    """A block 1.5 s late ends in eight lines 2 s late. Its run ends before them, and their heard words put them in
    the block, so they lie within its edges, past it, and under AGREE of its heard cues agree. With onsets that agree
    they leave the share, and the block moves. On Whisper alone they stay in it, and the block stays."""
    where = scenes(10, 13)
    trk = track(late=lambda i: (2.0 if i >= 148 else 1.5) if i in where else 0.0)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True) if timed else None)
    lines = moved_lines(trk, got)
    assert set(range(122, 145)) <= lines if timed else not set(range(122, 140)) & lines, sorted(lines)   # the 2 s lines may move apart


@pytest.mark.parametrize("late, block", [(2.6, range(122, 156)), (-2.6, range(120, 148))])
def test_on_whisper_alone_the_outermost_heard_cue_stays_when_the_move_would_pass_it(late, block):
    """A block 2.6 s late from line 122, or 2.6 s early to line 147, mid-scene, with no onsets. On Whisper alone the
    outermost heard cue at each end stays, and the lines are 2.5 s apart, so the next line of the block would move past
    it. The late block then does not move. Within the early block's last 2 heard cues lies the scene pause before line
    144, which the move closes to 2.7 s and so reads as the mark of an edit. Its end moves in there, and only lines up
    to 143 move. Line 121 shows 3 s, so line 122 ends no long silence."""
    trk = track(late=lambda i: late if i in block else 0.0, show=lambda i: 3.0 if i == 121 else SHOW)
    got = quiet(trk, [(AT[100], AT[175])])
    lines = moved_lines(trk, got)
    assert keeps_order(trk, got) and lines <= set(block) - {block[0], block[-1]}, sorted(lines)


@pytest.mark.parametrize("block, beside, show", [(range(122, 156), 121, SHOW), (range(120, 151), 151, SHOW), (range(122, 156), 121, 2.45),
                                                 (range(120, 151), 151, 2.45)])
def test_a_cue_in_time_beside_an_edit_stays_when_its_speech_reads_like_the_block(block, beside, show):
    """A block 0.8 s early sits beside a line in time whose speech starts 0.8 s after its cue, as an author may set a
    line. Whisper and the onsets then both put that line in the block. Before the block, the first line of the block
    overlaps it by 0.5 s, and after the block, the line beside it starts 1.1 s after the last line ends. The move ends
    that overlap or gap, so it marks the edit, and the line beside it stays. When the line before the edge shows 2.45 s,
    the move leaves only 0.05 s, and that still marks the edit."""
    edge = block[-1] if beside > block[-1] else beside
    trk = track(late=lambda i: -0.8 if i in block else 0.0, show=lambda i: show if i == edge else SHOW)
    ons = [(t + 0.8, q) if abs(t - AT[beside] - LEAD) < 0.01 else (t, q) for t, q in onsets(every=True)]
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={beside: 0.8}), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert beside not in lines and set(block[2:-2]) - HUSHED <= lines <= set(block), sorted(lines)   # a scene's first line has only its onset


def test_an_edit_mark_after_the_trim_keeps_the_cues_before_it():
    """A block 1.2 s late starts at line 124, mid-scene, and has no onsets. Whisper hears lines 121 to 123, in time,
    1.2 s early, so they read like the block and join it at its start. The outermost heard cue then stays, and the start
    sits at line 122. Line 124 starts 1.5 s after line 123 ends, which the move closes to 0.3 s: the mark of the edit,
    among the first EDIT_CUES cues. So lines 121 to 123 stay."""
    where = range(124, 156)
    trk = track(late=lambda i: 1.2 if i in where else 0.0)
    got = quiet(trk, [(AT[100], AT[175])], delay={i: -1.2 for i in (121, 122, 123)})
    lines = moved_lines(trk, got)
    assert not {121, 122, 123} & lines and set(range(126, 154)) - HUSHED <= lines <= set(where), sorted(lines)


def test_a_block_whose_edges_leave_no_heard_cue_stays():
    """A block 0.8 s early, with onsets that agree 0.12 s from where the block puts its lines. A second onset lies 0.6 s
    past each, within ONSET_REACH, so no onset times a cue alone. Words heard late prove nothing, and the block sits
    under BLOCK_ALONE. So no heard cue is clearly in the block at its edges, the edges move in past every heard cue,
    and the block stays."""
    where = scenes(10, 13)
    trk = track(late=lambda i: -0.8 if i in where else 0.0)
    inside = lambda t: AT[where[0]] <= t < AT[where[-1]] + 1
    ons = sorted([(t + 0.12, q) if inside(t) else (t, q) for t, q in onsets(every=True)] + [(t + 0.72, q) for t, q in onsets(every=True) if inside(t)])
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, ons)
    assert got["blocks"] == [] and got["parts"][0]["why"] == "after its edges stay, 0 heard cues are left in it, under 3", got


@pytest.mark.parametrize("timed, mute, room", [(True, (), 1.5), (False, (), 1.5), (True, (150,), 1.5), (True, (), 1.9)])
def test_a_cue_in_time_that_a_late_block_passed_stays(timed, mute, room):
    """A block 2.0 s late ends at line 150, and line 151, in time, starts 1.5 s after line 150 should. So line 150 now
    starts after line 151, within the block's times. Whisper hears line 151 2.0 s early, so its anchor and its words
    read like the block, and line 149 ends before it starts, so no edit mark lies there. Its onset lies where it sits.
    Within the shift of the block's last cue, a cue needs an onset where the block puts it, so line 151 stays. When
    Whisper hears nothing of line 150, line 151 is the block's last cue, and line 150 lies within the shift beyond it.
    When line 151 starts 1.9 s after line 150 should and has no onset of its own, the onset of line 150 lies where the
    block puts line 151 too. Line 150 then starts 0.1 s after it, so that onset may be either's, and it times neither."""
    at = [a - (2.5 - room if i >= 151 else 0.0) for i, a in enumerate(AT)]
    where = range(132, 151)
    trk = track(late=lambda i: 2.0 if i in where else 0.0, at=at, show=lambda i: 1.0 if i == 149 else SHOW)
    part = [(at[100], at[175])]
    ons = sorted((at[i] + LEAD, 1.0) for i in range(LINES) if room < 1.9 or i != 151) if timed else None
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, at=at, delay={151: -2.0}, mute=mute), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert 151 not in lines and set(range(135, 149)) - HUSHED <= lines <= set(where), sorted(lines)


@pytest.mark.parametrize("silent", [125, 126])
def test_an_early_block_needs_onsets_within_its_shift_of_its_first_cue(silent):
    """A block 1.2 s early starts at line 125. Line 125 should start 1.8 s after line 124, and line 126 1.15 s after line
    125, so both lie within the shift of the cue before them. A block early moves toward the cues before it. So its
    first cue, when the cue before it lies within the shift, and each cue within the shift after it, needs an onset
    where the block puts it. The one with no onset stays, with every cue before it."""
    at = [a - (0.7 if i >= 125 else 0.0) - (1.35 if i >= 126 else 0.0) for i, a in enumerate(AT)]
    where = range(125, 141)
    trk = track(late=lambda i: -1.2 if i in where else 0.0, at=at, show=lambda i: 1.0 if i == 125 else SHOW)   # no edit mark between
    part = [(at[100], at[175])]
    ons = sorted((at[i] + LEAD, 1.0) for i in range(LINES) if i != silent)
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, at=at), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert not set(range(124, silent + 1)) & lines and set(range(silent + 1, 139)) - HUSHED <= lines <= set(where), sorted(lines)


def test_a_move_never_ties_two_starts_in_centiseconds():
    """With the ratio fix 25025/24000, a cue at 216.40 s that moves by 1.294 s lands 10.3 ms after a cue at 215.04 s that
    stays. remux.time_plan() writes ASS in centiseconds, where both start at 203.36 s, so the move would tie them. The
    test uses the written times. A move by 1.25 s keeps them apart."""
    fix = {"rate": "25025/24000", "offset": 3.0}
    assert s.written(216.40, 1.294, fix) == s.written(215.04, 0.0, fix) == 20336
    assert s.crossing([215.04, 216.40], {1}, 1, 1, 1.294, fix) == (1, 0) and s.crossing([215.04, 216.40], {1}, 1, 1, 1.25, fix) is None
    assert s.crossing([10.0, 12.0], {1}, 1, 1, 2.5, None) == (1, 0) and s.crossing([10.0, 12.0, 14.0], {1, 2}, 1, 2, 1.5, None) is None
    assert s.crossing([10.0, 12.0, 14.0], {0, 1}, 0, 1, -2.5, None) == (1, 2)   # an early cue moved past the cue after it


def test_a_block_must_sit_off_the_fitted_line_too():
    """The lines around a block sit 0.3 s early against the fitted line, and the block 0.25 s late: 0.55 s off the
    cues around it, with onsets that agree. But it sits under BLOCK_SHIFT off the fitted line, as cues in time do when a
    block pulls the part's line away. So it stays."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 0.25 if i in where else -0.3)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True))
    assert got["blocks"] == [] and "off the fitted line" in got["parts"][0]["why"], got


def test_an_onset_two_reads_found_counts_once():
    """Two reads of the same audio find each onset twice, 0.02 s apart. blocks() counts each once, so the onsets still
    agree, and a block 0.8 s late, under BLOCK_ALONE, moves."""
    where = scenes(10, 13)
    ons = onsets(every=True)
    got = forced(lambda i: 0.8 if i in where else 0.0, 100, 175, sorted(ons + [(t + 0.02, q - 0.1) for t, q in ons]))
    assert [b["onsets"]["verdict"] for b in got["blocks"]] == ["agree"], got


def test_on_whisper_alone_edge_cues_under_block_alone_stay():
    """A block 1.2 s late has no onsets, and Whisper hears its last three lines 0.25 s late. They read 0.95 s off the
    line, within the block's run but under BLOCK_ALONE. On Whisper alone the outermost heard cue stays, and so does each
    next one that reads under BLOCK_ALONE off the line. Lines 153 to 155 stay, and the rest of the block moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.2 if i in where else 0.0)
    got = quiet(trk, [(AT[100], AT[175])], delay={i: 0.25 for i in (153, 154, 155)})
    lines = moved_lines(trk, got)
    assert not {153, 154, 155} & lines and set(range(122, 152)) - HUSHED <= lines <= set(where), sorted(lines)


@pytest.mark.parametrize("late, where, beside, mute", [(-0.6, range(125, 156), 122, (123, 124)), (0.6, range(108, 131), 133, (131, 132))])
def test_the_edit_mark_window_counts_heard_cues(late, where, beside, mute):
    """A block 0.6 s early starts at line 125, mid-scene. Line 122, in time, has its speech 0.6 s after its cue, so both
    clocks put it in the block, and Whisper hears nothing of lines 123 and 124. So the run starts at line 122. The edit
    mark, line 125 overlapping line 124 by 0.3 s, lies three cues in, but within the first 2 heard cues. The edge moves
    in to it, and lines 122 to 124 stay. A block 0.6 s late that ends at line 130 mirrors that with lines 131 to 133."""
    trk = track(late=lambda i: late if i in where else 0.0)
    ons = [(t - late, q) if abs(t - AT[beside] - LEAD) < 0.01 else (t, q) for t, q in onsets(every=True)]
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, delay={beside: -late}, mute=mute), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert not ({beside} | set(mute)) & lines and set(where[2:-2]) - HUSHED <= lines <= set(where), sorted(lines)


@pytest.mark.parametrize("off, verdict", [(0.1, "agree"), (-0.1, "agree"), (0.2, "disagree"), (-0.2, "disagree")])
def test_the_onsets_agree_only_near_whisper_s_shift(off, verdict):
    """Every cue of a block 1 s late has an onset, off seconds from where Whisper's shift puts it. The search reaches
    0.3 s, and the onsets agree only when they put the block within 0.15 s of Whisper's shift."""
    at = {k: 10.0 * k for k in range(30)}
    ons = sorted([(at[k], 1.0) for k in range(10)] + [(at[k], 1.0) for k in range(20, 30)] + [(at[k] - 1.0 - off, 1.0) for k in range(10, 20)])
    got = s.clock(ons, at, range(10, 20), [*range(10), *range(20, 30)], 1.0)
    assert got["verdict"] == verdict and got["inside"] == 10, got


def test_a_block_of_200_cues_with_onsets_agrees():
    """Every cue of a block of 200 cues 1 s late has its onset. The chance sum takes 200 terms, and chance ** i / i!
    overflows a float past i = 170."""
    at = {k: 2.0 * k for k in range(240)}
    ons = sorted((at[k] - (1.0 if 20 <= k < 220 else 0.0), 1.0) for k in at)
    got = s.clock(ons, at, range(20, 220), [*range(20), *range(220, 240)], 1.0)
    assert got["verdict"] == "agree" and got["inside"] == 200, got

def test_onsets_scattered_over_the_reach_never_agree():
    """A right track that Whisper heard 0.98 s early reads as a block of 16 cues. Four stray onsets lie 0.1 to 0.27 s from
    where the block would put four cues, and one cue has its own onset. The strays meet the count, and their median lies
    near Whisper's shift, but only one lies within half of TOLERANCE of its place, so the onsets are too few."""
    at = {k: 10.0 * k for k in range(40)}
    outside = [*range(12), *range(28, 40)]
    ons = [(at[k], 1.0) for k in outside] + [(at[12], 1.0)] + [(at[k] - 0.98 - d, 1.0) for k, d in zip((14, 17, 20, 23), (-0.27, -0.1, 0.17, 0.24))]
    got = s.clock(sorted(ons), at, range(12, 28), outside, 0.98)
    assert got["verdict"] == "few" and got["inside"] == 4 and got["original"] == 1, got


@pytest.mark.parametrize("hits, verdict", [(4, "few"), (13, "agree")])
def test_onsets_where_the_block_puts_its_cues_must_beat_chance(hits, verdict):
    """A block of 13 cues 0.82 s late among stray onsets at 0.25 a second. At the NULL offsets from where the block puts
    its cues, chance gives about one onset within 0.15 s. Four onsets there are not rare enough, so the onsets are too
    few. Thirteen agree."""
    at = {k: 10.0 * k for k in range(40)}
    inside, outside = range(12, 25), [*range(12), *range(25, 40)]
    r = random.Random(1)
    places = [at[k] - 0.82 for k in inside] + [at[k] for k in inside]
    strays = [(x, 1.0) for x in (r.uniform(0, 400) for _ in range(100)) if all(abs(x - p) > 0.35 for p in places)]
    ons = sorted([(at[k], 1.0) for k in outside] + [(at[k] - 0.82, 1.0) for k in list(inside)[:hits]] + strays)
    got = s.clock(ons, at, inside, outside, 0.82)
    assert got["verdict"] == verdict and got["chance"] >= 0.4, got


def test_onsets_on_a_regular_pitch_still_count_chance():
    """Lines start every 2.5 s, and each line outside a block of 13 cues 0.82 s late has its onset. Every NULL offset
    from where the block puts a cue then lies between two onsets and counts none. The rate of all onsets still gives
    about one by chance within 0.15 s of those places, so four onsets there are too few."""
    at = {k: 2.5 * k for k in range(40)}
    inside, outside = range(12, 25), [*range(12), *range(25, 40)]
    ons = sorted([(at[k], 1.0) for k in outside] + [(at[k] - 0.82, 1.0) for k in (13, 16, 19, 22)])
    got = s.clock(ons, at, inside, outside, 0.82)
    assert got["verdict"] == "few" and got["chance"] >= 1.0, got


def test_evidence_of_a_cue_of_its_own():
    """Evidence for the line wins. The anchor of a cue after a long silence only ever puts it on the line, and its words
    put it in the block only with an onset there. An onset never counts with an anchor off the block or unsure words."""
    on, at = (lambda x: abs(x) <= abs(x - 1.5)), (lambda x: abs(x - 1.5) <= 0.3)
    ev = lambda late=None, hushed_late=None, words=None, new=False, old=False, hushed=False: s.evidence(late, hushed_late, words, new, old, on, at, hushed)
    assert ev(1.5) == ev(words="block") == ev(words="block", hushed=True, new=True) == "block"
    assert ev(0.1) == ev(1.5, old=True) == ev(words="line", new=True) == ev(hushed_late=0.1, words="block") == "line"
    assert ev() is ev(0.9) is ev(words="unsure") is ev(new=True) is ev(words="block", hushed=True) is ev(hushed_late=1.5) is None
    assert ev(0.9, new=True) is ev(words="unsure", new=True) is ev(words="unsure", hushed=True, new=True) is None


def test_a_cue_in_time_between_two_blocks_stays():
    """A block 1.4 s late and a block 1.05 s late lie one line apart, and the onsets agree. Line 132 between them is in
    time and starts a scene, so its anchor never counts, and Whisper hears it on time. Its own anchor puts it on the
    line, so it cuts the run, each block is judged on its own, and line 132 stays."""
    late = lambda i: 1.4 if 120 <= i < 132 else 1.05 if 133 <= i < 150 else 0.0
    trk = track(late=late)
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03), trk, "eng", {"fix": None}, part, onsets(every=True))
    lines = moved_lines(trk, got)
    assert 132 not in lines and lines <= set(range(120, 150)) and len(lines) > 20, (sorted(lines), got["parts"][0]["whys"])


def test_a_cue_with_no_evidence_stays_unproved():
    """On Whisper alone, a block 1.5 s late, where Whisper hears nothing of lines 125 and 140. They have no evidence of
    their own, so they stay. The block names their starts in "keep", counts them as unproved, and the rest moves."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    got = quiet(trk, [(AT[100], AT[175])], mute=(125, 140))
    (b,) = got["blocks"]
    lines = moved_lines(trk, got)
    assert b["unproved"] >= 2 and {round(trk[125][0], 3), round(trk[140][0], 3)} <= set(b["keep"]), b
    assert not {125, 140} & lines and set(range(122, 154)) - {125, 140} - HUSHED <= lines, sorted(lines)


def test_an_onset_where_a_cue_sits_cuts_the_block_there():
    """A block 1.5 s late, timed by onsets that agree. Whisper hears nothing of line 137, and its onset lies, alone,
    where it sits, not where the block puts it. That puts it on the line, so the block splits there into two blocks,
    and line 137 stays."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 1.5 if i in where else 0.0)
    ons = [(t + 1.5, q) if abs(t - AT[137] - LEAD) < 0.01 else (t, q) for t, q in onsets(every=True)]
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, mute=(137,)), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert 137 not in lines and len(got["blocks"]) == 2 and set(range(123, 135)) | set(range(139, 154)) <= lines | HUSHED, (sorted(lines), got)


def test_the_rule_own_evidence_catches_a_cue_moved_with_none(monkeypatch, tmp_path):
    """A planted bug in evidence() puts every cue between a block's edges in the block. Whisper hears nothing of line
    137, so the block would move it with no evidence of its own. The check of the rule raises, see INVARIANTS, and
    writes the case to AMG_INVARIANT_DUMP, with the inputs that call blocks() again."""
    monkeypatch.setattr(s, "evidence", lambda *a: "block")
    monkeypatch.setenv("AMG_INVARIANT_DUMP", str(tmp_path))
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.0)
    part = [(AT[100], AT[175])]
    with pytest.raises(s.Broken, match=f"rule own evidence broken at the cue at {trk[137][0]} s: it has no anchor"):
        s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, mute=(137,)), trk, "eng", {"fix": None}, part, onsets(every=True))
    (case,) = [json.load(open(p)) for p in tmp_path.iterdir()]
    assert case["rule"] == "own evidence" and case["call"]["cues"] == [list(c) for c in trk] and case["call"]["parts"] == [list(p) for p in part]


@pytest.mark.parametrize("cue, ref, move, freed", [
    ((0.45, False, "block", False, False), 0.3, 0.7, False),   # its anchor sits nearer the part's line than the run
    ((None, True, "block", False, False), 0.3, 0.7, True),     # after a long silence, its words move it with no onset there
    ((0.05, True, "block", False, False), 0.3, 0.7, False),    # after a long silence, its anchor sits on the line
    ((0.62, False, "block", False, False), 0.0, 1.0, True),    # its anchor sits over TOLERANCE off the block, with no onset there
])
def test_the_rule_own_evidence_mirrors_evidence(cue, ref, move, freed):
    """Each cue is one that evidence() keeps where it is, and a block that moves it breaks the rule, see check_blocks().
    Its own onset where the block puts it lets the words move the two cues that only lacked one."""
    block = {"from": 0.0, "to": 100.0, "shift": move, "keep": []}
    entry = lambda c: [{"block": block, "mid": 1.0, "tol": 0.3, "m": 1.0, "base": 0.0, "ref": ref, "cues": {0: c}}]
    with pytest.raises(s.Broken, match="rule own evidence broken at the cue at 50.0 s"):
        s.check_blocks([(50.0, 52.0, "x")], [block], entry(cue), {})
    if freed:
        s.check_blocks([(50.0, 52.0, "x")], [block], entry(cue[:4] + (True,)), {})


def test_the_rule_own_evidence_needs_a_cut_at_a_cue_on_the_line():
    """Evidence that puts a cue on the line cuts the block there, see blocks(). A block that moves the cues on both sides
    of such a cue breaks the rule. Its anchor, its anchor after a long silence, its words or its own onset put it there."""
    block = {"from": 0.0, "to": 100.0, "shift": 1.0, "keep": [52.0]}
    cues, moved = [(50.0, 51.0, "a"), (52.0, 53.0, "b"), (54.0, 55.0, "c")], (1.0, False, "block", False, False)
    entry = lambda between: [{"block": block, "mid": 1.0, "tol": 0.3, "m": 1.0, "base": 0.0, "ref": 0.0, "cues": {0: moved, 2: moved}, "between": {1: between}}]
    for between in ((0.1, False, None, False), (0.1, True, None, False), (None, False, "line", False), (None, False, None, True)):
        with pytest.raises(s.Broken, match="rule own evidence broken at the cue at 52.0 s"):
            s.check_blocks(cues, [block], entry(between), {})
    s.check_blocks(cues, [block], entry((None, False, "unsure", False)), {})


def test_the_rule_nearer_its_speech_catches_a_move_off_an_own_onset_or_past_the_speech(monkeypatch):
    """A block 1.5 s late, timed by onsets that agree. Line 137's own onset lies where it sits. A planted bug in
    evidence() drops that onset, so the block moves line 137 away from it, and the check of the rule raises, see
    INVARIANTS. A block moved three times its shift takes its cues past their speech, and the check raises too."""
    trk = track(late=lambda i: 1.5 if i in scenes(10, 13) else 0.0)
    ons = [(t + 1.5, q) if abs(t - AT[137] - LEAD) < 0.01 else (t, q) for t, q in onsets(every=True)]
    part = [(AT[100], AT[175])]
    heard = hear(s.dense(trk, part, LENGTH), 0.03)
    evidence, checked = s.evidence, s.check_blocks
    seen = []
    monkeypatch.setattr(s, "check_blocks", lambda *a: seen.append(a) or checked(*a))
    assert len(s.blocks(heard, trk, "eng", {"fix": None}, part, ons)["blocks"]) == 2   # line 137 cuts the block
    cues, blocks, proved, case = seen[-1]
    blocks[0]["shift"] *= 3
    with pytest.raises(s.Broken, match="rule nearer its speech broken .* after the move"):
        checked(cues, blocks, proved, case)
    monkeypatch.setattr(s, "evidence", lambda *a: evidence(*a[:4], False, *a[5:]))
    with pytest.raises(s.Broken, match=f"rule nearer its speech broken at the cue at {trk[137][0]} s: its own onset lies where it sat"):
        s.blocks(heard, trk, "eng", {"fix": None}, part, ons)


def test_further_counts_a_short_piece_as_a_window():
    """dense() hears a piece under WINDOW seconds as one whole window, so further() counts it as WINDOW seconds."""
    part = lambda lo, hi, more: {"lo": lo, "hi": hi, "more": more}
    results = {"s1": {"parts": [part(100.0, 300.0, [(300.0, 330.0)])]}}
    # 25 of the 30 seconds were heard, and the 5 left cost a whole window: 15 seconds left is enough, 9 is not
    assert s.further(results, [(100.0, 325.0), (400.0, 520.0)], 600.0) == {"s1": [(300.0, 330.0)]}
    assert s.further(results, [(100.0, 325.0), (400.0, 526.0)], 600.0) == {}


def test_a_block_whose_neighbours_sit_off_the_part_s_line_stays():
    """The six lines on each side of a block 0.9 s early sit 0.27 s late, and the rest of the part on the line. The line
    beside the block then sits 0.27 s off the part's line, over half of TOLERANCE, as when a block of the other sign
    lies beside it. A move to it would overshoot, so the block stays."""
    late = lambda i: -0.9 if 132 <= i < 144 else 0.27 if 126 <= i < 132 or 144 <= i < 150 else 0.0
    trk = track(late=late)
    got = quiet(trk, [(AT[100], AT[175])])
    assert got["blocks"] == [] and "off the part's line" in got["parts"][0]["why"], got


def test_the_window_beside_the_edge_reaches_the_shift_and_tolerance():
    """A block 1.0 s late ends at line 149, which starts 1.2 s after line 148 should, past the shift but within the shift
    and TOLERANCE, as Whisper may read the shift short. Line 148 has no onset, so it stays with line 149."""
    at = [a - (1.3 if i >= 149 else 0.0) for i, a in enumerate(AT)]
    trk = track(late=lambda i: 1.0 if 132 <= i <= 149 else 0.0, at=at, show=lambda i: 1.0 if i == 148 else SHOW)
    part = [(at[100], at[175])]
    ons = sorted((at[i] + LEAD, 1.0) for i in range(LINES) if i != 148)
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, at=at), trk, "eng", {"fix": None}, part, ons)
    lines = moved_lines(trk, got)
    assert not {148, 149} & lines and set(range(135, 148)) - HUSHED <= lines, sorted(lines)


def test_an_onset_that_may_be_a_neighbour_s_proves_nothing():
    """A block 2.4 s late, with lines 2.5 s apart, timed by onsets that agree. Whisper hears nothing of line 137, and the
    only onset near lies where it sits. That is also within TOLERANCE of where the block puts line 138, which has no
    onset, so it may be line 138's onset, and it proves nothing about line 137. Line 137 stays as unproved, and the block
    stays whole."""
    where = scenes(10, 13)
    trk = track(late=lambda i: 2.4 if i in where else 0.0)
    ons = [(t + 2.4, q) if abs(t - AT[137] - LEAD) < 0.01 else (t, q) for t, q in onsets(every=True) if abs(t - AT[138] - LEAD) > 0.01]
    part = [(AT[100], AT[175])]
    got = s.blocks(hear(s.dense(trk, part, LENGTH), 0.03, mute=(137,)), trk, "eng", {"fix": None}, part, ons)
    assert len(got["blocks"]) == 1 and round(trk[137][0], 3) in got["blocks"][0]["keep"], got


# --- live captions: each cue to its own speech ------------------------------------------------------------------------

WHOLE = [7.5 * k for k in range(int((LENGTH - 10) // 7.5) + 1)]   # dense hearing of the whole track


def captioned(lag=lambda i: 6.0 + 3.0 * math.sin(i / 9), text=lambda i, x: x):
    """RIGHT as live captions: line i shows lag(i) seconds after its speech at AT[i], until the next line shows."""
    starts = [AT[i] + lag(i) for i in range(LINES)]
    return [(a, b, text(i, x)) for i, (a, b, x) in enumerate(zip(starts, starts[1:] + [starts[-1] + SHOW], RIGHT))]


def landed(trk, got):
    """Where each cue of trk starts after the moves of live_moves(), and whether it moved."""
    shifts = got["blocks"][0]["shifts"] if got["blocks"] else {}
    return [(a - shifts.get(round(a, 3), 0.0), round(a, 3) in shifts) for a, _, _ in trk]


def sweep_of(offs, cues=3):
    return [{"at": 60.0 * k, "words": 15, "overlap": 0.9, "cues": cues, "offset": o, "off": o} for k, o in enumerate(offs, 1)]


def test_live_reads_a_sweep_late_by_a_different_time_each_minute():
    """Rows 3 to 18 s late, 9 s at their median, look live-captioned. Rows of a right track, of a track 2 s late by one
    shift, or scattered around the line, do not. Neither do too few rows. A fix for the whole track leaves the rows on
    its line, and the scatter around it still counts."""
    r = random.Random(4)
    late = [round(9 + r.uniform(-6, 9) * (k % 3 != 0), 2) for k in range(30)]
    assert s.live(sweep_of(late)) == {"lag": 9.0, "off": 9.0, "scatter": s.live(sweep_of(late))["scatter"], "rows": 30}
    assert s.live(sweep_of(late))["scatter"] >= s.LIVE_SCATTER
    right = [round(r.gauss(-0.1, 0.12), 2) for _ in range(30)]
    shifted = [round(2 + r.gauss(0, 0.12), 2) for _ in range(30)]
    around = [round(r.choice((-1, 1)) * r.uniform(0.3, 1.5), 2) for _ in range(30)]
    assert [s.live(sweep_of(x)) for x in (right, shifted, around, late[:s.LIVE_ROWS - 1])] == [None] * 4
    fixed = [dict(w, off=round(w["offset"] - 9, 2)) for w in sweep_of(late)]
    assert s.live(fixed)["off"] == 0.0 and s.live(sweep_of(late, cues=1)) is None


def test_live_takes_fewer_rows_when_each_sits_far_late():
    """Roll-up captions let few rows count. 7 rows from 7 to 11 s late, scattered, look live-captioned. One row under
    LIVE_FAR late, or under LIVE_FEW rows, does not."""
    late = [11.1, 11.0, 9.1, 9.6, 7.0, 8.9, 9.1]
    assert s.live(sweep_of(late)) == {"lag": 9.1, "off": 9.1, "scatter": 0.5, "rows": 7}
    assert s.live(sweep_of(late[:-1] + [2.9])) is None and s.live(sweep_of(late[:s.LIVE_FEW - 1])) is None


def test_each_cue_of_a_live_track_moves_to_its_own_speech():
    """Captions 3 to 9 s late, by a different time each line. Every cue moves to its own speech, and the starts keep
    their order on their own centiseconds. The first line's first word lies before the first window that heard enough,
    so it takes the lag of the line after it."""
    trk = captioned()
    got = s.live_moves(hear(WHOLE, 0.1), trk, "eng", {"fix": None})
    out = landed(trk, got)
    assert all(m and abs(t - AT[i]) <= (0.2 if i else 0.4) for i, (t, m) in enumerate(out)), [(i, t - AT[i]) for i, (t, m) in enumerate(out) if abs(t - AT[i]) > 0.2]
    new = [s.written(a, a - t, None) for (a, _, _), (t, _) in zip(trk, out)]
    assert all(x < y for x, y in zip(new, new[1:])) and got["live"]["fixed"] and got["live"]["left"] == 0, got["live"]


def test_a_cue_with_no_anchor_moves_between_the_anchors_around_it():
    """Whisper mishears the first word of a few lines, so they never anchor. Their speech lies between the speech of
    the lines around them, so each takes its place between their anchors. A speaker's name before a line is never
    spoken, so a line that starts with one still anchors at its first spoken word. A line whose first word, a name, is
    said again soon after moves between anchors too, see unsure()."""
    lost = {40, 41, 90, 200, 201, 202}
    trk = captioned(text=lambda i, x: f">> Reporter: {x}" if i % 5 == 0 else x)
    got = s.live_moves(hear(WHOLE, 0.1, lost=lost), trk, "eng", {"fix": None})
    out = landed(trk, got)
    assert all(m and abs(t - AT[i]) <= (1.0 if i in lost else 0.4 if i == 0 else 0.2) for i, (t, m) in enumerate(out)), got["live"]
    assert got["live"]["between"] + got["live"]["agree"] >= len(lost) and got["live"]["own"] >= LINES - len(lost) - 40, got["live"]


def test_roll_up_captions_anchor_on_their_new_line():
    """Roll-up captions show each new line under the line before, so each cue's top line repeats the last cue's bottom
    line, spoken a line earlier. The repeated line leaves the anchoring text, so each cue moves to the speech of its new
    line, not one line early."""
    roll = [(a, b, (RIGHT[i - 1] + "\n" if i else "") + x) for i, (a, b, x) in enumerate(captioned())]
    got = s.live_moves(hear(WHOLE, 0.1), roll, "eng", {"fix": None})
    out = landed(roll, got)
    assert all(m and abs(t - AT[i]) <= 0.4 for i, (t, m) in enumerate(out)), [(i, round(t - AT[i], 2)) for i, (t, m) in enumerate(out) if abs(t - AT[i]) > 0.4][:5]
    assert s.rolled([(0, 1, "A b\\Nc d"), (1, 2, "c d\\Ne f"), (2, 3, "e f"), (3, 4, "x\ny\nz"), (4, 5, "y\nz\nw")]) == ["A b\\Nc d", "e f", "e f", "x\ny\nz", "w"]


def test_a_hearing_that_stopped_part_way_moves_no_cue_past_it():
    """The hearing heard the first half of the audio, then failed. The cues past it have no anchor, and the lag of the
    last anchors says nothing about them, so they stay and count as left, and the track is not fixed. The cues of the
    heard half move to their speech."""
    half = hear(WHOLE[:len(WHOLE) // 2], 0.1)
    end = half[-1]["at"] + s.WINDOW
    trk = captioned()
    got = s.live_moves(half, trk, "eng", {"fix": None})
    out = landed(trk, got)
    assert not any(m for (t, m), (a, _, _) in zip(out, trk) if t > end) and not got["live"]["fixed"], got["live"]
    assert sum(m and abs(t - AT[i]) <= 0.2 for i, (t, m) in enumerate(out)) >= sum(AT[i] < end - 5 for i in range(LINES)) - 5, got["live"]
    assert got["live"]["left"] >= sum(a - 9.5 > end for a, _, _ in trk), got["live"]   # live lag is 3 to 9 s


def test_an_anchor_whose_word_is_heard_again_soon_after_moves_no_cue_the_wrong_way():
    """A mild live track sits 1.5 s late. The captioner wrote the first "yeah" as "Yes." and the second as "Yeah.".
    The match keeps one "yeah" of the two said in a row, the first, so the "Yeah." cue anchors on the line before. A
    move to that anchor would land it 2.5 s early. The word is heard again 2.5 s later, and the cue sits under twice
    that late, so it moves only as a cue with no anchor, and never farther from its speech."""
    said = {39: "Yes.", 40: "Yeah."}
    trk = captioned(lag=lambda i: 1.5, text=lambda i, x: said.get(i, x))
    heard = hear(WHOLE, 0.05, mute=(39, 40), extra=[(AT[i] + LEAD, "Yeah.") for i in (39, 40)])
    got = s.live_moves(heard, trk, "eng", {"fix": None})
    t, m = landed(trk, got)[40]
    assert abs(t - AT[40]) <= 1.5 + 0.01, (t - AT[40], m)


def test_rising_keeps_the_longest_run_of_anchors_in_order():
    """An anchor that pairs a word said elsewhere breaks the order of the speech, and only the longest rising run counts."""
    assert s.rising([(0, 1.0), (1, 5.0), (2, 2.0), (3, 3.0), (4, 4.0), (5, 9.0)]) == {0, 2, 3, 4, 5}
    assert len(s.rising([(0, 1.0), (1, 1.0)])) == 1 and s.rising([]) == set()   # two anchors at one time never both count


def test_a_cue_near_its_speech_stays():
    """Captions 0.3 s late sit within BLOCK_SHIFT of their speech, so nothing moves, and the track counts as fixed."""
    got = s.live_moves(hear(WHOLE, 0.05), captioned(lag=lambda i: 0.3), "eng", {"fix": None})
    assert got["blocks"] == [] and got["live"]["fixed"] and "none moved" in got["parts"][0]["why"], got["live"]


def test_a_cue_with_no_proof_moves_only_where_the_cue_after_it_proves_it():
    """The lines sit in time up to line 149 and 9 s late from line 151 on, as a live part after a taped one. Line 150 sits
    4 s late and has no anchor. Line 149 does not move, so line 150 has no two moving neighbours to agree, and the span
    between their speech does not prove the place between them. As it stands, line 151 would pass it. Its speech comes
    before line 151's, so it takes the nearest place to its guess that is nearer every speech up to that anchor. Then
    the lines after it move, and the order of the cues never changes."""
    trk = captioned(lag=lambda i: 0.2 if i < 150 else 4.0 if i == 150 else 9.0)
    got = s.live_moves(hear(WHOLE, 0.05, lost={150}), trk, "eng", {"fix": None})
    out = landed(trk, got)
    new = [s.written(a, a - t, None) for (a, _, _), (t, _) in zip(trk, out)]
    assert not any(m for t, m in out[:150]) and all(m and abs(t - AT[i]) <= 0.2 for i, (t, m) in enumerate(out) if i > 150), got["live"]
    assert out[150][1] and abs(out[150][0] - AT[150]) < abs(trk[150][0] - AT[150]) and all(x < y for x, y in zip(new, new[1:])), out[148:153]
    assert got["live"]["fixed"], got["live"]


def test_a_move_that_would_pass_a_cue_that_stays_stops_after_it():
    """As above, but line 150 sits 2.7 s late, just past line 151's speech and within the span up to it, so no place
    is proved, and it stays. Line 151 would pass it, so it stops a centisecond after it, still nearer its own anchor.
    The lines after it move in full, and the order of the cues never changes."""
    trk = captioned(lag=lambda i: 0.2 if i < 150 else 2.7 if i == 150 else 9.0)
    got = s.live_moves(hear(WHOLE, 0.05, lost={150}), trk, "eng", {"fix": None})
    out = landed(trk, got)
    new = [s.written(a, a - t, None) for (a, _, _), (t, _) in zip(trk, out)]
    assert not any(m for t, m in out[:151]) and new[151] == new[150] + 1 and abs(out[151][0] - AT[151]) <= 0.3, out[149:153]
    assert all(m and abs(t - AT[i]) <= 0.2 for i, (t, m) in enumerate(out) if i > 151) and all(x < y for x, y in zip(new, new[1:])), got["live"]


def test_a_cue_whose_speech_lies_before_the_file_start_starts_at_0():
    """A cut file starts after the speech of its first line, which shows 5 s into it. Its speech lies before the file's
    start, so it starts at 0 and keeps its length, instead of shrinking to a millisecond. The cues after it move to
    their speech, and the order of the cues never changes. Dense hearing hears from the file's start."""
    trk = [(a - 61.0, b - 61.0, x) for a, b, x in captioned()]   # speech at AT[i] - 61 s, the first line's before 0
    heard = [dict(w, at=w["at"] - 61.0) for w in hear([61.0] + WHOLE, 0.05) if w["at"] >= 61.0]
    got = s.live_moves(heard, trk, "eng", {"fix": None})
    out = landed(trk, got)
    new = [s.written(a, a - t, None) for (a, _, _), (t, _) in zip(trk, out)]
    assert out[0] == (0.0, True) and all(x < y for x, y in zip(new, new[1:])), out[:3]
    assert all(m and abs(t - (AT[i] - 61)) <= 0.2 for i, (t, m) in enumerate(out) if i), got["live"]


def test_two_cues_that_stay_on_one_centisecond_end_the_order_loop():
    """Two cue starts 3 ms apart round to one centisecond, and neither moves, as with no words heard. The order loop
    leaves such a pair as the file has it, and returns."""
    import threading
    trk = [(10.0, 11.0, "first line here"), (100.001, 101.0, "two lines"), (100.004, 102.0, "shown at once"), (110.0, 111.0, "last line")]
    got = []
    t = threading.Thread(target=lambda: got.append(s.live_moves([], trk, "eng", None, {"lag": 9.0})), daemon=True)
    t.start()
    t.join(10)
    assert got and got[0]["blocks"] == [] and got[0]["live"]["moved"] == 0, "live_moves() did not return"


def test_check_live_refuses_a_move_that_nothing_proves():
    """A block that moves line 10 2 s later, away from its anchor, breaks nearer its speech. With no anchor and no span,
    it breaks own evidence."""
    trk = captioned()
    said = [a - s.BLOCK_LEAD for a, _, _ in trk]
    forged = [{"from": trk[0][0], "to": trk[-1][0] + 1.0, "shift": -2.0, "shifts": {round(trk[10][0], 3): -2.0},
               "keep": [round(a, 3) for k, (a, _, _) in enumerate(trk) if k != 10], "live": True}]
    with pytest.raises(s.Broken, match="nearer its speech"):
        s.check_live(trk, forged, {"said": said, "at": {10: AT[10] + LEAD}, "own": {10}, "how": {10: "own"}, "span": {}}, {})
    with pytest.raises(s.Broken, match="own evidence"):
        s.check_live(trk, forged, {"said": said, "at": {}, "own": set(), "how": {}, "span": {}}, {})
    # Line 10 moves 2 s earlier, to its anchor. A word heard 1.5 s after the anchor may be its speech, and the old start
    # lies nearer that word, so the move breaks nearer its speech. With no doubt it stands.
    to = [dict(forged[0], shifts={round(trk[10][0], 3): 2.0})]
    proof = {"said": said, "at": {10: said[10] - 2.0}, "own": {10}, "how": {10: "own"}, "span": {}, "heard": [(0.0, LENGTH)]}
    s.check_live(trk, to, proof, {})
    with pytest.raises(s.Broken, match="nearer its speech"):
        s.check_live(trk, to, dict(proof, doubt={10: 1.5}), {})
    # Line 10 takes the shift of two moving neighbours, between their anchors, but past the heard windows
    agree = dict(proof, own=set(), how={10: "agree"}, span={10: (-math.inf, math.inf, 3.0, 3.0)})
    s.check_live(trk, to, agree, {})
    with pytest.raises(s.Broken, match="outside the heard windows"):
        s.check_live(trk, to, dict(agree, heard=[(0.0, said[10] - 3.0)]), {})
