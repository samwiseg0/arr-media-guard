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
"""Unit tests for lid.py, the spoken-language identification.

The detector is mocked, so these run with the stdlib alone. One test runs the real model. It runs only where
faster-whisper and a model directory exist (ARR_LID_MODEL_DIR, default /opt/arr-media-guard-lid/models).

Run: pytest tests/test_arr_lid.py
"""
import array
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

import amg

FILES = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, FILES)
from arr_media_guard import decide as arr_decide, ff, lid as arr_lid, store  # noqa: E402


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
    for t, b in {**arr_lid.ALIAS, **{t: b for b, t in arr_decide.TWINS.items()}}.items():
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


def test_the_spans_of_speech_come_from_the_cache_and_follow_an_edit(tmp_path, monkeypatch):
    """speech() answers from the cache by path, size, mtime, stream and the VAD settings, with no model and no ffmpeg.
    An edit changes the mtime and never the audio, so carry() moves the spans to the new stat. A change of the VAD
    settings misses the row, so the audio is read again, and so does a new faster-whisper, which bundles the VAD model.
    A row of spans from before the file clock, see ff.CLOCK, misses too."""
    media, cache = tmp_path / "a.mkv", str(tmp_path / "lid.sqlite")
    media.write_bytes(b"x")
    st, spans = os.stat(media), [[1.0, 2.5], [4.0, 6.0]]
    row = lambda how: (str(media), st.st_size, st.st_mtime_ns, 0, how, json.dumps(spans), time.time())
    with contextlib.closing(arr_lid._db(cache)) as db, db:
        db.execute("INSERT INTO speech VALUES (?, ?, ?, ?, ?, ?, ?)", row(arr_lid.speech_how().removesuffix("/" + ff.CLOCK)))
    with pytest.raises(Exception):   # a miss reads the audio, and this file holds none
        arr_lid.speech(str(media), 0, cache)
    with contextlib.closing(arr_lid._db(cache)) as db, db:
        db.execute("INSERT INTO speech VALUES (?, ?, ?, ?, ?, ?, ?)", row(arr_lid.speech_how()))
    assert arr_lid.speech(str(media), 0, cache) == {"spans": spans, "cached": True, "took": pytest.approx(0, abs=1)}
    os.utime(media, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    arr_lid.carry(str(media), st, cache)
    assert arr_lid.speech(str(media), 0, cache)["spans"] == spans
    how = arr_lid.speech_how()
    monkeypatch.setattr("importlib.metadata.version", lambda name: "9.9.9")
    assert arr_lid.speech_how() == how.replace(how.split("/")[0], "silero@faster-whisper-9.9.9") != how
    monkeypatch.setattr(arr_lid, "speech_how", lambda: "0.6/0.4/0.5")
    with pytest.raises(Exception):   # a miss reads the audio, and this file holds none
        arr_lid.speech(str(media), 0, cache)


SPEECH = """
import json, sys, types
sys.path.insert(0, sys.argv[1])
import numpy as np
vad = types.ModuleType("faster_whisper.vad")
vad.get_vad_model = lambda: lambda a: (np.abs(a.reshape(-1, 512)).max(axis=1) > 0.1).astype(np.float32)   # loud frames speak
sys.modules["faster_whisper"], sys.modules["faster_whisper.vad"] = types.ModuleType("faster_whisper"), vad
from arr_media_guard import lid
print(json.dumps(lid.speech(sys.argv[2], 0, sys.argv[3])["spans"]))
"""   # the spans of speech of a file, in a Python that holds numpy


def numpy_python():
    """This Python when it holds numpy, else the venv of the language detection install that ARR_LID_DIR names, where
    the speech read runs. Skips with neither."""
    for py in (sys.executable, os.path.join(os.environ.get("ARR_LID_DIR", "/opt/arr-media-guard-lid"), "venv", "bin", "python")):
        if os.path.exists(py) and subprocess.run([py, "-c", "import numpy"], capture_output=True).returncode == 0:
            return py
    pytest.skip("needs numpy here or in the venv of ARR_LID_DIR")


CLOCK_VIDEO = ["-f", "lavfi", "-i", "color=size=32x32:rate=5:duration=50"]
CLOCK_AUDIO = ["-f", "lavfi", "-i", "aevalsrc=if(between(t\\,5\\,7)+between(t\\,15\\,17)\\,0.5*sin(2*PI*440*t)\\,0):s=16000:d=25"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("mkvmerge") is None, reason="no ffmpeg or no mkvmerge")
@pytest.mark.parametrize("shape, at", [("in time", (5, 15)), ("1.5 s late", (6.5, 16.5)), ("5 s late", (10, 20)), ("22 s late", (27, 37)),
                                       ("a gap of 4 s", (5, 19)), ("MPEG-TS", (5, 15)), ("MP4 edit list", (7, 17)), ("MP4 from 10 s", (5.064, 15.064))])
def test_the_speech_runs_on_the_files_clock(tmp_path, shape, at):
    """The audio holds a tone at 5 s and 15 s of its own samples, and the fake VAD hears the tone as speech. speech()
    decodes through aresample, which puts each frame at its time from the file's start. So the speech of a track that
    starts late, or that has a gap, sits where the cues and the speech onsets put it, within 50 ms. ffprobe reads the
    start of the tracks 5 s and 22 s late as 0. An MPEG-TS starts at 1.4 s and the last MP4 at 9.936 s, and the
    onsets and the spans count from there. at is where each tone starts on the file's clock."""
    from arr_media_guard import subtitles
    ff = lambda *argv: amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", *argv], check=True)
    path = tmp_path / {"MPEG-TS": "f.ts", "MP4 edit list": "f.mp4", "MP4 from 10 s": "f.mp4"}.get(shape, "f.mkv")
    if path.suffix == ".mkv":
        ff(*CLOCK_VIDEO, *CLOCK_AUDIO, "-c:v", "mpeg4", "-c:a", "flac", str(tmp_path / "base.mkv"))
        if shape == "a gap of 4 s":   # every audio packet from 10 s on moves 4 s later
            ff("-i", str(tmp_path / "base.mkv"), "-map", "0", "-c", "copy", "-bsf:a", "setts=ts=if(gte(PTS*TB\\,10)\\,PTS+4/TB\\,PTS)", str(tmp_path / "gap.mkv"))
        srt = tmp_path / "cues.srt"
        srt.write_text("".join(f"{k + 1}\n00:00:{t:06.3f} --> 00:00:{t + 2:06.3f}\nLine {k + 1}\n\n".replace(".", ",") for k, t in enumerate(at)))
        late = {"1.5 s late": "1500", "5 s late": "5000", "22 s late": "22000"}.get(shape)
        subprocess.run(["mkvmerge", "-q", "-o", str(path), *(["--sync", f"1:{late}"] if late else []),
                        str(tmp_path / ("gap.mkv" if shape == "a gap of 4 s" else "base.mkv")), str(srt)], check=True)
    else:
        audio = ["-itsoffset", "2", *CLOCK_AUDIO] if shape == "MP4 edit list" else CLOCK_AUDIO
        ff(*CLOCK_VIDEO, *audio, "-c:v", "mpeg4", *(["-c:a", "mp2", "-f", "mpegts"] if shape == "MPEG-TS" else ["-c:a", "aac"]),
           *(["-output_ts_offset", "10"] if shape == "MP4 from 10 s" else []), str(path))
    got = subprocess.run([numpy_python(), "-c", SPEECH, FILES, str(path), str(tmp_path / "lid.sqlite")], capture_output=True, text=True, check=True)
    starts = [a for a, _ in json.loads(got.stdout)]
    j = {"tracks": [{"type": "audio", "properties": {"audio_channels": 1}}]}
    onsets = [t for t, quiet in subtitles.onsets(str(path), j, 0, [(0.0, 50.0)]) if quiet >= 1 and t < at[-1] + 5]   # the audio ends there
    assert len(starts) == len(onsets) == 2 and all(abs(a - b) <= 0.05 and abs(a - t) <= 0.1 for a, b, t in zip(starts, onsets, at)), (starts, onsets)
    if path.suffix == ".mkv":
        j = json.loads(subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, check=True).stdout)
        cues = [c[0] for c in subtitles.subtitle_cues(str(path), j, {"s1"})["s1"]]
        assert cues == list(at) and all(abs(a - c) <= 0.05 for a, c in zip(starts, cues)), (starts, cues)


