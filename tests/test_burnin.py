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
"""Unit tests for burnin.py, the burned-in subtitle check.

The rules run on made-up boxes and lines with the stdlib alone. The tests on media make clips with ffmpeg: a plain
background, a tone while someone "speaks", and made-up subtitle lines burned in with the subtitles filter. A fake
Silero VAD hears the tone as speech. They run only where numpy, onnxruntime, PyAV, ffmpeg and the models exist
(ARR_LID_MODEL_DIR, default /opt/arr-media-guard-lid/models, with the models in its ocr folder).

Run: pytest tests/test_burnin.py
"""
import fcntl
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, FILES)
from arr_media_guard import burnin, lid  # noqa: E402

MODEL_DIR = os.environ.get("ARR_LID_MODEL_DIR", lid.MODEL_DIR)
W, H = 640, 352


@pytest.fixture(autouse=True)
def priority(monkeypatch):
    """main() lowers the priority of its process. A test records the call and keeps its own priority."""
    calls = []
    monkeypatch.setattr(burnin.lid, "lower_priority", lambda threads: calls.append(threads))
    return calls


def box(cx, cy, w, h):
    """A line box of the detector, centred at cx, cy, as shares of a W x H frame."""
    return (int((cx - w / 2) * W), int((cy - h / 2) * H), int((cx + w / 2) * W), int((cy + h / 2) * H), 0.9)


def frames(kinds, sub_at, dur=1000.0):
    """Frames spread evenly over dur. sub_at(i, kind) says whether frame i has a subtitle line."""
    return [{"kind": k, "at": dur * (i + 0.5) / len(kinds), "sub": {"bottom": [1]} if sub_at(i, k) else {}} for i, k in enumerate(kinds)]


# --- the rules, stdlib only -------------------------------------------------------------------------------------------

def test_windows_sit_at_their_centres_and_merge_in_a_short_file():
    tens = [(k + 0.5) / 10 for k in range(10)]
    assert burnin.windows(3000, tens) == [[150 * (2 * k + 1) - 15, 150 * (2 * k + 1) + 15] for k in range(10)]
    assert burnin.windows(120, tens) == [[0.0, 120]]
    nineteen = burnin.windows(3000, tens + [k / 10 for k in range(1, 10)])
    assert len(nineteen) == 19 and [a for a, _ in nineteen] == [150 * k - 15 for k in range(1, 20)]


def test_spread_order_covers_the_range_at_every_prefix():
    assert burnin.spread(5) == [0, 4, 2, 1, 3] and burnin.spread(1) == [0] and sorted(burnin.spread(100)) == list(range(100))
    assert burnin.quantile_points([[0, 10], [20, 30]], 4, 0.5) == [2.75, 7.25, 22.75, 27.25]
    assert burnin.quantile_points([[0, 0.8]], 4, 0.5) == []   # a span under two pads holds no point


def test_the_label_needs_share_lift_and_spread():
    """full needs 0.40 of the speech frames, a lift of 0.10 over the silence frames, and 4 tenths of the file. partial
    needs 0.08 and 2 tenths, and the same lift. Each case misses one condition by one frame."""
    kinds = ["speech"] * 20 + ["silence"] * 20
    v = lambda sub_at: burnin.verdict(frames(kinds, sub_at), 1000.0)
    assert v(lambda i, k: k == "speech" and i % 5 < 2)["label"] == "full"   # 8 of 20, in 5 tenths
    assert v(lambda i, k: k == "speech" and i % 5 < 2 and i != 0)["label"] == "partial"   # 7 of 20 is under 0.40
    lifted = v(lambda i, k: (k == "speech" and i % 5 < 2) or (k == "silence" and i < 26))
    assert (lifted["lift"], lifted["label"]) == (0.1, "full")
    assert v(lambda i, k: (k == "speech" and i % 5 < 2) or (k == "silence" and i < 27))["label"] == "none"   # a lift of 0.05 makes neither
    assert v(lambda i, k: k == "speech" and i in (0, 15))["label"] == "partial"
    assert v(lambda i, k: k == "speech" and i in (0, 1))["label"] == "none"   # 2 of 20 bunched in one tenth
    assert v(lambda i, k: k == "speech" and i == 0)["label"] == "none"   # 1 of 20 is under 0.08
    # the spread counts tenths of the whole file: hits in 4 tenths make full, 3 do not
    spread = lambda tenths: burnin.verdict([{"kind": "speech", "at": t * 100 + 50, "sub": {"bottom": [1]}} for t in tenths], 1000.0)["label"]
    assert (spread([0, 3, 6, 9]), spread([0, 3, 6, 6])) == ("full", "partial")
    assert burnin.verdict([], 0)["label"] == "none"


