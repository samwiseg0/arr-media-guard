# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The frame reader of a transport stream, ts.frames() (docs/design.md, "Frame times"). The proof pairs frame k of a PID
with ffmpeg's packet k of the stream whose id is that PID. So each test holds the reader to ffprobe on the same file:
the frame counts match, and each stored time matches ffprobe's pts within a tick. Every file is made up, by ffmpeg or
byte by byte."""
import json
import pathlib
import shutil
import subprocess

import pytest

import amg
from arr_media_guard import matroska, ts

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
WRAP = 1 << 33


def clip(tmp_path, name, audio, *extra, secs=2):
    """A made-up file name of ffmpeg's mpegts muxer: 25 fps H.264 and a 48 kHz sine coded by audio, the encoder options.
    The muxer puts several audio frames in one PES."""
    path = str(tmp_path / name)
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", str(secs), "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", *audio, *extra, "-f", "mpegts", path], check=True)
    return path


def probe(path):
    """{PID: [(pts in ticks or None, pos or None) of each packet]} from ffprobe -show_packets."""
    got = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=index,id:packet=stream_index,pts,pos", "-of", "json", path],
                                    capture_output=True, text=True, check=True).stdout)
    pid = {s["index"]: int(s["id"], 16) for s in got["streams"]}
    out = {}
    for p in got["packets"]:
        out.setdefault(pid[p["stream_index"]], []).append((int(p["pts"]) if "pts" in p else None, int(p["pos"]) if "pos" in p else None))
    return out


def ticks(ns):
    return ns * 9 / 100000


def paired(path, shift=0):
    """frames() of path, held to ffprobe: each PID has as many frames as ffprobe has packets, and each stored or bitstream
    time sits within a tick of ffprobe's pts plus shift ticks. A stored frame's place is ffprobe's pos."""
    got, ff = ts.frames(path), probe(path)
    assert got["error"] is None and sorted(t["pid"] for t in got["tracks"]) == sorted(ff), got["error"]
    for t in got["tracks"]:
        assert len(t["time"]) == len(ff[t["pid"]]), (t["pid"], len(t["time"]), len(ff[t["pid"]]))
        for k, ((pts, pos), time, tag) in enumerate(zip(ff[t["pid"]], t["time"], t["tag"])):
            assert tag in (matroska.STORED, matroska.BITSTREAM) and abs(ticks(time) - pts - shift) <= 1, (t["pid"], k, ticks(time), pts)
            assert tag != matroska.STORED or t["place"][k] == pos, (t["pid"], k, t["place"][k], pos)
    return got


@pytest.mark.parametrize("audio, codec", [(["aac"], "aac"), (["ac3"], "ac3"), (["eac3"], "eac3"), (["mp2"], "mp2"), (["libmp3lame"], "mp3"),
                                          (["dca", "-strict", "-2", "-b:a", "255k"], "dts")])
def test_each_audio_frame_pairs_with_ffprobes_packet(tmp_path, audio, codec):
    """ffmpeg's muxer puts each video frame in its own PES and several audio frames in one. The first frame of each
    audio PES is stored at its PTS, and each later one is bitstream: the time before plus the codec length of the frame
    before. Each matches ffprobe, whose parser splits the PES the same way."""
    got = paired(clip(tmp_path, f"{codec}.ts", audio))
    video, sound = got["tracks"]
    assert got["packet"] == 188 and (video["kind"], video["codec"], sound["kind"], sound["codec"]) == ("video", "h264", "audio", codec)
    assert set(video["tag"]) == {matroska.STORED} and list(video["dur"]) == [matroska.NONE] * len(video["dur"]) and all(video["full"])
    assert sound["tag"][0] == matroska.STORED and sound["tag"].count(matroska.BITSTREAM) >= len(sound["tag"]) // 2, codec
    assert sound["frame"] == pytest.approx({"aac": 1024, "mp2": 1152, "mp3": 1152, "dts": 512}.get(codec, 1536) * 1e9 / 48000)
    assert set(sound["dur"]) == {round(sound["frame"])} and sound["pes_no_pts"] == 0


def test_an_m2ts_file_has_192_byte_packets(tmp_path):
    """A Blu-ray M2TS packet starts with a 4-byte time before the sync byte. A frame's place is the byte of that time,
    as ffprobe's pos is."""
    got = paired(clip(tmp_path, "a.m2ts", ["ac3"], "-mpegts_m2ts_mode", "1"))
    assert got["packet"] == 192 and [t["pid"] for t in got["tracks"]] == [0x1011, 0x1100] and all(t["place"][0] % 192 == 0 for t in got["tracks"])


