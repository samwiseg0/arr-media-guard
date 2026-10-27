# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The stream of the read pass lines of the whole-file check (docs/design.md, "Whole-file check"), and the sample of
each audio track after audio lines. Made-up media: 30-second clips with H.264 video, a keyframe every 2 s, and one or
two audio tracks, with random bytes over frames of one stream. Each packet keeps its 4 bytes of Matroska block header,
so the container stays intact. A sample at 3 s then starts at the keyframe at 2 s, past the frames the probe decodes,
as in a real file."""
import io
import json
import re
import shutil
import subprocess

import pytest

import amg
from test_arr_media_guard import hook, noise

D = hook.arr_decide
SIZE = 1000
AUDIO = {"eac3": ["-c:a", "eac3", "-b:a", "192k"], "ac3": ["-c:a", "ac3", "-b:a", "192k"], "aac": ["-c:a", "aac", "-b:a", "128k"],
         "dca": ["-c:a", "dca", "-strict", "-2", "-b:a", "768k"], "truehd": ["-c:a", "truehd", "-strict", "-2"]}


def read(text):
    """parse_copy() and whole_verdict() of the stderr text of a read pass of a file of SIZE bytes."""
    got = D.parse_copy(text, 0, SIZE)
    return got, D.whole_verdict([], SIZE, got)


def test_a_line_names_the_stream_of_its_logger():
    assert D.copy_kind("[eac3 @ 0x57add3a04940] exponent -2 is out-of-range") == "audio"
    assert D.copy_kind("[dca @ 0x64ed] Invalid bit allocation index") == D.copy_kind("[pcm_s24le @ 0x1] invalid PCM packet") == "audio"
    assert D.copy_kind("[h264 @ 0x1] missing picture in access unit with size 9839") == "video"
    assert D.copy_kind("[matroska,webm @ 0x1] File ended prematurely") == "container"
    assert D.copy_kind("[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] stream 1, offset 0x20: partial file") == "container"
    assert D.copy_kind("[h264 @ 0x1] bad bytes at pos 500") == "container"   # a line that names a byte
    assert D.copy_kind("Truncating packet of size 5036 to 69") is None and D.copy_kind("[foo @ 0x1] bar") is None
    # ffmpeg 7's loggers of an input file and of an input stream (review N1 2026-10-26)
    assert D.copy_kind("[in#0/matroska,webm @ 0x5d41] Error during demuxing: Invalid data found when processing input") == "container"
    assert D.copy_kind("[in#0 @ 0x5d41] Error opening input: Invalid data found when processing input") == "container"
    assert D.copy_kind("[aist#0:1/eac3 @ 0x5a] corrupt decoded frame") == "audio" and D.copy_kind("[vist#0:0/h264 @ 0x5b] x") == "video"
    assert D.copy_kind("[sist#0:2/subrip @ 0x5c] x") is None


def test_the_lines_of_one_bad_audio_frame_log_only():
    """Owner 2026-10-26: one bad E-AC-3 block alerted "Video may be broken". The read pass decodes only the first
    frame of each audio track, and one bad frame printed at most 2 lines in E-AC-3, AC-3, AAC and DTS. Two tracks
    with a bad first frame each stay under the bound."""
    live = "[eac3 @ 0x57add3a04940] exponent -2 is out-of-range\n[eac3 @ 0x57add3a04940] error decoding the audio block\n"
    got, verdict = read(live)
    assert (got["kinds"], got["audio"], got["loose"]) == ({"audio": 2}, [["eac3", 2]], 2)
    assert verdict == (None, [], [], [], {"doubts": ["the start of the audio failed to decode, with 2 errors"], "posts": False})
    two = live + "[aac @ 0x5a14] Reserved bit set.\n[aac @ 0x5a14] Prediction is not allowed in AAC-LC.\n"
    assert read(two)[1][4] == {"doubts": ["the start of the audio failed to decode, with 4 errors"], "posts": False}
    assert read("[dca @ 0x6] Invalid bit allocation index\n")[1][4]["doubts"] == ["the start of the audio failed to decode, with 1 error"]


def test_the_repeats_of_an_audio_line_count():
    """A DTS decoder decodes on while its frames fail, up to about 5 s, and ffmpeg prints one line and its repeat count.
    A repeat after a NOISE line or a video line adds nothing."""
    got, verdict = read("[dca @ 0x64ed] Invalid bit allocation index\n    Last message repeated 469 times\n")
    assert got["audio"] == [["dca", 470]] and verdict[4] == {"doubts": ["the start of the audio failed to decode, with 470 errors"], "posts": True}
    eleven = "[dca @ 0x6] Invalid bit allocation index\n    Last message repeated 9 times\n[dca @ 0x6] Failed to decode block code(s)\n"
    assert read(eleven)[1][4]["posts"] and not read("[dca @ 0x6] Invalid bit allocation index\n    Last message repeated 9 times\n")[1][4]["posts"]
    null = "[eac3 @ 0x1] error decoding the audio block\n[null @ 0x2] Application provided invalid dts\n    Last message repeated 5 times\n"
    assert read(null)[0]["audio"] == [["eac3", 1]]
    video = "[eac3 @ 0x1] error decoding the audio block\n[h264 @ 0x2] error while decoding MB 0 0\n    Last message repeated 5 times\n"
    assert read(video)[0]["audio"] == [["eac3", 1]]
    got = {"kept": [], "lines": 0, "noise": 0}   # copy_lines() keeps the repeat count of a kept line only
    hook.copy_lines(io.StringIO("[dca @ 0x6] x\n    Last message repeated 9 times\n[null @ 0x2] y\n    Last message repeated 5 times\n"), got)
    assert got == {"kept": ["[dca @ 0x6] x", "    Last message repeated 9 times"], "lines": 1, "noise": 3}


def test_the_other_lines_keep_their_doubt():
    """A video decoder or parser line and a demuxer line stay doubts of the read pass, beside the audio doubt."""
    mix = "[h264 @ 0x1] error while decoding MB 0 0\nTruncating packet of size 5036 to 69\n[eac3 @ 0x2] error decoding the audio block\n"
    got, verdict = read(mix)
    assert got["kinds"] == {"video": 1, "other": 1, "audio": 1} and verdict[2] == ["a full read of the file found 2 errors"]
    assert verdict[4] == {"doubts": ["the start of the audio failed to decode, with 1 error"], "posts": False}
    ebml = "[matroska,webm @ 0x5] 0x00 at pos 500 (0x1f4) invalid as first byte of an EBML number\n"
    got, verdict = read(ebml)
    assert got["kinds"] == {"container": 1} and got["audio"] == [] and verdict[2:] == (["a full read of the file found damaged data at 50% of the file size"], [], None)


def test_truehd_lines_name_no_place_and_count_past_the_kept_lines():
    """Review W3 2026-10-26: the TrueHD and MLP parser print a line for a bad frame at any place of the file, so their
    doubt names no place. copy_lines() counts the audio lines past the COPY_KEEP lines it keeps, with their repeats,
    and the doubt adds them. A video line past them adds nothing."""
    thd = "[truehd @ 0x5c35] major sync info header checksum error"
    got, verdict = read(thd)
    assert verdict[4] == {"doubts": ["a full read of the file found 1 audio error"], "posts": False}
    assert D.whole_verdict([], SIZE, dict(got, audio_unkept=1799))[4] == {"doubts": ["a full read of the file found 1800 audio errors"], "posts": False}
    assert read("[eac3 @ 0x1] x\n[mlp @ 0x2] y\n")[1][4]["doubts"] == ["a full read of the file found 2 audio errors"]
    got = {"kept": [], "lines": 0, "noise": 0}
    hook.copy_lines(io.StringIO("[h264 @ 0x1] x\n" + f"{thd}\n" * hook.COPY_KEEP + "    Last message repeated 449 times\n[h264 @ 0x1] y\n"
                                "    Last message repeated 3 times\n"), got)
    assert (len(got["kept"]), got["lines"], got["audio_unkept"]) == (hook.COPY_KEEP, hook.COPY_KEEP + 2, 1 + 449)

def test_a_sample_past_the_lines_of_the_probe_posts():
    """Review F1 2026-10-26: a track with every frame bad printed 2 lines in the read pass, as one bad frame does. Each
    sample opens the file and prints the probe's lines again, so only the errors past them count. A sample that did
    not run posts the doubt of the read pass. ffmpeg stops a sample on its error rate when every frame fails, exit 69.
    Its errors give the doubt of its track (review W1 2026-10-26)."""
    got, verdict = read("[eac3 @ 0x1] exponent -2 is out-of-range\n[eac3 @ 0x1] error decoding the audio block\n")
    sample = lambda errors, ran=True: {"errors": errors, "ran": ran, "window": 20.0, "at": 63}
    quiet = D.audio_sampled(verdict[4], got, [(0, sample(2)), (1, sample(2 + D.AUDIO_BAD))])
    assert (quiet["posts"], quiet["doubts"], [s["track"] for s in quiet["samples"]]) == (False, verdict[4]["doubts"], [1, 2])
    loud = D.audio_sampled(verdict[4], got, [(0, sample(2)), (1, sample(1130))])
    assert (loud["posts"], loud["doubts"]) == (True, ["a 20 s sample of audio track 2 found 1130 decode errors"])
    assert D.audio_sampled(verdict[4], got, [(0, sample(3 + D.AUDIO_BAD))])["posts"]
    failed = D.audio_sampled(verdict[4], got, [(0, sample(2)), (1, sample(1604, False)), (2, None), (3, sample(2 + D.AUDIO_BAD, False))])
    assert (failed["posts"], failed["doubts"]) == (True, ["the start of the audio failed to decode, with 2 errors, and the sample of audio tracks 3 and 4 did not run",
                                                          "a 20 s sample of audio track 2 found 1604 decode errors"])
    assert D.audio_sampled(verdict[4], got, [])["doubts"] == ["the start of the audio failed to decode, with 2 errors, and no audio track could be sampled"]
    # review N2 2026-10-26: each track that has no sample is named, and a window under 1 s keeps one decimal
    assert D.audio_sampled(verdict[4], got, [(i, None) for i in range(7)])["doubts"] == [
        "the start of the audio failed to decode, with 2 errors, and the sample of audio tracks 1, 2, 3, 4, 5, 6 and 7 did not run"]
    assert D.audio_sampled(verdict[4], got, [(0, dict(sample(1130), window=0.4))])["doubts"] == ["a 0.4 s sample of audio track 1 found 1130 decode errors"]
    # the probe prints every line of the read pass again: a video line and the repeats of an audio line
    got, verdict = read("[h264 @ 0x2] error while decoding MB 0 0\n[dca @ 0x6] Invalid bit allocation index\n    Last message repeated 3 times\n")
    assert not D.audio_sampled(verdict[4], got, [(0, sample(5 + D.AUDIO_BAD))])["posts"] and D.audio_sampled(verdict[4], got, [(0, sample(6 + D.AUDIO_BAD))])["posts"]


def test_the_sample_of_a_short_file_runs_to_its_end(monkeypatch):
    """One sample of each audio track starts at 10 percent of the duration, as the import's first sample. A 6 s file
    is sampled from 0.6 s to its end. With no duration no sample runs."""
    calls = []
    monkeypatch.setattr(hook, "sample", lambda path, at, i, window: calls.append((round(at, 3), i, round(window, 3))) or {"ran": True})
    info = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}, {"codec_type": "subtitle"}, {"codec_type": "audio"}]}
    monkeypatch.setattr(hook, "ffprobe_duration", lambda path: 6.0)
    assert [i for i, _ in hook.audio_samples("x.mkv", info)] == [0, 1] and calls == [(0.6, 0, 5.4), (0.6, 1, 5.4)]
    monkeypatch.setattr(hook, "ffprobe_duration", lambda path: 1800.0)
    calls.clear()
    assert len(hook.audio_samples("x.mkv", info)) == 2 and calls == [(180.0, 0, 20), (180.0, 1, 20)]
    monkeypatch.setattr(hook, "ffprobe_duration", lambda path: None)
    assert hook.audio_samples("x.mkv", info) == [(0, None), (1, None)]
    # review W2 2026-10-26: a TrueHD core that the read pass leaves out gets no sample, and the track numbers skip it
    info = {"streams": [{"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "audio"},
                        {"index": 3, "codec_type": "audio"}]}
    monkeypatch.setattr(hook, "ffprobe_duration", lambda path: 1800.0)
    calls.clear()
    assert [i for i, _ in hook.audio_samples("x.m2ts", info, [2])] == [0, 1] and [i for _, i, _ in calls] == [0, 2]


def test_a_truehd_core_that_the_read_pass_leaves_out_gets_no_sample(tmp_path, settings):
    """Review W2 2026-10-26: ffmpeg names the AC-3 core of a Blu-ray TrueHD PID with no PES of the core, and gives it
    no sample rate. The read pass leaves it out, and a player lists the TrueHD track only. The sample of the core did
    not run, so one bad first TrueHD frame posted "the sample of audio track 2 did not run". The samples leave the
    core out too, so the doubt logs only."""
    from test_ts_frames import clip
    settings(state_dir=str(tmp_path))
    path = clip(tmp_path, "core.m2ts", ["truehd", "-strict", "-2"], "-g", "25", "-mpegts_m2ts_mode", "1", secs=12)
    assert [int(s.get("sample_rate") or 0) for s in hook.truehd_cores(hook.stream_probe(path)["streams"])] == [0]
    pos = packets(path, "a:0")[0][0]
    with open(path, "r+b") as f:   # 8 random bytes in the first TrueHD frame, past the PES header
        f.seek(pos + 40)
        f.write(noise(8, pos))
    rec = hook.whole_act(None, {"app": "radarr", "path": path, "grab": {"check": True}}, {"id": "x"}, [], post=False)
    ff, audio = rec["whole"]["ffmpeg"], rec["whole"]["audio"]
    assert ff["kinds"] == {"audio": ff["lines"]} and [n for n, _ in ff["audio"]] in (["truehd"], ["truehd", "truehd"]), ff
    assert (audio["posts"], [(s["track"], s["ran"]) for s in audio["samples"]], rec["outcome"], "findings" in rec) == (
        False, [(1, True)], "audio_doubt", False), rec
    assert rec["result"].endswith(", logged only, at most 10 audio errors"), rec["result"]   # TrueHD lines name no place


def test_truehd_beside_another_decoder_logs_with_no_place(tmp_path, settings, monkeypatch):
    """Review C3 2026-10-26: the log-only line names the start of each audio track only when no TrueHD or MLP decoder
    printed. A TrueHD line beside E-AC-3 lines can come from any place, as the doubt of whole_verdict() says too."""
    settings(state_dir=str(tmp_path))
    (tmp_path / "x.mkv").write_bytes(b"x")
    got, verdict = read("[eac3 @ 0x1] x\n[eac3 @ 0x1] y\n[truehd @ 0x2] z\n")
    fields = {"zeros": {"runs": [], "count": []}, "ffmpeg": got, "size": 1, "pcm": False, "audio": verdict[4]}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], [], fields))
    rec = hook.whole_act(None, {"app": "radarr", "path": str(tmp_path / "x.mkv"), "grab": {"check": True}}, {"id": "x"}, [], post=False)
    assert rec["result"] == "audio may be broken: a full read of the file found 3 audio errors, logged only, at most 10 audio errors", rec

def packets(path, stream):
    """(byte, size) of each packet of stream, by ffprobe, in file order. A packet that ffmpeg's parser splits off has no byte."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pos,size", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return [(int(p["pos"]), int(p["size"])) for p in json.loads(r.stdout)["packets"] if "pos" in p]


