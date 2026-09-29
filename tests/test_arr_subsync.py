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
"""Unit tests for arr_subsync.py, the subtitle match check.

The dialogue is written for these tests from a word list. Whisper is faked: a window hears the words of the lines
spoken in it, at the times the audio holds them. A line is spoken LEAD seconds after its cue starts in a right track.

Run: pytest tests/test_arr_subsync.py
"""
import os
import random
import sys
from fractions import Fraction

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import arr_subsync as s  # noqa: E402

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
    stop = s.arr_decide.STOPWORDS["eng"]
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
