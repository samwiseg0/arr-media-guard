# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The spoken language of the windows of a subtitle mismatch (docs/design.md, "Subtitle match"). A window whose speech
reads as another language than the subtitle's does not count, see subtitles.sub_spoken(). The flow tests fake the
model, see the env fixture. Every file and every line is made up."""
import json
import os
import subprocess

import pytest

from test_arr_media_guard import decided, env, hook, lid, removal_film  # noqa: F401


def spoken(env, monkeypatch, langs):
    """lid_langs() names langs[k] for the k-th window it is asked, at prob 0.97, or gives langs when it is a dict.
    env["langs"] records (audio index, windows) per read."""
    def lid_langs(path, index, j, wins, timeout):
        env.setdefault("langs", []).append((index, list(wins)))
        if isinstance(langs, dict):
            return langs
        return {"windows": [{"at": a, "secs": s, "lang": langs[k], "prob": 0.97} for k, (a, s) in enumerate(wins)], "model": "small", "took": 1.0}
    monkeypatch.setattr(hook, "lid_langs", lid_langs)


def test_windows_in_another_language_make_the_mismatch_unknown(env, monkeypatch, tmp_path):
    """The English track translates Arabic scenes, so Whisper told "en" hears few of its words. Both windows speak
    Arabic, so no window counts: the track stays with no change and no alert, and the decision line logs each window's
    language."""
    got = removal_film(env, monkeypatch, tmp_path)
    spoken(env, monkeypatch, ["ara", "ara"])
    hook.main([])
    rec = decided(env)
    s1 = rec["subcheck"]["s1"]
    assert got == [] and not rec.get("alerts"), (got, rec.get("alerts"))
    assert s1["verdict"] == "unknown" and s1["held"] and s1["why"].endswith(", but 2 of its 2 windows speak ara, so under two windows count"), s1
    assert [(w["lang"], w["prob"]) for w in s1["windows"]] == [("ara", 0.97)] * 2, s1["windows"]
    assert [(i, len(ws)) for i, ws in env["langs"]] == [(0, 2)], env["langs"]   # one read, on a mismatch only: s2 matched


def test_a_mismatch_in_the_tag_language_still_removes(env, monkeypatch, tmp_path):
    """Both windows speak English, so the mismatch counts and the track leaves the file."""
    got = removal_film(env, monkeypatch, tmp_path)
    spoken(env, monkeypatch, ["eng", "eng"])
    hook.main([])
    rec = decided(env)
    assert [g[2] for g in got] == [[2]], got
    s1 = rec["subcheck"]["s1"]
    assert s1["verdict"] == "mismatch" and [w["lang"] for w in s1["windows"]] == ["eng", "eng"], s1


@pytest.mark.parametrize("langs, verdict, why", [
    (["ara", "eng", "eng"], "mismatch", "at best, with 1 window in ara left out"),   # two windows still count
    (["ara", "fre", "eng"], "unknown", "at best, but 2 of its 3 windows speak ara, fre, so under two windows count"),
    ([None, None, "ara"], "mismatch", "at best, with 1 window in ara left out"),   # a window that names no language counts
    (["eng", "eng", "eng"], "mismatch", "at best"),
    ({"why": "no answer in 50 seconds"}, "unknown", "at best, but the language of its windows could not be heard: no answer in 50 seconds"),
])
def test_each_window_counts_unless_it_speaks_another_language(monkeypatch, langs, verdict, why):
    """Three windows of a mismatch, each with 12 heard words. Under two windows left in the tag's language make it
    unknown, as does a read that gives no answer. A window under MIN_WORDS words is never read."""
    env = {}
    spoken(env, monkeypatch, langs)
    ws = [{"at": a, "words": 12, "overlap": 0.1, "offset": None, "cues": 0, "late": None} for a in (100.0, 200.0, 300.0)]
    ws.append({"at": 400.0, "words": 3, "overlap": 0.0, "offset": None, "cues": 0, "late": None})
    res = {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 10%, 10%, 10% at best", "windows": ws}}
    out = hook.sub_spoken("/f.mkv", {}, res, {"s1": ("eng", 0, [])})["s1"]
    assert (out["verdict"], out["why"].removeprefix("the heard words match the cues at 10%, 10%, 10% ")) == (verdict, why), out
    assert env["langs"] == [(0, [(100.0, 10.0), (200.0, 10.0), (300.0, 10.0)])], env["langs"]


def test_a_stop_reads_no_language(monkeypatch):
    """A stop asked before the read leaves the mismatch unknown, and no read starts."""
    monkeypatch.setattr(hook, "lid_langs", lambda *a: pytest.fail("no read after a stop"))
    monkeypatch.setattr(hook, "stop_asked", lambda: True)
    res = {"s1": {"verdict": "mismatch", "why": "x", "windows": [{"at": 1.0, "words": 9}, {"at": 2.0, "words": 9}]}}
    out = hook.sub_spoken("/f.mkv", {}, res, {"s1": ("eng", 0, [])})["s1"]
    assert (out["verdict"], out["why"]) == ("unknown", "x, but the language of its windows could not be heard: the run was stopped"), out


REAL_LID_LANGS = hook.lid_langs   # before the env fixture fakes it
LID_PY = os.path.join(os.environ.get("ARR_LID_DIR", "/opt/arr-media-guard-lid"), "venv", "bin", "python")


def test_one_lid_run_reads_the_windows_and_takes_a_turn(env, monkeypatch, tmp_path):
    """lid_langs() runs lid.py --langs through the hook's real call site. A cached hearing of the stream never stands
    in for it."""
    lid(env, monkeypatch, tmp_path, lambda argv: None)
    monkeypatch.setattr(hook, "lid_cached", lambda *a, **k: True)
    turns = []
    monkeypatch.setattr(hook, "lid_turn", lambda deadline, background=False: turns.append(1) or (open(os.devnull), 0))
    REAL_LID_LANGS(env["path"], 1, {}, [(305.0, 10.0), (1000.0, 24.0)], 50)
    (argv, _), = env["lid"]
    assert argv[argv.index("--langs"):][:3] == ["--langs", "305.0:10.0", "1000.0:24.0"] and turns == [1], argv


def test_tongues_reads_each_window_and_cuts_a_kept_sample(monkeypatch, tmp_path):
    """Each window gets one detect() at the floor of WINDOW_SPEECH. A window inside a kept sample is cut from it, and a
    read under SAMPLE_MIN_PROB names no language."""
    lid_ = hook.arr_lid
    media, cache, calls = tmp_path / "Film A.mkv", str(tmp_path / "state" / "lid.sqlite"), []
    media.write_bytes(b"x" * 100)
    monkeypatch.setattr(lid_, "load", lambda *a: "model")
    monkeypatch.setattr(lid_, "extract", lambda path, index, start, secs=30: calls.append(("extract", start, secs)) or b"\0\0" * int(16000 * secs))
    answers = iter([("ara", 0.98), ("eng", 0.55)])

    def detect(whisper, pcm, least=lid_.MIN_SPEECH):   # the samples of identify() hear English
        calls.append(("detect", len(pcm) // 32000, least))
        lang, prob = next(answers) if least != lid_.MIN_SPEECH else ("eng", 0.97)
        return {"speech": 6.0, "lang": lang, "prob": prob, "top": [[lang, prob]]}
    monkeypatch.setattr(lid_, "detect", detect)
    lid_.identify(str(media), 1, 1200, cache=cache, keep=True)   # keeps samples at 300, 600 and 900 s
    calls.clear()
    r = lid_.tongues(str(media), 1, [(305.0, 10.0), (1000.0, 24.0)], cache=cache)
    assert calls == [("detect", 10, lid_.WINDOW_SPEECH), ("extract", 1000.0, 24.0), ("detect", 24, lid_.WINDOW_SPEECH)], calls
    assert [(w["at"], w["secs"], w["lang"], w["prob"]) for w in r["windows"]] == [(305.0, 10.0, "ara", 0.98), (1000.0, 24.0, None, 0.55)], r


def test_the_cli_reads_the_windows_of_langs(monkeypatch, tmp_path, capsys):
    lid_ = hook.arr_lid
    monkeypatch.setattr(lid_, "tongues", lambda path, index, wins, model, model_dir, cache: {"windows": [list(w) for w in wins], "index": index})
    assert lid_.main(["/f.mkv", "1", "1200", "--langs", "305:10", "1000.5:24", "--cache", str(tmp_path / "lid.sqlite")]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["windows"], got["index"]) == ([[305.0, 10.0], [1000.5, 24.0]], 1), got


@pytest.mark.skipif(not os.path.exists(LID_PY), reason="needs the venv of a language detection install, see ARR_LID_DIR")
def test_a_window_needs_window_speech_seconds_of_speech():
    """detect() in the venv, with Silero VAD faked to find 5 s of speech. The floor of a window hears it, and the floor
    of a sample, MIN_SPEECH, does not."""
    script = """import json, sys