def test_times_keep_growing_across_the_pts_wrap(tmp_path):
    """The PTS counts 90 kHz ticks in 33 bits, so it wraps after 95443.7 s. Each PTS takes the value closest to the one
    before it, so the times after the wrap keep growing. ffmpeg instead takes 2^33 ticks from every time of a file that
    starts within 60 s of the wrap, so its times sit exactly 2^33 ticks lower."""
    got = paired(clip(tmp_path, "wrap.ts", ["aac"], "-output_ts_offset", "95441", secs=4), shift=WRAP)
    for t in got["tracks"]:
        x = list(t["time"])
        assert ticks(x[0]) < WRAP < ticks(x[-1]) and x == sorted(x), t["pid"]


def pts5(t):
    return bytes([0x21 | t >> 29 & 0x0E, t >> 22 & 0xFF, 0x01 | t >> 14 & 0xFE, t >> 7 & 0xFF, 0x01 | t << 1 & 0xFE])


def pes_packets(pid, data, pts, cc):
    """The 188-byte packets of a PES of data with pts or no PTS. The last packet ends with stuffing in its adaptation
    field. cc holds the next continuity count."""
    head = b"\x80\x80\x05" + pts5(pts) if pts is not None else b"\x80\x00\x00"
    pes, out = b"\0\0\1\xbd" + (len(head) + len(data)).to_bytes(2, "big") + head + data, []
    for i in range(0, len(pes), 184):
        part, af = pes[i:i + 184], b""
        if len(part) < 184:
            af = bytes([183 - len(part)]) + (b"\0" + b"\xff" * (182 - len(part)) if len(part) < 183 else b"")
        out.append(bytes([0x47, (0x40 if i == 0 else 0) | pid >> 8, pid & 0xFF, (0x30 if af else 0x10) | cc[0] & 15]) + af + part)
        cc[0] += 1
    return b"".join(out)


