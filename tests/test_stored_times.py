# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The MP4 sample reader, mp4.samples(), and the proof that reads it, and the start check of two files with stored
times (docs/design.md, "Time reads"). Every file is made up by ffmpeg, and some get their edit list patched."""
import json, re, shutil, struct, subprocess

import pytest

from test_arr_media_guard import hook
from arr_media_guard import matroska, mp4, proof, ts

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("mkvmerge"), reason="needs ffmpeg and mkvmerge")


def make(path, fmt="mp4", seconds=4, rate="24000/1001"):
    """A file of x264 video at 24000/1001 fps with B-frames and 5.1 AAC at 48 kHz, its moov first. ffmpeg's muxer
    writes an edit list for each track: the video's starts at its first frame's composition time, the audio's cuts
    one AAC priming frame."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={rate}:duration={seconds}", "-f", "lavfi",
                    "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-filter_complex", "[1:a]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "3", "-c:a", "aac", "-movflags", "+faststart",
                    "-f", fmt, str(path)], check=True)
    return path


def patch_edit(path, track, entry=0, media_time=None, rate=None):
    """Set the media_time or the media rate of entry of the edit list of track (1 is the first trak) of path."""
    b = bytearray(path.read_bytes())
    at = -1
    for _ in range(track):
        at = b.index(b"elst", at + 1)
    e = at + 12 + 12 * entry   # version 0: 4-byte duration, 4-byte media_time, 2+2-byte rate
    if media_time is not None:
        struct.pack_into(">i", b, e + 4, media_time)
    if rate is not None:   # a 16.16 fixed-point number
        struct.pack_into(">i", b, e + 8, round(rate * 0x10000))
    path.write_bytes(bytes(b))
    return path


def packets(path):
    """[(ffprobe stream index, the track_ID, pts in s or None, byte)] of every packet of path."""
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=index,id:packet=stream_index,pts_time,pos", "-of", "json", str(path)],
                                  capture_output=True, text=True, check=True).stdout)
    ids = {s["index"]: int(s["id"], 16) for s in d["streams"]}
    return [(p["stream_index"], ids[p["stream_index"]], float(p["pts_time"]) if "pts_time" in p else None, int(p["pos"])) for p in d["packets"]]


def remux(src, dst, *opts):
    subprocess.run(["mkvmerge", "-q", "--disable-lacing", "-o", str(dst), *opts, str(src)], check=True)
    return dst


def drop_video_edit(path):
    """Rename the video's elst box to free, so the track has no edit list."""
    b = path.read_bytes()
    at = b.index(b"elst")
    path.write_bytes(b[:at] + b"free" + b[at + 4:])
    return path


@pytest.mark.parametrize("edit", [None, "late by half a frame", "none"])
def test_the_reader_gives_ffmpegs_times_for_each_video_edit(tmp_path, edit):
    """ffmpeg's start rule moves a video with a media edit so its first frame from the edit's media_time on plays at
    the edit's start, see mp4.ffmpeg_lead(). A frame before that media_time counts for no gap. A video with no edit
    list keeps its composition times. Each packet's time then equals ffmpeg's."""
    path = make(tmp_path / "a.mp4")
    if edit == "late by half a frame":
        patch_edit(path, 1, media_time=2002 + 500)
    elif edit == "none":
        drop_video_edit(path)
    tracks = {t["id"]: t for t in mp4.samples(str(path))["tracks"]}
    for _, tid, pts, pos in packets(path):
        t = tracks[tid]
        assert abs(t["time"][list(t["place"]).index(pos)] / 1e9 - pts) < 1e-6, (tid, pos)
    assert tracks[1]["shift"] == {None: 0, "late by half a frame": round(501 / 24000 * 1e9), "none": 0}[edit]


@pytest.mark.parametrize("fmt", ["mp4", "mov"])
def test_the_sample_tables_give_each_packet_its_time(tmp_path, fmt):
    path = make(tmp_path / f"a.{fmt}", fmt)
    r = mp4.samples(str(path))
    assert r["error"] is None and [(t["id"], t["kind"], t["codec"]) for t in r["tracks"]] == [(1, "video", "avc1"), (2, "audio", "mp4a")]
    tracks = {t["id"]: t for t in r["tracks"]}
    for _, tid, pts, pos in packets(path):   # ffmpeg honours these edits, so its times are the presentation times here
        t = tracks[tid]
        k = list(t["place"]).index(pos)
        assert abs(t["time"][k] / 1e9 - pts) < 1e-6, (tid, k)
    v, a = tracks[1], tracks[2]
    assert min(v["time"]) == 0 and v["frame"] == 41708333 and a["frame"] == 21333333
    assert list(a["tag"][:2]) == [mp4.CUT, 0] and a["time"][0] == -21333333   # the priming frame stays on the edit's line
    patch_edit(path, 2, media_time=2112)   # Apple's priming: frame 2 plays from sample 64 on, so it is presented in part
    a = mp4.samples(str(path))["tracks"][1]
    assert list(a["tag"][:3]) == [mp4.CUT, mp4.CUT, 0] and a["time"][2] == -1333333


@pytest.mark.parametrize("fps, ms", [("24000/1001", -83), ("30000/1001", -66)])
def test_ffmpegs_start_rule_moves_a_video_whose_edit_keeps_its_delay(tmp_path, fps, ms):
    """The video's edit starts at media time 0, before its first frame's composition time (2 frames). ISO/IEC 14496-12
    8.6.6 presents that frame 2 frames late, and mkvmerge 102 writes it so. ffmpeg, and so the players built on it,
    play it at 0, see mp4.ffmpeg_lead(). That is AMG's reference for an MP4. The audio has no priming edit. A
    conversion gives mkvmerge the move with --sync in whole ms toward 0, and the proof expects that exact move. 2.18.5
    refused the plain copy: "stream audio 1 starts -0.083 s from the video in the new file, +0.000 s in the original"."""
    src = make(tmp_path / "a.mp4", rate=fps)
    path = patch_edit(patch_edit(src, 1, media_time=0), 2, media_time=0)
    v, a = mp4.samples(str(path))["tracks"]
    assert min(v["time"]) == 0 and proof.start_plan(v["shift"]) == ms and a["time"][0] == 0 and a["shift"] == 0 and a["tag"][0] != mp4.CUT
    assert a["tag"][-1] == mp4.CUT   # the edit ends where the priming edit ended, so the last frame is cut
    assert min(t for i, _, t, _ in packets(path) if i == 0) == 0   # ffmpeg's read
    refused, _ = hook.prove(str(path), str(remux(path, tmp_path / "plain.mkv")), [], str(tmp_path))
    assert refused[1] == f"stream video 0 moves +0 ms in the conversion, and ffmpeg's start rule of the MP4 needs {ms:+d} ms", refused
    new = remux(path, tmp_path / "synced.mkv", "--sync", f"0:{ms}")
    refused, rows = hook.prove(str(path), str(new), [], str(tmp_path), synced={0: ms})
    assert refused is None and rows[1]["start"][0] == rows[1]["start"][1], rows   # the move's sub-ms rest counts too
    assert hook.prove(str(path), str(new), [], str(tmp_path), synced={0: ms - 1})[0][1].startswith(f"stream video 0 moves {ms - 1:+d} ms")
    j, streams = hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1]
    assert hook.mp4_sync(str(path), j, streams) == [{"stream": "video 0", "track": 0, "ms": ms, "rule": "ffmpeg's MP4 start"}]


