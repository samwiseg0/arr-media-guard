# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The display shape of a conversion (docs/design.md, "Display shape"). Every copy keeps the shape a player shows
the original at, and the proof refuses one that does not. Every file is made up by ffmpeg."""
import fractions, json, os, shutil, subprocess

import pytest

from test_arr_media_guard import film_path, hook

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("mkvmerge"), reason="needs ffmpeg and mkvmerge")


@pytest.fixture(autouse=True)
def state(tmp_path, settings):
    """The decision log and STATE_DIR are folders of the test, so no test writes to the host's."""
    (tmp_path / "state").mkdir()
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path / "state"))


def made(path, fmt, vf, video, audio="mp2"):
    """A file of 2 s at path in the ffmpeg format fmt, from a 160x120 test picture through the filter vf, with the video
    options video and the audio codec audio."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2", "-f", "lavfi", "-i",
                    "sine=duration=2:sample_rate=48000", "-vf", vf, *video, "-c:a", audio, "-f", fmt, str(path)], check=True)
    return path


def sar(path):
    """ffprobe's sample aspect of the first video stream of path."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=sample_aspect_ratio", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0].get("sample_aspect_ratio")


def convert(path):
    """(result, info) of the conversion of path, which sits in its own folder, so no app is asked."""
    _, result, info, _ = hook.convert("radarr", str(path), hook.mkvmerge(str(path)), os.stat(path), True, {"app_id": 7})
    return result, info


@pytest.mark.parametrize("fmt", ["mpeg", "mpegts"])
@pytest.mark.parametrize("kept", [True, False])
def test_an_mpeg1_picture_keeps_its_shape(tmp_path, monkeypatch, fmt, kept):
    """The MPEG-1 sequence header stores the pel aspect code 12, SAR 200:219, and a program stream or a TS stores no
    shape of its own. mkvmerge 102 never reads that code, so its copy showed square pixels, 9.5% wider. The remux gives
    the track the display size of the original's shape at the picture height, 146x120 for 146.1 pixels. ffprobe reads
    SAR 73:80 from the copy, 0.1 pixel off 200:219. Without the size the proof refuses."""
    path = made(film_path(tmp_path), fmt, "setsar=200/219", ["-c:v", "mpeg1video", "-b:v", "200k"])
    assert sar(path) == "200:219"
    if not kept:
        monkeypatch.setattr(hook, "display_dims", lambda *a: {})
    result, info = convert(path)
    if not kept:
        assert result == ("repack failed: the MKV would show the picture 9.5% wider than the original, at 160x120 where the original shows "
                          "160x131. Detail: stream video 0 (mpeg1video) has the display shape 4:3 in the new file, 800:657 in the original"), result
        return
    assert result == "repacked" and info["display_dims"] == ["0:146x120"], (result, info)
    assert info["proof"][0]["shape"] == ["800:657", "73:60"] and sar(path) == "73:80", info["proof"]


@pytest.mark.parametrize("fmt, vf, video", [("mp4", "setsar=200/219", ["-c:v", "mpeg1video"]), ("vob", "setdar=16/9", ["-c:v", "mpeg2video"]),
                                            ("mpeg", "setsar=1", ["-c:v", "mpeg1video"])])
def test_a_shape_mkvmerge_keeps_gets_no_display_size(tmp_path, fmt, vf, video):
    """MPEG-1 video in an MP4 keeps its shape in the track header, and an MPEG-2 sequence header stores a display
    aspect that mkvmerge reads. Square pixels need no display size either. So none of them gets one."""
    path = made(tmp_path / f"a.{fmt}", fmt, vf, video)
    assert hook.display_dims(str(path), hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1]) == {}


def test_a_display_width_on_half_a_pixel_rounds_up():
    """mkvmerge 102 writes the display width of a bitstream's shape rounded half up, 163x120 for 160x120 at SAR
    325:320, 162.5 pixels. The display size of an MPEG-1 track of a program stream rounds the same way."""
    j = {"container": {"type": "MPEG program stream"}, "tracks": [{"id": 0, "type": "video", "properties": {"stream_id": 0xe0}}]}
    streams = [{"index": 0, "id": "0x1e0", "codec_name": "mpeg1video", "width": 160, "height": 120, "sample_aspect_ratio": "325:320"}]
    assert hook.display_dims("a.mpg", j, streams) == {0: "163x120"}