def test_analyse_keeps_subtitle_lines_and_drops_overlays_and_crowds():
    """A clock in the same place in every frame is an overlay. A line in the top band counts as a top subtitle. Two
    centred lines between the bands make a frame credits. A line off centre, too thin or too short is no subtitle."""
    clock = box(0.5, 0.9, 0.2, 0.04)
    fs = [{"kind": "speech", "boxes": [clock]} for _ in range(8)]
    fs[0]["boxes"].append(box(0.5, 0.82, 0.5, 0.04))
    fs[1]["boxes"].append(box(0.5, 0.08, 0.45, 0.04))
    fs[2]["boxes"] += [box(0.5, 0.82, 0.4, 0.04), box(0.5, 0.4, 0.3, 0.04), box(0.5, 0.5, 0.3, 0.04)]
    fs[3]["boxes"].append(box(0.75, 0.82, 0.3, 0.04))
    fs[4]["boxes"].append(box(0.5, 0.82, 0.5, 0.01))
    fs[5]["boxes"] += [box(0.5, 0.82, 0.3, 0.04), box(0.5, 0.4, 0.3, 0.04)]
    fs[6]["boxes"].append(box(0.5, 0.82, 0.05, 0.04))   # a jersey number is too short for its height
    burnin.analyse(fs, W, H)
    assert [sorted(f["sub"]) for f in fs] == [["bottom"], ["top"], [], [], [], ["bottom"], [], []]
    assert fs[0]["sub"]["bottom"] == [box(0.5, 0.82, 0.5, 0.04)]   # the clock left out
    few = [{"kind": "speech", "boxes": [clock] if i < 2 else []} for i in range(9)]   # 2 of 9 frames is under 0.25
    assert [bool(f["sub"]) for f in burnin.analyse(few, W, H)] == [True, True] + [False] * 7
    assert not any(f["sub"] for f in burnin.analyse(few[:8], W, H))   # 2 of 8 is an overlay
    rows = [box(0.5, y, 0.3 + y / 10, 0.04) for y in (0.75, 0.81, 0.87, 0.93)]
    many = burnin.analyse([{"kind": "speech", "boxes": rows}, {"kind": "speech", "boxes": rows[:3]},
                           {"kind": "speech", "boxes": rows[1:] + [box(0.5, 0.05, 0.3, 0.04), box(0.5, 0.12, 0.35, 0.04)]}] + [{"kind": "speech", "boxes": []}] * 20, W, H)
    assert [sorted(f["sub"]) for f in many[:3]] == [[], ["bottom"], []]   # 4 lines in a band, and 5 centred lines, are credits


def test_the_class_of_a_keyframe():
    """Speech sits 0.3 s inside a span. Silence sits 0.8 s from speech, and from a window's edge in quick()."""
    spans = [[10.0, 20.0], [30.0, 40.0]]
    file = lambda k: burnin.kind_in_file(k, spans, [10.0, 30.0])
    assert [file(k) for k in (5.0, 9.3, 10.2, 10.3, 19.7, 19.8, 20.5, 20.8, 25.0, 29.2, 29.3, 41.0)] == [
        "silence", None, None, "speech", "speech", None, None, "silence", "silence", "silence", None, "silence"]
    win = lambda k: burnin.kind_in_windows(k, [[0.0, 22.0], [25.0, 45.0]], spans)
    assert [win(k) for k in (0.5, 0.8, 9.3, 10.3, 20.8, 21.1, 21.3, 23.0, 25.7, 25.9, 44.3, 46.0)] == [
        None, "silence", None, "speech", "silence", "silence", None, None, None, "silence", None, None]


def test_quick_sorts_by_the_speech_frames_and_the_share():
    b = lambda frames, share: {"speech_frames": frames, "speech_share": share}
    assert [burnin.sort(b(*x)) for x in ((9, 1.0), (10, 0.05), (10, 0.049), (40, 0.0))] == ["unsure", "flagged", "clean", "clean"]
    assert [burnin.sort(b(*x), hdr=True) for x in ((9, 1.0), (10, 0.05), (10, 0.049), (40, 0.0))] == ["unsure", "flagged", "unsure", "unsure"]


def test_method_b_drops_a_line_that_recurs():
    """Method B's overlay rule: a line within 0.015 in a quarter of all frames is an overlay."""
    logo, line = (0.9, 0.95, 0.4, 0.6), (0.85, 0.9, 0.3, 0.7)
    fs = [{"kind": "speech" if i < 8 else "silence", "at": 100.0 * i, "look": {"bot": [logo] + ([line] if i in (1, 4, 6) else []), "top": []}}
          for i in range(16)]
    v = burnin.method_b_verdict(fs, 1600)
    assert (v["speech_share"], v["silence_share"], v["spread"], v["label"]) == (0.375, 0.0, 3, "partial")
    fs[1]["look"]["bot"] = [(0.89, 0.95, 0.41, 0.61)]   # the logo moved under 0.015: still the logo
    assert burnin.method_b_verdict(fs, 1600)["speech_share"] == 0.25
    for f in fs[2:4]:
        f["look"]["bot"].append(line)   # the line in 4 of 16 frames is an overlay too
    assert burnin.method_b_verdict(fs, 1600)["speech_share"] == 0.0


class FakeOcr:
    """The recognizers' answers in the order they are asked."""
    def __init__(self, answers):
        self.answers, self.models = iter(answers), []

    def read(self, rgb, bx, model="ch"):
        self.models.append(model)
        return next(self.answers)


ENGLISH = ["Where are we going", "I want to know what you think", "There is nothing left for us there", "Come back to me now", "Why did you leave"]