@pytest.mark.parametrize("chunks", [0, 1])
def test_a_trak_with_no_samples_turns_ffmpegs_start_rule_off_for_the_traks_after_it(tmp_path, chunks):
    """ffmpeg turns its advanced edit lists off for the whole file at the first trak with no samples (mov.c,
    mov_read_trak()). The video after it then keeps the edit list's times, and the start rule moves nothing, so the
    conversion plans no --sync. An empty trak that names a chunk fails ffmpeg's check before that switch, and the rule
    stays. The reader matches ffprobe on every packet in both files."""
    src = make(tmp_path / "a.mp4")
    path = tmp_path / "b.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", str(path)], check=True)   # the moov last, so no chunk moves
    patch_edit(patch_edit(path, 1, media_time=0), 2, media_time=0)
    box = lambda kind, body: struct.pack(">I", 8 + len(body)) + kind + body
    full = lambda kind, body: box(kind, bytes(4) + body)
    stbl = full(b"stts", bytes(4)) + full(b"stsc", bytes(4)) + full(b"stsz", bytes(8)) + full(b"stco", struct.pack(">I", chunks) + bytes(4 * chunks))
    empty = box(b"trak", full(b"tkhd", struct.pack(">5I", 0, 0, 9, 0, 0) + bytes(60)) + box(b"mdia", full(b"mdhd", struct.pack(">4I", 0, 0, 1000, 0) + bytes(4))
                + full(b"hdlr", bytes(4) + b"sbtl" + bytes(13)) + box(b"minf", box(b"stbl", stbl))))
    b = path.read_bytes()
    moov, first = b.rindex(b"moov") - 4, b.rindex(b"moov") + b[b.rindex(b"moov"):].index(b"trak") - 4
    path.write_bytes(b[:moov] + struct.pack(">I", len(b) - moov + len(empty)) + b[moov + 4:first] + empty + b[first:])
    r = mp4.samples(str(path))
    v = r["tracks"][1]
    assert r["error"] is None and [t["id"] for t in r["tracks"]] == [9, 1, 2] and v["shift"] == (0 if not chunks else round(2002 / 24000 * 1e9))
    tracks = {t["id"]: t for t in r["tracks"]}
    for _, tid, pts, pos in packets(path):
        assert abs(tracks[tid]["time"][list(tracks[tid]["place"]).index(pos)] / 1e9 - pts) < 1e-6, (tid, pos)
    j, streams = hook.mkvmerge(str(path)), hook.ff_streams(str(path))[1]
    assert hook.mp4_sync(str(path), j, streams) == ([] if not chunks else [{"stream": "video 1", "track": 1, "ms": -83, "rule": "ffmpeg's MP4 start"}])


def test_an_empty_edit_starts_the_track_later(tmp_path):
    src = make(tmp_path / "a.mp4")
    path = tmp_path / "late.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-itsoffset", "0.5", "-i", str(src), "-map", "0:v", "-map", "1:a", "-c", "copy",
                    "-movflags", "+faststart", str(path)], check=True)
    v, a = mp4.samples(str(path))["tracks"]
    assert a["edits"][0][1] == -1 and a["time"][0] == round(a["edits"][0][0] * 1e6) and mp4.CUT not in a["tag"]
    refused, rows = hook.prove(str(path), str(remux(path, tmp_path / "a.mkv")), [], str(tmp_path))
    assert refused is None and rows[1]["start"][0] == round(a["time"][0] / 1e9, 3), rows


@pytest.mark.parametrize("case, why", [("fragmented", "the file holds movie fragments"), ("two media edits", "track 2: the edit list holds 2 media edits"),
                                       ("rate", "track 1: the edit list plays the media at rate 2"),
                                       ("rate 1.5", "track 1: the edit list plays the media at rate 1.5")])
def test_a_file_the_reader_cannot_time_names_why(tmp_path, case, why):
    src = make(tmp_path / "a.mp4")
    path = tmp_path / "b.mp4"
    if case == "fragmented":
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", "-movflags", "frag_keyframe+empty_moov", str(path)], check=True)
    elif case == "two media edits":   # the empty edit of a late track becomes a media edit
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-itsoffset", "0.5", "-i", str(src), "-map", "0:v", "-map", "1:a", "-c", "copy",
                        "-movflags", "+faststart", str(path)], check=True)
        patch_edit(path, 2, entry=0, media_time=0)
    else:   # the 16 bits after the integer part of 1.5 read as 1
        path.write_bytes(patch_edit(src, 1, rate=2 if case == "rate" else 1.5).read_bytes())
    assert mp4.samples(str(path))["error"].startswith(why)
    with pytest.raises(RuntimeError, match="^the sample tables of b.mp4 do not read: " + why):
        hook.prove(str(path), str(remux(src, tmp_path / "a.mkv")), [], str(tmp_path))


@pytest.mark.parametrize("track", [0, 1])
@pytest.mark.parametrize("ms", [2, 5, 40])
@pytest.mark.parametrize("source", ["mp4", "mkv"])
def test_a_track_that_moves_against_the_others_refuses(tmp_path, track, ms, source):
    """A copy that moves one track by 2 ms or more refuses, for an MP4 original and for a Matroska one. Both read
    their stored times, so the start check takes the slack of their rounding, see mkv_slack(). 2.18.5 held a start to
    SYNC (50 ms) and passed each move of a Matroska original."""
    src = make(tmp_path / "a.mp4")
    if source == "mkv":
        src = remux(src, tmp_path / "a.mkv")
    assert hook.prove(str(src), str(remux(src, tmp_path / "plain.mkv")), [], str(tmp_path))[0] is None
    refused, _ = hook.prove(str(src), str(remux(src, tmp_path / "moved.mkv", "--sync", f"{track}:{ms}")), [], str(tmp_path))
    assert refused and " starts " in refused[1], refused


