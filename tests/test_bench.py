# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The bench tools in tools/bench: the synthetic cases and the truth score. Every case is made up."""
import importlib.util
import json
import os
import shutil
import statistics
import subprocess
import sys

import pytest

BENCH = os.path.join(os.path.dirname(__file__), "..", "tools", "bench")


def tool(name):
    spec = importlib.util.spec_from_file_location(f"bench_{name}", os.path.join(BENCH, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


synth, score, regressions = tool("synth"), tool("score"), tool("regressions")


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


def test_the_corpus_counts_name_each_case_that_changed(tmp_path, capsys):
    rec = lambda c, *k, runs=(): json.dumps({"id": c, "kinds": list(k), "runs": [list(r) for r in runs]}) + "\n"
    (tmp_path / "base.jsonl").write_text(rec("a", "wrong_text") + rec("b"))
    (tmp_path / "new.jsonl").write_text(rec("a") + rec("b", "wrong_silence", runs=[(5, 9, 5, -1.1)]))
    regressions.main([str(tmp_path / "new.jsonl"), "--base", str(tmp_path / "base.jsonl"), "--known", str(tmp_path / "known.json")])
    out = capsys.readouterr().out
    assert "2 cases, wrong_silence 1 (1 at 1.0 s or more), wrong_alert 0, wrong_text 0" in out
    assert "  + wrong_silence b" in out and "  - wrong_text a" in out
    assert json.loads((tmp_path / "known.json").read_text()) == {"b": ["wrong_silence"]}


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg and mkvmerge")
def test_a_replay_finds_the_words_lid_listen_wrote(tmp_path, monkeypatch):
    """Review N2. replay.py reads a words row by lid.word_key(), as lid.listen() writes it. The first replay of a
    made-up film with an empty cache names the windows it misses. lid.listen() with a fake Whisper writes the first
    two, and the second replay finds them."""
    from arr_media_guard import lid
    words = "apple bridge candle dragon engine forest garden harbor island jacket kettle ladder marble napkin orange pepper".split()
    at = lambda x: f"00:{int(x // 60):02d}:{int(x % 60):02d},{round(x % 1 * 1000):03d}"
    (tmp_path / "a.srt").write_text("\n".join(f"{k + 1}\n{at(10 + 2 * k)} --> {at(11.8 + 2 * k)}\n{words[k % 16]} {words[(k + 5) % 16]}\n"
                                              for k in range(160)))
    film, cache = str(tmp_path / "Film A (2020).mkv"), str(tmp_path / "words.sqlite")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=1:d=330", "-f", "lavfi", "-i", "sine=sample_rate=8000:duration=330",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "1", str(tmp_path / "v.mkv")], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", film, "--language", "1:eng", str(tmp_path / "v.mkv"), "--language", "0:eng", str(tmp_path / "a.srt")], check=True)
    lid._db(cache).close()

    def replay(n):
        out = tmp_path / f"r{n}.json"
        subprocess.run([sys.executable, os.path.join(BENCH, "replay.py"), film, "--cache", cache, "--out", str(out)], check=True, capture_output=True, timeout=300)
        got = json.loads(out.read_text())
        assert got["error"] is None, got
        return got["words"]
    first = replay(1)["missing_at"]
    monkeypatch.setattr(lid, "load", lambda *a: "model")
    monkeypatch.setattr(lid, "extract", lambda path, index, start, secs=30: b"\0\0" * int(16000 * secs))
    monkeypatch.setattr(lid, "transcribe", lambda whisper, pcm, lang: [[1.0, "hello"]])
    lid.listen(film, 0, first[:2], "eng", cache=cache)
    second = replay(2)
    assert second["exact"] == 2 and second["missing_at"] == first[2:], (first, second)