@pytest.mark.parametrize("read, want", [
    ([(t, 0.9) for t in ENGLISH], (True, "eng", "latin", 5, 5)),
    ([("Dónde está el coche", 0.9), ("No sé qué hacer ahora", 0.9), ("Tengo que hablar con ella", 0.9), ("Es muy tarde para eso", 0.9),
      ("Vamos a casa por favor", 0.9)], (False, None, "latin", 5, 5)),
    ([("我们走吧", 0.9), ("你在哪里", 0.9)], (False, "chi", "cjk", 2, 2)),
    ([("か", 0.2), ("ab", 0.2), ("c", 0.1)], (False, None, "other", 3, 0)),
    ([("Где ты", 0.9), ("Я здесь", 0.9)], (None, None, "other", 2, 2)),
    ([(t, 0.9) for t in ("Kowalski and McNamara", "Brzezinski Vladivostok", "Przemyslaw Krzysztof Wojciechowski", "Why did Szczepanski",
                         "Grzegorz Lewandowski")],
     (None, None, "latin", 5, 5)),   # 3 of 13 words are English: neither English nor another language
    ([(t, 0.9) for t in ("Is het in orde met je moeder", "We zijn in de auto", "Ik weet het niet meer", "Is hij in het huis",
                         "We gaan naar huis toe")],
     (None, None, "latin", 5, 5)),   # 7 of 27 Dutch words are on the English list too, under 1.5 times the Dutch share
    ([("Hi", 0.9)], (None, None, "latin", 1, 1)),   # too few letters to tell
    ([], (None, None, None, 0, 0)),
])
def test_language_of_the_lines(read, want):
    """English reads as English. Spanish Latin text is not English and names no language. Han letters are Chinese.
    Lines that mostly do not read are in a script the recognizers lack. Cyrillic, few letters and a small English share
    name no language."""
    latin = lambda t: burnin.script_of(t)["latin"] > sum(burnin.script_of(t).values()) / 2
    ocr = FakeOcr([x for t, c in read for x in [(t, c)] * (2 if latin(t) else 1)])   # the English recognizer reads a Latin line again
    fs = [{"kind": "speech", "sub": {"bottom": [i]}, "rgb": None} for i in range(len(read))]
    got = burnin.language(ocr, fs)
    assert (got["english"], got["text_lang"], got["script"], got["lines"], got["lines_read"]) == want
    assert ocr.models.count("en") == sum(1 for t, _ in read if latin(t))


def test_the_recognizers_read_at_most_ten_frames_spread_over_the_file():
    fs = [{"kind": "speech", "sub": {"bottom": [i]} if i % 2 else {}, "rgb": None} for i in range(60)] + [{"kind": "silence", "sub": {"bottom": [0]}}]
    seen = []
    ocr = type("O", (), {"read": lambda self, rgb, bx, model="ch": seen.append(bx) or ("", 0.0)})()
    burnin.language(ocr, fs)
    assert seen == [1, 7, 13, 19, 25, 31, 37, 43, 49, 55]


def fake_models(tmp_path, monkeypatch):
    """MODELS pinned to small made-up files, served by a fake urlopen. Returns the served URLs."""
    blobs = {name: f"weights {name}".encode() for name in burnin.MODELS}
    monkeypatch.setattr(burnin, "MODELS", {n: (f"v/{n}.onnx", hashlib.sha256(b).hexdigest()) for n, b in blobs.items()})
    served = []

    def urlopen(url, timeout=None):
        served.append(url)
        return io.BytesIO(blobs[url.rsplit("/", 1)[1][:-5]])
    monkeypatch.setattr(burnin.urllib.request, "urlopen", urlopen)
    return blobs, served


def test_fetch_checks_each_sha256_and_ready_reports_a_missing_or_wrong_model(tmp_path, monkeypatch):
    blobs, served = fake_models(tmp_path, monkeypatch)
    d = str(tmp_path / "models")
    ok, why = burnin.ready(d)
    assert not ok and "det model does not read" in why
    assert burnin.fetch(d) == "downloaded" and len(served) == 3 and burnin.ready(d) == (True, "")
    assert burnin.fetch(d) == "present" and len(served) == 3   # no download, no network
    (tmp_path / "models" / "ocr" / "en.onnx").write_bytes(b"cut")
    ok, why = burnin.ready(d)
    assert not ok and "en.onnx has sha256" in why
    assert burnin.fetch(d) == "downloaded" and served[-1] == burnin.MODEL_URL + "v/en.onnx" and burnin.ready(d)[0]
    blobs["ch"] = b"changed upstream"
    os.remove(tmp_path / "models" / "ocr" / "ch.onnx")
    with pytest.raises(RuntimeError, match="the pin is"):
        burnin.fetch(d)
    assert sorted(os.listdir(tmp_path / "models" / "ocr")) == ["det.onnx", "en.onnx"]   # no half or wrong file left


def test_cli_prints_one_json_line(tmp_path, capsys, monkeypatch, priority):
    rc = burnin.main([str(tmp_path / "missing.mkv"), "0", "100", "--full", "--cache", str(tmp_path / "lid.sqlite")])
    assert rc == 1 and json.loads(capsys.readouterr().out)["error"] and priority == [1]   # one thread, nice 10
    got = []
    monkeypatch.setattr(burnin, "full", lambda *a: got.append(a) or {"yielded": True, "took": 0.0})
    assert burnin.main(["f.mkv", "1", "60", "--full", "--second", "--yield-gate", "g", "--model-dir", "m", "--cache", "c"]) == 0
    assert got == [("f.mkv", 1, 60.0, True, "g", None, "c", "m")] and json.loads(capsys.readouterr().out)["yielded"]
    for argv in (["f.mkv", "0", "60"], ["f.mkv", "0", "60", "--quick", "--full"]):
        with pytest.raises(SystemExit):
            burnin.main(argv)


