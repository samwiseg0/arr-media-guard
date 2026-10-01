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
"""Unit tests for arr_lid.py, the spoken-language identification.

The detector is mocked, so these run with the stdlib alone. One test runs the real model. It runs only where
faster-whisper and a model directory exist (ARR_LID_MODEL_DIR, default /opt/arr-media-guard-lid/models).

Run: pytest tests/test_arr_lid.py
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, FILES)
import arr_decide  # noqa: E402
import arr_lid  # noqa: E402


def s(lang, prob=0.97, speech=20.0):
    return {"at": 0, "speech": speech, "lang": lang, "prob": prob, "top": []}


def test_whisper_codes_map_to_classifier_codes():
    assert [arr_lid.code(c) for c in ("en", "ja", "es", "pt", "fr", "de", "zh", "yue", "ko", "no", "nn", "cy", "sq")] == [
        "eng", "jpn", "spa", "por", "fre", "ger", "chi", "chi", "kor", "nor", "nor", "wel", "alb"]
    assert [arr_lid.code(c) for c in ("fra", "deu", "zho", "nob", "ENG", "jap", "gle")] == ["fre", "ger", "chi", "nor", "eng", "jpn", "gle"]
    assert len(arr_lid.WHISPER) == 100 and all(len(v) == 3 for v in arr_lid.WHISPER.values())
    # every code the module returns is one the classifier reads as that language
    for w, b in arr_lid.WHISPER.items():
        assert b in arr_decide.codes(b), (w, b)
    for t, b in arr_lid.ALIAS.items():
        if t != "jap":   # a bad tag some files carry, the classifier does not know it yet
            assert b in arr_decide.codes(t) or arr_decide.codes(t) == {t}, (t, b)


def test_identifiable():
    assert arr_lid.identifiable("eng") and arr_lid.identifiable("deu") and arr_lid.identifiable("nob")
    assert not arr_lid.identifiable("gle") and not arr_lid.identifiable("gla") and not arr_lid.identifiable("bel")


def test_agreement_rule():
    c = arr_lid.combine
    assert c([s("spa"), s("spa", 0.9), s("spa", 0.8)]) == ("spa", 0.89, None)
    assert c([s("jpn"), s(None, 0.0, 1.0), s("jpn")])[0] == "jpn"                       # one sample without speech
    assert c([s("jpn"), s(None, 0.0, 1.0), s(None, 0.0, 0.0)]) == (None, 0.0, "1 of 3 samples name a language clearly")
    assert c([s("eng"), s("kor", 0.61), s("eng")])[2] == "the samples disagree: eng, kor, eng"
    assert c([s("eng"), s("kor", 0.52), s("eng")])[0] == "eng"                          # a sample under 0.6 does not vote
    assert c([s("nor", 0.7), s("nor", 0.75), s("nor", 0.8)]) == (None, 0.75, "nor at 0.75 is under the threshold")
    assert c([s("rus"), s("rus"), s("rus")], expect=["bel", "eng"]) == (None, 0.0, "whisper cannot identify bel")
    assert c([s("eng"), s("eng"), s("eng")], expect=["gle", "eng"])[2] == "whisper cannot identify gle"
    assert c([s("bel"), s("bel"), s("bel")])[2] == "whisper cannot tell bel from its neighbours"
    assert c([s("eng"), s("eng"), s("eng")], expect=["und", "", "jpn"])[0] == "eng"      # untagged is no claim


def test_close_relatives_withhold_and_never_bias():
    c = arr_lid.combine
    # a Galician original heard as Spanish, a Hindi one heard as Urdu: withheld, no false wrong-language alarm
    assert c([s("spa"), s("spa"), s("spa")], expect=["eng", "glg"]) == (None, 0.97, "whisper cannot tell spa from glg")
    assert c([s("urd"), s("urd"), s("urd")], expect=["hin"])[2] == "whisper cannot tell urd from hin"
    assert c([s("dan"), s("dan"), s("dan")], expect=["nob", "eng"])[2] == "whisper cannot tell dan from nor"
    # the expected language itself, or a language with no relative in play, still answers
    assert c([s("spa"), s("spa"), s("spa")], expect=["eng", "spa"])[0] == "spa"
    assert c([s("nor"), s("nor"), s("nor")], expect=["nor", "eng"])[0] == "nor"
    assert c([s("jpn"), s("jpn"), s("jpn")], expect=["eng", "spa"])[0] == "jpn"
    assert c([s("spa"), s("spa"), s("spa")])[0] == "spa"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """identify() with ffmpeg and the model mocked. Returns the call log and a media file."""
    calls = {"load": 0, "extract": [], "detect": 0}
    # the third sample holds no speech, so a fourth is taken
    answers = iter([s("jpn", 0.98), s("jpn", 0.96), s(None, 0.0, 2.0), s("jpn", 0.97)] * 4)

    def load(*a):
        calls["load"] += 1
        return "model"

    def extract(path, index, start, secs=30):
        calls["extract"].append((index, round(start)))
        return b"\0\0" * 16000

    def detect(whisper, pcm):
        calls["detect"] += 1
        r = dict(next(answers)); r.pop("at")
        return r

    monkeypatch.setattr(arr_lid, "load", load)
    monkeypatch.setattr(arr_lid, "extract", extract)
    monkeypatch.setattr(arr_lid, "detect", detect)
    media = tmp_path / "Show A S01E01.mkv"
    media.write_bytes(b"x" * 100)
    return calls, str(media), str(tmp_path / "state" / "lid.sqlite")


def test_identify_and_cache(fake):
    calls, media, cache = fake
    r = arr_lid.identify(media, 1, 1200, expect=["eng", "jpn"], cache=cache)
    assert (r["lang"], r["prob"], r["cached"], r["engine"], r["model"]) == ("jpn", 0.97, False, "faster-whisper", "small@536b066")
    assert calls["extract"] == [(1, 300), (1, 600), (1, 900), (1, 480)] and calls["detect"] == 4
    assert [x["at"] for x in r["samples"]] == [300, 600, 900, 480]
    # the same file state is never analysed twice, and another stream is a new key
    again = arr_lid.identify(media, 1, 1200, cache=cache)
    assert again["cached"] and again["lang"] == "jpn" and calls["load"] == 1 and calls["detect"] == 4
    arr_lid.identify(media, 0, 1200, cache=cache)
    assert calls["detect"] == 8
    # a cache hit re-runs combine() with the thresholds of today
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(arr_lid, "MIN_PROB", 0.99)
        hit = arr_lid.identify(media, 1, 1200, cache=cache)
    assert hit["cached"] and hit["lang"] is None and hit["why"] == "jpn at 0.97 is under the threshold" and calls["detect"] == 8
    # another model misses
    assert not arr_lid.identify(media, 1, 1200, model="base", cache=cache)["cached"] and calls["detect"] == 12
    # a new mtime misses, unless carry() moved the entry after an in-place edit
    before = os.stat(media)
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
    arr_lid.carry(media, before, cache)
    assert arr_lid.identify(media, 1, 1200, cache=cache)["cached"] and calls["detect"] == 12
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 2 * 10**9))
    assert not arr_lid.identify(media, 1, 1200, cache=cache)["cached"] and calls["detect"] == 16


def test_carry_never_fails_an_edit(tmp_path):
    media = tmp_path / "a.mkv"
    media.write_bytes(b"x")
    missing = str(tmp_path / "state" / "lid.sqlite")
    arr_lid.carry(str(media), os.stat(media), missing)
    assert not os.path.exists(os.path.dirname(missing))                  # no cache is created
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not a database")
    arr_lid.carry(str(media), os.stat(media), str(broken))                # sqlite3.Error is swallowed
    arr_lid.carry(str(tmp_path / "gone.mkv"), os.stat(media), str(broken))   # so is OSError


def test_one_window_never_votes_twice(fake):
    """A 40-second file fits one window. It gets one cut, so one vote and no answer."""
    calls, media, cache = fake
    r = arr_lid.identify(media, 0, 40, cache=cache)
    assert calls["extract"] == [(0, 10)] and r["lang"] is None and len(r["samples"]) == 1


def test_no_duration_never_poisons_the_cache(fake):
    """A probe with no duration cuts nothing and caches nothing. A later call with the real
    duration hears the file, and the duration is part of the key."""
    calls, media, cache = fake
    for d in (0, -1, None):
        r = arr_lid.identify(media, 0, d, cache=cache)
        assert (r["lang"], r["why"], r["samples"], r["cached"]) == (None, "no duration", [], False)
    assert calls == {"load": 0, "extract": [], "detect": 0} and not os.path.exists(cache)
    assert arr_lid.identify(media, 0, 1200, cache=cache)["lang"] == "jpn" and calls["detect"] == 4
    assert arr_lid.identify(media, 0, 1200.4, cache=cache)["cached"]
    assert not arr_lid.identify(media, 0, 1300, cache=cache)["cached"] and calls["detect"] == 8


def test_unidentifiable_language_skips_the_model(fake):
    calls, media, cache = fake
    r = arr_lid.identify(media, 0, 6000, expect=["gle", "eng"], cache=cache)
    assert r["lang"] is None and r["why"] == "whisper cannot identify gle" and calls == {"load": 0, "extract": [], "detect": 0}


def test_cli_prints_one_json_line_on_error(tmp_path, capsys):
    rc = arr_lid.main([str(tmp_path / "missing.mkv"), "0", "100", "--cache", str(tmp_path / "lid.sqlite")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["lang"] is None and out["why"].startswith("error: ")


def test_fetch_keeps_a_whole_model_and_downloads_a_partial_one(tmp_path, monkeypatch):
    import hashlib
    import types
    (tmp_path / "small").mkdir()
    (tmp_path / "small" / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(arr_lid, "MODEL_SHA256", hashlib.sha256(b"weights").hexdigest())
    got = []   # a killed download left model.bin and lost the tokenizer: fetch downloads again
    fake = types.ModuleType("faster_whisper")
    fake.download_model = lambda model, output_dir, revision: got.append(model) or [
        open(os.path.join(output_dir, n), "w").close() for n in arr_lid.MODEL_FILES]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert arr_lid.fetch(str(tmp_path)) == "downloaded" and got == ["small"]
    assert arr_lid.fetch(str(tmp_path)) == "present" and got == ["small"]   # no download, no network


def test_fresh_hears_past_the_cache(fake):
    calls, media, cache = fake
    arr_lid.identify(media, 1, 1200, cache=cache)
    assert arr_lid.identify(media, 1, 1200, cache=cache)["cached"] and calls["detect"] == 4
    r = arr_lid.identify(media, 1, 1200, cache=cache, fresh=True)   # the second check before a re-grab
    assert not r["cached"] and calls["detect"] == 8 and arr_lid.identify(media, 1, 1200, cache=cache)["cached"]


MODEL_DIR = os.environ.get("ARR_LID_MODEL_DIR", arr_lid.MODEL_DIR)


@pytest.mark.skipif(not os.path.isdir(os.path.join(MODEL_DIR, arr_lid.MODEL)) or shutil.which("ffmpeg") is None,
                    reason="no Whisper model directory or no ffmpeg")
def test_real_model_on_a_generated_clip(tmp_path):
    """A 5-minute tone runs the real ffmpeg, VAD and model. It holds no speech, so no language comes back.
    Every language the model can name must be in WHISPER, or it would map to nothing."""
    pytest.importorskip("faster_whisper")
    clip = tmp_path / "tone.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=300", "-c:a", "flac",
                    str(clip)], check=True)
    r = arr_lid.identify(str(clip), 0, 300, model_dir=MODEL_DIR, cache=str(tmp_path / "lid.sqlite"))
    assert r["lang"] is None and [x["at"] for x in r["samples"]] == [75, 150, 225, 120, 180] and all(x["speech"] < arr_lid.MIN_SPEECH for x in r["samples"])
    import numpy as np
    _, _, probs = arr_lid.load(model_dir=MODEL_DIR).detect_language(np.zeros(16000 * 5, np.float32))
    assert {w for w, _ in probs} <= set(arr_lid.WHISPER) and len(probs) >= 99


# --- the subtitle check's words (docs/design.md, "Subtitle match") -------------------------------------------------

@pytest.fixture
def ears(monkeypatch, tmp_path):
    """listen() with ffmpeg and the model mocked. Each second of the audio is one byte that names the second, and the
    model hears one word in each second of the clip, the byte it holds. Returns the call log, a media file and the cache."""
    calls = {"load": 0, "extract": [], "clips": []}

    def extract(path, index, start, secs=30):
        calls["extract"].append((index, start, secs))
        return b"".join(bytes([(int(start) + k) % 250 + 1]) * 32000 for k in range(int(secs)))

    def transcribe(whisper, pcm, lang):
        calls["clips"].append((len(pcm), lang))
        return [[k + 0.25, f"w{pcm[k * 32000]}"] for k in range(len(pcm) // 32000)]

    monkeypatch.setattr(arr_lid, "load", lambda *a: calls.__setitem__("load", calls["load"] + 1) or "model")
    monkeypatch.setattr(arr_lid, "extract", extract)
    monkeypatch.setattr(arr_lid, "transcribe", transcribe)
    media = tmp_path / "Show A S01E01.mkv"
    media.write_bytes(b"x" * 100)
    return calls, str(media), str(tmp_path / "state" / "lid.sqlite")


def test_listen_hears_both_windows_as_one_clip_and_caches_the_words(ears):
    calls, media, cache = ears
    r = arr_lid.listen(media, 1, [200.0, 1000.0], "eng", cache=cache)
    n = arr_lid.WORD_SECS
    assert calls["clips"] == [(2 * n * 32000, "eng")] and calls["extract"] == [(1, 200.0, n), (1, 1000.0, n)]   # one model run
    assert [w["at"] for w in r["windows"]] == [200.0, 1000.0] and not r["cached"]
    assert r["windows"][0]["words"][0] == [0.25, "w201"] and r["windows"][1]["words"][0] == [0.25, f"w{1000 % 250 + 1}"]
    assert all(0 <= t < n for w in r["windows"] for t, _ in w["words"]) and len(r["windows"][1]["words"]) == n
    again = arr_lid.listen(media, 1, [200.0, 1000.0], "eng", cache=cache)
    assert again["cached"] and again["windows"] == r["windows"] and calls["load"] == 1
    assert not arr_lid.listen(media, 1, [200.0, 1001.0], "eng", cache=cache)["cached"]   # other windows are another key


def test_listen_cuts_its_windows_from_the_samples_the_language_check_kept(ears, monkeypatch):
    """No audio is decoded twice: a window inside a kept sample is cut from it."""
    calls, media, cache = ears
    monkeypatch.setattr(arr_lid, "detect", lambda whisper, pcm: {"speech": 20.0, "lang": "eng", "prob": 0.97, "top": []})
    arr_lid.identify(media, 1, 1200, cache=cache, keep=True)
    kept = sorted(arr_lid.kept_pcm(cache, media, 1))
    assert kept == [(300, 30), (600, 30), (900, 30)] and arr_lid.kept_pcm(cache, media, 0) == []
    calls["extract"].clear()
    r = arr_lid.listen(media, 1, [305.0, 1000.0], "eng", cache=cache)
    # the window at 305 s is cut 5 s into the sample at 300 s: its first second is second 305, byte 305 % 250 + 1
    assert calls["extract"] == [(1, 1000.0, arr_lid.WORD_SECS)] and r["reused"] == 1 and r["windows"][0]["words"][0] == [0.25, "w56"]
    with monkeypatch.context() as mp:   # after PCM_KEEP the samples are gone
        mp.setattr(arr_lid, "PCM_KEEP", -1)
        assert arr_lid.kept_pcm(cache, media, 1) == []


def test_listen_refuses_a_language_whisper_cannot_write(ears):
    calls, media, cache = ears
    with pytest.raises(ValueError, match="whisper cannot transcribe gle"):
        arr_lid.listen(media, 1, [200.0, 1000.0], "gle", cache=cache)
    assert calls["load"] == 0


def test_the_words_follow_an_edit_and_a_conversion(ears, tmp_path):
    calls, media, cache = ears
    arr_lid.listen(media, 1, [200.0, 1000.0], "eng", cache=cache)
    new = str(tmp_path / "Show A S01E01 new.mkv")
    before = os.stat(media)
    os.rename(media, new)
    arr_lid.carry(new, before, cache, was=media)   # a conversion: the proof shows the same audio
    assert arr_lid.listen(new, 1, [200.0, 1000.0], "eng", cache=cache)["cached"]


def test_the_verdict_cache_holds_the_pending_mark(tmp_path):
    media = tmp_path / "a.mkv"
    media.write_bytes(b"x")
    cache = str(tmp_path / "lid.sqlite")
    assert arr_lid.verdict_get(cache, str(media)) is None and not os.path.exists(cache)   # a read never creates the cache
    arr_lid.verdict_put(cache, str(media), {"s1": "match"}, False)
    assert arr_lid.verdict_get(cache, str(media)) == ({"s1": "match"}, False)
    arr_lid.verdict_put(cache, str(media), {"s1": "mismatch"}, True)
    assert arr_lid.verdict_get(cache, str(media)) == ({"s1": "mismatch"}, True)
    before = os.stat(media)
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
    arr_lid.carry(str(media), before, cache)   # an edit changes the file, so the verdict must be made again
    assert arr_lid.verdict_get(cache, str(media)) is None


def test_transcribe_drops_a_looping_segment(monkeypatch):
    """A segment whose text compresses past MAX_COMPRESSION is Whisper saying one phrase again and again."""
    import types
    w = lambda t, x: types.SimpleNamespace(start=t, word=x)
    segs = [types.SimpleNamespace(compression_ratio=1.4, words=[w(0.5, " Mira"), w(0.9, " runs.")]),
            types.SimpleNamespace(compression_ratio=9.0, words=[w(2.0 + k / 10, " hey") for k in range(108)])]
    asked = {}
    model = types.SimpleNamespace(transcribe=lambda audio, **kw: asked.update(kw) or (iter(segs), None))
    fake = types.ModuleType("numpy")   # the tests run without numpy
    fake.int16 = fake.float32 = None
    fake.frombuffer = lambda b, t: types.SimpleNamespace(astype=lambda t: 1.0)
    monkeypatch.setitem(sys.modules, "numpy", fake)
    assert arr_lid.transcribe(model, b"\0\0" * 16000, "eng") == [[0.5, "Mira"], [0.9, "runs."]]
    assert not asked["condition_on_previous_text"] and asked["beam_size"] == 1 and asked["compression_ratio_threshold"] == arr_lid.MAX_COMPRESSION


def test_the_model_loads_once_per_process(monkeypatch):
    import types
    made = []
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: made.append(k["cpu_threads"]) or object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(arr_lid, "LOADED", {})
    first = arr_lid.load("small", "/models", 4)
    assert arr_lid.load("small", "/models", 1) is first and made == [4]   # the subtitle check reuses the language check's model


def test_listen_hears_a_third_window_in_the_same_process(ears, monkeypatch):
    """The early window hears two words. The window of its part in more is heard in the same process, with the model
    loaded once. The late window heard enough, so its entry in more is never heard."""
    calls, media, cache = ears
    def transcribe(whisper, pcm, lang):
        calls["clips"].append((len(pcm), lang))
        n = len(pcm) // 32000
        return [[k + 0.25, f"word{pcm[k * 32000]}x{k}"] for k in range(n) if not 201 <= pcm[k * 32000] <= 210 or k < 2]   # the window at 200 s
    monkeypatch.setattr(arr_lid, "transcribe", transcribe)
    r = arr_lid.listen(media, 1, [200.0, 1000.0], "eng", cache=cache, more=[150.0, 1100.0])
    assert [(w["at"], w["secs"]) for w in r["windows"]] == [(200.0, arr_lid.WORD_SECS), (1000.0, arr_lid.WORD_SECS), (150.0, arr_lid.THIRD_SECS)]
    assert calls["load"] == 1 and len(calls["clips"]) == 2 and set(r["profile"]) == {"decode", "whisper", "load"}
    again = arr_lid.listen(media, 1, [200.0, 1000.0], "eng", cache=cache)
    assert again["cached"] and again["windows"] == r["windows"]   # the third window is cached with the first two
    # a lone window, the middle one, that hears too little gets its window in more
    r = arr_lid.listen(media, 1, [200.0], "eng", cache=cache, more=[600.0])
    assert [(w["at"], w["secs"]) for w in r["windows"]] == [(200.0, arr_lid.WORD_SECS), (600.0, arr_lid.THIRD_SECS)]
    # two windows that both hear too little get none: one more window cannot give two good ones
    r = arr_lid.listen(media, 1, [200.0, 450.0], "eng", cache=cache, more=[150.0, 1100.0])
    assert [w["at"] for w in r["windows"]] == [200.0, 450.0]


def test_jobs_hear_the_windows_the_hook_would_pick(ears, tmp_path):
    """After a language check, the subtitle check's hearing runs in the same process. The hook's own check then finds
    the words in the cache."""
    import arr_subsync
    calls, media, cache = ears
    cues = [[60.0 + 3 * i, 62.0 + 3 * i, f"garden window lantern number{i}"] for i in range(400)]
    spec = tmp_path / "jobs.json"
    spec.write_text(json.dumps([{"index": 1, "lang": "eng", "cues": cues, "duration": 1320.0}]))
    (got,) = arr_lid.jobs(media, str(spec), cache)
    first = arr_subsync.windows(cues, 1320.0, arr_decide.STOPWORDS["eng"])
    assert got == {"index": 1, "starts": first} and arr_lid.words_get(cache, media, 1, arr_lid.tag(arr_lid.MODEL), "eng", first) is not None


def test_the_sweep_hears_each_group_as_one_clip_in_one_process(monkeypatch, capsys):
    """--group hears the --words windows two at a time through listen(), so the model loads once and each pair is
    cached as the subtitle check caches its windows."""
    calls = []
    monkeypatch.setattr(arr_lid, "listen", lambda path, idx, starts, lang, secs, **kw: calls.append(starts) or
                        {"windows": [{"at": s, "secs": secs, "words": []} for s in starts], "cached": len(calls) > 1, "reused": 0, "took": 1.5,
                         "model": "small@x", "profile": {"whisper": [2.0, 2.5]}})
    assert arr_lid.main(["f.mkv", "0", "600", "--words", "eng", "10", "70", "130", "--secs", "10", "--group", "2"]) == 0
    got = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert calls == [[10.0, 70.0], [130.0]] and [w["at"] for w in got["windows"]] == [10.0, 70.0, 130.0], (calls, got)
    assert not got["cached"] and got["took"] == 3.0 and got["profile"] == {"whisper": [4.0, 5.0]}


def test_the_sweep_yields_between_groups_when_a_job_or_a_hearing_waits(monkeypatch, tmp_path):
    """A job file in the queue, or another hearing at the gate, stops the sweep after the group it is on. The first
    group always runs, so a sweep never stalls."""
    calls, queue, gate = [], tmp_path / "queue", tmp_path / "lid.turn.gate"
    queue.mkdir()
    monkeypatch.setattr(arr_lid, "listen", lambda path, idx, starts, lang, secs, **kw: calls.append(starts) or
                        {"windows": [{"at": s, "secs": secs, "words": []} for s in starts], "cached": False, "reused": 0, "took": 1.0, "model": "m"})
    (queue / "1-2.json").write_text("{}")
    got = arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0, 190.0], "eng", 2, gate=str(gate), queue=str(queue))
    assert got["yielded"] and calls == [[10.0, 70.0]] and len(got["windows"]) == 2
    (queue / "1-2.json").unlink()
    calls.clear()
    import fcntl
    with open(gate, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)   # a hook job waits for the model
        assert arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=str(queue)).get("yielded") and len(calls) == 1
    calls.clear()
    assert "yielded" not in arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=str(queue)) and len(calls) == 2