def test_a_frame_that_runs_past_its_pes_belongs_to_the_pes_where_it_starts(tmp_path):
    """The PES ends of this file fall inside the 768-byte AC-3 frames. A PTS times the first frame that starts in its PES
    (ISO/IEC 13818-1 2.4.3.7), and a frame that runs on into the next PES keeps its place. Each frame of a PES with no
    PTS is bitstream. ffprobe agrees on every frame but one. A 400-byte PES holds no frame start and still stores a PTS,
    1000 ticks late. That PTS times no frame, and the frame after it stays bitstream. ffmpeg gives that PTS to the frame
    that ends in that PES instead."""
    src, raw = clip(tmp_path, "ac3.ts", ["ac3", "-b:a", "192k"]), str(tmp_path / "a.ac3")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-c:a", "ac3", "-b:a", "192k", "-f", "ac3", raw],
               check=True)
    b, raw = pathlib.Path(src).read_bytes(), pathlib.Path(raw).read_bytes()
    psi = [b[i:i + 188] for i in range(0, len(b), 188) if (b[i + 1] & 0x1F) << 8 | b[i + 2] in (0, 0x1000)][:2]   # the PAT and the PMT
    out, cc, p, k, want = psi, [0], 0, 0, {}
    while p < len(raw):   # PES payloads of 1000, 1000, 500, 400 and 1300 bytes in turn, every fourth with no PTS
        n, first = (1000, 1000, 500, 400, 1300)[k % 5], -(-p // 768) * 768
        pts = None if k % 4 == 2 else 90000 + first // 768 * 2880 + (1000 if first >= p + n else 0)
        if pts is not None and first < min(p + n, len(raw)):
            want[first // 768] = len(b"".join(out))
        out.append(pes_packets(0x101, raw[p:p + n], pts, cc))
        p, k = p + n, k + 1
    (tmp_path / "s.ts").write_bytes(b"".join(out))
    t = ts.frames(str(tmp_path / "s.ts"))["tracks"][1]
    assert len(t["time"]) == len(raw) // 768 and list(t["time"]) == [round((90000 + k * 2880) * 100000 / 9) for k in range(len(raw) // 768)]
    assert [k for k, g in enumerate(t["tag"]) if g == matroska.STORED] == sorted(want) and {t["place"][k] for k in want} == set(want.values())
    ff = probe(str(tmp_path / "s.ts"))[0x101]
    moved = [k for k, (pts, _) in enumerate(ff) if abs(ticks(t["time"][k]) - pts) > 1]
    assert len(ff) == len(t["time"]) and len(moved) == 1 and ff[moved[0]][0] == 90000 + (moved[0] + 1) * 2880 + 1000, moved


def test_bytes_that_start_no_frame_join_the_frame_before(tmp_path):
    """A broadcast splice can leave bytes after a frame that start no frame. Each run of them belongs to the frame
    before, as ffmpeg's parser keeps them in that packet, and junk counts the runs. A run may cross a PES end. The frame
    after a run is bitstream when no PTS times it."""
    src, raw = clip(tmp_path, "ac3.ts", ["ac3", "-b:a", "192k"]), str(tmp_path / "a.ac3")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1", "-c:a", "ac3", "-b:a", "192k", "-f", "ac3", raw],
               check=True)
    b, raw = pathlib.Path(src).read_bytes(), pathlib.Path(raw).read_bytes()
    f = [raw[k:k + 768] for k in range(0, 5 * 768, 768)]
    psi = [b[i:i + 188] for i in range(0, len(b), 188) if (b[i + 1] & 0x1F) << 8 | b[i + 2] in (0, 0x1000)][:2]
    cc, junk = [0], b"\x55" * 300
    body = [pes_packets(0x101, f[0] + f[1] + junk, 90000, cc), pes_packets(0x101, junk[:100] + f[2] + f[3], None, cc),
            pes_packets(0x101, f[4] + junk, 90000 + 4 * 2880, cc)]
    (tmp_path / "j.ts").write_bytes(b"".join(psi + body))
    t = ts.frames(str(tmp_path / "j.ts"))["tracks"][1]
    assert list(t["time"]) == [round((90000 + k * 2880) * 100000 / 9) for k in range(5)] and t["junk"] == 2
    assert [matroska.TAGS[g] for g in t["tag"]] == ["stored", "bitstream", "bitstream", "bitstream", "stored"]
    ff = probe(str(tmp_path / "j.ts"))[0x101]
    assert [round(ticks(x)) for x in t["time"]] == [pts for pts, _ in ff], ff


@pytest.mark.parametrize("shape", ["false0", "false1", "false2", "cut"])
def test_bytes_before_a_latm_frame_join_that_frame(tmp_path, shape):
    """A cut LATM stream can start at a LOAS sync word inside the payload of a frame. ffmpeg's LATM parser takes it as a
    frame of the length it declares, and then skips to the next sync word. It puts the bytes it skips in the frame
    after them, and that frame takes the PTS of the PES where those bytes start. A stream cut inside a frame starts
    with such bytes too. Its first PES holds no frame start, and its PTS times the first frame. The reader does the
    same, so each frame pairs with ffprobe's packet and each known time matches. Before 2.18.6 such a frame took the
    PTS of the PES where its sync word sits, up to 213 ms off ffprobe's time."""
    src, raw = clip(tmp_path, "l.ts", ["aac"], "-mpegts_flags", "latm"), tmp_path / "a.latm"
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3", "-ac", "2", "-c:a", "aac", "-f", "latm",
                str(raw)], check=True)
    b, raw = pathlib.Path(src).read_bytes(), raw.read_bytes()
    starts, p = [], 0
    while p + 3 <= len(raw):   # each LOAS frame by the length after its sync word
        starts.append(p)
        p += 3 + ((raw[p + 1] & 0x1F) << 8 | raw[p + 2])
    false = [i for i in range(len(raw) - 1) if raw[i] == 0x56 and raw[i + 1] >> 5 == 7 and i not in set(starts)]
    start, size = (starts[3] + 5, 100) if shape == "cut" else (false[int(shape[-1])], 500)
    out, cc = [b[i:i + 188] for i in range(0, len(b), 188) if (b[i + 1] & 0x1F) << 8 | b[i + 2] in (0, 0x1000)][:2], [0]
    for p in range(start, len(raw), size):   # each PES at the time of the first frame that starts in it
        k = next((k for k, x in enumerate(starts) if x >= p), len(starts))
        none = 90000 + (k - 1) * 1920 + 7 if shape == "cut" else None   # a PES with no frame start: a PTS that times no frame start, or none
        out.append(pes_packets(0x101, raw[p:p + size], 90000 + k * 1920 if k < len(starts) and starts[k] < p + size else none, cc))
    (tmp_path / "f.ts").write_bytes(b"".join(out))
    t, ff = ts.frames(str(tmp_path / "f.ts"))["tracks"][1], probe(str(tmp_path / "f.ts"))[0x101]
    known = [(ticks(x), pts) for x, (pts, _) in zip(t["time"], ff) if x != matroska.NONE]
    assert t["codec"] == "aac_latm" and (t["junk"] or shape == "cut") and len(t["time"]) == len(ff) and len(known) > len(ff) // 2, (t["junk"], len(t["time"]), len(ff))
    assert all(abs(x - pts) <= 1 for x, pts in known), [(round(x), pts) for x, pts in known if abs(x - pts) > 1]