def test_a_copy_into_a_coarser_timestamp_scale_passes(tmp_path):
    """mkvmerge copies an original at a timestamp scale of 0.1 ms into 1 ms. Each frame time rounds, and so does the
    video's first frame, which every time counts from. With B-frames, 30 of 144 frames then moved 1 ms, and the copy
    refused where 2.18.5 passed it. mkv_slack() holds both roundings, so the copy passes. A track moved 1 ms or 2 ms
    still refuses, as it does at equal scales, see test_a_track_that_moves_against_the_others_refuses()."""
    src = remux(make(tmp_path / "a.mp4", seconds=6), tmp_path / "fine.mkv", "--timestamp-scale", "100000")
    assert matroska.frames(str(src))["scale"] == 100000
    refused, rows = hook.prove(str(src), str(remux(src, tmp_path / "b.mkv")), [], str(tmp_path))
    assert refused is None and rows[0]["times"]["worst_ms"] > 0.6, (refused, rows)
    for ms in (1, 2):   # 1 ms reads 1.3 ms here, past the bound of 1.05 ms
        refused, _ = hook.prove(str(src), str(remux(src, tmp_path / f"moved{ms}.mkv", "--sync", f"1:{ms}")), [], str(tmp_path))
        assert refused and " starts " in refused[1], (ms, refused)


@pytest.mark.parametrize("cut, want", [("none", [0, 40, 100, 140, 200]), ("after a whole Cluster", [0, 40, 100, 140]),
                                       ("in a Cluster at a block boundary", "the block walk of cut.mkv failed: the Cluster at byte")])
def test_the_header_repair_counts_the_frames_of_a_walk_that_read_every_cluster(tmp_path, monkeypatch, cut, want):
    """The header repair counts the video frames the original stores, see remux.original_frames(). A Segment size past
    the end of the file is the walk's only fault when the file was cut after a whole Cluster, as a failed edit leaves
    it. Every Cluster then read whole, so the frames are all known, and the repair takes them. A file cut inside a
    Cluster has no known count, and the repair refuses with the walk's reason."""
    from test_frame_times import NS, entry, mkv, simple
    path = mkv(tmp_path / "e.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS)],
               [(0, [simple(1, 0, [b"a"]), simple(1, 40, [b"b"])]), (100, [simple(1, 0, [b"c"]), simple(1, 40, [b"d"])]), (200, [simple(1, 0, [b"e"])])])
    data = open(path, "rb").read()
    t = matroska.frames(path)["tracks"][0]
    third = data.rindex(b"\xe7", 0, t["place"][4]) - 12   # the Cluster of block e
    (tmp_path / "cut.mkv").write_bytes({"none": data, "after a whole Cluster": data[:third], "in a Cluster at a block boundary": data[:t["place"][3]]}[cut])
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("matroska,webm", [{"index": 0, "codec_type": "video", "codec_name": "h264"}], 1.0))
    monkeypatch.setattr(hook, "main_stream", lambda p: {"index": 0, "codec_name": "h264"})
    got = hook.original_frames(str(tmp_path / "cut.mkv"))
    assert (got.startswith(want) if isinstance(want, str) else got == ([x / 1000 for x in want], NS, {"video": [0.0], "audio": [], "subtitle": []})), got


@pytest.mark.parametrize("place, whole", [("after the last Cluster", True), ("between two Clusters", False)])
def test_a_level1_element_whose_size_does_not_read_ends_the_count_only_after_the_last_cluster(tmp_path, monkeypatch, place, whole):
    """A level-1 element whose size runs past the end stops the walk. When no Cluster follows it, every Cluster read
    whole, as for the Tags of a failed edit, and the repair counts the frames. When a Cluster follows, its frames are
    not known."""
    from test_frame_times import NS, el, entry, simple, uint
    bad = b"\x12\x54\xc3\x67" + (1 << 56 | 10 ** 6).to_bytes(8, "big") + bytes(16)   # Tags with a size past the end
    one, two = (el(0x1F43B675, uint(0xE7, ts) + simple(1, 0, [b"a"]) + simple(1, 40, [b"b"])) for ts in (0, 100))
    body = el(0x1549A966, uint(0x2AD7B1, NS)) + el(0x1654AE6B, entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS)) + one
    body += two + bad if whole else bad + two
    (tmp_path / "e.mkv").write_bytes(bytes.fromhex("1a45dfa3" "84" "42860181") + b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff" + body)
    got = matroska.frames(str(tmp_path / "e.mkv"))
    assert got["error"].startswith("the element at byte") and got["whole"] is whole, got
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("matroska,webm", [{"index": 0, "codec_type": "video", "codec_name": "h264"}], 1.0))
    monkeypatch.setattr(hook, "main_stream", lambda p: {"index": 0, "codec_name": "h264"})
    frames = hook.original_frames(str(tmp_path / "e.mkv"))
    assert frames[:2] == ([0.0, 0.04, 0.1, 0.14], NS) if whole else frames.startswith("the block walk of e.mkv failed: the element at byte"), frames



@pytest.mark.parametrize("toc, samples", [(bytes([0x00]), 480), (bytes([0x18]), 2880), (bytes([0x61]), 480 * 2), (bytes([0x78]), 960),
                                          (bytes([0x80]), 120), (bytes([0xfb, 0x05]), 960 * 5), (bytes([0xfb]), None), (bytes([0x58]), 2880),
                                          (bytes([0x02, 0x10]), 480 * 2), (bytes([0x83, 0xe4]), 120 * 36), (bytes([0x68]), 960)])
def test_an_opus_packet_gives_its_length_by_its_toc_byte(toc, samples):
    """RFC 6716 3.1: the config picks the frame size (SILK 10 to 60 ms, hybrid 10 or 20 ms, CELT 2.5 to 20 ms) and
    the code the count of frames, which code 3 stores in the next byte."""
    assert matroska.frame_length("A_OPUS", toc) == (samples and (samples, 48000))


@pytest.mark.parametrize("codec", ["libvorbis", "libopus"])
def test_vorbis_and_opus_laces_get_bitstream_times(tmp_path, codec):
    """mkvmerge laces Vorbis and Opus with no DefaultDuration. Each frame of a lace gets the time of the frame before
    plus its length from the bitstream: the TOC byte of an Opus packet, and the block sizes of the Vorbis modes. They
    equal the stored times of an unlaced copy. The first lace of Vorbis stays unknown, as its first packet's block
    before it is not known."""
    src = tmp_path / "a.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5", "-c:a", codec, str(src)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(tmp_path / "laced.mkv"), str(src)], check=True)   # mkvmerge laces them by default
    laced, plain = (matroska.frames(str(f))["tracks"][0] for f in (tmp_path / "laced.mkv", remux(src, tmp_path / "plain.mkv")))
    assert laced["dd"] is None and len(laced["time"]) == len(plain["time"]) and set(plain["tag"]) == {matroska.STORED}
    known = [k for k, g in enumerate(laced["tag"]) if g == matroska.BITSTREAM]
    assert len(known) > 150 and all(abs(laced["time"][k] - plain["time"][k]) < 50000 for k in known)
    assert [k for k, g in enumerate(laced["tag"]) if g == matroska.UNKNOWN] == ([] if codec == "libopus" else list(range(1, laced["tag"].index(matroska.STORED, 1))))