sys.path.insert(0, sys.argv[1])
import faster_whisper.vad as vad
vad.get_speech_timestamps = lambda audio, opts: [{"start": 0, "end": 80000}]
from arr_media_guard import lid
class Whisper:
    def detect_language(self, audio):
        return None, None, [("ar", 0.9), ("en", 0.1)]
pcm = bytes(320000)
print(json.dumps([lid.detect(Whisper(), pcm, lid.WINDOW_SPEECH), lid.detect(Whisper(), pcm)]))
"""
    r = subprocess.run([LID_PY, "-c", script, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))], capture_output=True, text=True, timeout=120)
    window, sample = json.loads(r.stdout.strip().splitlines()[-1])
    assert (window["speech"], window["lang"], window["prob"]) == (5.0, "ara", 0.9) and (sample["lang"], sample["prob"]) == (None, 0.0), r


def test_no_time_left_reads_no_language(monkeypatch):
    """A read needs 10 seconds of the check's time. With less, the mismatch is unknown and no read starts."""
    monkeypatch.setattr(hook, "lid_langs", lambda *a: pytest.fail("no read without time"))
    monkeypatch.setattr(hook, "SUB_TIMEOUT", 9)
    res = {"s1": {"verdict": "mismatch", "why": "x", "windows": [{"at": 1.0, "words": 9}, {"at": 2.0, "words": 9}]}}
    out = hook.sub_spoken("/f.mkv", {}, res, {"s1": ("eng", 0, [])})["s1"]
    assert (out["verdict"], out["why"]) == ("unknown", "x, but the language of its windows could not be heard: no time left of the 9 seconds the check may take"), out