@pytest.mark.parametrize("damage", ["cut", "sync", "audio", "video"])
def test_damage_stops_the_read(tmp_path, damage):
    """A file cut inside a packet, a lost sync byte, a lost audio packet and a lost video packet each stop the read, and
    error says where. The tracks keep the frames whose PES starts before the damage, the same frames a whole read gives
    there. A lost video packet shows at the next PES start, and the frame of the PES that lost it goes too."""
    path = clip(tmp_path, "a.ts", ["aac"])
    whole, b = ts.frames(path), bytearray(pathlib.Path(path).read_bytes())
    if damage == "cut":
        b, why = b[:len(b) - 100], f"the file ends inside the packet at byte {len(b) // 188 * 188 - 188}"
    elif damage == "sync":
        b[188 * 100], why = 0x46, f"the packet at byte {188 * 100} holds no sync byte"
    elif damage == "audio":   # the 40th audio packet that starts no PES
        at = [i for i in range(0, len(b), 188) if (b[i + 1] & 0x5F) << 8 | b[i + 2] == 0x101][40]
        b, why = b[:at] + b[at + 188:], f"the packet at byte {at} of PID 257 breaks the continuity count"
    else:   # the last video packet that starts no PES, inside the frame whose PES starts at byte pes
        at = [i for i in range(0, len(b), 188) if (b[i + 1] & 0x5F) << 8 | b[i + 2] == 0x100][-1]
        pes = max(p for p in whole["tracks"][0]["place"] if p < at)
        b, why = b[:at] + b[at + 188:], f"the PES at byte {pes} of PID 256 lost a packet: the continuity count skips"
    (tmp_path / "d.ts").write_bytes(b)
    got = ts.frames(str(tmp_path / "d.ts"))
    assert got["error"] == why, got["error"]
    for t, w in zip(got["tracks"], whole["tracks"]):
        n = len(t["time"])
        assert n and list(t["time"]) == list(w["time"][:n]) and list(t["place"]) == list(w["place"][:n]), (damage, t["pid"])
    assert sum(len(t["time"]) for t in got["tracks"]) < sum(len(t["time"]) for t in whole["tracks"])


def test_the_codec_gives_the_size_and_the_length_of_a_unit():
    """Each audio header gives the bytes of its unit, and the samples and rate of its frame. An E-AC-3 dependent
    substream and a DTS-HD extension substream belong to the frame before them. Other bytes give none."""
    adts = (0xFFF1 << 40 | 1 << 38 | 3 << 34 | 2 << 30 | 300 << 13 | 0x7FF << 2 | 1).to_bytes(7, "big")   # 48 kHz, 2 raw data blocks
    assert ts.unit(adts + bytes(9)) == (300, 2048, 48000, True)
    ac3 = b"\x0b\x77\0\0" + bytes([1 << 6 | 1, 8 << 3]) + bytes(10)   # 44.1 kHz, 32 kbit/s, the code that adds a word
    assert ts.unit(ac3) == (140, 1536, 44100, True) and ts.unit(ac3[:4] + bytes([1 << 6]) + ac3[5:])[0] == 138
    eac3 = b"\x0b\x77" + bytes([1 << 6 | 1, 0x7F, 3 << 4, 16 << 3]) + bytes(10)   # a dependent substream of 384 words
    assert ts.unit(eac3) == (768, 1536, 48000, False) and ts.unit(eac3[:2] + b"\x01" + eac3[3:])[3] is True
    core = b"\x7f\xfe\x80\x01" + (1 << 47 | 31 << 42 | 15 << 34 | 1023 << 20 | 2 << 14 | 13 << 10).to_bytes(6, "big") + bytes(6)
    assert ts.unit(core) == (1024, 512, 48000, True)
    ext = b"\x64\x58\x20\x25" + (15 << 29 | 999 << 13).to_bytes(6, "big") + bytes(6)
    assert ts.unit(ext) == (1000, None, None, False)
    assert ts.unit(b"\x64\x58\x20\x25" + (1 << 37 | 15 << 25 | 99999 << 5).to_bytes(6, "big") + bytes(6))[0] == 100000
    assert ts.unit(bytes([0xFF, 0xFF, 12 << 4 | 1 << 1]) + bytes(13)) == (420, 384, 44100, True)   # MPEG-1 layer 1, 384 kbit/s, padded
    assert ts.unit(bytes([0xFF, 0xF3, 8 << 4]) + bytes(13)) == (208, 576, 22050, True)   # MPEG-2 layer 3, 64 kbit/s
    assert ts.unit(bytes(16)) is None and ts.unit(ac3[:9]) is None and ts.unit(bytes([0xFF, 0xFB, 0]) + bytes(13)) is None   # free format


@pytest.mark.parametrize("audio, extra, name", [(["truehd", "-strict", "-2"], [], "t.ts"), (["truehd", "-strict", "-2"], ["-mpegts_m2ts_mode", "1"], "t.m2ts"),
                                                (["aac"], ["-mpegts_flags", "latm"], "l.ts"), (["aac"], ["-mpegts_flags", "latm", "-mpegts_m2ts_mode", "1"], "l.m2ts")],
                         ids=["truehd ts", "truehd m2ts", "latm ts", "latm m2ts"])