def vorbis_private():
    """The three Vorbis headers of a made-up track, Xiph laced: block sizes 256 and 2048 at 48 kHz, and a setup header
    whose end holds two modes, 0 short and 1 long, then the framing bit. Vorbis packs its bits from the low bit of
    each byte up."""
    ident = b"\x01vorbis" + bytes(4) + b"\x02" + (48000).to_bytes(4, "little") + bytes(12) + b"\xb8\x01"
    bits = [1, 0] * 60 + [1, 0, 0, 0, 0, 0]   # any fields, then the mode count less 1, 6 bits from the low one
    for flag in (0, 1):   # blockflag, windowtype 16 bits, transformtype 16 bits, mapping 8 bits
        bits += [flag] + [0] * 40
    bits += [1]   # the framing bit
    setup = b"\x05vorbis" + bytes(sum(b << k for k, b in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))
    comment = b"\x03vorbis" + bytes(8)
    return bytes([2, len(ident), len(comment)]) + ident + comment + setup


SHORT, LONG_AFTER_SHORT, LONG_AFTER_LONG = b"\x00s", b"\x02l", b"\x06l"   # mode 0, mode 1 with the window flag of the block before


@pytest.mark.parametrize("first, frames, want", [
    ([SHORT], [SHORT, LONG_AFTER_SHORT, SHORT], [128 / 48, 128 / 48 + 12]),   # the unlaced packet gives the block size before
    ([LONG_AFTER_LONG], [SHORT, SHORT], [12]),
    (None, [LONG_AFTER_LONG, SHORT, SHORT], [64 / 3, 64 / 3 + 12]),   # a long packet names the block before it
    (None, [SHORT, SHORT], [None]),   # the first packet's block before is not known
])
def test_a_vorbis_lace_takes_the_block_sizes_of_its_modes(tmp_path, monkeypatch, first, frames, want):
    """Each Vorbis packet gives (the block size before it + its own) / 4 samples. A long packet names the size before it
    in its window flag. A short one takes the packet before, so the walk reads every packet of the track in order, an
    unlaced one too. want holds the ms of each lace frame after the first, from the lace's block. The walk keeps the
    first byte of each packet, so the times read the same with every byte of the file 0 after the walk."""
    from test_frame_times import entry, mkv, simple
    kids = ([simple(1, 0, first)] if first else []) + [simple(1, 10, frames, lacing="xiph")]
    t = matroska.frames(mkv(tmp_path / "v.mkv", [entry(1, 2, "A_VORBIS", private=vorbis_private())], [(0, kids)]))["tracks"][0]
    got = [None if x == matroska.NONE else round(x / 1e6 - 10, 6) for x in t["time"][len(first or []) + 1:]]
    assert got == [None if w is None else round(w, 6) for w in want], got
    real = matroska.track_frames
    monkeypatch.setattr(matroska, "track_frames", lambda b, *a: real(bytes(len(b)), *a))
    matroska.WALKS.clear()
    assert matroska.frames(str(tmp_path / "v.mkv"))["tracks"][0]["time"] == t["time"]


@pytest.mark.parametrize("name, audio, extra", [("a.ts", "aac", []), ("a.m2ts", "ac3", ["-mpegts_m2ts_mode", "1"]),
                                               ("wrap.ts", "mp2", ["-output_ts_offset", "95441"])])
def test_a_ts_copy_proves_by_its_pes_times_and_a_moved_track_refuses(tmp_path, name, audio, extra):
    """A TS or M2TS original reads its PES times, see ts.frames(): each audio PES holds several frames, timed by their
    codec lengths after the PES's PTS, and a PTS that wraps at 2^33 ticks keeps growing. mkvmerge's copy passes. A copy
    that moves one track by 2 ms or more refuses."""
    src = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2", "-c:a", audio,
                    *extra, "-f", "mpegts", str(src)], check=True)
    refused, rows = hook.prove(str(src), str(remux(src, tmp_path / "plain.mkv")), [], str(tmp_path))
    assert refused is None and all(r["times"].get("worst_ms", 9) <= 1 for r in rows), rows
    for track, ms in ((0, 2), (1, 5), (1, 40)):
        refused, _ = hook.prove(str(src), str(remux(src, tmp_path / "moved.mkv", "--sync", f"{track}:{ms}")), [], str(tmp_path))
        assert refused and " starts " in refused[1], (track, ms, refused)


def test_a_ts_stream_the_reader_does_not_split_keeps_ffmpegs_times_on_the_readers_clock(tmp_path, monkeypatch):
    """The reader gives TrueHD and LATM AAC one frame a PES, so ffmpeg's read times such a stream. Across a PTS wrap
    ffmpeg counts down from 2^33 ticks and the reader counts up, so that stream's times move by the wrap first. Here
    the audio of a made-up file plays such a stream."""
    src = tmp_path / "wrap.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-output_ts_offset", "95441", "-f", "mpegts", str(src)], check=True)
    real = hook.ts_places
    monkeypatch.setattr(hook, "ts_places", lambda path, streams: {i: v for i, v in real(path, streams).items() if i == 0})
    refused, rows = hook.prove(str(src), str(remux(src, tmp_path / "plain.mkv")), [], str(tmp_path))
    assert refused is None and rows[1]["start"] == rows[1]["start"][:1] * 2, rows


def test_a_ts_audio_stream_never_keeps_ffmpegs_times(tmp_path, monkeypatch):
    """ts_places() gives every TS stream an entry. An audio PID the reader names no codec gives no frames, so the proof
    refuses its count and never takes ffmpeg's times. 2.18.5 timed TrueHD and LATM AAC that way."""
    src = tmp_path / "a.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=1", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts",
                    str(src)], check=True)
    streams = hook.ff_streams(str(src))[1]
    assert sorted(proof.ts_places(str(src), streams)) == [0, 1]
    real, empty = ts.frames, {a: [] for a in ("time", "dts", "tag", "place", "dur", "full")}
    monkeypatch.setattr(ts, "frames", lambda path: dict(r := real(path), tracks=[dict(t, kind=None, codec=None, split=False, **empty) if t["pid"] == 0x101 else t
                                                                                 for t in r["tracks"]]))
    assert len(proof.ts_places(str(src), streams)[1][0]) == 0
    refused = proof.prove(str(src), str(remux(src, tmp_path / "b.mkv")), [], str(tmp_path))[0]
    assert re.fullmatch(r"the PES walk of the original finds 0 frames of stream audio 1 \(aac\), and ffmpeg \d+", refused[1]), refused


