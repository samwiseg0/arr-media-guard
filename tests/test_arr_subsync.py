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