def clock_file(tmp_path, shape, audio=CLOCK_AUDIO):
    """A made-up Matroska file of CLOCK_VIDEO and audio, with the audio 22 s late ("22 s late") or with every audio
    packet from 10 s on 4 s later ("a gap of 4 s")."""
    base, path = tmp_path / "base.mkv", tmp_path / "f.mkv"
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", *CLOCK_VIDEO, *audio, "-c:v", "mpeg4", "-c:a", "flac", str(base)], check=True)
    if shape == "a gap of 4 s":
        amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(base), "-map", "0", "-c", "copy", "-bsf:a",
                    "setts=ts=if(gte(PTS*TB\\,10)\\,PTS+4/TB\\,PTS)", str(path)], check=True)
    else:
        subprocess.run(["mkvmerge", "-q", "-o", str(path), "--sync", "1:22000", str(base)], check=True)
    return path


def sound_starts(pcm, at):
    """The file times of each start of sound in raw mono 16 kHz PCM that starts at at seconds: a sample over -20 dB
    after 0.1 s or more of quiet."""
    out, quiet = [], 1600
    for k, x in enumerate(array.array("h", pcm[:len(pcm) // 2 * 2])):
        if abs(x) > 3277 and quiet >= 1600:
            out.append(round(at + k / 16000, 3))
        quiet = 0 if abs(x) > 3277 else quiet + 1
    return out


CLOCK_51 = ["-f", "lavfi", "-i", "aevalsrc=0|0|if(between(t\\,5\\,7)+between(t\\,15\\,17)\\,0.9*sin(2*PI*440*t)\\,0)|0|0|0:s=48000:d=25:c=5.1"]
# the tones of CLOCK_AUDIO in the centre channel of a 5.1 track at 48 kHz


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("mkvmerge") is None, reason="no ffmpeg or no mkvmerge")
@pytest.mark.parametrize("shape, window, at, audio", [("22 s late", (12, 30), (27, 37), CLOCK_AUDIO), ("22 s late", (30, 15), (37,), CLOCK_AUDIO),
                                                     ("a gap of 4 s", (0, 30), (5, 19), CLOCK_AUDIO), ("a gap of 4 s", (12, 15), (19,), CLOCK_AUDIO),
                                                     ("22 s late", (12, 30), (27, 37), CLOCK_51), ("a gap of 4 s", (12, 15), (19,), CLOCK_51)])
def test_a_window_of_extract_runs_on_the_files_clock(tmp_path, shape, window, at, audio):
    """extract() seeks to the start of the window, and ff.clock() fills a late start and a gap with silence. So each
    tone sits at the start of the window plus its sample offset, within 50 ms of its time in the file. The first window
    of the late track starts inside its delay and the second after it. A gap window starts before the gap or inside it.
    The read of 2.18.8 put the last tone of each window 10 s, 0 s, 4 s and 2 s early. A 5.1 track at 48 kHz mixes to
    mono 16 kHz before the fill, and its centre channel counts."""
    got = sound_starts(arr_lid.extract(str(clock_file(tmp_path, shape, audio)), 0, *window), window[0])
    assert len(got) == len(at) and all(abs(g - t) <= 0.05 for g, t in zip(got, at)), got


GAP = """
import json, resource, sys
sys.path.insert(0, sys.argv[1])
from arr_media_guard import lid, subtitles
rss = lambda: resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
open(sys.argv[3], "wb").write(lid.extract(sys.argv[2], 0, 0.0, 1830.0))
read = rss()
j = {"tracks": [{"type": "audio", "properties": {"audio_channels": 6}}]}
on = subtitles.onsets(sys.argv[2], j, 0, [(0.0, 600.0), (1210.0, 1820.0)])
print(json.dumps({"read": read, "all": rss(), "onsets": on}))
"""   # the audio of the whole gap shape, its onsets in a part of 600 s and in one that starts 600 s before the gap
# ends, and the peak RSS of their ffmpeg in MB, in a process of its own


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_a_gap_of_30_minutes_in_a_51_track_fills_on_mono_16_khz(tmp_path):
    """A 5.1 AAC track at 48 kHz sounds from 0 to 10 s and from 10 to 20 s of its samples, and every packet from 10 s on
    sits 30 minutes later. ff.clock() mixes to mono 16 kHz before aresample fills the gap, so the ffmpeg of the read
    peaks under 300 MB, and the gap holds no sound. A fill before the mix peaked at 2.3 GB and left 30 ms of sound at
    941.8 s, inside the gap. The onsets fill at 48 kHz, 600 s at most in a part, and peak under 300 MB too. The one
    onset is the sound after the gap."""
    base, path, raw = tmp_path / "base.mkv", tmp_path / "gap.mkv", tmp_path / "gap.raw"
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*440*t):s=48000:d=20:c=5.1", "-c:a", "aac",
                str(base)], check=True)
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(base), "-c", "copy", "-bsf:a", "setts=ts=if(gte(PTS*TB\\,10)\\,PTS+1800/TB\\,PTS)",
                str(path)], check=True)
    got = json.loads(subprocess.run([sys.executable, "-c", GAP, FILES, str(path), str(raw)], capture_output=True, text=True, check=True).stdout)
    pcm, at = raw.read_bytes(), lambda t: int(t * 16000) * 2
    assert got["read"] < 300 and got["all"] < 300 and len(pcm) == pytest.approx(at(1820), abs=at(0.1)), (got, len(pcm) / 32000)
    assert [t for t, _ in got["onsets"]] == [pytest.approx(1810, abs=0.05)], got["onsets"]
    assert not pcm[at(10.05):at(1809.95)].strip(b"\0"), "sound inside the gap"
    assert sound_starts(pcm[:at(10)], 0) == [pytest.approx(0, abs=0.05)] and sound_starts(pcm[at(1809):], 1809) == [pytest.approx(1810, abs=0.05)]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
