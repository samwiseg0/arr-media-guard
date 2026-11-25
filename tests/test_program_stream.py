# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The conversion of an MPEG program stream (docs/design.md, "Kept audio times" and "MPEG-PS"). The proof reads the
original with -fflags +genpts, and kept_times() keeps the stored times of an audio track whose PTS jump forward.
Every file is made up by ffmpeg."""
import os, shutil, subprocess

import pytest

from test_arr_media_guard import film_path, hook

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("mkvmerge"), reason="needs ffmpeg and mkvmerge")


def ps(path, jump=0.0, bf=2, rate="100k"):
    """A program stream of 6 s at path: MPEG-1 video at 25 fps with bf B-frames at the bit rate rate, and MP2 at
    44.1 kHz. With B-frames at a low bit rate some I and P pictures start inside a PES, so they store no PTS. jump
    moves the audio from 3 s on by that many seconds. ffmpeg's encoder writes a DTS that steps back at one GOP join
    with its default GOP of 12, so the GOP is 15."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=6", "-f", "lavfi", "-i",
                    "sine=duration=6:sample_rate=44100", "-c:v", "mpeg1video", "-bf", str(bf), "-g", "15", "-sc_threshold", "1000000000",
                    "-b:v", rate, "-af", f"asetpts=PTS+gte(T\\,3)*{jump}/TB", "-c:a", "mp2", "-b:a", "64k", "-f", "mpeg", str(path)], check=True)
    return path


def pts(path, kind):
    """ffprobe's PTS of each packet of the first stream of kind ("v" or "a") of path, in 90 kHz ticks, None for none."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"{kind}:0", "-show_entries", "packet=pts", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.split()
    return [None if p == "N/A" else int(p) for p in out]


@pytest.fixture(autouse=True)
def state(tmp_path, settings):
    """The decision log and STATE_DIR are folders of the test, so no test writes to the host's."""
    (tmp_path / "state").mkdir()
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path / "state"))


def convert(path):
    """(result, info) of the conversion of path, which sits in its own folder, so no app is asked."""
    _, result, info, _ = hook.convert("radarr", str(path), hook.mkvmerge(str(path)), os.stat(path), True, {"app_id": 7})
    return result, info


def test_a_picture_with_no_pts_keeps_its_display_time(tmp_path):
    """Some I and P pictures store no PTS. A plain ffmpeg read times each one at its DTS, 120 ms early here, and the
    proof refused the copy. With -fflags +genpts the read gives each one its display time, as mkvmerge writes it."""
    path = ps(film_path(tmp_path))
    assert pts(path, "v").count(None) > 10
    result, info = convert(path)
    assert result == "repacked" and "kept_times" not in info and info["proof"][0]["times"]["worst_ms"] <= 1.0, (result, info.get("proof"))


@pytest.mark.parametrize("kept", [True, False])
def test_an_mp2_pts_jump_keeps_its_stored_times(tmp_path, monkeypatch, kept):
    """The MP2 PTS jumps 250 ms forward at 3 s. mkvmerge 102 closes the gap, so its copy plays the rest of the audio
    250 ms early, and the proof refuses it. kept_times() gives the track the times of the proof's read less the file
    start. Then every frame keeps its time, and the decision line names the track. The test holds the start to 1 ms."""
    path = ps(film_path(tmp_path), jump=0.25)
    a = pts(path, "a")
    assert round((max(y - x for x, y in zip(a, a[1:])) - 2351) / 90) == 250, a
    if not kept:
        monkeypatch.setattr(hook, "kept_times", lambda *a: [])
    result, info = convert(path)
    if not kept:
        assert result.startswith("repack failed: the MKV would play the audio 250 ms out of sync with the video"), result
        return
    row = info["proof"][1]
    assert result == "repacked" and info["kept_times"] == [1] and row["times"]["worst_ms"] <= 1.0, (result, info.get("proof"))
    assert abs(row["start"][0] - row["start"][1]) <= 0.001, row


def test_a_kept_times_file_one_frame_late_refuses(tmp_path, monkeypatch):
    """Each line of the kept times moves one MP2 frame, 26 ms, later. Every frame keeps its time against the track's
    first frame, so only the start check sees the move. The proof holds the start of a track with kept times to
    TIME_SLACK, so it refuses. Without that hold the start check allows 50 ms, SYNC, and the copy converts."""
    path = ps(film_path(tmp_path), jump=0.25)
    real = hook.remux.v4_file
    monkeypatch.setattr(hook.remux, "v4_file", lambda folder, name, tid, ns: real(folder, name, tid, [v + 26122449 for v in ns]))
    result, info = convert(path)
    assert info["kept_times"] == [1] and result.startswith("repack failed: ") and "audio" in result, (result, info.get("proof"))
    assert round((info["proof"][1]["start"][1] - info["proof"][1]["start"][0]) * 1000) == 26, info["proof"]