def test_truehd_and_latm_frames_pair_with_ffprobes_packets(tmp_path, audio, extra, name):
    """A TrueHD PES splits into access units of 40 samples at 48 kHz, and a LATM PES into LOAS frames of 1024. Each
    frame pairs with ffprobe's packet and its time. ffmpeg's TS gives TrueHD no Blu-ray registration, and its M2TS
    gives LATM a private PES. So the reader takes each codec from the first PES there, as ffmpeg probes it."""
    got = paired(clip(tmp_path, name, audio, *extra))
    sound = got["tracks"][1]
    assert (sound["kind"], sound["codec"]) == ("audio", "truehd" if audio[0] == "truehd" else "aac_latm") and sound["tag"][0] == matroska.STORED
    assert sound["frame"] == pytest.approx((40 if audio[0] == "truehd" else 1024) * 1e9 / 48000) and sound["tag"].count(matroska.BITSTREAM) > len(sound["tag"]) // 2


@pytest.mark.parametrize("rate", [44100, 96000])
def test_a_truehd_unit_keeps_its_exact_length(tmp_path, rate):
    """A TrueHD access unit lasts 40 samples at 48 or 44.1 kHz, doubled with the rate, so 80 at 96 kHz. At 44.1 kHz that is
    81.63 ticks. The reader times each unit at the PTS of its PES plus that length per unit before it. ffprobe's
    parser adds 81 ticks a unit at 44.1 kHz, so its times run early within a PES. The counts match, and so does each
    time a PTS stores."""
    path = str(tmp_path / "t.ts")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}", "-t", "2", "-c:a", "truehd", "-strict", "-2", "-f",
                "mpegts", path], check=True)
    t, ff, unit = ts.frames(path)["tracks"][0], probe(path)[0x100], 40e9 / (44100 if rate == 44100 else 48000)
    stored = [k for k, g in enumerate(t["tag"]) if g == matroska.STORED]
    assert len(t["time"]) == len(ff) and t["frame"] == pytest.approx(unit) and all(round(ticks(t["time"][k])) == ff[k][0] for k in stored)
    for k in range(len(t["time"])):
        s = max(x for x in stored if x <= k)
        assert abs(t["time"][k] - t["time"][s] - round((k - s) * unit)) <= 1, k
    assert (max(abs(ticks(x) - pts) for x, (pts, _) in zip(t["time"], ff)) > 10) == (rate == 44100)   # ffprobe's estimates


def ext_pes(pid, data, pts, ext, cc):
    """The 188-byte packets of a PES of stream_id 0xFD with the stream_id_extension ext, as a Blu-ray TrueHD PID holds."""
    head = b"\x80\x81\x08" + pts5(pts) + b"\x0f\x81" + bytes([ext])   # PTS and PES_extension_flag_2
    pes, out = b"\0\0\1\xfd" + (len(head) + len(data)).to_bytes(2, "big") + head + data, []
    for i in range(0, len(pes), 184):
        part, af = pes[i:i + 184], b""
        if len(part) < 184:
            af = bytes([183 - len(part)]) + (b"\0" + b"\xff" * (182 - len(part)) if len(part) < 183 else b"")
        out.append(bytes([0x47, (0x40 if i == 0 else 0) | pid >> 8, pid & 0xFF, (0x30 if af else 0x10) | cc[0] & 15]) + af + part)
        cc[0] += 1
    return b"".join(out)