def garble(src, dst, stream, picks, skip=12):
    """A copy of src with random bytes over the packets picks of stream, from skip bytes into each one: the 4 bytes of
    block header and the first bytes of the frame stay."""
    shutil.copy(src, dst)
    got = packets(src, stream)
    with open(dst, "r+b") as f:
        for pos, size in [got[k] for k in picks if k < len(got) and got[k][1] > skip]:
            f.seek(pos + skip)
            f.write(noise(size - skip, pos))


EVERY = range(10 ** 5)   # every packet of a stream


@pytest.fixture(scope="module")
def streams_media(tmp_path_factory):
    """The clips of each codec of AUDIO, with one audio track and with two, and their garbled copies. <codec>_one has
    a bad first frame, <codec>_all every frame bad, two_<codec>_all every frame of track 2 bad."""
    d = tmp_path_factory.mktemp("streams")
    lavfi = lambda src: ["-f", "lavfi", "-i", src]
    video = lavfi("testsrc2=size=320x180:rate=24:duration=30")
    for codec, args in AUDIO.items():
        for name, tones in ((codec, (440,)), (f"two_{codec}", (440, 660))):
            if name == "two_truehd":
                continue
            maps = [a for k in range(len(tones) + 1) for a in ("-map", str(k))]
            amg.ffmpeg(["ffmpeg", "-v", "error", *video, *(a for f in tones for a in lavfi(f"sine=frequency={f}:duration=30:sample_rate=48000")), *maps,
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", *args, "-ac", "2", "-shortest", str(d / f"{name}.mkv")], check=True)
        garble(d / f"{codec}.mkv", d / f"{codec}_one.mkv", "a:0", [0])
        garble(d / f"{codec}.mkv", d / f"{codec}_all.mkv", "a:0", EVERY)
        if codec != "truehd":
            garble(d / f"two_{codec}.mkv", d / f"two_{codec}_all.mkv", "a:1", EVERY)
    garble(d / "two_eac3_all.mkv", d / "two_eac3_both.mkv", "a:0", EVERY)
    garble(d / "dca.mkv", d / "dca_many.mkv", "a:0", range(30))
    garble(d / "dca.mkv", d / "dca_three.mkv", "a:0", range(3))
    garble(d / "eac3.mkv", d / "video_start.mkv", "v:0", range(1, 6), 40)
    shutil.copy(d / "eac3.mkv", d / "container.mkv")
    with open(d / "container.mkv", "r+b") as f:
        f.seek(f.seek(0, 2) // 2)
        f.write(noise(100 << 10))
    return d


def check(media, name):
    """whole_act() of the clip name, as --check runs it."""
    return hook.whole_act(None, {"app": "radarr", "path": str(media / f"{name}.mkv"), "grab": {"check": True}}, {"id": "x"}, [], post=False)


@pytest.mark.parametrize("name", ["eac3_one", "ac3_one", "aac_one", "truehd_one", "dca_one", "dca_three"])
def test_one_bad_audio_frame_logs_only(streams_media, tmp_path, settings, name):
    """Owner 2026-10-26: an alert posts only when it is actionable. mpv plays past one bad first frame of each codec
    and writes as many samples as from the clean clip, and past 3 bad DTS frames. The sample of the track finds no
    error past the probe's own lines."""
    settings(state_dir=str(tmp_path))
    rec = check(streams_media, name)
    ff, audio = rec["whole"]["ffmpeg"], rec["whole"]["audio"]
    assert ff["kinds"] == {"audio": ff["lines"]} and 1 <= ff["lines"] <= D.AUDIO_BAD, ff
    assert (audio["posts"], [s["ran"] for s in audio["samples"]], rec["outcome"], "findings" in rec) == (False, [True], "audio_doubt", False), rec
    said, bound = (r"a full read of the file found [123] audio errors?", "at most 10 audio errors") if name == "truehd_one" else (
        r"the start of the audio failed to decode, with [123] errors?", "at most 10 errors at the start of each audio track")
    assert re.fullmatch(rf"audio may be broken: {said}, logged only, {bound}", rec["result"]), rec["result"]


@pytest.mark.parametrize("name, track", [("eac3_all", 1), ("ac3_all", 1), ("aac_all", 1), ("two_eac3_all", 2), ("two_ac3_all", 2),
                                         ("two_aac_all", 2)])
def test_a_track_with_every_frame_bad_posts_its_sample(streams_media, tmp_path, settings, name, track):
    """Review F1 2026-10-26: the read pass prints 1 or 2 lines for an E-AC-3, AC-3 or AAC track with every frame bad,
    as for one bad frame, and mpv plays it as noise or silence. The sample of that track finds hundreds of errors, and
    the alert names the track. The other track of a two-track clip samples clean."""
    settings(state_dir=str(tmp_path))
    rec = check(streams_media, name)
    (f,) = rec["findings"]
    assert rec["whole"]["ffmpeg"]["lines"] <= 2 and (rec["outcome"], f["kind"], rec["alert_kinds"], hook.title(f)[0]) == (
        "audio_doubt", "audio", ["audio"], "Audio may be broken"), rec
    assert re.fullmatch(rf"A 20 s sample of audio track {track} found \d{{3,}} decode errors\. AMG kept the file and did not re-grab it\.",
                        hook.texts(f, "done")[0]), f


def test_each_track_with_every_frame_bad_has_its_sentence(streams_media, tmp_path, settings):
    """Both tracks of a two-track E-AC-3 clip with every frame bad: one alert, a sentence for each track."""
    settings(state_dir=str(tmp_path))
    (f,) = check(streams_media, "two_eac3_both")["findings"]
    assert re.fullmatch(r"A 20 s sample of audio track 1 found \d{3,} decode errors\. A 20 s sample of audio track 2 found \d{3,} "
                        r"decode errors\. AMG kept the file and did not re-grab it\.", hook.texts(f, "done")[0]), f


@pytest.mark.parametrize("name", ["dca_all", "two_dca_all", "truehd_all", "dca_many"])
def test_bad_audio_frames_from_the_start_post_an_audio_alert(streams_media, tmp_path, settings, name):
    """A DTS decoder decodes on in the probe while its frames fail, and the TrueHD parser prints a line for a bad frame
    at any place. So a track with every frame bad, or 30 bad DTS frames from the start, print more than AUDIO_BAD
    lines. The alert posts with no sample. The TrueHD doubt names no place (review W3 2026-10-26)."""
    settings(state_dir=str(tmp_path))
    rec = check(streams_media, name)
    (f,) = rec["findings"]
    n = sum(k for _, k in rec["whole"]["ffmpeg"]["audio"])
    assert n > D.AUDIO_BAD and (rec["outcome"], f["kind"], rec["alert_kinds"]) == ("audio_doubt", "audio", ["audio"]), rec
    assert "samples" not in rec["whole"]["audio"] and hook.title(f) == ("Audio may be broken", "amber")
    said = f"A full read of the file found {n} audio errors" if name == "truehd_all" else f"The start of the audio failed to decode, with {n} errors"
    assert hook.texts(f, "done")[0] == f"{said}. AMG kept the file and did not re-grab it."


def test_the_truehd_count_takes_the_lines_past_the_kept_ones(streams_media, tmp_path, settings, monkeypatch):
    """Review W3 2026-10-26: the read pass keeps COPY_KEEP lines. A TrueHD track bad in its middle printed 1800 lines,
    and its doubt said 1000. The audio lines past the kept ones count too. Here the kept lines stop after the first
    TrueHD line and its repeat, and the doubt keeps its count."""
    settings(state_dir=str(tmp_path))
    full = check(streams_media, "truehd_all")["whole"]
    monkeypatch.setattr(hook, "COPY_KEEP", 2)
    cut = check(streams_media, "truehd_all")["whole"]
    assert "audio_unkept" not in full["ffmpeg"] and cut["ffmpeg"]["audio_unkept"] > D.AUDIO_BAD and cut["audio"] == full["audio"], (full, cut)


@pytest.mark.parametrize("name, kind, words", [
    ("video_start", "video", r"a full read of the file found \d+ errors?"),
    ("container", "container", r"a full read of the file found damaged data at 0:\d\d")])
def test_video_and_container_lines_keep_the_video_alert(streams_media, tmp_path, settings, name, kind, words):
    """Bad H.264 frames at the start and random bytes in the middle print lines of the video decoder and of the
    demuxer. Their alert stays "Video may be broken", and the decision line names the stream."""
    settings(state_dir=str(tmp_path))
    rec = check(streams_media, name)
    (f,) = rec["findings"]
    assert (rec["outcome"], f["kind"], hook.title(f)[0]) == ("video_doubt", "video", "Video may be broken"), rec
    assert kind in rec["whole"]["ffmpeg"]["kinds"] and "audio" not in rec["whole"] and re.fullmatch(words, f["doubts"][0]), rec["whole"]