@pytest.mark.parametrize("core", [False, True], ids=["empty core", "full core"])
def test_a_bluray_truehd_copy_pairs_the_core_as_mkvmerge_writes_it(tmp_path, core):
    """ffmpeg names the AC-3 core of a Blu-ray TrueHD PID as a stream of its own, also when the PID holds no PES of it.
    mkvmerge 102 writes the core as an AC-3 track after the TrueHD only when it finds core frames. lone_cores() names a
    core that mkvmerge -J leaves out, and the proof pairs it with nothing. 2.18.5 refused every copy with no core,
    with "the new file holds 1 audio streams, not 2". A core with frames that mkvmerge leaves out refuses with that reason,
    as when its first PES comes after mkvmerge's probe."""
    from test_ts_frames import bluray_m2ts
    src, _, m = bluray_m2ts(tmp_path, core)
    j, streams = hook.mkvmerge(str(src)), hook.ff_streams(str(src))[1]
    assert [s["codec_name"] for s in streams] == ["h264", "truehd", "ac3"] and [t["codec"] for t in j["tracks"]][1:] == ["TrueHD", *["AC-3"][:core]]
    cores = proof.lone_cores(j, streams)
    assert sorted(proof.ts_places(str(src), streams)) == [0, 1, 2]   # the reader times TrueHD and its core, never ffmpeg
    assert cores == ({2} if not core else set()) and proof.lone_cores(dict(j, tracks=j["tracks"][:2]), streams) == {2}
    refused, rows = proof.prove(str(src), str(remux(src, tmp_path / "a.mkv")), [], str(tmp_path), cores=cores)
    assert refused is None and [r["codec"] for r in rows] == ["h264", "truehd", *["ac3"][:core]], (refused, rows)
    assert all(r["times"]["worst_ms"] <= 1 for r in rows), rows
    refused = proof.prove(str(src), str(remux(src, tmp_path / "b.mkv", "-a", "1")), [], str(tmp_path), cores={2})[0]
    assert refused == ((2, f"stream 2 holds {m} frames of the AC-3 core of TrueHD stream 1, and mkvmerge left it out") if core else None), refused


def test_a_pcm_copy_keeps_the_rechunked_times(tmp_path):
    """PCM has no frames, so both files cut it again into packets of 4096 samples, and ffmpeg times each from its
    block's stored time and its sample offset. The block walk counts other frames. The reader of 14abe61 refused
    every PCM copy for that count."""
    src = tmp_path / "a.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", str(src)], check=True)
    refused, rows = hook.prove(str(src), str(remux(src, tmp_path / "b.mkv")), [], str(tmp_path))
    assert refused is None and rows[1]["codec"] == "pcm_s16le" and rows[1]["times"]["worst_ms"] <= 1, rows
    assert hook.prove(str(src), str(remux(src, tmp_path / "c.mkv", "--sync", "1:5")), [], str(tmp_path))[0][1].startswith("stream audio 1 starts")



@pytest.mark.parametrize("fmt, codec", [("mov", "sowt"), ("mp4", "ipcm")])
def test_pcm_in_an_mp4_gets_one_entry_a_chunk(tmp_path, fmt, codec):
    """An audio track whose stts is one entry of delta 1 stores one sample a tick. ffmpeg reads each of its chunks as
    one entry (mov.c, mov_build_index()), and so does the reader. The entry holds the chunk's byte, the time of its
    first sample, and the ticks of its samples. One entry a sample took 35 s and 577 MB for a 3-minute track, and its
    proof 42 s and 1.2 GB. The proof keeps ffmpeg's times for PCM, see stored_places()."""
    src = tmp_path / f"a.{fmt}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-ac", "2",
                    "-f", fmt, str(src)], check=True)
    v, a = mp4.samples(str(src))["tracks"]
    assert a["codec"] == codec and len(a["time"]) == len(v["time"]) == 96 and a["time"][-1] + a["dur"][-1] == pytest.approx(4e9, abs=1)
    at = {pos: pts for i, _, pts, pos in packets(src) if i == 1}   # ffmpeg cuts a chunk into entries of 1024 samples
    assert [at[p] for p in a["place"]] == pytest.approx([t / 1e9 for t in a["time"]], abs=1e-6) and a["frame"] == 20833
    if fmt == "mov":   # mkvmerge 102 writes no codec for ipcm
        assert hook.prove(str(src), str(remux(src, tmp_path / "b.mkv")), [], str(tmp_path))[0] is None


def regrow(path, at, cut, new):
    """Put the bytes new in place of the cut bytes at byte at of path, and grow each box around them by the change.
    The moov box must come last, so no chunk offset moves."""
    b = path.read_bytes()
    out, grow = bytearray(b[:at] + new + b[at + cut:]), len(new) - cut

    def walk(s, e):
        for kind, d, x in mp4.boxes(b, s, e):
            if kind in mp4.CONTAINERS and d - 8 < at < x:
                struct.pack_into(">I", out, d - 8, x - d + 8 + grow)
                walk(d, x)
    walk(0, len(b))
    path.write_bytes(bytes(out))


@pytest.mark.parametrize("change", ["three stts entries", "ctts"])
def test_ffmpegs_chunk_rule_takes_one_stts_entry(tmp_path, change):
    """ffmpeg's chunk rule needs an stts of one entry of delta 1. A PCM track whose stts holds that delta in two
    entries, with an empty entry between them, keeps one entry a sample, and one tick is its frame. A chunk takes the
    composition offset of its first sample, as ffprobe reads it. Both files are made up from a QuickTime file whose
    moov comes last."""
    src = tmp_path / "a.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-c:a", "pcm_s16le",
                    "-f", "mov", str(src)], check=True)
    b = src.read_bytes()
    at = b.index(b"stts") - 4
    n = struct.unpack_from(">I", b, at + 16)[0]
    if change == "ctts":
        regrow(src, at, 0, struct.pack(">I4s4s3I", 24, b"ctts", bytes(4), 1, n, 5))
    else:
        regrow(src, at, 24, struct.pack(">I4s4s7I", 40, b"stts", bytes(4), 3, n - 1, 1, 0, 7, 1, 1))
    (t,) = mp4.samples(str(src))["tracks"]
    if change == "ctts":
        at = {pos: pts for _, _, pts, pos in packets(src)}
        assert len(t["time"]) < n and [at[p] for p in t["place"]] == pytest.approx([x / 1e9 for x in t["time"]], abs=1e-6)
    else:
        assert len(t["time"]) == n and t["frame"] == 20833