def test_the_cli_runs_by_path(tmp_path):
    """The hook runs burnin.py by its path, as it runs lid.py. --help needs the stdlib only."""
    r = subprocess.run([sys.executable, os.path.join(FILES, "arr_media_guard", "burnin.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--fetch" in r.stdout, r.stderr


def test_full_yields_when_the_speech_read_yields(monkeypatch):
    """The speech read yields first, before any model loads."""
    monkeypatch.setattr(burnin.lid, "speech", lambda path, idx, cache, gate, queue: {"yielded": True, "cached": False})
    monkeypatch.setattr(burnin, "Ocr", None)
    monkeypatch.setattr(burnin, "Sampler", lambda path: FakeSampler())
    assert burnin.full("f.mkv", 0, 600, gate="g")["yielded"]


# --- the sampling and the checks with stdlib fakes, so CI runs them ----------------------------------------------------

class FakeSampler:
    """Sampler with no media: 5 frames a second, a keyframe every gop seconds. A picture is ("key" or "frame", its
    time). decode() gives None for the times in bad."""

    def __init__(self, gop=2, bad=(), hdr=False, cueless=False):
        self.gop, self.bad, self.size, self.closed, self.hdr, self.cueless = gop, set(bad), (W, H), False, hdr, cueless

    def no_cues(self, duration):
        return self.cueless

    def keyframe(self, t):
        k = math.floor(t / self.gop) * self.gop
        return float(k), ("key", float(k))

    def frame_at(self, t):
        i = math.ceil(round(t * 5, 6))
        i += i % (self.gop * 5) == 0   # the frame after a keyframe
        return i / 5, ("frame", i / 5)

    def decode(self, p, rgb):
        return None if p[1] in self.bad else p

    def close(self):
        self.closed = True


class FakeDetector:
    """Ocr with no model: a subtitle line on every picture whose time burned(t) names, and an English text."""

    def __init__(self, burned):
        self.burned = burned

    def boxes(self, pic):
        return [box(0.5, 0.85, 0.3 + (pic[1] % 7) / 50, 0.04)] if self.burned(pic[1]) else []

    def read(self, pic, bx, model="ch"):
        return "We have to leave before the storm comes back", 0.9


def fake_full(monkeypatch, spans, burned=lambda t: False, sampler=None):
    sm = sampler or FakeSampler()
    monkeypatch.setattr(burnin, "Sampler", lambda path: sm)
    monkeypatch.setattr(burnin, "Ocr", lambda model_dir: FakeDetector(burned))
    monkeypatch.setattr(burnin.lid, "speech", lambda path, idx, cache, gate=None, queue=None: {"spans": spans, "cached": True})
    return sm


def test_sample_takes_each_frame_once_and_stops_when_asked():
    sm, n = FakeSampler(gop=25), 20   # 10 candidates to a keyframe: 20 keyframes of each kind, under N
    kind = lambda k: "speech" if k < 500 else "silence"
    want = {"speech": [x * 2.5 for x in range(200)], "silence": [500 + x * 2.5 for x in range(200)]}
    got = burnin.sample(sm, want, kind, lambda sm, p, k: sm.decode(p, True))
    assert [f["kind"] for f in got] == ["speech"] * n + ["silence"] * n and len({f["at"] for f in got}) == 2 * n
    assert [f["at"] for f in got[:3]] == [0.0, 300.0, 150.0]   # the spread order covers the range from the first frames
    bad = FakeSampler(gop=25, bad={0.0, 300.0})   # a frame that does not decode is passed over
    assert {f["at"] for f in burnin.sample(bad, want, kind, lambda sm, p, k: sm.decode(p, True))} & {0.0, 300.0} == set()
    asked = []
    assert burnin.sample(sm, want, kind, lambda sm, p, k: p, stop=lambda: asked.append(1) or len(asked) == 2) is None
    assert len(asked) == 2   # asked after 10 frames and after 20
    other = burnin.sample(sm, want, kind, lambda sm, p, k: p, exact=True)
    assert all(p["look"][0] == "frame" and p["at"] % 25 for p in other) and not {f["at"] for f in got} & {f["at"] for f in other}
    assert len(other) == 2 * burnin.N   # frames between keyframes are many


SPANS = [[k * 8 + 1, k * 8 + 5] for k in range(150)]   # speech for 4 s of every 8, 1200 s


def test_full_with_fakes_labels_and_takes_other_frames_the_second_time(monkeypatch):
    """Lines on all speech make full. The second check takes frames between the keyframes. A keyframe that does not
    decode changes nothing about that: the two checks never share a frame."""
    sm = fake_full(monkeypatch, SPANS, burned=lambda t: any(a <= t <= b for a, b in SPANS), sampler=FakeSampler(gop=2, bad={2.0, 10.0}))
    seen, analyse = [], burnin.analyse
    monkeypatch.setattr(burnin, "analyse", lambda frames, W, H: seen.append({f["at"] for f in frames}) or analyse(frames, W, H))
    first, second = burnin.full("f.mkv", 0, 1200.0), burnin.full("f.mkv", 0, 1200.0, second=True)
    assert (first["label"], second["label"], first["english"], second["second"]) == ("full", "full", True, True), (first, second)
    assert first["speech_frames"] == second["speech_frames"] == burnin.N and not seen[0] & seen[1] and sm.closed
    assert {2.0, 10.0}.isdisjoint(seen[0])


def test_full_is_unsure_under_the_speech_floor_and_reads_the_silence_after_the_last_speech(monkeypatch):
    """Two short spans of speech hold under MIN_SPEECH_FRAMES keyframes: unsure. The silence after the last speech to
    the end of the file gives silence frames. A file with neither has nothing to read and is unsure too."""
    fake_full(monkeypatch, [[100.0, 104.0], [300.0, 306.0]], burned=lambda t: True)
    r = burnin.full("f.mkv", 0, 1200.0)
    assert (r["label"], r["speech_frames"], r["silence_frames"]) == ("unsure", 3, burnin.N), r
    fake_full(monkeypatch, [[0.0, 600.0]])
    assert burnin.full("f.mkv", 0, 1200.0)["silence_frames"] == burnin.N   # all of them after 600 s
    fake_full(monkeypatch, [])
    assert burnin.full("f.mkv", 0, 1.0)["label"] == "unsure"   # no candidate, no frame, no error


def test_full_fails_when_nothing_decodes_and_before_the_speech_read_without_video(monkeypatch):
    fake_full(monkeypatch, SPANS, sampler=FakeSampler(bad={float(x) for x in range(0, 1201)} | {x / 5 for x in range(6001)}))
    with pytest.raises(RuntimeError, match="no keyframe decoded"):
        burnin.full("f.mkv", 0, 1200.0)
    read = []
    monkeypatch.setattr(burnin.lid, "speech", lambda *a: read.append(a))

    def no_video(path):
        raise ValueError("the file has no video stream")
    monkeypatch.setattr(burnin, "Sampler", no_video)
    with pytest.raises(ValueError):
        burnin.full("f.mka", 0, 1200.0)
    assert not read


def test_full_yields_inside_the_sampling(monkeypatch):
    sm = fake_full(monkeypatch, SPANS)
    monkeypatch.setattr(burnin.lid, "waits", lambda gate, queue: (gate, queue) == ("g", "q"))
    assert burnin.full("f.mkv", 0, 1200.0, gate="g", queue="q") == {"yielded": True, "took": pytest.approx(0, abs=5)} and sm.closed
    assert burnin.full("f.mkv", 0, 1200.0)["label"] == "none"


def fake_quick(monkeypatch, speech, burned=lambda t: False, hdr=False, cueless=False):
    """quick() with no media and no model: hear_window() hears speech(t) a second at a time, and method B sees a line
    on every frame whose time burned(t) names. hdr says the video has a PQ or HLG transfer. Returns the windows heard."""
    heard = []

    def hear(path, idx, a, b, vad):
        heard.append((a, b))
        sp = [[float(t), float(t + 1)] for t in range(int(a), int(b)) if speech(t)]
        joined = []
        for x in sp:
            if joined and x[0] <= joined[-1][1]:
                joined[-1][1] = x[1]
            else:
                joined.append(x)
        return joined, burnin.gaps_of(joined, a, b)
    vad = type(sys)("faster_whisper.vad")
    vad.get_vad_model = lambda: None
    monkeypatch.setitem(sys.modules, "faster_whisper", type(sys)("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setattr(burnin, "hear_window", hear)
    monkeypatch.setattr(burnin, "Sampler", lambda path: FakeSampler(gop=1, hdr=hdr, cueless=cueless))
    monkeypatch.setattr(burnin, "method_b_lines", lambda pic: {"bot": [(0.85, 0.9, 0.3, 0.6 + (pic[1] % 7) / 100)] if burned(pic[1]) else [], "top": []})
    return heard


def test_quick_with_fakes_sorts_adds_windows_and_is_unsure(monkeypatch):
    talk = lambda t: t % 8 in (1, 2, 3, 4)
    heard = fake_quick(monkeypatch, talk, burned=lambda t: talk(int(t)))
    q = burnin.quick("f.mkv", 0, 3000.0)
    assert (q["result"], q["windows"], q["speech_frames"]) == ("flagged", 10, burnin.N) and len(heard) == 10, q
    fake_quick(monkeypatch, talk)
    assert burnin.quick("f.mkv", 0, 3000.0)["result"] == "clean"
    between = lambda t: any(abs(t - 300 * k) < 10 for k in range(1, 10))   # speech only between the first windows
    heard = fake_quick(monkeypatch, between)
    q = burnin.quick("f.mkv", 0, 3000.0)
    assert (q["result"], q["windows"]) == ("clean", 19) and len(heard) == len(set(heard)) == 19, (q, heard)
    heard = fake_quick(monkeypatch, lambda t: t in (1500, 1501))
    q = burnin.quick("f.mkv", 0, 3000.0)
    assert (q["result"], q["windows"]) == ("unsure", 19) and len(heard) == 19, q
    heard = fake_quick(monkeypatch, talk)
    assert burnin.quick("f.mkv", 0, None) == dict(burnin.quick("f.mkv", 0, 0), took=pytest.approx(0, abs=5), cpu=pytest.approx(0, abs=5))
    q = burnin.quick("f.mkv", 0, 0)
    assert (q["result"], q["windows"], q["hdr"], q["no_cues"], heard) == ("unsure", 0, False, False, [])


def test_quick_is_unsure_at_once_on_a_matroska_file_without_cues(monkeypatch):
    """Each seek in a Matroska file with no cues reads the file up to the time. quick() hears no window then."""
    talk = lambda t: t % 8 in (1, 2, 3, 4)
    for cueless, want in ((True, ("unsure", True, 0, 0)), (False, ("flagged", False, 10, 10))):
        heard = fake_quick(monkeypatch, talk, burned=lambda t: talk(int(t)), cueless=cueless)
        q = burnin.quick("f.mkv", 0, 3000.0)
        assert (q["result"], q["no_cues"], q["windows"], len(heard)) == want, q


def test_quick_is_unsure_on_hdr_video_that_method_b_finds_clean(monkeypatch):
    """Method B misses white text on PQ and HLG video. Such a file that B finds clean is unsure, so full() decides. A
    file that B flags stays flagged."""
    talk = lambda t: t % 8 in (1, 2, 3, 4)
    for hdr, burned, want in ((True, lambda t: False, "unsure"), (False, lambda t: False, "clean"), (True, lambda t: talk(int(t)), "flagged")):
        fake_quick(monkeypatch, talk, burned=burned, hdr=hdr)
        q = burnin.quick("f.mkv", 0, 3000.0)
        assert (q["result"], q["hdr"], q["speech_frames"]) == (want, hdr, burnin.N), q


def test_the_detector_runs_one_thread_with_no_busy_wait(monkeypatch):
    """The ONNX sessions take one thread for a shared host, and the CPU provider."""
    made = []

    class Options:
        def __init__(self):
            self.entries = {}

        def add_session_config_entry(self, k, v):
            self.entries[k] = v

    class Session:
        def __init__(self, path, o, providers):
            made.append((os.path.basename(path), o.intra_op_num_threads, o.inter_op_num_threads, dict(o.entries), providers))

        def get_modelmeta(self):
            return type("M", (), {"custom_metadata_map": {"character": "a\nb"}})()
    ort = type(sys)("onnxruntime")
    ort.SessionOptions, ort.InferenceSession = Options, Session
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "numpy", sys.modules.get("numpy") or type(sys)("numpy"))
    burnin.Ocr("/m")
    assert [m[1:] for m in made] == [(1, 1, {"session.intra_op.allow_spinning": "0"}, ["CPUExecutionProvider"])] * 3
    assert [m[0] for m in made] == [os.path.basename(burnin.MODELS[k][0]) for k in ("det", "ch", "en")]


# --- media ------------------------------------------------------------------------------------------------------------

def need_media():
    for m in ("numpy", "onnxruntime", "av"):
        pytest.importorskip(m)
    if shutil.which("ffmpeg") is None or not burnin.ready(MODEL_DIR)[0]:
        pytest.skip("no ffmpeg or no models")


CYCLE, ON, OFF = 8, 1, 5   # someone speaks from 1 to 5 seconds of every 8
LINES = ["Where did you put the keys", "I told you we would be late again", "No", "Come here and look at this",
         "Nobody ever listens to me in this house", "Fine", "We should have left an hour ago", "What is that noise",
         "It is only the wind", "Close the door before the cat gets out", "Are you hungry", "Then we eat now",
         "I never said that and you know it", "Wait", "Tell me what happened at the station"]


def srt(path, cycles):
    stamp = lambda s: f"00:{int(s) // 60:02d}:{int(s) % 60:02d},000"
    with open(path, "w") as f:
        for n, k in enumerate(cycles, 1):
            f.write(f"{n}\n{stamp(k * CYCLE + ON)} --> {stamp(k * CYCLE + OFF)}\n{LINES[k % len(LINES)]}\n\n")
    return path


def make(folder, name, secs=120, vf=None, speech=None, size="640x360", rate=5, ext="mkv", extra=()):
    """A clip of secs at rate frames a second with a keyframe each second. The audio holds a tone while someone
    speaks, by speech(t) as an ffmpeg expression, every CYCLE seconds by default. extra goes to the output."""
    out = os.path.join(folder, f"{name}.{ext}")
    if not os.path.exists(out):
        speech = speech or f"between(mod(t\\,{CYCLE})\\,{ON}\\,{OFF})"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x304050:size={size}:rate={rate}:duration={secs}",
                        "-f", "lavfi", "-i", f"aevalsrc=if({speech}\\,0.5*sin(2*PI*440*t)\\,0):s=16000:d={secs}",
                        *(["-vf", vf] if vf else []), "-c:v", "mpeg4", "-q:v", "2", "-g", str(rate), "-c:a", "flac" if ext == "mkv" else "aac",
                        *extra, "-shortest", out], check=True)
    return out


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    need_media()
    d = str(tmp_path_factory.mktemp("burnin"))
    every = srt(os.path.join(d, "every.srt"), range(15))
    clock = "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='%{pts\\:hms}':fontsize=20:fontcolor=white:borderw=2:x=(w-tw)/2:y=h-th-24"
    if not os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        clock = None
    return {"clean": make(d, "clean"),
            "full": make(d, "full", vf=f"subtitles={every}"),
            "top": make(d, "top",   # the subtitles filter reads the old SSA numbers, where 6 is the top centre
                         vf=f"subtitles={every}:force_style='Alignment=6'"),
            "partial": make(d, "partial", vf=f"subtitles={srt(os.path.join(d, 'part.srt'), [2, 3, 10, 11])}"),
            "clock": clock and make(d, "clock", vf=clock),
            "burst": make(d, "burst", speech="between(t\\,60\\,64)"),
            "mid": make(d, "mid", secs=1200, size="320x180", rate=1, speech="lt(abs(t-120*floor(t/120+0.5))\\,10)*gt(t\\,60)"),
            "offset": {ext: make(d, "offset", vf=f"subtitles={every}", ext=ext, extra=("-output_ts_offset", "100")) for ext in ("ts", "mp4", "mkv")},
            "dir": d}


@pytest.fixture
def fake(monkeypatch):
    """N is 12 keyframes per class, so a check takes seconds. A fake Silero VAD hears each loud frame of 512 samples
    as speech, and lid.speech() gives the spans of every CYCLE."""
    import numpy as np
    monkeypatch.setattr(burnin, "N", 12)
    vad = type(sys)("faster_whisper.vad")
    vad.get_vad_model = lambda: lambda a: (np.abs(a.reshape(-1, 512)).max(axis=1) > 0.1).astype(np.float32)
    monkeypatch.setitem(sys.modules, "faster_whisper", type(sys)("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    spans = [[k * CYCLE + ON, k * CYCLE + OFF] for k in range(15)]
    monkeypatch.setattr(burnin.lid, "speech", lambda path, idx, cache, gate=None, queue=None: {"spans": spans, "cached": True})
    return spans


def run_full(path, **kw):
    return burnin.full(path, 0, 120.0, model_dir=MODEL_DIR, **kw)


@pytest.mark.parametrize("name, quick, label", [
    ("clean", "clean", "none"),
    ("full", "flagged", "full"),
    ("top", "flagged", "full"),
    ("partial", "flagged", "partial"),
    ("clock", "clean", "none"),
])
def test_the_checks_on_made_up_clips(clips, fake, name, quick, label):
    """A clean clip is clean and none. Lines burned in while someone speaks are flagged and full, at the bottom or at
    the top. Lines in 4 of 15 stretches of speech are partial. A clock at the bottom of every frame is an overlay."""
    if not clips[name]:
        pytest.skip("no DejaVu font for the clock")
    q = burnin.quick(clips[name], 0, 120.0)
    assert (q["result"], q["windows"], q["speech_frames"], q["silence_frames"], q["hdr"], q["no_cues"]) == (quick, 1, 12, 12, False, False), q
    r = run_full(clips[name])
    assert (r["label"], r["speech_frames"], r["silence_frames"], r["second"], r["spans_cached"]) == (label, 12, 12, False, True), r
    if label == "full":
        assert (r["english"], r["text_lang"], r["script"]) == (True, "eng", "latin") and r["lines_read"] >= 5, r


@pytest.mark.parametrize("trc", ["smpte2084", "arib-std-b67"])
def test_quick_reads_the_transfer_of_pq_and_hlg_video(clips, fake, trc):
    """The sampler reads the transfer from the video stream. A clean PQ or HLG clip is unsure. A burned one is
    flagged, because the lines of a made-up clip are bright enough for method B."""
    tag = f"setparams=color_trc={trc}"
    clean = make(clips["dir"], f"clean-{trc}", vf=tag)
    burned = make(clips["dir"], f"full-{trc}", vf=f"subtitles={os.path.join(clips['dir'], 'every.srt')},{tag}")
    got = [burnin.quick(path, 0, 120.0) for path in (clean, burned)]
    assert [(q["result"], q["hdr"], q["speech_frames"]) for q in got] == [("unsure", True, 12), ("flagged", True, 12)], got


def test_quick_finds_a_matroska_file_without_cues(clips, fake, tmp_path):
    """ffmpeg writes no cues into a pipe. quick() finds that before it hears a window. A Matroska file with cues
    passes, and so do MP4 and TS files, which seek by other means."""
    path = str(tmp_path / "nocues.mkv")
    with open(path, "wb") as out:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", clips["full"], "-c", "copy", "-f", "matroska", "-"], stdout=out, check=True)
    q = burnin.quick(path, 0, 120.0)
    assert (q["result"], q["no_cues"], q["windows"], q["speech_frames"]) == ("unsure", True, 0, 0), q
    for path in (clips["full"], clips["offset"]["mp4"], clips["offset"]["ts"]):
        sm = burnin.Sampler(path)
        assert sm.no_cues(120.0) is False
        sm.close()


def test_quick_adds_the_windows_between_and_is_unsure_without_speech(clips, fake, monkeypatch):
    """In a 20-minute file whose speech lies only between the first 10 windows, the 9 windows between find it. A file
    with one burst of speech has under MIN_SPEECH_FRAMES speech keyframes after both rounds: it is unsure."""
    q = burnin.quick(clips["mid"], 0, 1200.0)
    assert (q["result"], q["windows"]) == ("clean", 19) and q["speech_frames"] == 12, q
    rounds = []
    sample = burnin.sample
    monkeypatch.setattr(burnin, "sample", lambda *a: rounds.append(1) or sample(*a))
    q = burnin.quick(clips["burst"], 0, 120.0)
    assert q["result"] == "unsure" and q["speech_frames"] == 3 and len(rounds) == 2, (q, rounds)
    assert burnin.quick(clips["burst"], 0, 0)["result"] == "unsure"   # no duration, no windows


def test_the_recognizers_read_every_letter_and_the_spaces(clips, tmp_path):
    """The detector's box holds a shrunk core of the line. The first and last letters and the tails of j and y sit
    outside it, so the crop grows the box first. The English recognizer's space reads as a space."""
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(font):
        pytest.skip("no DejaVu font")
    import av
    text, png = "In the middle of the year just until", str(tmp_path / "line.png")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x304050:size=960x544:d=1", "-vf",
                    f"drawtext=fontfile={font}:text='{text}':fontsize=28:fontcolor=white:borderw=2:x=(w-tw)/2:y=h-60", "-frames:v", "1", png], check=True)
    with av.open(png) as c:
        rgb = next(c.decode(video=0)).to_ndarray(format="rgb24")
    ocr = burnin.Ocr(MODEL_DIR)
    (box,) = ocr.boxes(rgb)
    assert [ocr.read(rgb, box, m)[0] for m in ("en", "ch")] == [text, text]


def test_the_second_pass_takes_frames_between_the_keyframes(clips, fake, monkeypatch):
    """The first pass takes keyframes, here one each second. The second pass takes frames that are no keyframe, so
    the two passes share no frame. It still finds N of each kind."""
    seen, analyse = [], burnin.analyse
    monkeypatch.setattr(burnin, "analyse", lambda frames, W, H: seen.append([f["at"] for f in frames]) or analyse(frames, W, H))
    first = run_full(clips["full"])
    second = run_full(clips["full"], second=True)
    one, two = seen
    assert len(set(one)) == len(set(two)) == 24 and not set(one) & set(two), (one, two)
    assert all(t == int(t) for t in one) and not any(t == int(t) for t in two), (one, two)
    assert (first["label"], second["label"], second["second"]) == ("full", "full", True)


def test_full_yields_while_another_hearing_waits(clips, fake, tmp_path):
    """A hearing that waits holds the gate file. The sampling asks after every 10 frames, stops, and answers yielded."""
    gate = str(tmp_path / "lid.turn.gate")
    with open(gate, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        r = run_full(clips["full"], gate=gate)
    assert r["yielded"] and set(r) == {"yielded", "took"}


def test_a_file_whose_keyframes_do_not_decode_fails_the_check(clips, fake, monkeypatch):
    monkeypatch.setattr(burnin.Sampler, "decode", lambda self, p, rgb: None)
    with pytest.raises(RuntimeError, match="no keyframe decoded"):
        run_full(clips["full"])


def test_a_missing_model_fails_the_check(clips, fake, tmp_path):
    shutil.copytree(os.path.join(MODEL_DIR, burnin.MODEL_SUB), tmp_path / burnin.MODEL_SUB)
    os.remove(tmp_path / burnin.MODEL_SUB / os.path.basename(burnin.MODELS["en"][0]))
    assert burnin.ready(str(tmp_path))[0] is False
    with pytest.raises(Exception):
        burnin.full(clips["full"], 0, 120.0, model_dir=str(tmp_path))


@pytest.mark.parametrize("ext", ["ts", "mp4", "mkv"])
def test_a_file_that_starts_late_runs_on_its_own_clock(clips, fake, ext):
    """A file whose times start at 100 s, as a TS recording or a remux that kept its times. The keyframe times run
    from the file's start, as the speech times do, so the checks see the burned lines."""
    import av
    path = clips["offset"][ext]
    with av.open(path) as c:
        assert c.start_time / 1e6 >= 99.9
    assert burnin.quick(path, 0, 120.0)["result"] == "flagged"
    r = run_full(path)
    assert (r["label"], r["speech_frames"]) == ("full", 12), r
    sm = burnin.Sampler(path)
    assert 29.9 < sm.keyframe(30.5)[0] < 31.2 and 30.5 <= sm.frame_at(30.5)[0] < 31.2   # a TS seek lands on the next keyframe
    sm.close()


def test_the_keyframe_read_skips_the_packets_before_a_keyframe(clips):
    """A TS file has no index, so a seek lands on any packet. keyframe() reads on to the next keyframe."""
    sm = burnin.Sampler(clips["offset"]["ts"])
    got = [sm.keyframe(t)[0] for t in (2.5, 7.3, 13.9, 44.4)]
    sm.close()
    assert len({round(k % 1, 3) for k in got}) == 1 and all(t - 1 <= k <= t + 1.1 for k, t in zip(got, (2.5, 7.3, 13.9, 44.4))), got


def test_the_video_is_the_first_stream_that_is_no_cover(clips, tmp_path, monkeypatch):
    """Cover art comes as a video stream marked attached_pic, and it may come first. A file with no other video
    fails."""
    import av
    stream = lambda disposition, width: type("V", (), {"disposition": disposition, "codec_context": type("C", (), {"width": width, "height": 360, "color_trc": 1})()})()
    cover, video = stream(av.stream.Disposition.attached_pic, 200), stream(av.stream.Disposition.default, 640)
    box_ = type("Box", (), {"start_time": None, "close": lambda self: None})()
    box_.streams = type("S", (), {"video": [cover, video]})()
    monkeypatch.setattr(av, "open", lambda path: box_)
    sm = burnin.Sampler("cover.mp4")
    assert (sm.v, sm.size, sm.start, sm.hdr) == (video, (640, 352), 0.0, False)
    box_.streams.video = [cover]
    with pytest.raises(ValueError, match="no video"):
        burnin.Sampler("cover.mp4")
    monkeypatch.undo()
    audio = str(tmp_path / "audio.mka")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=5", audio], check=True)
    with pytest.raises(ValueError, match="no video"):
        burnin.Sampler(audio)


def test_method_b_needs_enough_strokes_with_dark_on_both_sides():
    """Bright strokes 2 pixels wide with dark pixels on both sides make a line, at 8 strokes a row or more. 7 strokes
    are too few. Strokes with a mid-grey edge on one side are no subtitle."""
    np = pytest.importorskip("numpy")

    def frame(n, edge=None):
        Y = np.full((360, 480), 20, np.uint8)
        for i in range(n):
            x = 240 - 6 * n + 12 * i
            Y[300:313, x:x + 2] = 230
            if edge:
                Y[300:313, x + 2:x + 5] = edge
        return Y
    assert len(burnin.method_b_lines(frame(12))["bot"]) == 1 and burnin.method_b_lines(frame(12))["top"] == []
    assert len(burnin.method_b_lines(frame(8))["bot"]) == 1 and burnin.method_b_lines(frame(7))["bot"] == []
    assert burnin.method_b_lines(frame(12, edge=145))["bot"] == []   # 145 is within 90 of the stroke's 230


def test_a_window_ends_where_its_audio_ends(monkeypatch):
    """A window past the end of the audio holds less audio than asked. Its speech and its silence end where the
    audio ends, so no silence keyframe lands after it."""
    np = pytest.importorskip("numpy")
    vad = lambda a: (np.abs(a.reshape(-1, 512)).max(axis=1) > 0.1).astype(np.float32)
    tone = lambda a, b: (np.sin(np.arange(int(a * 16000), int(b * 16000)) * 0.17) * 16000).astype(np.int16)
    quiet = lambda a, b: np.zeros(int((b - a) * 16000), np.int16)
    monkeypatch.setattr(burnin.lid, "extract", lambda path, idx, a, secs: np.concatenate([tone(0, 6.016), quiet(6.016, 10)]).tobytes())
    assert burnin.hear_window("f.mkv", 0, 100.0, 130.0, vad) == ([[100.0, 106.016]], [(106.016, 110.0)])
    monkeypatch.setattr(burnin.lid, "extract", lambda path, idx, a, secs: np.concatenate([quiet(0, 4), tone(4, 10)]).tobytes())
    assert burnin.hear_window("f.mkv", 0, 100.0, 130.0, vad) == ([[104.0, 110.0]], [(100.0, 104.0)])   # Silero frames end at 110.016