def bluray_m2ts(tmp_path, core, secs=2):
    """A made-up Blu-ray M2TS with ffmpeg's H.264 and TrueHD on PID 0x1100. The TrueHD units go into a PES of extension
    0x72 for each 1536 samples. With core, a PES of extension 0x76 with one AC-3 frame of those samples goes before each, as a
    Blu-ray holds its core. Each audio PES goes before the first video PES whose DTS is later."""
    src, thd, ac3 = clip(tmp_path, "s.m2ts", ["truehd", "-strict", "-2"], "-mpegts_m2ts_mode", "1", secs=secs), tmp_path / "a.thd", tmp_path / "a.ac3"
    for codec, out in ((["truehd", "-strict", "-2"], thd), (["ac3"], ac3)):   # AC-3 at 192 kbit/s: 768 bytes a frame
        amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(secs), "-ac", "2", "-c:a", *codec,
                    str(out)], check=True)
    b, thd, ac3 = pathlib.Path(src).read_bytes(), thd.read_bytes(), ac3.read_bytes()
    units, p = [], 0
    while p < len(thd):   # each access unit by the length in its first 2 bytes
        units.append(thd[p:p + ((thd[p] << 8 | thd[p + 1]) & 0xFFF) * 2])
        p += len(units[-1])
    pid = lambda i: (b[i + 5] & 0x1F) << 8 | b[i + 6]
    base, audio, cc = next(ts.pes(b, i + 4, i)[0] for i in range(0, len(b), 192) if pid(i) == 0x1100 and b[i + 5] & 0x40), [], [0]
    for f in range(len(ac3) // 768):
        lo, hi = -(-f * 1536 // 40), -(-(f + 1) * 1536 // 40)
        audio += [(base + f * 2880, ext_pes(0x1100, ac3[f * 768:f * 768 + 768], base + f * 2880, ts.CORE, cc))] if core else []
        audio.append((base + lo * 75, ext_pes(0x1100, b"".join(units[lo:hi]), base + lo * 75, 0x72, cc)))
    out, k = [], 0
    for i in range(0, len(b), 192):
        if pid(i) == 0x1011 and b[i + 5] & 0x40:
            pts, dts = ts.pes(b, i + 4, i)[:2]
            while k < len(audio) and audio[k][0] <= (pts if dts is None else dts):
                out += [b[i:i + 4] + audio[k][1][x:x + 188] for x in range(0, len(audio[k][1]), 188)]
                k += 1
        out += [b[i:i + 192]] if pid(i) != 0x1100 else []
    out += [b[-192:-188] + a[x:x + 188] for _, a in audio[k:] for x in range(0, len(a), 188)]
    path = tmp_path / ("core.m2ts" if core else "nocore.m2ts")
    path.write_bytes(b"".join(out))
    return path, len(units), len(ac3) // 768 if core else 0


@pytest.mark.parametrize("core", [True, False], ids=["core", "no core"])
def test_a_bluray_truehd_pid_gives_its_ac3_core_frames_of_its_own(tmp_path, core):
    """A Blu-ray TrueHD PID carries the AC-3 core in PES of extension 0x76 between the PES of its TrueHD units (0x72).
    ffmpeg reads the core as a stream of its own, also when the PID holds no PES of it. The reader gives the core its
    own frames, so each TrueHD unit and each AC-3 frame pairs with ffprobe's packet of its stream."""
    path, n, m = bluray_m2ts(tmp_path, core)
    t = next(t for t in ts.frames(str(path))["tracks"] if t["pid"] == 0x1100)
    got = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts:stream=index,codec_name", "-of", "json",
                                     str(path)], capture_output=True, text=True, check=True).stdout)
    codec = {s["index"]: s["codec_name"] for s in got["streams"]}
    for track, name in ((t, "truehd"), (t["core"], "ac3")):
        pts = [p["pts"] for p in got["packets"] if codec[p["stream_index"]] == name]
        assert track["codec"] == name and len(track["time"]) == len(pts) == (n if name == "truehd" else m), (name, len(track["time"]), len(pts))
        assert all(abs(ticks(x) - p) <= 1 for x, p in zip(track["time"], pts)), name
    assert "core" not in ts.frames(clip(tmp_path, "t.ts", ["truehd", "-strict", "-2"]))["tracks"][1]   # no Blu-ray registration: no core


