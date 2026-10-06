# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The bench tools in tools/bench: the synthetic cases and the truth score. Every case is made up."""
import importlib.util
import os
import statistics

import pytest

BENCH = os.path.join(os.path.dirname(__file__), "..", "tools", "bench")


def tool(name):
    spec = importlib.util.spec_from_file_location(f"bench_{name}", os.path.join(BENCH, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


synth, score = tool("synth"), tool("score")


def errors(case):
    return [c[0] - t["word_start"] for c, t in zip(case["cues"], case["truth"])]


def test_a_right_track_sits_on_its_speech():
    e = errors(synth.make("right", sigma=0.0))
    assert all(abs(x) < 1e-6 for x in e) and len(e) == 400


def test_a_drift_grows_from_its_offset_by_its_ratio():
    case = synth.make("drift", sigma=0.0)
    e = errors(case)
    assert e[0] == pytest.approx(-0.3 + 0.001 * case["cues"][0][0], abs=0.01)
    assert e[-1] - e[0] == pytest.approx(0.001 * (case["truth"][-1]["word_start"] - case["truth"][0]["word_start"]), abs=0.01)


def test_a_step_and_a_block_move_only_their_cues():
    step, block = synth.make("step", sigma=0.0, share=0.5, shift=1.2), synth.make("block", sigma=0.0, start=300, length=60, shift=-1.5)
    assert {round(x, 3) for x in errors(step)} == {0.0, 1.2}
    moved = [c[0] for c, x in zip(block["cues"], errors(block)) if abs(x) > 1e-6]
    assert moved and all(298.5 <= s < 360 for s in moved) and all(round(x, 3) in (0.0, -1.5) for x in errors(block))


def test_scene_lag_is_one_lag_per_scene():
    e = errors(synth.make("scene", sigma=0.0, lag_sd=0.5))
    assert len({round(x, 6) for x in e[:12]}) == 1 and len({round(x, 6) for x in e}) > 10


def test_live_captions_lag_their_speech_and_keep_their_order():
    case = synth.make("live", lag_lo=2.0, lag_hi=9.0)
    starts = [c[0] for c in case["cues"]]
    assert starts == sorted(starts) and min(errors(case)) >= 2.0 - 0.05 and statistics.median(errors(case)) > 4
    assert all(c[1] == n[0] for c, n in zip(case["cues"], case["cues"][1:]))   # a line shows until the next one


def test_roll_up_cues_repeat_the_two_lines_before_their_own():
    case = synth.make("rollup")
    assert case["cues"][5][2].split("\\N")[-1] == synth.lines_of(1, 400)[5] and case["cues"][5][2].count("\\N") == 2


def test_a_window_holds_the_words_heard_in_it():
    case = synth.make("right")
    w = synth.window(case, 100.0, 10.0)
    assert w["words"] and all(0 <= t < 10 for t, _ in w["words"]) and w["at"] == 100.0


def test_the_score_counts_both_kinds_of_error():
    truth = [{"start": s, "word_start": s} for s in (10.0, 20.0, 30.0, 40.0)] + [{"start": 50.0, "word_start": None}]
    # cue 1 stays right, cue 2 is put right, cue 3 is put wrong, cue 4 stays wrong
    cues = [[10.0, 10.0, 10.0], [20.0, 21.0, 20.1], [30.0, 30.2, 31.0], [40.0, 42.0, 41.5], [50.0, 50.0, 50.0], [60.0, 60.0, 60.0]]
    got = score.score(cues, truth)
    assert (got["fixed"], got["wrong"], got["before"]["within"], got["after"]["within"], got["unpaired"]) == (1, 1, 2, 2, 1)
    assert got["per_1000"]["wrong"] == 250.0