WORDS = ["apple", "bridge", "candle", "dragon", "engine", "forest", "garden", "harbor", "island", "jacket", "kettle", "ladder",
         "marble", "napkin", "orange", "pepper", "quartz", "rabbit", "saddle", "tunnel", "violin", "walnut", "yellow", "zipper"]


def test_the_second_hearing_leaves_out_a_window_in_another_language(monkeypatch):
    """Review F1. a1 mismatches, and its window at 300 s speaks Arabic, so sub_spoken() leaves it out. a2 holds the
    subtitle's words. It hears only the two windows in English, matches on them, and the track stays. The fake
    hearing gives the Arabic window too, so the check must also leave it out."""
    cues = [(t, t + 1.8, f"{WORDS[t // 2 % 24]} {WORDS[(t // 2 + 7) % 24]} {WORDS[(t // 2 + 13) % 24]}") for t in range(0, 1200, 2)]
    right = lambda at: {"at": at, "secs": 10.0, "words": [[round(a - at + k * 0.5, 2), w] for a, _, x in cues if at <= a < at + 9
                                                          for k, w in enumerate(x.split())]}
    arabic = {"at": 300.0, "secs": 10.0, "words": [[k * 0.8, w] for k, w in enumerate("salam habibi yalla khalas inshallah wallah shukran marhaba".split())]}
    asked = []
    monkeypatch.setattr(hook, "lid_langs", lambda path, idx, j, wins, timeout: {"windows": [
        {"at": a, "secs": s, "lang": "ara" if a == 300.0 else "eng", "prob": 0.97} for a, s in wins]})
    monkeypatch.setattr(hook, "lid_run", lambda path, idx, j, expect, timeout, words=None, **k: asked.append((idx, words[1], words[3])) or
                        {"windows": [right(100.0), right(200.0), arabic], "took": 1.0, "cpu": 1.0})
    win = lambda a: {"at": a, "words": 10, "overlap": 0.1, "offset": None, "cues": 0, "late": None}
    res = {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 10%, 10%, 10% at best", "windows": [win(a) for a in (100.0, 200.0, 300.0)],
                  "starts": [[[100.0, 200.0, 300.0], 10.0]]}}
    items, j = {"s1": ("eng", 0, cues)}, {"container": {"properties": {"duration": 1200e9}}}
    ts = [{"kind": "a", "lang": "eng", "pos": "a1", "role": "main"}, {"kind": "a", "lang": "eng", "pos": "a2", "role": "main"}]
    out = hook.sub_others("/f.mkv", j, hook.sub_spoken("/f.mkv", j, res, items), items, ts)["s1"]
    (other,) = out["others"]
    assert (other["verdict"], other["why"]) == ("match", "the heard words match the cues at 100%, 100%"), other
    assert out["verdict"] == "unknown" and out["held"] and out["why"].endswith(", with 1 window in ara left out, but on a2 the heard words "
                                                                               "match the cues at 100%, 100%"), out
    assert [(i, ws, len(more or ())) for i, ws, more in asked] == [(1, [100.0, 200.0], 2)], asked   # a longer window per window heard