@pytest.mark.parametrize("codec, layout", [("eac3", "5.1(side)"), ("aac", "5.1")])
def test_the_clock_mixes_a_healthy_51_track_as_ac_1_does(tmp_path, codec, layout):
    """A healthy 5.1 track with a tone of its own in each channel, the centre included. Its whole decode through
    ff.clock() equals the decode of 2.18.8 with -ac 1 -ar 16000 alone, sample for sample, as lid.speech() reads it. A
    float aformat would mix 5.1 with another matrix, so CLOCK asks for s16."""
    path = tmp_path / "h.mkv"
    tones = "|".join(f"0.3*sin(2*PI*{f}*t)" for f in (300, 500, 700, 60, 900, 1100))
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"aevalsrc={tones}:s=48000:d=20:c={layout}", "-c:a", codec, str(path)],
               check=True)
    read = lambda *af: subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
                                       *af, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    old, new = read(), read("-af", ff.clock())
    assert len(new) == len(old) > 600000 and new == old


LOUD_AUDIO = ["-f", "lavfi", "-i", "aevalsrc=if(between(t\\,0\\,2)+between(t\\,5\\,7)+between(t\\,8\\,12)+between(t\\,15\\,17)\\,"
              "0.5*sin(2*PI*440*t)\\,0):s=16000:d=25"]   # sound from the first sample, and across 10 s, where the gap goes


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("mkvmerge") is None, reason="no ffmpeg or no mkvmerge")
@pytest.mark.parametrize("shape, part, at", [("22 s late", (10, 50), [(22, 12), (27, 3), (30, 1), (37, 3)]),
                                            ("22 s late", (25, 50), [(27, 2), (30, 1), (37, 3)]),
                                            ("a gap of 4 s", (0, 30), [(5, 3), (8, 1), (14, 4), (19, 3)])])