def test_a_cut_top_level_box_header_names_its_byte(tmp_path):
    """A file that ends inside its last top-level box header, after a size of 1 and before the 64-bit size, gives the
    reader's error. A raw struct.error passed start_shift() and mp4_sync(), which catch OSError, and failed the
    conversion with its text."""
    src = make(tmp_path / "a.mp4")
    data = src.read_bytes()
    src.write_bytes(data + struct.pack(">I4s", 1, b"free") + bytes(4))
    assert mp4.samples(str(src))["error"] == f"the top-level box at byte {len(data)} does not read"
    streams = hook.ff_streams(str(src))[1]
    assert hook.start_shift(str(src), streams, set()) == 0 and hook.mp4_sync(str(src), hook.mkvmerge(str(src)), streams) == []


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_a_track_of_another_kind_keeps_one_entry_a_sample(tmp_path, kind):
    """ffmpeg's chunk rule takes an audio track whose stts is one entry of delta 1. A video at a timescale of its frame
    rate stores one entry of delta 1 too, and an AC-3 track of whole frames one entry of delta 1536. Each keeps one
    entry a sample at its own byte, as ffprobe reads it."""
    path = tmp_path / "a.mp4"
    src = ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0",
           "-video_track_timescale", "24"] if kind == "video" else ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.92", "-c:a", "ac3"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *src, str(path)], check=True)
    (t,) = mp4.samples(str(path))["tracks"]
    assert list(t["place"]) == [pos for *_, pos in packets(path)] and len(t["place"]) == {"video": 48, "audio": 60}[kind]


def test_a_quicktime_moov_compressed_with_zlib_reads(tmp_path):
    """Old QuickTime files compress the moov box: a cmov box holds dcom ("zlib") and cmvd (the size, then the data)."""
    src = tmp_path / "a.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=1", "-c:v", "libx264", "-preset",
                    "ultrafast", "-f", "mov", str(src)], check=True)   # the moov follows the mdat, so no chunk offset moves
    data = src.read_bytes()
    at = data.rindex(b"moov") - 4
    moov = data[at:]
    box = lambda kind, body: struct.pack(">I", 8 + len(body)) + kind + body
    packed = box(b"moov", box(b"cmov", box(b"dcom", b"zlib") + box(b"cmvd", struct.pack(">I", len(moov)) + __import__("zlib").compress(moov))))
    (tmp_path / "b.mov").write_bytes(data[:at] + packed)
    plain, comp = (mp4.samples(str(tmp_path / n)) for n in ("a.mov", "b.mov"))
    assert comp["error"] is None and [list(t["time"]) for t in comp["tracks"]] == [list(t["time"]) for t in plain["tracks"]]


def test_an_mp4_packet_with_no_sample_at_its_byte_refuses(tmp_path, monkeypatch):
    """Each packet of ffprobe's read pairs with the sample that starts at its byte. A packet at any other byte names
    it, and the proof reads no time."""
    path = make(tmp_path / "a.mp4")
    real = hook.packet_places
    monkeypatch.setattr(hook, "packet_places", lambda *a, **k: {i: (t, type(p)("q", [x + 1 for x in p]), d) for i, (t, p, d) in real(*a, **k).items()})
    with pytest.raises(RuntimeError, match=r"^ffprobe reads a packet of stream 0 of a.mp4 at byte \d+, where no sample starts"):
        hook.stored_places(str(path), hook.ff_streams(str(path))[1], "mov", str(tmp_path), 60)


def test_the_start_of_a_split_input_is_mkvmerges(tmp_path):
    """start_shift() takes the times as mkvmerge writes them, without ffmpeg's start rule: a video whose edit keeps
    its 2-frame delay starts 83 ms in, so an input of that video alone moves by -83 ms."""
    path = patch_edit(make(tmp_path / "a.mp4"), 1, media_time=0)
    streams = hook.ff_streams(str(path))[1]
    assert hook.start_shift(str(path), streams, {1}) == pytest.approx(-2002 / 24000, abs=1e-6)
    assert hook.start_shift(str(path), streams, {0}) == pytest.approx(1024 / 48000, abs=1e-6)   # the audio's priming frame


@pytest.mark.parametrize("cut", ["none", "after a whole Cluster", "inside a Cluster"])
def test_the_subtitle_ends_come_from_a_walk_that_read_every_cluster(tmp_path, cut):
    """subtitles.subtitle_ends() reads each subtitle block's end from the walk, the next block for a block with no
    duration. A Segment size past the end of the file, as a failed edit leaves it, still reads, because every Cluster
    read whole. A file cut inside a Cluster gives no ends."""
    from test_frame_times import NS, entry, group, mkv, simple
    path = mkv(tmp_path / "s.mkv", [entry(1, 17, "S_TEXT/UTF8")], [(0, [group(1, 1000, b"a", 500), simple(1, 2000, [b"b"])]),
                                                                  (3000, [group(1, 0, b"c", 700)]), (5000, [group(1, 0, b"d", 400)])])
    data = open(path, "rb").read()
    third = data.rindex(b"\x1f\x43\xb6\x75")
    (tmp_path / "c.mkv").write_bytes({"none": data, "after a whole Cluster": data[:third], "inside a Cluster": data[:third - 3]}[cut])
    lines = {}
    got = hook.subtitle_ends(str(tmp_path / "c.mkv"), {"tracks": [{"id": 0, "type": "subtitles"}]}, 60, 2.5, lines)
    assert got == {"none": {0: 5.4}, "after a whole Cluster": {0: 3.7}, "inside a Cluster": None}[cut], got
    assert lines == ({} if cut == "inside a Cluster" else {0: [4 if cut == "none" else 3, 2 if cut == "none" else 1]})


def test_an_empty_last_subtitle_frame_past_the_streams_ends_nothing(tmp_path):
    """subtitles.subtitle_ends() leaves out a block whose frame holds no data, as ffmpeg, mkvextract and mkvmerge drop
    it. Here the video ends at 3 s and an empty frame at 9 s ends the subtitle track. The last cue ends at 2.5 s, and
    the count holds the 2 cues only."""
    from test_frame_times import NS, entry, group, mkv, simple
    path = mkv(tmp_path / "e.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 17, "S_TEXT/UTF8")],
               [(0, [simple(1, 0, [b"v"]), group(2, 1000, b"a", 500), group(2, 2000, b"b", 500), simple(1, 2960, [b"v"])]),
                (9000, [simple(2, 0, [b""])])])
    lines = {}
    assert hook.subtitle_ends(path, {"tracks": [{"id": 0, "type": "video"}, {"id": 1, "type": "subtitles"}]}, 60, 2.5, lines) == {1: 2.5}
    assert lines == {1: [2, 0]}