def test_the_second_hearing_keeps_each_longer_window_with_its_window(monkeypatch):
    """The first hearing heard one window per part, at 100 s and 900 s, and a later hearing heard 500 s. The window at
    900 s speaks Arabic, so the other audio track hears 100 s alone, with the longer window of the first part only."""
    cues = [(t, t + 1.8, f"{WORDS[t // 2 % 24]} {WORDS[(t // 2 + 7) % 24]}") for t in range(0, 1200, 2)]
    asked = []
    monkeypatch.setattr(hook, "lid_langs", lambda path, idx, j, wins, timeout: {"windows": [
        {"at": a, "secs": s, "lang": "ara" if a == 900.0 else "eng", "prob": 0.97} for a, s in wins]})
    monkeypatch.setattr(hook, "lid_run", lambda path, idx, j, expect, timeout, words=None, **k: asked.append((words[1], words[3])) or {"why": "x"})
    more = hook.arr_subsync.windows(cues, 1200.0, hook.arr_decide.STOPWORDS["eng"], secs=hook.arr_subsync.THIRD, taken=[100.0, 900.0])
    win = lambda a: {"at": a, "words": 10, "overlap": 0.1, "offset": None, "cues": 0, "late": None}
    res = {"s1": {"verdict": "mismatch", "why": "x", "windows": [win(a) for a in (100.0, 500.0, 900.0)], "starts": [[[100.0, 900.0], 10.0]]}}
    items, j = {"s1": ("eng", 0, cues)}, {"container": {"properties": {"duration": 1200e9}}}
    ts = [{"kind": "a", "lang": "eng", "pos": "a1", "role": "main"}, {"kind": "a", "lang": "eng", "pos": "a2", "role": "main"}]
    hook.sub_others("/f.mkv", j, hook.sub_spoken("/f.mkv", j, res, items), items, ts)
    assert len(more) == 2 and asked == [([100.0], more[:1])], (asked, more)