def test_the_speech_onsets_count_a_late_start_and_a_gap_as_silence(tmp_path, shape, part, at):
    """The audio holds sound from its first sample and across 10 s. ff.clock() fills a late start and a gap with
    silence, so silencedetect ends a silence where the sound starts after either one. at holds each onset and the
    seconds of silence before it, on the file's clock, within 50 ms. The read of 2.18.8 lost the onsets at 22 s and 14 s."""
    from arr_media_guard import subtitles
    subtitles.ONSETS.clear()
    j = {"tracks": [{"type": "audio", "properties": {"audio_channels": 1}}]}
    got = [(t, q) for t, q in subtitles.onsets(str(clock_file(tmp_path, shape, LOUD_AUDIO)), j, 0, [part]) if t < at[-1][0] + 5]   # the audio ends there
    assert len(got) == len(at) and all(abs(t - a) <= 0.05 and abs(q - b) <= 0.05 for (t, q), (a, b) in zip(got, at)), got


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_the_onsets_of_a_51_track_come_from_its_centre_channel(tmp_path):
    """The centre channel of a 5.1 track holds tones at 5 s and 15 s, and the front pair a bed that never stops. The
    onsets hear the centre alone before ff.clock(), so they start at the tones. The mono mix of ff.clock() alone holds
    the bed, which has no silence."""
    from arr_media_guard import subtitles
    path, bed = tmp_path / "c.mkv", "0.5*sin(2*PI*220*t)"
    centre = "if(between(t\\,5\\,7)+between(t\\,15\\,17)\\,0.5*sin(2*PI*440*t)\\,0)"
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"aevalsrc={bed}|{bed}|{centre}|0|0|0:s=48000:d=25:c=5.1", "-c:a", "flac",
                str(path)], check=True)
    subtitles.ONSETS.clear()
    got = subtitles.onsets(str(path), {"tracks": [{"type": "audio", "properties": {"audio_channels": 6}}]}, 0, [(0.0, 25.0)])
    assert [t for t, q in got if q >= 1] == [pytest.approx(5, abs=0.05), pytest.approx(15, abs=0.05)], got


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_the_onsets_hear_a_track_at_its_own_sample_rate(tmp_path):
    """A 48 kHz track holds bursts of 10 kHz at 5 s and 15 s. The onsets fill the clock at the track's rate, so they find
    both bursts, as silencedetect found them before the clock. At 16 kHz the bursts lie past the band, and on a film
    about 3 percent of the onsets came out other."""
    from arr_media_guard import subtitles
    path = tmp_path / "hi.mkv"
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                "aevalsrc=if(between(t\\,5\\,7)+between(t\\,15\\,17)\\,0.5*sin(2*PI*10000*t)\\,0):s=48000:d=25", "-c:a", "flac", str(path)], check=True)
    subtitles.ONSETS.clear()
    got = subtitles.onsets(str(path), {"tracks": [{"type": "audio", "properties": {"audio_channels": 1}}]}, 0, [(0.0, 25.0)])
    assert [t for t, q in got if q >= 1] == [pytest.approx(5, abs=0.05), pytest.approx(15, abs=0.05)], got