def test_the_packet_read_takes_stored_times_and_reads_on_past_damage(tmp_path):
    """The packet read of the audio checks takes the times each file stores, see checks.stored_frames(). Here the audio
    stops at 0:05 and comes back at 1:52, and 512 zero bytes at 1:40 damage a Cluster. A walk that stopped there
    would give a certain "stops early". The walk reads on past the damage, as ffprobe does, so the audio ends at 2:00
    and no verdict is certain. An MP4 cut short reads only the samples whose bytes are in the file."""
    src = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=120", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=120", "-af", "aselect='not(between(t,5,112))'", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "ac3", str(src)], check=True)
    path = remux(src, tmp_path / "hole.mkv")
    video = matroska.frames(str(path))["tracks"][0]
    at = video["place"][next(k for k, x in enumerate(video["time"]) if x >= 100e9)]
    b = bytearray(path.read_bytes())
    b[at:at + 512] = bytes(512)
    path = tmp_path / "damaged.mkv"
    path.write_bytes(bytes(b))
    walk = matroska.frames(str(path))
    assert max(walk["tracks"][1]["time"]) < 6e9 and max(walk["tracks"][0]["time"]) < 100e9 and walk["resyncs"], walk["error"]
    pk = hook.packet_read(str(path), 0)
    assert pk["resyncs"] == len(walk["resyncs"]) and pk["audio"] == pytest.approx(120, abs=0.1) and pk["video"] == pytest.approx(120, abs=0.2), pk
    assert pk["hole"] == [pytest.approx(5, abs=0.1), pytest.approx(112, abs=0.1)], pk
    silent = [{"at": at, "n": 0, "ran": True} for at in (12, 60, 102)]
    assert hook.arr_decide.stops_early(silent, pk) is None and hook.arr_decide.stops_early(silent, dict(pk, audio=5.0))
    mp = make(tmp_path / "a.mp4", seconds=60)
    size = mp.stat().st_size
    with open(mp, "r+b") as f:
        f.truncate(size * 6 // 10)
    pk = hook.packet_read(str(mp), 0)
    assert 30 < pk["video"] < 45 and abs(pk["audio"] - pk["video"]) < 1, pk


@pytest.mark.parametrize("at", [3, 22])
def test_a_lace_that_does_not_split_is_damage_and_the_packet_read_reads_on(tmp_path, at):
    """mkvmerge laces AC-3 frames. One lace whose frame count is one too high does not split into its frames. The walk
    takes it as damage and reads on from the next Cluster, so the packet read gives a verdict, as ffprobe's read did.
    The proof still refuses the file."""
    src = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=30", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", str(src)], check=True)
    path = tmp_path / "laced.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(path), str(src)], check=True)
    audio = matroska.frames(str(path))["tracks"][1]
    place = next(audio["place"][k] for k in range(1, len(audio["time"])) if audio["time"][k] >= at * 1e9 and audio["tag"][k] == matroska.DEFAULT)
    b = bytearray(path.read_bytes())
    d = hook.arr_decide.element(b, place)[1]
    assert b[d + 3] & 0x06 and b[d] == 0x82   # a laced block of track 2
    b[d + 4] += 1   # the frame count
    path.write_bytes(bytes(b))
    walk = matroska.frames(str(path))
    assert "lace" in walk["error"] and walk["whole"] is False and len(walk["resyncs"]) == 1, walk["error"]
    pk = hook.packet_read(str(path), 0)
    assert "error" not in pk and pk["resyncs"] == 1 and pk["audio"] == pytest.approx(30, abs=0.2) and pk["video"] == pytest.approx(30, abs=0.2), pk
    with pytest.raises(RuntimeError, match="^the block walk of laced.mkv failed: "):
        hook.stored_places(str(path), [{"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}])


def test_stored_frames_give_the_frames_with_data_and_a_known_time(tmp_path):
    """checks.stored_frames() gives each frame with data and a known time, stream by stream, in ns. A frame with no
    duration lasts 0, as ffprobe's N/A counts. An empty subtitle block and a lace frame of unknown time leave out. An
    MP4 sample that no edit presents, as the AAC priming frame, leaves out, and a TS cut inside its last packet reads."""
    from test_frame_times import NS, block, el, entry, mkv, simple
    path = mkv(tmp_path / "f.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 2, "A_FLAC"), entry(3, 17, "S_TEXT/UTF8")],
               [(0, [simple(1, 0, [b"v"]), simple(2, 0, [b"ab", b"cd"], "xiph"), simple(3, 5, [b"hi"]), simple(3, 9, [b""])])])
    streams = [{"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "subtitle"}]
    assert list(hook.stored_frames(path, streams, "matroska,webm", {})) == [(0, 0, 40 * NS), (1, 0, 0), (2, 5 * NS, 0)]
    mp = make(tmp_path / "a.mp4")
    ff = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(mp)], capture_output=True, text=True).stdout)["streams"]
    audio = [x for i, x, _ in hook.stored_frames(str(mp), ff, "mov,mp4", {}) if i == 1]
    cut = [t for t in mp4.samples(str(mp))["tracks"] if t["kind"] == "audio"][0]
    assert mp4.CUT in cut["tag"] and min(audio) >= 0 and len(audio) == len(cut["tag"]) - cut["tag"].count(mp4.CUT), (min(audio), cut["tag"][:3])
    src = tmp_path / "a.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", str(src)], check=True)
    data = src.read_bytes()
    src.write_bytes(data[:len(data) * 7 // 10 // 188 * 188 + 100])   # the download stopped inside a packet
    pk = hook.packet_read(str(src), 0)
    assert "error" not in pk and 1.5 < pk["audio"] < 3.5 and abs(pk["audio"] - pk["video"]) < 0.5, pk


def test_the_stored_end_reads_the_last_frames_the_file_holds(tmp_path):
    """checks.stored_end() gives where the last video or audio frame ends: the last Clusters of a Matroska file, the
    sample tables of an MP4 whose bytes are in the file. AVI has no such reader."""
    mp = make(tmp_path / "a.mp4", seconds=20)
    mkv = remux(mp, tmp_path / "a.mkv")
    probe = lambda p: json.loads(subprocess.run(["mkvmerge", "-J", str(p)], capture_output=True, text=True).stdout)
    assert hook.stored_end(str(mkv), probe(mkv)) == pytest.approx(20, abs=0.1)
    assert hook.stored_end(str(mp), probe(mp)) == pytest.approx(20, abs=0.1)
    size = mp.stat().st_size
    with open(mp, "r+b") as f:
        f.truncate(size // 2)
    assert 5 < hook.stored_end(str(mp), probe(mp)) < 15
    assert hook.stored_end(str(mkv), {"container": {"type": "AVI"}}) is None


def test_a_reader_that_fails_gives_no_stored_end(tmp_path, monkeypatch):
    """An MP4 whose last box header holds a 64-bit size cut by the end of the file broke the sample table read with
    struct.error, and the metadata checks of the import stopped. Any failure of a reader now gives no end. The job's
    time limit still raises."""
    mp = make(tmp_path / "a.mp4", seconds=20)
    j = json.loads(subprocess.run(["mkvmerge", "-J", str(mp)], capture_output=True, text=True).stdout)
    with open(mp, "ab") as f:
        f.write(b"\x00\x00\x00\x01free\x00\x00\x00\x00")   # size 1: a 64-bit size follows, and 4 of its 8 bytes are there
    got = hook.stored_end(str(mp), j)
    assert got == 0 or got == pytest.approx(20, abs=0.1), got
    for ex in (struct.error("unpack requires a buffer of 8 bytes"), KeyError("stts"), hook.arr_meta.OutOfTime("stopped")):
        monkeypatch.setattr(hook.stored_end.__globals__["mp4"], "samples", lambda path, ex=ex: (_ for _ in ()).throw(ex))
        if isinstance(ex, hook.arr_meta.OutOfTime):
            with pytest.raises(hook.arr_meta.OutOfTime):
                hook.stored_end(str(mp), j)
        else:
            assert hook.stored_end(str(mp), j) == 0


def test_a_reader_that_fails_gives_no_packet_verdict_and_the_proof_refuses(tmp_path, monkeypatch):
    """The packet read gives {"error"} for any failure of a reader, and stored_places() refuses with the reader's
    reason. An MP4 whose last box header holds a 64-bit size cut by the end of the file broke both with struct.error."""
    mp = make(tmp_path / "a.mp4")
    with open(mp, "ab") as f:
        f.write(b"\x00\x00\x00\x01free\x00\x00\x00\x00")   # size 1: a 64-bit size follows, and 4 of its 8 bytes are there
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(mp)], capture_output=True, text=True).stdout)["streams"]
    for why in ("", "'stts'"):
        for f in (hook.packet_read, hook.stored_places) if why else ():
            monkeypatch.setattr(f.__globals__["mp4"], "samples", lambda path: {}["stts"])
        pk = hook.packet_read(str(mp), 0)
        assert list(pk) == ["error"] and pk["error"].startswith("the frame read failed: " + why), pk
        with pytest.raises(RuntimeError, match="^the sample tables of a.mp4 do not read: " + why):
            hook.stored_places(str(mp), streams, "mov,mp4,m4a,3gp,3g2,mj2")


