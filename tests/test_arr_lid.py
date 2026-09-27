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