@pytest.mark.parametrize("video, size", [(["-c:v", "dvvideo", "-s", "720x480", "-r", "30000/1001", "-pix_fmt", "yuv411p"], "759x480"),
                                         (["-c:v", "mjpeg", "-pix_fmt", "yuvj420p"], "190x120"), (["-c:v", "prores_ks"], "190x120"),
                                         (["-c:v", "qtrle"], "190x120"), (["-c:v", "png"], "190x120"), (["-c:v", "cinepak"], "190x120"),
                                         (["-c:v", "svq1", "-pix_fmt", "yuv410p"], "190x120"), (["-c:v", "h263", "-s", "176x144"], "228x144"),
                                         (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error"], "190x120"),
                                         (["-c:v", "libx264", "-preset", "ultrafast", "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1"], "190x120")],
                         ids=["dv", "mjpeg", "prores", "qtrle", "png", "cinepak", "svq1", "h263", "hevc", "h264-vui-1:1"])
def test_a_mov_track_keeps_the_shape_of_its_pasp(tmp_path, video, size):
    """ffmpeg writes the tkhd box of a MOV at the picture size and the shape in pasp. mkvmerge 102 writes the tkhd
    size and never reads pasp. Only a shape that an H.264 bitstream states wins over tkhd, and here the VUI states 1:1.
    So each copy showed square pixels. The remux gives the track the display size of the pasp shape at the picture
    height. A scaled picture keeps the shape 128:81 of 160x120 at SAR 32:27. HEVC goes through the remux of
    split_sync()."""
    path = made(film_path(tmp_path), "mov", "setsar=32/27", video)
    result, info = convert(path)
    w, h = map(int, size.split("x"))
    assert result == "repacked" and info["display_dims"] == [f"0:{size}"], (result, info)
    assert info["proof"][0]["shape"] == ["128:81", hook.aspect(fractions.Fraction(w, h))], info["proof"]
    assert ("split_sync" in info) == ("libx265" in video), info


def tkhd(path, width):
    """path with the tkhd width of its video track set to width pixels, as another muxer may store it."""
    data = bytearray(path.read_bytes())
    at = data.index(b"tkhd") - 4
    end = at + int.from_bytes(data[at:at + 4], "big")
    data[end - 8:end - 4] = round(width * 65536).to_bytes(4, "big")
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("vf, width, dims", [("setsar=32/27", 0, ["0:190x120"]), ("setsar=1", 160 * 16 / 9, ["0:160x120"]), ("setsar=32/27", 189.6, []),
                                             ("setsar=32/27", 190.7, ["0:190x120"]), ("setsar=1", 160.5, ["0:160x120"])])
def test_the_display_size_follows_tkhd(tmp_path, vf, width, dims):
    """mkvmerge 102 writes the tkhd size of a MOV track as its display size, rounded half up to whole pixels, or the
    picture size when tkhd holds 0. A track gets a display size when that shape differs from ffprobe's by 1 pixel or
    more: at SAR 32:27 with tkhd 0, with square pixels and a tkhd 16:9 wide, with a tkhd of 190.7 that mkvmerge writes
    as 191, 1.4 pixels over the 189.6 of SAR 32:27, and with square pixels and a tkhd of 160.5 that mkvmerge writes as
    161. A tkhd of 189.6 needs none, as ffmpeg writes it in an MP4. Each copy shows the shape ffprobe reads from the
    original."""
    path = tkhd(made(film_path(tmp_path), "mov", vf, ["-c:v", "mjpeg", "-pix_fmt", "yuvj420p"]), width)
    result, info = convert(path)
    assert result == "repacked" and info.get("display_dims", []) == dims, (result, info)


def test_a_copy_at_another_shape_refuses(tmp_path):
    """The original stores SAR 32:27. mkvmerge writes its display width as 190 pixels, 0.4 pixels over 189.6, and that
    copy passes. A copy at 160x120 shows square pixels, and the proof refuses it with a refusal of its own, after the
    packet and time checks passed."""
    path = made(tmp_path / "a.mp4", "mp4", "setsar=32/27", ["-c:v", "libx264", "-preset", "ultrafast"], "aac")
    good, bad = tmp_path / "good.mkv", tmp_path / "bad.mkv"
    subprocess.run(["mkvmerge", "-q", "--disable-lacing", "-o", str(good), str(path)], check=True)
    subprocess.run(["mkvmerge", "-q", "--disable-lacing", "-o", str(bad), "--display-dimensions", "0:160x120", str(path)], check=True)
    refused, rows = hook.prove(str(path), str(good), [], str(tmp_path))
    assert refused is None and rows[0]["shape"] == ["128:81", "19:12"], (refused, rows)
    every = []
    refused, rows = hook.prove(str(path), str(bad), [], str(tmp_path), refusals=every)
    assert refused == (None, "the MKV would show the picture 15.6% narrower than the original, at 160x120 where the original shows 189x120. "
                             "Detail: stream video 0 (h264) has the display shape 4:3 in the new file, 128:81 in the original"), refused
    assert len(every) == 1 and rows[0]["times"]["worst_ms"] <= 1.0 and not rows[0]["match"], rows


def test_a_stream_with_no_sample_aspect_has_square_pixels():
    """ffprobe names no sample aspect, or 0:1, for a stream that states none, and a player shows its pixels square."""
    assert [hook.aspect(hook.display_shape({"width": 720, "height": 480, **s})) for s in ({}, {"sample_aspect_ratio": "0:1"},
                                                                                          {"sample_aspect_ratio": "32:27"})] == ["3:2", "3:2", "16:9"]
    assert hook.display_shape({"width": 720}) is None


def no_read(path):
    """mp4.samples() of a file whose box header is cut."""
    raise ValueError("the moov box does not read")


@pytest.mark.parametrize("read", [lambda path: {"tracks": [{"id": 1, "display": (0, 0)}], "error": "track 2: the sample tables are not complete"}, no_read])
def test_a_mov_whose_sample_tables_do_not_read_gets_no_display_size(tmp_path, monkeypatch, read):
    """A track gets no display size when the sample tables of its file do not read, also a track read before the error.
    prove() then decides."""
    path = made(tmp_path / "a.mov", "mov", "setsar=32/27", ["-c:v", "mjpeg", "-pix_fmt", "yuvj420p"])
    j, streams = hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1]
    assert hook.display_dims(str(path), j, streams) == {0: "190x120"}
    monkeypatch.setattr(hook.mp4, "samples", read)
    assert hook.display_dims(str(path), j, streams) == {}