@pytest.mark.parametrize("bf, rate", [(2, "100k"), (0, "2M")])
def test_the_audio_of_a_program_stream_starts_within_time_slack(tmp_path, bf, rate):
    """ffmpeg reads the stored PTS of each audio frame, so a copy keeps the audio's start against the video's to the
    tick. The proof holds it to TIME_SLACK. A copy with the video one frame (40 ms) late, or the audio one MP2 frame
    (26 ms) late, refuses. Within SYNC, 50 ms, both converted. The source with bf 2 has pictures that store no PTS."""
    path = ps(film_path(tmp_path), bf=bf, rate=rate)
    good = tmp_path / "good.mkv"
    subprocess.run(["mkvmerge", "-q", "--disable-lacing", "-o", str(good), str(path)], check=True)
    assert hook.prove(str(path), str(good), [], str(tmp_path))[0] is None
    for tid, ms in ((0, 40), (1, 26)):
        moved = tmp_path / f"moved-{tid}.mkv"
        subprocess.run(["mkvmerge", "-q", "--disable-lacing", "-o", str(moved), "--sync", f"{tid}:{ms}", str(good)], check=True)
        refused, rows = hook.prove(str(path), str(moved), [], str(tmp_path))
        assert refused and refused[1].startswith("stream audio 1 starts "), (refused, rows)


def test_an_mp2_pts_step_back_gets_no_kept_times(tmp_path):
    """The MP2 PTS steps back at 3 s. ffmpeg's muxer moves each time that goes back to the time before it, so its read
    differs from the stored times there. The track gets no file, and the proof refuses."""
    path = ps(film_path(tmp_path), jump=-0.25)
    a = pts(path, "a")
    assert any(y < x for x, y in zip(a, a[1:])), a
    assert hook.kept_times(str(path), hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1], set(), str(tmp_path)) == []
    result, _ = convert(path)
    assert result.startswith("repack failed: the MKV would play the audio"), result


def test_a_plain_program_stream_converts_as_before(tmp_path):
    """Every picture stores its PTS, and the audio holds no jump. So no track gets a file, and the command line of the
    remux stays the same."""
    path = ps(film_path(tmp_path), bf=0, rate="2M")
    assert None not in pts(path, "v")
    j, streams = hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1]
    assert hook.kept_times(str(path), j, streams, set(), str(tmp_path)) == []
    result, info = convert(path)
    assert result == "repacked" and "kept_times" not in info and all(r["times"]["worst_ms"] <= 1.0 for r in info["proof"]), (result, info.get("proof"))


def test_a_track_pairs_with_its_stream_by_id(tmp_path):
    """A VOB holds an MP2 track with a PTS jump, then an AC-3 track in private stream 1. ffprobe lists the streams in
    that order, and mkvmerge lists the AC-3 track first. The file goes to the MP2 track, mkvmerge's track 2, one line
    per frame, with the jump."""
    path = tmp_path / "a.vob"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=6", "-f", "lavfi", "-i",
                    "sine=duration=6:sample_rate=48000", "-filter_complex", "[1:a]asplit[a][b];[b]asetpts=PTS+gte(T\\,3)*0.25/TB[c]",
                    "-map", "0:v", "-map", "[c]", "-map", "[a]", "-c:v", "mpeg2video", "-c:a:0", "mp2", "-c:a:1", "ac3", "-f", "vob", str(path)],
                   check=True)
    j = hook.mkvmerge(str(path))
    assert [t["codec"] for t in j["tracks"]] == ["MPEG-1/2", "AC-3", "MP2"], j["tracks"]
    got = hook.kept_times(str(path), j, hook.ff_streams(str(path))[1], set(), str(tmp_path))
    assert got == ["--timestamps", f"2:{tmp_path / 'kept-2.txt'}"], got
    lines = [float(x) for x in (tmp_path / "kept-2.txt").read_text().splitlines()[1:]]
    assert len(lines) == len(pts(path, "a")) and round(max(y - x for x, y in zip(lines, lines[1:])) - 24) == 250, lines[:3]


def test_a_track_pairs_with_the_stream_of_its_id_and_codec():
    """A TrueHD PID of a TS and its AC-3 core share the PID, so the AC-3 track pairs with the ac3 stream of that PID.
    In a program stream a track in private stream 1 pairs by its substream id, any other by 0x100 plus its stream id."""
    ts = {"container": {"type": "MPEG transport stream"}}
    streams = [{"index": 1, "id": "0x1100", "codec_name": "truehd"}, {"index": 2, "id": "0x1100", "codec_name": "ac3"}]
    assert [hook.track_stream(ts, {"codec": c, "properties": {"number": 0x1100}}, streams)["index"] for c in ("TrueHD", "AC-3")] == [1, 2]
    mpeg = {"container": {"type": "MPEG program stream"}}
    streams = [{"index": 1, "id": "0x1c0", "codec_name": "mp2"}, {"index": 2, "id": "0x80", "codec_name": "ac3"}]
    assert hook.track_stream(mpeg, {"codec": "MP2", "properties": {"stream_id": 0xc0}}, streams)["index"] == 1
    assert hook.track_stream(mpeg, {"codec": "AC-3", "properties": {"stream_id": 0xbd, "sub_stream_id": 0x80}}, streams)["index"] == 2