def test_a_bluray_truehd_pid_leaves_out_its_ac3_core(tmp_path):
    """A Blu-ray TrueHD PID carries the AC-3 core in PES of extension 0x76 between the PES of its TrueHD units (0x72).
    ffmpeg reads the core as a stream of its own. The reader leaves those PES out, so each TrueHD unit pairs with
    ffprobe's packet and each AC-3 frame stays out of the TrueHD frames."""
    src, thd, ac3 = clip(tmp_path, "s.m2ts", ["truehd", "-strict", "-2"], "-mpegts_m2ts_mode", "1"), str(tmp_path / "a.thd"), str(tmp_path / "a.ac3")
    for codec, out, secs in ((["truehd", "-strict", "-2"], thd, "1"), (["ac3"], ac3, "2")):   # 1200 units, then a core frame for each 20 of them
        amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", secs, "-ac", "2", "-c:a", *codec, out], check=True)
    b, thd, ac3 = pathlib.Path(src).read_bytes(), pathlib.Path(thd).read_bytes(), pathlib.Path(ac3).read_bytes()
    psi = [b[i + 4:i + 192] for i in range(0, len(b), 192) if (b[i + 5] & 0x1F) << 8 | b[i + 6] in (0, 0x100)][:2]   # the PAT and the HDMV PMT
    units, p = [], 0
    while p < len(thd):   # each access unit by the length in its first 2 bytes
        units.append(thd[p:p + ((thd[p] << 8 | thd[p + 1]) & 0xFFF) * 2])
        p += len(units[-1])
    out, cc = psi, [0]
    for k in range(0, len(units), 20):   # 20 units a PES, and one AC-3 frame of 1536 samples after each
        out.append(ext_pes(0x1100, b"".join(units[k:k + 20]), 90000 + k * 75, 0x72, cc))
        out.append(ext_pes(0x1100, ac3[k // 20 * 768:k // 20 * 768 + 768], 90000 + k * 75, ts.CORE, cc))
    (tmp_path / "c.ts").write_bytes(b"".join(out))
    t = next(t for t in ts.frames(str(tmp_path / "c.ts"))["tracks"] if t["pid"] == 0x1100)
    assert t["codec"] == "truehd" and len(t["time"]) == len(units) and len(t["core"]["time"]) == len(units) // 20
    assert max(abs(x - (90000 + k * 75) * 100000 / 9) for k, x in enumerate(t["time"])) <= 1   # 1 ns of rounding
    got = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries", "stream=codec_name,nb_read_packets", "-of", "json",
                                     str(tmp_path / "c.ts")], capture_output=True, text=True, check=True).stdout)["streams"]
    assert {s["codec_name"]: int(s["nb_read_packets"]) for s in got if s["codec_name"] != "h264"} == {"truehd": len(units), "ac3": len(units) // 20}, got


def test_the_extension_byte_follows_every_optional_field():
    """extension() finds the stream_id_extension of a PES of stream_id 0xFD after each optional field that its flags
    name (ISO/IEC 13818-1 2.4.3.7). A PES of another stream_id gives none. So do a PES with no PES_extension_flag_2
    and one whose extension byte sets the stream_id_extension_flag."""
    def head(sid=0xFD, pd=2, extra=0, private=False, pack=0, seq=False, pstd=False, ext=0x76, flag=0, two=1):
        fields = bytes((0, 0, 5, 10)[pd]) + bytes(6 * (extra >> 5 & 1) + 3 * (extra >> 4 & 1) + (extra >> 3 & 1) + (extra >> 2 & 1) + 2 * (extra >> 1 & 1))
        e = private << 7 | (pack > 0) << 6 | seq << 5 | pstd << 4 | two
        fields += bytes([e]) + b"\xaa" * (16 * private) + (bytes([pack]) + b"\xaa" * pack if pack else b"") + b"\xaa" * (2 * seq + 2 * pstd)
        c = b"\0\0\1" + bytes([sid]) + b"\0\0" + bytes([0x80, pd << 6 | extra | 1, len(fields) + 2]) + fields + bytes([0x81, flag << 7 | ext])
        return ts.extension(c, 0, len(c))
    assert head() == head(pd=0) == head(pd=3) == head(extra=0x3E) == 0x76 and head(ext=0x72) == 0x72   # PTS, none, PTS and DTS, every other field
    assert head(private=True) == head(pack=3) == head(seq=True) == head(pstd=True) == head(pd=3, private=True, pack=2, seq=True, pstd=True) == 0x76
    assert head(sid=0xBD) is None and head(two=0) is None and head(flag=1) is None


def test_a_private_pes_with_no_latm_config_names_no_codec(tmp_path):
    """ffmpeg's M2TS gives LATM a private PES, so the reader takes the codec from the first PES. That PES must start with
    a LOAS frame that holds its StreamMuxConfig. With useSameStreamMux set in that frame, the PID names no codec and
    gives no frames."""
    b = bytearray(pathlib.Path(clip(tmp_path, "l.m2ts", ["aac"], "-mpegts_flags", "latm", "-mpegts_m2ts_mode", "1")).read_bytes())
    i = next(i for i in range(0, len(b), 192) if (b[i + 5] & 0x1F) << 8 | b[i + 6] == 0x1100 and b[i + 5] & 0x40)
    x = ts.pes(b, i + 4, i)[2]
    assert b[x] == 0x56 and b[x + 3] < 0x80, bytes(b[x:x + 4])   # a LOAS frame whose config follows
    b[x + 3] |= 0x80   # useSameStreamMux
    (tmp_path / "n.m2ts").write_bytes(bytes(b))
    t = next(t for t in ts.frames(str(tmp_path / "n.m2ts"))["tracks"] if t["pid"] == 0x1100)
    assert t["codec"] is None and not len(t["time"]), t["codec"]


def test_a_latm_config_of_version_1_reads_in_the_stream(tmp_path):
    """A StreamMuxConfig of audioMuxVersion 1 holds a taraBufferFullness of up to 4 bytes before its AudioSpecificConfig.
    The reader reads HEAD bytes of each frame, enough for that config. So each frame gets its length, and the frame
    after a stored one gets its time."""
    bits = "".join(format(v, f"0{w}b") for v, w in ((0, 1), (1, 1), (0, 1), (2, 2), (0x123456, 24), (1, 1), (0, 6), (0, 4), (0, 3), (0, 2), (2, 8),
                                                     (2, 5), (3, 4), (2, 4), (0, 3)))   # tara of 3 bytes, ascLen of 1, AAC LC at 48 kHz
    bits += "0" * (-len(bits) % 8)
    frame = b"\x56" + bytes([0xE0 | 300 >> 8, 300 & 0xFF]) + int(bits, 2).to_bytes(len(bits) // 8, "big").ljust(300, b"\0")
    b, cc = pathlib.Path(clip(tmp_path, "l.ts", ["aac"], "-mpegts_flags", "latm")).read_bytes(), [0]
    out = [b[i:i + 188] for i in range(0, len(b), 188) if (b[i + 1] & 0x1F) << 8 | b[i + 2] in (0, 0x1000)][:2]   # the PAT and the PMT
    out += [pes_packets(0x101, frame * 2, 90000 + k * 3840, cc) for k in range(10)]   # 2 frames a PES
    (tmp_path / "v1.ts").write_bytes(b"".join(out))
    t = ts.frames(str(tmp_path / "v1.ts"))["tracks"][1]
    want = [(90000 + k * 1920) * 100000 / 9 for k in range(20)]   # each in ns, rounded
    assert t["codec"] == "aac_latm" and len(t["time"]) == 20 and all(abs(x - w) <= 1 for x, w in zip(t["time"], want)), list(t["time"])[:4]


def test_a_latm_config_names_the_frame_length():
    """A LOAS frame that holds its StreamMuxConfig names its subframes and its AudioSpecificConfig, in audioMuxVersion
    0 and 1. A frame with no config, or audioMuxVersionA 1, names none. Other bytes give no frame."""
    def frame(*fields):   # a LOAS frame of 300 bytes whose AudioMuxElement starts with the (value, bits) fields
        bits = "".join(format(v, f"0{w}b") for v, w in fields)
        bits += "0" * (-len(bits) % 8 + 64)
        return b"\x56" + bytes([0xE0 | 300 >> 8, 300 & 0xFF]) + int(bits, 2).to_bytes(len(bits) // 8, "big")
    asc = [(2, 5), (3, 4), (2, 4), (0, 3)]   # AAC LC at 48 kHz, 2 channels, 1024 samples
    v0 = frame((0, 1), (0, 1), (1, 1), (1, 6), (0, 4), (0, 3), *asc)   # version 0, 2 subframes
    v1 = frame((0, 1), (1, 1), (0, 1), (0, 2), (0x55, 8), (1, 1), (0, 6), (0, 4), (0, 3), (0, 2), (2, 8), *asc)   # tara and ascLen of 1 byte each
    assert ts.latm_unit(v0) == (303, 2048, 48000, True) and ts.latm_unit(v1) == (303, 1024, 48000, True)
    config = [(0, 1), (0, 2), (0x55, 8), (1, 1), (0, 6), (0, 4), (0, 3), (0, 2), (2, 8), *asc]   # v1's fields after audioMuxVersionA
    assert ts.latm_unit(frame((1, 1))) == (303, None, None, True) and ts.latm_unit(frame((0, 1), (1, 1), (1, 1), *config[1:])) == (303, None, None, True)
    assert ts.latm_unit(b"\x56\x1f" + bytes(10)) is None and ts.latm_unit(v0[:6]) == (303, None, None, True)


def test_a_truehd_major_sync_names_the_rate():
    """A TrueHD access unit gives its length in 16-bit words after the check nibble. A unit with a major sync (0xBA)
    names the rate code in its next 4 bits. Code 0 is 48 kHz, 2 is 192 kHz and 8 is 44.1 kHz, each with 40 samples
    doubled with the rate. MLP (0xBB) names it 4 bits later. Code 15 names no rate, and so does a unit with no major sync."""
    unit = lambda sync, a, b: bytes([0x10 | 0, 60, 0, 0]) + b"\xf8\x72\x6f" + bytes([sync, a, b]) + bytes(6)
    assert ts.truehd_unit(unit(0xBA, 0x00, 0)) == (120, 40, 48000, True) and ts.truehd_unit(unit(0xBA, 0x20, 0)) == (120, 160, 192000, True)
    assert ts.truehd_unit(unit(0xBA, 0x80, 0)) == (120, 40, 44100, True) and ts.truehd_unit(unit(0xBB, 0x00, 0x90)) == (120, 80, 88200, True)
    assert ts.truehd_unit(unit(0xBA, 0xF0, 0)) == (120, None, None, True) and ts.truehd_unit(unit(0xBC, 0x00, 0)) == (120, None, None, True)
    assert ts.truehd_unit(bytes([0x10, 1]) + bytes(14)) is None and ts.truehd_unit(unit(0xBA, 0, 0)[:9]) is None