HEAR = """
import json, sys
sys.path.insert(0, sys.argv[1])
import numpy as np
from arr_media_guard import burnin
vad = lambda a: (np.abs(a.reshape(-1, 512)).max(axis=1) > 0.1).astype(np.float32)   # loud frames speak
print(json.dumps([burnin.hear_window(sys.argv[2], 0, a, b, vad)[0] for a, b in json.loads(sys.argv[3])]))
"""   # the spans of speech of burn-in windows, in a Python that holds numpy


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("mkvmerge") is None, reason="no ffmpeg or no mkvmerge")
@pytest.mark.parametrize("shape, windows, at", [("22 s late", [(12, 42), (30, 45)], [[27, 37], [37]]),
                                               ("a gap of 4 s", [(0, 30), (12, 27)], [[5, 19], [19]])])
def test_the_burn_in_windows_hear_speech_on_the_files_clock(tmp_path, shape, windows, at):
    """burnin.hear_window() reads its window with extract(), so a span of speech starts where the tone starts in the
    file, within 50 ms, in a window that starts inside a late start or a gap and in one after it."""
    path = clock_file(tmp_path, shape)
    got = subprocess.run([numpy_python(), "-c", HEAR, FILES, str(path), json.dumps(windows)], capture_output=True, text=True, check=True)
    starts = [[a for a, _ in spans] for spans in json.loads(got.stdout)]
    assert [len(s) for s in starts] == [len(t) for t in at] and all(abs(a - t) <= 0.05 for s, w in zip(starts, at) for a, t in zip(s, w)), starts


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_the_speech_read_asks_waits_with_its_job(tmp_path, monkeypatch):
    src = tmp_path / "a.mkv"
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=12", "-c:a", "flac", str(src)], check=True)
    np = pytest.importorskip("numpy")
    vad = type(sys)("faster_whisper.vad")
    vad.get_vad_model = lambda: lambda a: np.zeros(len(a) // 512, np.float32)
    monkeypatch.setitem(sys.modules, "faster_whisper", type(sys)("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setattr(arr_lid, "SPEECH_SECS", 4)
    asked = []
    monkeypatch.setattr(arr_lid, "waits", lambda gate, queue, job=None: asked.append((queue, job)) or False)
    assert arr_lid.speech(str(src), 0, str(tmp_path / "lid.sqlite"), None, "q", ("deep-analysis-x.json", 3, 2))["spans"] == []
    assert asked and set(asked) == {("q", ("deep-analysis-x.json", 3, 2))}, asked


def test_a_run_takes_the_model_lock_of_its_turn(tmp_path, monkeypatch, capsys):
    """--slot K names the hook's turn at the model, see checks.lid_turn(). identify() and listen() then lock
    cache.lock.K, so hearings of other turns run at once. A run with no turn takes cache.lock."""
    cache = str(tmp_path / "lid.sqlite")
    monkeypatch.setattr(arr_lid, "SLOT", 0)
    assert arr_lid.model_lock(cache) == cache + ".lock"
    locks = []
    monkeypatch.setattr(arr_lid, "identify", lambda *a, **k: locks.append(arr_lid.model_lock(a[6])) or {"lang": "eng"})
    assert arr_lid.main(["f.mkv", "0", "600", "--cache", cache, "--slot", "2"]) == 0 and locks == [cache + ".lock.2"], locks
    capsys.readouterr()


def test_the_speech_read_yields_between_its_chunks(tmp_path, monkeypatch, capsys):
    """--speech passes the gate and the queue to speech(), and checks.lid_speech() passes them to lid.py. speech()
    asks waits() after each chunk, stops ffmpeg, and caches nothing."""
    got = []
    monkeypatch.setattr(arr_lid, "speech", lambda path, idx, cache, gate=None, queue=None, job=None: got.append((gate, queue, job)) or {"yielded": True})
    assert arr_lid.main(["f.mkv", "0", "600", "--speech", "--yield-queue", "q.sqlite", "--yield-job", "deep-analysis-x.json", "4", "2"]) == 0, got
    assert got == [(None, "q.sqlite", ("deep-analysis-x.json", 4, 2))], got
    assert json.loads(capsys.readouterr().out.strip())["yielded"]


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
    amg.ffmpeg(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=300", "-c:a", "flac",
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


def test_words_and_kept_audio_from_before_the_file_clock_read_again(ears):
    """Versions before ff.CLOCK cut the audio from the first packet. Their words rows name the model alone, so they miss
    and listen() hears again. Their kept audio sat in table pcm, which _db() drops."""
    calls, media, cache = ears
    st, tag, starts = os.stat(media), arr_lid.tag(arr_lid.MODEL), [200.0, 1000.0]
    os.makedirs(os.path.dirname(cache))
    with contextlib.closing(sqlite3.connect(cache)) as db, db:
        db.execute("CREATE TABLE pcm (path TEXT, size INTEGER, mtime_ns INTEGER, idx INTEGER, start INTEGER, secs INTEGER, pcm BLOB, at REAL)")
        db.execute("INSERT INTO pcm VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (media, st.st_size, st.st_mtime_ns, 1, 200, 30, b"\0" * 64, time.time()))
    with contextlib.closing(arr_lid._db(cache)) as db, db:
        old = [{"at": s, "secs": arr_lid.WORD_SECS, "words": [[0.25, "old"]]} for s in starts]
        db.execute("INSERT INTO words VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (media, st.st_size, st.st_mtime_ns, 1, tag, "eng",
                                                                           json.dumps([[s, arr_lid.WORD_SECS] for s in starts]), json.dumps(old), time.time()))
        assert not db.execute("SELECT name FROM sqlite_master WHERE name = 'pcm'").fetchall()
    assert arr_lid.words_get(cache, media, 1, tag, "eng", starts) is None and arr_lid.windows_get(cache, media, 1, tag, "eng") == []
    r = arr_lid.listen(media, 1, starts, "eng", cache=cache)
    assert not r["cached"] and calls["extract"] == [(1, 200.0, arr_lid.WORD_SECS), (1, 1000.0, arr_lid.WORD_SECS)] and r["model"] == tag
    assert arr_lid.listen(media, 1, starts, "eng", cache=cache)["cached"] and arr_lid.words_get(cache, media, 1, tag, "eng", starts) == r["windows"]


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


def test_every_window_the_cache_holds_comes_once_in_time_order(ears):
    """Two hearings heard the window at 200 s, each in its own pair. It comes once, from the first. Another language,
    a file that changed and a cache that does not exist give none, and the look never creates the cache."""
    calls, media, cache = ears
    tag = arr_lid.tag(arr_lid.MODEL)
    assert arr_lid.windows_get(cache, media, 1, tag, "eng") == [] and not os.path.exists(cache)
    first = arr_lid.listen(media, 1, [1000.0, 200.0], "eng", cache=cache)["windows"]
    arr_lid.listen(media, 1, [200.0, 600.0], "eng", cache=cache)
    got = arr_lid.windows_get(cache, media, 1, tag, "eng")
    assert [w["at"] for w in got] == [200.0, 600.0, 1000.0] and got[0] == first[1], got
    assert arr_lid.windows_get(cache, media, 1, tag, "spa") == [] and arr_lid.windows_get(cache, media, 0, tag, "eng") == []
    with open(media, "ab") as f:
        f.write(b"y")
    assert arr_lid.windows_get(cache, media, 1, tag, "eng") == []


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
    from arr_media_guard import subsync as arr_subsync
    calls, media, cache = ears
    cues = [[60.0 + 3 * i, 62.0 + 3 * i, f"garden window lantern number{i}"] for i in range(400)]
    spec = tmp_path / "jobs.json"
    spec.write_text(json.dumps([{"index": 1, "lang": "eng", "cues": cues, "duration": 1320.0}]))
    (got,) = arr_lid.jobs(media, str(spec), cache)
    first = arr_subsync.windows(cues, 1320.0, arr_decide.STOPWORDS["eng"])
    assert got == {"index": 1, "starts": first}
    assert arr_lid.words_get(cache, media, 1, arr_lid.tag(arr_lid.MODEL), "eng", first, arr_subsync.WINDOW) is not None   # the hook's key


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
    """An import job in the queue of the hook's state store, or another hearing at the gate, stops the sweep after the
    group it is on. The first group always runs, so a sweep never stalls. A deep analysis job and a job put back for
    later never stop it."""
    calls, queue, gate = [], str(tmp_path / "state.sqlite"), tmp_path / "lid.turn.gate"
    with contextlib.closing(sqlite3.connect(queue, isolation_level=None)) as db:
        for t in store.TABLES:   # the hook's store, as store.db() makes it
            db.execute(t)
        db.execute("INSERT INTO jobs (name, at, job) VALUES ('deep-analysis-ab.json', 0, '{}')")
        db.execute("INSERT INTO jobs (name, due, at, job) VALUES ('9-2.json', ?, 0, '{}')", (time.time_ns() + 10**12,))
    monkeypatch.setattr(arr_lid, "listen", lambda path, idx, starts, lang, secs, **kw: calls.append(starts) or
                        {"windows": [{"at": s, "secs": secs, "words": []} for s in starts], "cached": False, "reused": 0, "took": 1.0, "model": "m"})
    assert "yielded" not in arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=queue) and len(calls) == 2
    calls.clear()
    with contextlib.closing(sqlite3.connect(queue, isolation_level=None)) as db:
        db.execute("INSERT INTO jobs (name, at, job) VALUES ('1-2.json', 0, '{}')")
    got = arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0, 190.0], "eng", 2, gate=str(gate), queue=queue)
    assert got["yielded"] and calls == [[10.0, 70.0]] and len(got["windows"]) == 2
    calls.clear()   # in a job process the import counts only while every slot is taken, see queued()
    job = lambda k: (f"deep-analysis-{k}.json", 3, 2)
    assert "yielded" not in arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=queue, job=job(0)) and len(calls) == 2
    with contextlib.closing(sqlite3.connect(queue, isolation_level=None)) as db:
        db.executemany("INSERT INTO jobs (name, at, job, claimed) VALUES (?, 0, '{}', 1)", [(f"deep-analysis-{k}.json",) for k in range(3)])
    calls.clear()
    assert arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=queue, job=job(0)).get("yielded") and len(calls) == 1
    calls.clear()   # job 0 yields, so its slot is free for the import, and job 1 goes on. Job 0 yields at every ask.
    assert "yielded" not in arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=queue, job=job(1)) and len(calls) == 2
    assert arr_lid.queued(queue, job(0)) and not arr_lid.queued(queue, job(2))
    with contextlib.closing(sqlite3.connect(queue, isolation_level=None)) as db:
        db.execute("DELETE FROM jobs WHERE claimed = 1")
    swept, real = [], arr_lid.sweep
    monkeypatch.setattr(arr_lid, "sweep", lambda *a, **k: swept.append(a) or {"windows": []})
    assert arr_lid.main(["f.mkv", "0", "600", "--words", "eng", "10", "--group", "2", "--yield-queue", queue, "--yield-job", *map(str, job(0))]) == 0
    assert swept[0][7:9] == (queue, job(0)), swept
    monkeypatch.setattr(arr_lid, "sweep", real)
    with contextlib.closing(sqlite3.connect(queue, isolation_level=None)) as db:
        db.execute("DELETE FROM jobs WHERE name = '1-2.json'")
    (tmp_path / "queue").mkdir()   # a job file the hook wrote while the store was busy
    (tmp_path / "queue" / ".3-2.json").write_text("{")   # half a job is no job
    assert not arr_lid.waits(None, queue)
    (tmp_path / "queue" / "3-2.json").write_text("{}")
    assert arr_lid.waits(None, queue)
    (tmp_path / "queue" / "3-2.json").unlink()
    gone = str(tmp_path / "other" / "state.sqlite")   # a store that does not read is no import, and the look writes nothing
    assert not arr_lid.waits(None, gone) and not os.path.exists(gone)
    calls.clear()
    import fcntl
    with open(gate, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)   # a hook job waits for the model
        assert arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=str(queue)).get("yielded") and len(calls) == 1
    calls.clear()
    assert "yielded" not in arr_lid.sweep("f.mkv", 0, [10.0, 70.0, 130.0], "eng", 2, gate=str(gate), queue=str(queue)) and len(calls) == 2
