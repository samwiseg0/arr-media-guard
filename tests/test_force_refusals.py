# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""--force-convert and a proof that refuses more than one stream (docs/design.md, "Conversion"). Made-up media: MP4
films of the env fixture, and two 4-second Matroska clips whose video and audio both differ."""
import os

import pytest

import amg
from test_arr_media_guard import REAL_PROVE, env, hook, log_lines, mp4_films, posted  # noqa: F401

VIDEO = "stream video 0 (h264) holds 1006 packets in the new file, 1000 in the original"
AUDIO = "the packet data of stream audio 1 (aac) differ"


def refusing(env, monkeypatch, *whys):
    """The proof refuses with whys from now on, the first one as its refusal, and every one in its list."""
    def prove(src, tmp, subs, folder, captions=None, refusals=None, **kw):
        if refusals is not None:
            refusals.extend(whys)
        return (0, whys[0]), []
    monkeypatch.setattr(hook, "prove", prove)


def last(env):
    """The last decision line."""
    return [r for r in log_lines(env) if r.get("outcome")][-1]


LONG = [f"stream audio {k} (aac) sits up to 0.{k}00 s behind the other streams in the new file, 0.000 s in the original, " + "x" * 200
        for k in range(1, 4)]   # three refusals whose text runs past the 500 characters of a result


@pytest.mark.parametrize("logged, now, converts", [
    ((VIDEO,), (VIDEO, AUDIO), False),          # an older version logged the first refusal alone
    ((VIDEO, AUDIO), (VIDEO, AUDIO), True),     # the log names both, and a person accepted both
    ((VIDEO, AUDIO), (VIDEO,), False),          # the proof now refuses another set
    ((VIDEO,), (VIDEO,), True),                 # one refusal, as before
    (LONG, LONG, True),                         # the log keeps every refusal past the cut of the result
    (LONG, LONG[:2] + [AUDIO], False)])         # a refusal past that cut differs
def test_a_force_converts_only_when_it_covers_every_refusal(env, monkeypatch, settings, tmp_path, logged, now, converts):
    """Owner 2026-11-25: a force matched only the first refusal, so a second refused stream converted unseen. The
    force now must name every refusal of the proof, in the decision log's words."""
    first, _ = mp4_films(env, monkeypatch, 2)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / "kept"))
    refusing(env, monkeypatch, *logged)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--ids", "7"])
    rec = last(env)
    assert rec["outcome"] == "repack_failed" and rec["result"] == f"repack failed: {'; '.join(logged)}"[:500] and rec["repack"]["refusals"] == list(logged), rec
    refusing(env, monkeypatch, *now)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", first])
    rec = last(env)
    if converts:
        assert rec["outcome"] == "repacked" and rec["repack"]["forced"] == "; ".join(now) and not os.path.exists(first), rec
    else:
        assert rec["outcome"] == "repack_failed" and os.path.exists(first) and "forced" not in rec["repack"], rec
        assert rec["repack"]["not_forced"] == f"the last refusal in the decision log differs: {'; '.join(logged)[:200]}", rec


def test_the_alert_of_a_refused_conversion_names_every_refused_stream(env, monkeypatch, settings):
    """Each refused stream gets its sentence. One refusal keeps the alert of before."""
    mp4_films(env, monkeypatch, 1)
    refusing(env, monkeypatch, VIDEO, AUDIO)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--ids", "7"])
    (f,) = last(env)["findings"]
    text = hook.unmarked(hook.FINDINGS["repack"][1](f, "done"))
    assert text == ("Couldn't convert the MP4/QuickTime file to MKV, so the original was kept. Stream video 0 (h264) holds 1006 packets in the new file, "
                    "1000 in the original. The packet data of stream audio 1 (aac) differ."), text
    one = {"container": "MP4", "why": VIDEO}
    assert hook.unmarked(hook.FINDINGS["repack"][1](one, "done")) == f"Couldn't convert the MP4 file to MKV, so the original was kept. {VIDEO[0].upper()}{VIDEO[1:]}."


@pytest.fixture(scope="module")
def two_off(tmp_path_factory):
    """a.mkv and b.mkv, 4 s each, H.264 and AAC, with other pictures and another tone in b. a.mp4 is a.mkv with a timed
    text track that says "Hello", and c.mkv is b.mkv with a SubRip track that says "Goodbye"."""
    d = tmp_path_factory.mktemp("two_off")
    (d / "hello.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    (d / "goodbye.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nGoodbye\n")
    for name, video, tone, text in (("a", "testsrc2", 440, None), ("b", "smptebars", 660, None), ("a.mp4", "testsrc2", 440, "hello.srt"),
                                    ("c", "smptebars", 660, "goodbye.srt")):
        sub = ["-i", str(d / text), "-map", "2", "-c:s", "mov_text" if name.endswith(".mp4") else "srt"] if text else []
        amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"{video}=size=320x180:rate=24:duration=4", "-f", "lavfi", "-i",
                    f"sine=frequency={tone}:duration=4:sample_rate=48000", *sub[:2], "-map", "0", "-map", "1", *sub[2:], "-c:v", "libx264",
                    "-preset", "ultrafast", "-g", "48", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest",
                    str(d / (name if name.endswith(".mp4") else f"{name}.mkv"))], check=True)
    return d


def test_the_proof_goes_on_past_a_refused_stream(two_off, tmp_path):
    """The real proof refuses the video and goes on to the audio. Its refusal is the first of the list."""
    every = []
    refused, rows = REAL_PROVE(str(two_off / "a.mkv"), str(two_off / "b.mkv"), [], str(tmp_path), refusals=every)
    assert len(every) >= 2 and refused == (0, every[0]), (refused, every)
    assert any("video 0" in w for w in every) and any("audio 1" in w for w in every), every
    assert [r["match"] for r in rows] == [False, False], rows


def test_the_proof_names_a_text_refusal_after_a_packet_refusal(two_off, tmp_path):
    """A timed text track and a sidecar refuse by their cues. Each one joins the list after the video's refusal."""
    every = []
    REAL_PROVE(str(two_off / "a.mp4"), str(two_off / "c.mkv"), [], str(tmp_path), refusals=every)
    assert any("video 0" in w for w in every) and any("mov_text stream 2" in w for w in every), every
    every, side = [], {"name": "a.en.srt", "path": str(two_off / "hello.srt"), "charset": "UTF-8"}
    REAL_PROVE(str(two_off / "a.mkv"), str(two_off / "c.mkv"), [side], str(tmp_path), refusals=every)
    assert any("video 0" in w for w in every) and any("the sidecar a.en.srt" in w for w in every), every


def test_a_forced_run_the_app_did_not_take_keeps_every_refusal_for_the_next_force(env, monkeypatch, settings, tmp_path):
    """The line of a forced run keeps the refusals it overrode. Its text runs past 500 characters, and a second force
    still covers each one."""
    first, _ = mp4_films(env, monkeypatch, 2)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / "kept"))
    refusing(env, monkeypatch, *LONG)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--ids", "7"])
    env["app_takes"] = False
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", first])
    rec = last(env)
    assert rec["outcome"] == "repack_failed" and rec["repack"]["forced"] == "; ".join(LONG), rec
    env["app_takes"] = True
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", first])
    rec = last(env)
    assert rec["outcome"] == "repacked" and rec["repack"]["forced"] == "; ".join(LONG), rec