@pytest.mark.parametrize("where", ["at the Segment end", "after the Cues"])
def test_the_stored_end_reads_past_zero_padding(tmp_path, where):
    """Zero padding inside the Segment parses as no element, so the read of the last Clusters stops before it. mkvpropedit
    can grow the Segment over a zero tail, and some muxers leave zeros after the Cues."""
    mkv = remux(make(tmp_path / "a.mp4", seconds=20), tmp_path / "a.mkv")
    j = json.loads(subprocess.run(["mkvmerge", "-J", str(mkv)], capture_output=True, text=True).stdout)
    b = bytearray(mkv.read_bytes())
    seg = b.index(b"\x18\x53\x80\x67") + 4
    assert b[seg] == 0x01   # an 8-byte size
    cues = b.rindex(b"\x1c\x53\xbb\x6b")
    at = len(b) if where == "at the Segment end" else sum(hook.arr_decide.element(b, cues)[1:])
    b[at:at] = bytes(600)
    b[seg + 1:seg + 8] = (int.from_bytes(b[seg + 1:seg + 8], "big") + 600).to_bytes(7, "big")
    mkv.write_bytes(bytes(b))
    assert hook.stored_end(str(mkv), j) == pytest.approx(20, abs=0.1)


def test_a_stale_subtitle_tag_takes_the_count_of_the_walk(tmp_path):
    """events_per_minute() takes NUMBER_OF_FRAMES only when the file's writing app wrote it, as MakeMKV does too. A
    tag that another app or an earlier write left is stale, and checks.mkvmerge() adds walk_frames, the track's block
    count from a walk that read every Cluster. Without it the density is unknown. A track with no tag gets no count."""
    from test_frame_times import NS, entry, mkv, simple
    path = mkv(tmp_path / "s.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 17, "S_TEXT/UTF8")],
               [(0, [simple(1, 0, [b"v"]), simple(2, 5, [b"a"]), simple(2, 9, [b"b"])]), (100, [simple(2, 0, [b"c"])])])
    app = "mkvmerge v97.0 ('playWEB') 64-bit"
    probe = lambda writer, tags: {"container": {"type": "Matroska", "properties": {"duration": 6000 * 10**9, "writing_application": writer}},
                                  "tracks": [{"type": "subtitles", "properties": {"number": 2, **tags}}]}
    sub = lambda j: j["tracks"][0]["properties"]
    stale = hook.walk_frames(path, probe(app, {"tag_number_of_frames": "1500", "tag__statistics_writing_app": "mkvmerge v97.0 ('Hear')"}))
    assert sub(stale)["walk_frames"] == 3 and hook.arr_decide.events_per_minute(stale, sub(stale)) == 0.03
    assert hook.arr_decide.events_per_minute(stale, dict(sub(stale), walk_frames="")) is None
    lavf = hook.walk_frames(path, probe("Lavf60.4.1", {"tag_number_of_frames": "1500", "tag__statistics_writing_app": app}))   # ffmpeg copied it
    assert sub(lavf)["walk_frames"] == 3
    own = hook.walk_frames(path, probe("MakeMKV v1.17.4", {"tag_number_of_frames": "1500", "tag__statistics_writing_app": "MakeMKV v1.17.4"}))
    assert "walk_frames" not in sub(own) and hook.arr_decide.events_per_minute(own, sub(own)) == 15
    fresh = hook.walk_frames(path, probe(app, {"tag_number_of_frames": "1500", "tag__statistics_writing_app": app}))
    assert "walk_frames" not in sub(fresh) and hook.arr_decide.events_per_minute(fresh, sub(fresh)) == 15
    assert "walk_frames" not in sub(hook.walk_frames(path, probe("Lavf60.4.1", {})))
    data = open(path, "rb").read()
    (tmp_path / "cut.mkv").write_bytes(data[:len(data) - 3])   # the last Cluster runs past the end, so the count is not whole
    assert "walk_frames" not in sub(hook.walk_frames(str(tmp_path / "cut.mkv"), probe("Lavf60.4.1", {"tag_number_of_frames": "1500",
                                                                                                         "tag__statistics_writing_app": app})))
