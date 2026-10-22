# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The frame reader, matroska.frames(), and the time check of two Matroska files that reads it (docs/design.md, "Frame
times"). Every file is made up, byte by byte."""
import random
import zlib

import pytest

from test_arr_media_guard import hook
from arr_media_guard import matroska

NS = 1000000   # ns of one tick at the usual TimestampScale


def el(i, data):
    """An EBML element with an 8-byte size."""
    return i.to_bytes((i.bit_length() + 7) // 8, "big") + (1 << 56 | len(data)).to_bytes(8, "big") + data


def uint(i, v):
    return el(i, v.to_bytes(8, "big"))


def entry(number, kind, codec, dd=None, private=None, strip=None):
    """A TrackEntry of type kind (1 video, 2 audio, 17 subtitle). strip is the bytes of a header stripping."""
    data = uint(0xD7, number) + uint(0x83, kind) + el(0x86, codec.encode()) + (uint(0x23E383, dd) if dd else b"")
    data += el(0x63A2, private) if private else b""
    data += el(0x6D80, el(0x6240, el(0x5034, uint(0x4254, 3) + el(0x4255, strip)))) if strip else b""
    return el(0xAE, data)


def block(track, rel, frames, lacing=None):
    """The data of a SimpleBlock of track at rel ticks from its Cluster, holding frames, laced "xiph", "ebml" or
    "fixed", or one frame."""
    flags = 0x80 | {None: 0, "xiph": 0x02, "ebml": 0x06, "fixed": 0x04}[lacing]
    head = bytes([0x80 | track]) + rel.to_bytes(2, "big", signed=True) + bytes([flags])
    if lacing is None:
        return head + frames[0]
    lace = bytes([len(frames) - 1])
    if lacing == "xiph":
        for f in frames[:-1]:
            lace += b"\xff" * (len(f) // 255) + bytes([len(f) % 255])
    elif lacing == "ebml":   # the first size, then each change of size, as 2-byte signed integers
        lace += (0x4000 | len(frames[0])).to_bytes(2, "big")
        for a, b in zip(frames, frames[1:-1]):
            lace += (0x4000 | (len(b) - len(a) + 8191)).to_bytes(2, "big")
    return head + lace + b"".join(frames)


def simple(*a, **k):
    return el(0xA3, block(*a, **k))


def group(track, rel, data, dur):
    """A BlockGroup of one frame with a BlockDuration of dur ticks."""
    return el(0xA0, el(0xA1, block(track, rel, [data])) + uint(0x9B, dur))


def mkv(path, entries, clusters, scale=NS, open_at=None):
    """A Matroska file at path: the EBML header, Info with scale, Tracks of entries, and a Cluster of each (timestamp,
    [children]) of clusters, a timestamp of None for none. The Cluster at place open_at gets an unknown size."""
    body = el(0x1549A966, uint(0x2AD7B1, scale)) + el(0x1654AE6B, b"".join(entries))
    for k, (ts, kids) in enumerate(clusters):
        data = (b"" if ts is None else uint(0xE7, ts)) + b"".join(kids)
        body += (b"\x1f\x43\xb6\x75\x01\xff\xff\xff\xff\xff\xff\xff" + data) if k == open_at else el(0x1F43B675, data)
    with open(path, "wb") as f:
        f.write(bytes.fromhex("1a45dfa3" "84" "42860181") + el(0x18538067, body))
    return str(path)


def dts(fsize=1024):
    """A DTS core frame of 512 samples at 48 kHz: the sync word, then FTYPE, SHORT, CPF, NBLKS 15, FSIZE, AMODE and
    SFREQ 13."""
    h = 1 << 47 | 31 << 42 | 15 << 34 | (fsize - 1) << 20 | 2 << 14 | 13 << 10
    return b"\x7f\xfe\x80\x01" + h.to_bytes(6, "big") + bytes(fsize - 10)


AAC_LC_48K = bytes([0x11, 0x90])   # AudioSpecificConfig: AAC LC, 48 kHz, 2 channels
DTS_FRAME = 512 * 1e9 / 48000


def tagged(t):
    return [(None if x == matroska.NONE else x, matroska.TAGS[g]) for x, g in zip(t["time"], t["tag"])]


def test_the_walk_tags_each_frame_by_where_its_time_comes_from(tmp_path):
    """An unlaced block stores its time, and so does frame 0 of a lace. Frame k of a lace sits k DefaultDurations
    later, else after the codec lengths of the frames before it, else its time is not known. A block lasts its
    BlockDuration, else the DefaultDuration, else a lace frame its codec length. An audio block that starts more than a tick before the end of the block before overlaps it, and one
    that starts more than a tick after it leaves a gap."""
    path = mkv(tmp_path / "a.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 2, "A_AAC", 21333333, AAC_LC_48K),
                                    entry(3, 2, "A_DTS"), entry(4, 2, "A_VORBIS"), entry(5, 17, "S_TEXT/UTF8"), entry(6, 2, "A_AAC", private=AAC_LC_48K)],
               [(0, [simple(1, 0, [b"v0"]), simple(2, 0, [b"a" * 300, b"b" * 7, b"c" * 9], "xiph"), simple(3, 0, [dts(), dts(), dts()], "ebml"),
                     simple(4, 0, [b"x" * 4, b"y" * 4], "fixed"), simple(5, 100, [b"one"]), group(5, 500, b"two", 200), simple(5, 900, [b"three"]),
                     simple(6, 0, [b"p", b"q"], "xiph")]),
                (1000, [simple(1, 0, [b"v1"]), simple(2, -950, [b"d"]), simple(3, -900, [dts()])])])
    got = {t["number"]: t for t in matroska.frames(path)["tracks"]}
    assert tagged(got[1]) == [(0, "stored"), (1000 * NS, "stored")] and list(got[1]["dur"]) == [40 * NS] * 2
    assert tagged(got[2]) == [(0, "stored"), (21333333, "default"), (42666666, "default"), (50 * NS, "stored")]
    assert [(round(x / 1e3) if x else x, g) for x, g in tagged(got[3])] == [(0, "stored"), (10667, "bitstream"), (21333, "bitstream"), (100000, "stored")]
    assert tagged(got[4]) == [(0, "stored"), (None, "unknown")] and got[4]["frame"] is None
    assert tagged(got[6]) == [(0, "stored"), (21333333, "bitstream")] and got[6]["frame"] == pytest.approx(1024 * 1e9 / 48000)
    assert list(got[5]["dur"]) == [matroska.NONE, 200 * NS, matroska.NONE]   # its BlockDuration, else none
    assert got[2]["overlaps"] == [1, 13999999, 50 * NS] and list(got[2]["lapped"]) == [50 * NS] and got[2]["gaps"][0] == 0
    assert got[3]["gaps"] == [1, pytest.approx(100 * NS - 3 * DTS_FRAME, abs=10), 100 * NS] and got[3]["overlaps"][0] == 0
    assert got[3]["frame"] == pytest.approx(DTS_FRAME) and got[2]["frame"] == 21333333


def test_an_empty_frame_of_a_lace_holds_no_data(tmp_path):
    """A lace may hold a frame of 0 bytes. It keeps its place in the lace, so the frames after it keep their times,
    and full marks it empty. ffmpeg makes no packet of it, so stored_places() leaves it out."""
    path = mkv(tmp_path / "z.mkv", [entry(1, 2, "A_AAC", private=AAC_LC_48K)], [(0, [simple(1, 0, [b"p", b"", b"q"], "xiph")])])
    t = matroska.frames(path)["tracks"][0]
    assert list(t["full"]) == [1, 0, 1] and t["time"][2] == round(2 * 1024 * 1e9 / 48000), t
    assert list(hook.stored_places(path, [{"index": 0, "codec_type": "audio"}])[0][0]) == [0.0, t["time"][2] / 1e9]


def test_a_packed_text_frame_that_unpacks_to_nothing_holds_no_data():
    """A frame of a packed text track holds data unless its zlib stream ends with no byte out. A cut stream and bytes
    that do not inflate hold data, so the proof refuses them. A frame of 0 bytes holds none. An unpacked track, a
    packed picture track and a packed video track count the stored bytes."""
    frames, b, blocks = [zlib.compress(b""), zlib.compress(b"Hi"), zlib.compress(b"Hi")[:3], b"no zlib", b""], b"", []
    for k, x in enumerate(frames):   # (time, frames, byte, frame start, end, lacing, BlockDuration, empty mask, heads)
        blocks.append((k, 1, len(b), len(b), len(b) + len(x), 0, 1, 0, None))
        b += x
    t = {"number": 3, "type": 17, "codec": "S_TEXT/UTF8", "dd": None, "private": b"", "strip": b"", "packed": True}
    full = lambda **k: list(matroska.track_frames(b, dict(t, **k), blocks, NS)["full"])
    assert full() == [0, 1, 1, 1, 0]
    assert full(packed=False) == full(codec="S_HDMV/PGS") == full(type=1) == [1, 1, 1, 1, 0]


def test_a_block_within_a_tick_of_the_end_before_it_is_no_overlap(tmp_path):
    """A lace of 3 frames of 21.333 ms ends at 63.999999 ms. Each block stores its time in whole ticks, so a next block at
    63 ms or 64 ms sits within a tick of that end, and the walk counts neither an overlap nor a gap. One at 62 ms
    overlaps."""
    lace = [b"a", b"b", b"c"]
    for at, over in ((63, 0), (64, 0), (62, 1)):
        path = mkv(tmp_path / f"t{at}.mkv", [entry(1, 2, "A_AAC", 21333333, AAC_LC_48K)], [(0, [simple(1, 0, lace, "xiph"), simple(1, at, lace, "xiph")])])
        t = matroska.frames(path)["tracks"][0]
        assert t["overlaps"][0] == over and t["gaps"][0] == 0 and len(t["lapped"]) == over, (at, t["overlaps"], t["gaps"])


def test_the_walk_skips_a_zero_run_before_tracks_at_the_end(tmp_path):
    """mkvpropedit can leave a run of zero bytes where Tracks stood and write Tracks at the end of the Segment. The walk
    skips the run and reads the Tracks after it. A zero run before the first Cluster reads too, and a zero run at the
    end of the Segment ends the walk with no error."""
    head = bytes.fromhex("1a45dfa3" "84" "42860181")
    clusters = el(0x1F43B675, uint(0xE7, 0) + simple(1, 0, [b"a"])) + el(0x1F43B675, uint(0xE7, 40) + simple(1, 0, [b"b"]))
    body = el(0x1549A966, uint(0x2AD7B1, NS)) + bytes(100) + clusters + bytes(300) + el(0x1654AE6B, entry(1, 2, "A_AC3", 32 * NS)) + bytes(50)
    (tmp_path / "z.mkv").write_bytes(head + el(0x18538067, body))
    got = matroska.frames(str(tmp_path / "z.mkv"))
    assert got["error"] is None and list(got["tracks"][0]["time"]) == [0, 40 * NS], got


def test_the_walk_takes_a_scale_an_open_cluster_and_a_header_stripping(tmp_path):
    """A TimestampScale of 100 us scales every time. A Cluster of unknown size ends at the next Cluster. A header
    stripping takes the DTS sync word from each frame, and the walk puts it back before it reads the frame length."""
    path = mkv(tmp_path / "b.mkv", [entry(1, 2, "A_DTS", strip=b"\x7f\xfe\x80\x01")],
               [(0, [simple(1, 0, [dts()[4:], dts()[4:]], "xiph")]), (500, [simple(1, 0, [dts()[4:]])]), (900, [simple(1, 3, [dts()[4:]])])],
               scale=100000)
    t = matroska.frames(path)["tracks"][0]
    assert [round(x / 1e3) for x in t["time"]] == [0, 10667, 50000, 90300] and t["tag"][1] == matroska.BITSTREAM
    path = mkv(tmp_path / "c.mkv", [entry(1, 2, "A_AC3", 32 * NS)], [(0, [simple(1, 0, [b"a"])]), (40, [simple(1, 0, [b"b"])])], open_at=0)
    assert list(matroska.frames(path)["tracks"][0]["time"]) == [0, 40 * NS]


@pytest.mark.parametrize("bad", ["a child that is no Cluster child", "a block before the Timestamp", "a lace past its block",
                                 "lace sizes past their block"])
def test_the_walk_stops_at_a_cluster_that_does_not_read(tmp_path, bad):
    """A damaged Cluster stops the walk. The frames before it stay, and error says where the walk stopped, so a caller
    never takes a part of a file for all of it. Lace sizes that do not read are damage too, also when they run to the
    end of the file."""
    kids = {"a child that is no Cluster child": [simple(1, 0, [b"a"]), el(0x4D80, b"junk")],
            "a block before the Timestamp": [simple(1, 0, [b"a"]), uint(0xE7, 0)],
            "a lace past its block": [el(0xA3, block(1, 0, [b"ab", b"cd"], "xiph")[:-3])],
            "lace sizes past their block": [el(0xA3, block(1, 0, [b"ab", b"cd"], "xiph")[:5] + b"\xff" * 4)]}[bad]
    path = mkv(tmp_path / "d.mkv", [entry(1, 2, "A_DTS")], [(0, [simple(1, 0, [dts()])]), (None if bad == "a block before the Timestamp" else 50, kids)])
    if bad.startswith("lace"):
        with open(path, "rb") as f:
            assert f.read().endswith(b"\xff" * 4)   # the size run reaches the end of the file
    with pytest.raises(RuntimeError, match="^the block walk of d.mkv failed: the "):   # the proof reads no part of a file
        hook.stored_places(path, [{"index": 0, "codec_type": "audio"}])
    got = matroska.frames(path)
    assert got["error"].startswith(("the Cluster at byte", "the block at byte", "the lace")) and got["whole"] is False, got
    assert len(got["tracks"][0]["time"]) == (2 if bad == "a child that is no Cluster child" else 1), got


@pytest.mark.parametrize("bad", ["a child that is no Cluster child", "a block before the Timestamp", "a zero run between two Clusters",
                                 "a Cluster size that does not read", "a Tags of unknown size after the last Cluster",
                                 "a Cluster id inside a frame before the damage"])
def test_the_walk_reads_on_from_the_next_cluster_after_damage(tmp_path, bad):
    """ffmpeg reads on past a damaged Cluster from the next level-1 id, and so does the walk, from the next Cluster id.
    read_on holds every block it read, and resumed marks the first frame after a resync. tracks and error stay as the
    first damage leaves them, and whole stays False, so the proof reads no part of the file. That holds when an element
    after the last Cluster does not read too, which alone leaves whole True. The search after damage takes a Cluster
    only when it reads whole, so it passes over the Cluster with a lace that does not read."""
    first = {"a block before the Timestamp": (None, [simple(1, 0, [b"b"]), uint(0xE7, 100)])}.get(bad, (100, [simple(1, 0, [b"b"])]))
    first = (100, [simple(1, 0, [b"b"]), el(0x4D80, b"junk")]) if bad.startswith(("a child", "a Tags")) else first
    first = (100, [simple(1, 0, [b"\x1f\x43\xb6\x75\x80"]), el(0x4D80, b"junk")]) if bad.startswith("a Cluster id") else first
    path = mkv(tmp_path / "r.mkv", [entry(1, 2, "A_AC3", 32 * NS)],
               [(0, [simple(1, 0, [b"a"])]), first, (200, [el(0xA3, block(1, 0, [b"ab", b"cd"], "xiph")[:-3])]),
                (300, [simple(1, 0, [b"c"]), simple(1, 40, [b"d"])])])
    data = bytearray(open(path, "rb").read())
    at = [k for k in range(len(data)) if data[k:k + 4] == b"\x1f\x43\xb6\x75"]
    if bad.startswith("a zero run"):
        data[at[1]:at[2]] = bytes(at[2] - at[1])
    elif bad.startswith("a Cluster size"):
        data[at[1] + 4] = 0   # a size with no length marker
    elif bad.startswith("a Tags"):   # the Segment grows by an element whose size runs past it
        data += b"\x12\x54\xc3\x67\x88" + bytes(3)
        size = int.from_bytes(data[13:21], "big") & ((1 << 56) - 1)
        data[13:21] = (1 << 56 | size + 8).to_bytes(8, "big")
    path = str(tmp_path / "z.mkv")
    open(path, "wb").write(data)
    got = matroska.frames(path)
    before = [0, 100] if bad.startswith(("a child", "a Tags", "a Cluster id")) else [0]
    assert list(got["tracks"][0]["time"]) == [x * NS for x in before] and got["whole"] is False and len(got["resyncs"]) == 1, got
    assert got["error"].startswith(("the Cluster at byte", "the block at byte", "a run of zero bytes", "the Segment holds no")), got
    on = got["read_on"][0]
    assert list(on["time"]) == [x * NS for x in before + [300, 340]] and on["resumed"] == len(before), on
    with pytest.raises(RuntimeError, match="^the block walk of z.mkv failed: "):
        hook.stored_places(path, [{"index": 0, "codec_type": "audio"}])
    if bad.startswith("a Tags"):
        assert got["error"].startswith("the Cluster at byte") and "unknown size" not in got["error"]   # the first damage stays the error


@pytest.mark.parametrize("grow", ["10 bytes", "to the Segment end"])
def test_a_cluster_size_that_runs_into_the_next_cluster_loses_no_cluster(tmp_path, grow):
    """A Cluster whose size runs into the next Cluster is damage at the byte of that Cluster's id, and the walk reads on
    from that very Cluster. Its CRC-32 comes first, as ffmpeg writes one, and its Timestamp equals the one before the
    damage, which counts as no earlier. A track with no block before the damage keeps no frame in tracks, and all of
    its frames in read_on. A size that runs to the Segment end leaves no Cluster after the damaged one, so its
    Timestamp still bounds the search, and its block stays."""
    crc = [el(0xBF, bytes(4)), uint(0xE7, 100), simple(1, 100, [b"c"]), simple(2, 100, [b"x"])]
    path = mkv(tmp_path / "s.mkv", [entry(1, 2, "A_AC3", 32 * NS), entry(2, 2, "A_AC3", 32 * NS)],
               [(0, [simple(1, 0, [b"a"])]), (100, [simple(1, 0, [b"b"])]), (None, crc), (300, [simple(1, 0, [b"d"]), simple(2, 0, [b"y"])])])
    data = bytearray(open(path, "rb").read())
    at = [k for k in range(len(data)) if data[k:k + 4] == b"\x1f\x43\xb6\x75"]
    size = int.from_bytes(data[at[1] + 4:at[1] + 12], "big") & ((1 << 56) - 1)
    size = size + 10 if grow == "10 bytes" else len(data) - at[1] - 12
    data[at[1] + 4:at[1] + 12] = (1 << 56 | size).to_bytes(8, "big")
    (tmp_path / "s.mkv").write_bytes(bytes(data))
    got = matroska.frames(path)
    assert got["error"] == f"the Cluster at byte {at[1] + 12} holds no Cluster child at byte {at[2]}" and got["resyncs"] == [(at[2], at[2])], got
    assert [list(t["time"]) for t in got["tracks"]] == [[0, 100 * NS], []], got["tracks"]
    assert [list(t["time"]) for t in got["read_on"]] == [[x * NS for x in (0, 100, 200, 300)], [200 * NS, 300 * NS]], got["read_on"]


@pytest.mark.parametrize("ts", [1, 0x7FFFFFFF, None])
def test_a_cluster_id_inside_damage_puts_no_frame_in_the_read(tmp_path, ts):
    """Damaged bytes can hold a Cluster id whose children read for a while. After damage the walk takes a Cluster only
    when its first child is a Timestamp no earlier than the damaged Cluster the walk was in, here 100 ms, and every
    child reads. The damaged Cluster sets that bound because its Timestamp is no later than the next Cluster that reads
    whole after its end, at 300 ms. A false Cluster at 1 ms reads whole but is too early. One at 2,147,483 s runs into the zeros. One that holds only a Void
    has no Timestamp. None gives a frame or a resync of its own, and the walk reads on from the real Cluster after
    them."""
    kids = el(0xEC, bytes(4)) if ts is None else uint(0xE7, ts) + simple(1, 0, [b"f"])
    fake = b"\x1f\x43\xb6\x75\x10\x00\x10\x00" + kids if ts == 0x7FFFFFFF else el(0x1F43B675, kids)   # 4096 bytes, past its data
    path = mkv(tmp_path / "f.mkv", [entry(1, 2, "A_AC3", 32 * NS)],
               [(0, [simple(1, 0, [b"a"])]), (100, [simple(1, 0, [b"b"]), bytes(16) + fake + bytes(64)]),
                (300, [simple(1, 0, [b"c"]), simple(1, 40, [b"d"])])])
    got = matroska.frames(path)
    assert list(got["tracks"][0]["time"]) == [0, 100 * NS] and len(got["resyncs"]) == 1, got
    assert list(got["read_on"][0]["time"]) == [x * NS for x in (0, 100, 300, 340)], got["read_on"][0]["time"]


def chain(path, items):
    """A Matroska file at path of one AC-3 track and a Cluster of one block at ms for each (ms, kind) of items. Kind w
    reads whole. d adds a child after the block that no Cluster holds, so the Cluster is damaged there. p puts a
    PrevSize before the Timestamp. wi and di hold a whole Cluster at 260 ms inside the frame, and dd a damaged one. j
    is two bytes of no level-1 element between Clusters, z 16 zero bytes, t a Tags whose size runs past the end, u
    a Tags of unknown size, g a whole Tags, and c a Cluster id and 2 bytes of its size, as the end of a cut file."""
    junk = el(0x4D80, b"junk")
    inner = {k: el(0x1F43B675, uint(0xE7, 260) + simple(1, 0, [b"z"]) + (junk if k == "dd" else b"")) for k in ("wi", "di", "dd")}
    other = {"j": b"\xe7\x80", "z": bytes(16), "t": b"\x12\x54\xc3\x67\x88", "u": b"\x12\x54\xc3\x67\x01" + b"\xff" * 7,   # no Cluster
             "g": el(0x1254C367, el(0x7373, b"x")), "c": b"\x1f\x43\xb6\x75\x01\x00"}
    body = el(0x1549A966, uint(0x2AD7B1, NS)) + el(0x1654AE6B, entry(1, 2, "A_AC3", 32 * NS))
    for ms, kind in items:
        frame = b"x" + inner.get(kind, b"") + b"x"
        body += other[kind] if kind in other else el(0x1F43B675, (uint(0xAB, 0) if kind == "p" else b"") + uint(0xE7, ms) + simple(1, 0, [frame])
                                                      + (junk if kind[0] == "d" else b""))
    path.write_bytes(bytes.fromhex("1a45dfa3" "84" "42860181") + el(0x18538067, body))
    return str(path)


W0, W100, J, Z, T, U, G, C = (0, "w"), (100, "w"), (None, "j"), (None, "z"), (None, "t"), (None, "u"), (None, "g"), (None, "c")
TRUST = {   # items, then the ms of the frames of read_on, or None when the walk read on from no Cluster
    "damaged Clusters in a row keep their blocks": ([W0, W100, (200, "d"), (250, "d"), (300, "w"), (400, "w")], [0, 100, 200, 250, 300, 400]),
    "a found Cluster earlier than the damaged one": ([W0, W100, (200, "d"), (199, "d"), (300, "w")], [0, 100, 200, 300]),
    "a found Cluster at the damaged one": ([W0, W100, (200, "d"), (200, "d"), (300, "w")], [0, 100, 200, 200, 300]),
    "a found Cluster at the next whole one": ([W0, W100, (200, "d"), (300, "d"), (300, "w")], [0, 100, 200, 300, 300]),
    "a found Cluster later than the next whole one": ([W0, W100, (200, "d"), (301, "d"), (300, "w")], [0, 100, 200, 300]),
    "a damaged Cluster earlier than the trusted one": ([W0, W100, (99, "d"), (300, "w"), (400, "w")], [0, 100, 300, 400]),
    "a damaged Cluster at the trusted one": ([W0, W100, (100, "d"), (300, "w")], [0, 100, 100, 300]),
    "a damaged Cluster at the next whole one": ([W0, W100, (300, "d"), (300, "w")], [0, 100, 300, 300]),
    "a damaged Cluster later than the next whole one": ([W0, W100, (301, "d"), (300, "w"), (400, "w")], [0, 100, 300, 400]),
    "a found Cluster at the trusted one after a late damaged one": ([W0, W100, (301, "d"), (100, "d"), (300, "w")], [0, 100, 100, 300]),
    "a found Cluster earlier than the trusted one after a late damaged one": ([W0, W100, (301, "d"), (99, "d"), (300, "w")], [0, 100, 300]),
    "a damaged first Cluster": ([(0, "d"), W100, (200, "w")], [0, 100, 200]),
    "a damaged first Cluster later than the next whole one": ([(301, "d"), (300, "w"), (400, "w")], [300, 400]),
    "nothing after the damage": ([W0, W100, (200, "d")], None),
    "nothing after a damaged Cluster earlier than the trusted one": ([W0, W100, (200, "w"), (99, "d")], None),
    "a whole Cluster later than the next whole one": ([W0, W100, J, (9000, "wi"), J, (300, "w"), (400, "w")], [0, 100, 300, 400]),
    "a whole Cluster at the next whole one": ([W0, W100, J, (200, "w"), (200, "w"), (300, "w")], [0, 100, 200, 200, 300]),
    "a late whole Cluster before the damage": ([W0, W100, (9000, "w"), J, (200, "w"), J, (250, "w"), (300, "w")], [0, 100, 9000, 200, 250, 300]),
    "a late whole Cluster that the next one does not trust": ([W0, (9000, "w"), W100, J, (150, "w"), (200, "w")], [0, 9000, 100, 150, 200]),
    "the next whole Cluster repeats the trusted one": ([W0, W100, J, (50, "d"), (100, "w"), (200, "w")], [0, 100, 100, 200]),
    "two whole Clusters at one time before the damage": ([W0, W100, W100, J, (50, "w"), (200, "w")], [0, 100, 100, 200]),
    "a Cluster whose first child is no Timestamp": ([W0, (50, "p"), W100], None),
    "a late whole Cluster in damage at the end": ([W0, W100, J, (9000, "w"), J], None),
    "a late whole Cluster after damaged ones in damage at the end": ([W0, W100, (200, "d"), (250, "d"), (9000, "w"), J], None),
    "a whole Cluster that ends the file after damage": ([W0, W100, J, (9000, "w")], [0, 100, 9000]),
    "a whole Cluster before zeros at the end": ([W0, W100, J, (300, "w"), Z], [0, 100, 300]),
    "a whole Cluster before a cut Tags": ([W0, W100, J, (300, "w"), T], [0, 100, 300]),
    "a whole Cluster before a Tags of unknown size": ([W0, W100, J, (300, "w"), U], [0, 100, 300]),
    "a whole Cluster before a Cluster header cut by the end": ([W0, W100, J, (300, "w"), C], [0, 100, 300]),
    "a late whole Cluster before a whole Tags and damage at the end": ([W0, W100, J, (9000, "w"), G, J], None),
    "a late whole Cluster in damage at the end after a resync": ([W0, W100, J, (200, "w"), (300, "w"), J, (9000, "w"), J],
                                                                 [0, 100, 200, 300]),
    "a late whole Cluster before zeros and damage at the end": ([W0, W100, J, (9000, "w"), Z, J], None),
    "a whole Cluster before a damaged last one": ([W0, W100, J, (300, "w"), (400, "d")], [0, 100, 300, 400]),
    "a damaged Cluster inside a kept one": ([W0, W100, (200, "d"), (250, "dd"), (300, "w")], [0, 100, 200, 250, 300]),
    "a whole Cluster inside a damaged one": ([W0, W100, (200, "d"), (250, "di"), (300, "w")], [0, 100, 200, 260, 300]),
}


@pytest.mark.parametrize("name", TRUST)
def test_the_walk_trusts_a_cluster_no_later_than_the_next_one_that_reads_whole(tmp_path, name):
    """A trusted Cluster reads whole, and its Timestamp is no later than the next Cluster that reads whole. Only it
    moves the lower bound of the search after damage, so a late Timestamp never drops the good Clusters after it. The
    damaged Cluster the walk was in sets the bound instead when its Timestamp lies between the trusted one and the
    next Cluster that reads whole after its end. Else its blocks leave read_on. The walk reads on from the first
    Cluster that reads whole, no earlier than the bound and no later than the next Cluster that reads whole. Each
    damaged Cluster between the damage and that Cluster keeps its blocks before its damage when its Timestamp lies
    between the two, and the search goes on from its damage. A damage past that Cluster keeps none, so no block reads
    twice. With no Cluster that reads whole after it, the walk reads on from it only when the bytes after it read as
    level-1 elements to the end, or as a header cut by the end of the file. So a late whole Cluster inside damage at
    the end of the file gives no frame. tracks keep the blocks before the first damage."""
    items, want = TRUST[name]
    got = matroska.frames(chain(tmp_path / "c.mkv", items))
    first = next((k for k, (_, kind) in enumerate(items) if kind[0] in "dj"), len(items))
    ms = lambda t: [x // NS for x in t["time"]]
    assert ms(got["tracks"][0]) == [x for x, kind in items[:first + 1] if kind != "j"], got
    assert (got["read_on"] and ms(got["read_on"][0])) == want, got["read_on"] and ms(got["read_on"][0])


@pytest.mark.parametrize("shape", ["a zero run between two Clusters", "a cut last Cluster with a Cluster in a frame"])
def test_the_search_after_damage_starts_where_the_damage_is(tmp_path, shape):
    """A zero run between two Clusters is damage at its first zero, so the walk reads on from the Cluster right after
    the run. A Cluster cut at a block boundary by the end of the file is damage at the end, so no Cluster id inside its
    frames is read."""
    inner = el(0x1F43B675, uint(0xE7, 500) + simple(1, 0, [b"z"]))   # a whole Cluster inside the frame data
    last = (200, [simple(1, 0, [b"c" + inner + b"c" * 40]), simple(1, 10, [b"d" * 40])])
    path = mkv(tmp_path / "s.mkv", [entry(1, 2, "A_AC3", 32 * NS)], [(0, [simple(1, 0, [b"a"])]), (50, [simple(1, 0, [b"x"])]),
                                                                    (100, [simple(1, 0, [b"b"])]), last])
    data = bytearray(open(path, "rb").read())
    at = [k for k in range(len(data)) if data[k:k + 4] == b"\x1f\x43\xb6\x75"]
    if shape.startswith("a zero run"):
        data[at[1]:at[2]] = bytes(at[2] - at[1])
    else:
        data = data[:-len(simple(1, 10, [b"d" * 40]))]   # cut at the start of the last block
    (tmp_path / "s.mkv").write_bytes(bytes(data))
    got = matroska.frames(path)
    if shape.startswith("a zero run"):
        assert got["resyncs"] == [(at[1], at[2])] and list(got["read_on"][0]["time"]) == [x * NS for x in (0, 100, 200, 210)], got
    else:
        assert got["error"].startswith("the Cluster at byte") and got["resyncs"] == [] and got["read_on"] is None, got


def test_a_bad_last_element_reads_whole_unless_a_joined_file_follows(tmp_path):
    """A Tags whose size runs past the Segment end comes after every Cluster, so the walk read them all, and whole is
    True. When cat joined a second file after it, a Cluster id follows, and whole is False. The search for the next
    Cluster stops at the Segment end, so the walk never reads on into the second file, whose Clusters come later."""
    path = mkv(tmp_path / "a.mkv", [entry(1, 2, "A_AC3", 32 * NS)], [(0, [simple(1, 0, [b"a"])]), (100, [simple(1, 0, [b"b"])])])
    data = bytearray(open(path, "rb").read())
    one = open(mkv(tmp_path / "b.mkv", [entry(1, 2, "A_AC3", 32 * NS)], [(500, [simple(1, 0, [b"z"])])]), "rb").read()
    data += b"\x12\x54\xc3\x67\x88" + bytes(3)
    size = int.from_bytes(data[13:21], "big") & ((1 << 56) - 1)
    data[13:21] = (1 << 56 | size + 8).to_bytes(8, "big")
    for name, tail in (("tags.mkv", b""), ("cat.mkv", one)):
        (tmp_path / name).write_bytes(bytes(data) + tail)
        got = matroska.frames(str(tmp_path / name))
        assert "unknown size or runs past the Segment end" in got["error"] and got["whole"] is (name == "tags.mkv"), (name, got)
        assert got["resyncs"] == [] and got["read_on"] is None and list(got["tracks"][0]["time"]) == [0, 100 * NS], (name, got)


@pytest.mark.parametrize("lacing", ["xiph", "ebml", "fixed"])
def test_the_walk_reads_the_lace_sizes_on_a_track_with_a_default_duration(tmp_path, lacing):
    """A track with a DefaultDuration times its lace frames from it, and the walk still reads the sizes of each lace.
    A frame of 0 bytes holds no data. Sizes that overrun their block are damage, as on a track with no DefaultDuration,
    so a damaged lace never reads as whole. The walk reads on from the next Cluster."""
    frames = [b"a" * 4, b"b" * 4] if lacing == "fixed" else [b"a" * 300, b"", b"c" * 9]
    path = mkv(tmp_path / "g.mkv", [entry(1, 2, "A_AC3", 32 * NS)], [(0, [simple(1, 0, frames, lacing)])])
    t = matroska.frames(path)["tracks"][0]
    assert list(t["full"]) == ([1, 1] if lacing == "fixed" else [1, 0, 1]) and t["tag"][1] == matroska.DEFAULT, t
    over = {"xiph": block(1, 0, [b"ab", b"cd"], "xiph")[:5] + b"\xfe" + b"abcd",   # a first frame of 254 bytes
            "ebml": block(1, 0, [b"ab", b"cd"], "ebml")[:5] + (0x4000 | 500).to_bytes(2, "big") + b"abcd",   # 500 bytes
            "fixed": block(1, 0, [b"ab", b"cd"], "fixed") + b"e"}[lacing]   # 5 bytes do not split into 2 frames
    path = mkv(tmp_path / "d.mkv", [entry(1, 2, "A_AC3", 32 * NS)],
               [(0, [simple(1, 0, [b"a"])]), (50, [el(0xA3, over)]), (100, [simple(1, 0, [b"z"])])])
    got = matroska.frames(path)
    assert "lace" in got["error"] and got["whole"] is False and list(got["tracks"][0]["time"]) == [0], got
    assert list(got["read_on"][0]["time"]) == [0, 100 * NS] and got["read_on"][0]["resumed"] == 1, got


def test_the_walk_keeps_the_first_bytes_of_each_lace_frame(tmp_path, monkeypatch):
    """After the walk, MADV_SEQUENTIAL has dropped the pages, and a read of a lace faults its page in again. So the
    walk keeps the first bytes of each lace frame of a DTS track with no DefaultDuration, and the frame times read
    with every byte of the file 0 after the walk. An AAC lace needs no bytes."""
    path = mkv(tmp_path / "h.mkv", [entry(1, 2, "A_DTS"), entry(2, 2, "A_AAC", private=AAC_LC_48K)],
               [(0, [simple(1, 0, [dts()]), simple(2, 0, [b"p"])]),
                (100, [simple(1, 0, [dts(), dts(), dts()], "xiph"), simple(2, 0, [b"p", b"q", b"r"], "ebml")])])
    real = matroska.track_frames
    monkeypatch.setattr(matroska, "track_frames", lambda b, *a: real(bytes(len(b)), *a))
    for t, length in zip(matroska.frames(path)["tracks"], (DTS_FRAME, 1024 * 1e9 / 48000)):
        assert tagged(t) == [(0, "stored"), (100 * NS, "stored")] + [(100 * NS + round(k * length), "bitstream") for k in (1, 2)], t


def test_a_late_tracks_reads_the_lace_frames_after_the_walk(tmp_path):
    """When Tracks comes after the Clusters, as mkvpropedit can write it, the walk kept no bytes of its laces. The
    frame lengths then read from the file after the walk."""
    cluster = el(0x1F43B675, uint(0xE7, 0) + simple(1, 0, [dts(), dts(), dts()], "xiph"))
    body = el(0x1549A966, uint(0x2AD7B1, NS)) + cluster + el(0x1654AE6B, entry(1, 2, "A_DTS"))
    (tmp_path / "s.mkv").write_bytes(bytes.fromhex("1a45dfa3" "84" "42860181") + el(0x18538067, body))
    t = matroska.frames(str(tmp_path / "s.mkv"))["tracks"][0]
    assert tagged(t) == [(0, "stored")] + [(round(k * DTS_FRAME), "bitstream") for k in (1, 2)], t


def test_the_walks_take_the_first_info_and_the_first_tracks(tmp_path):
    """A second Info and a second Tracks after the Clusters do not count, as ffmpeg, mpv and mkvmerge read the first.
    So the whole walk and the head walk take the first TimestampScale and the first DefaultDuration."""
    cluster = el(0x1F43B675, uint(0xE7, 0) + simple(1, 0, [dts(), dts(), dts()], "xiph"))
    path = headed(tmp_path / "two.mkv", el(0x1549A966, uint(0x2AD7B1, NS)) + el(0x1654AE6B, entry(1, 2, "A_DTS", 32 * NS)) + cluster
                  + el(0x1549A966, uint(0x2AD7B1, 2 * NS)) + el(0x1654AE6B, entry(1, 2, "A_DTS")))
    matroska.WALKS.clear()
    head, whole = matroska.head(path, lambda *a: True), matroska.frames(path)
    want = [(0, "stored"), (32 * NS, "default"), (64 * NS, "default")]
    assert head["scale"] == whole["scale"] == NS and tagged(head["tracks"][0]) == tagged(whole["tracks"][0]) == want, (head, whole)


def test_the_codec_gives_the_length_of_a_frame():
    """AC-3, E-AC-3, DTS core and MPEG audio carry their samples and sample rate in each frame header, and AAC in its
    AudioSpecificConfig. Any other bytes give none."""
    ac3 = bytes.fromhex("0b77") + b"\0\0" + bytes([0 << 6 | 20, 8 << 3])   # 48 kHz, bsid 8
    eac3 = bytes.fromhex("0b77") + b"\0\0" + bytes([1 << 6 | 1 << 4, 16 << 3])   # 44.1 kHz, 2 blocks
    assert matroska.frame_length("A_AC3", ac3) == (1536, 48000) and matroska.frame_length("A_EAC3", eac3) == (512, 44100)
    e = lambda b4: matroska.frame_length("A_EAC3", eac3[:4] + bytes([b4]) + eac3[5:])
    assert e(0 << 6 | 2 << 4) == (768, 48000) and e(2 << 6 | 3 << 4) == (1536, 32000)   # 3 and 6 blocks
    assert e(3 << 6 | 2 << 4) == (1536, 16000) and e(3 << 6 | 1 << 4) == (1536, 22050)   # fscod 3: fscod2, 6 blocks
    assert matroska.frame_length("A_AC3", ac3[:5] + bytes([9 << 3])) == (1536, 24000)   # bsid 9 halves the rate
    assert matroska.frame_length("A_DTS", dts()) == (512, 48000) and matroska.frame_length("A_DTS", b"\x7f\xfe\x80\x02" + dts()[4:]) is None
    assert matroska.frame_length("A_MPEG/L3", bytes.fromhex("fffb90")) == (1152, 44100)   # MPEG-1 layer 3
    assert matroska.frame_length("A_MPEG/L3", bytes.fromhex("fff390")) == (576, 22050)   # MPEG-2 layer 3
    assert matroska.frame_length("A_MPEG/L2", bytes.fromhex("fffd94")) == (1152, 48000)
    assert matroska.frame_length("A_MPEG/L3", bytes.fromhex("ffe390")) == (576, 11025)   # MPEG-2.5 layer 3: a quarter rate
    assert matroska.frame_length("A_MPEG/L1", bytes.fromhex("ffff90")) == (384, 44100)   # MPEG-1 layer 1
    assert matroska.frame_length("A_VORBIS", dts()) is None and matroska.frame_length("A_AC3", b"\x0b") is None
    assert matroska.aac_length(AAC_LC_48K) == (1024, 48000) and matroska.aac_length(bytes([0x11, 0x94])) == (960, 48000)
    assert matroska.aac_length(b"") is None and matroska.aac_length(bytes([0xf8, 0x00])) is None
    he = bytes.fromhex("2b1188")   # HE-AAC: core rate 24 kHz, SBR at 48 kHz, core AAC LC
    assert matroska.aac_length(he) == (1024, 24000) and matroska.aac_length(bytes.fromhex("2b118a00")) == (960, 24000)
    assert matroska.aac_length(he[:2]) is None and matroska.aac_length(AAC_LC_48K[:1]) is None   # the config ends before a field
    assert matroska.aac_length(bytes.fromhex("2b17805dc00a00")) == (960, 24000)   # an extension rate of 24 bits, then the core
    for other in ("b990", "8994", "2b11dc00"):   # AAC-LD, ER AAC LC, and HE-AAC over AAC-LD: the reader times no ER type
        assert matroska.aac_length(bytes.fromhex(other)) is None, other


def test_keyframe_reads_the_first_n_blocks_only(tmp_path):
    """keyframe() gives the stored time of the first keyframe among the first n blocks of a track. A SimpleBlock with no
    keyframe flag is no keyframe. A keyframe after the first n blocks gives None, so the read of a file stays short."""
    from arr_media_guard import remux
    plain = lambda rel: el(0xA3, bytes([0x81]) + rel.to_bytes(2, "big", signed=True) + b"\0v")   # flags 0: no keyframe
    path = mkv(tmp_path / "k.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS)], [(0, [plain(k * 40) for k in range(5)] + [simple(1, 200, [b"k"])])])
    t = matroska.frames(path)["tracks"][0]
    assert remux.keyframe(path, t, 6) == 200 * NS and remux.keyframe(path, t, 5) is None


def headed(path, segment):
    """The Matroska file path with the EBML header and a Segment of the bytes segment."""
    with open(path, "wb") as f:
        f.write(bytes.fromhex("1a45dfa3" "84" "42860181") + el(0x18538067, segment))
    return str(path)


def seekhead(*pairs):
    """A SeekHead of (element id, position in the Segment data) pairs. Its size does not change with the positions."""
    return el(0x114D9B74, b"".join(el(0x4DBB, el(0x53AB, i.to_bytes(4, "big")) + uint(0x53AC, pos)) for i, pos in pairs))


def test_the_head_walk_stops_after_the_cluster_that_holds_enough(tmp_path, monkeypatch):
    """matroska.head() walks the Clusters up to the first one after which enough() holds, so its tracks hold the first
    frames of the whole walk. It never goes into the cache of frames(), and a walk that frames() keeps serves it. A
    Cluster that ends HEAD_MAX bytes past the Segment start stops it too. Damage stops it, with the frames before the
    damage, as the tracks of the whole walk hold them. Tracks after the Clusters reads where a SeekHead lists it, also
    through a second SeekHead. With no SeekHead entry for it, or one at a Void, head() gives None. A Tags after the
    Clusters whose size runs past the end stops it too, and whole is False."""
    from arr_media_guard import remux
    entries = [entry(1, 1, "V_MPEG4/ISO/AVC"), entry(2, 2, "A_PCM/INT/LIT", dd=10 * NS)]
    kids = lambda: [simple(1, 0, [b"v"]), simple(2, 0, [b"a"]), simple(2, 50, [b"a"])]
    path = mkv(tmp_path / "h.mkv", entries, [(k * 100, kids()) for k in range(4)])
    enough = lambda b, e, blocks: sum(x[1] for x in blocks.get(2, ())) >= 4
    matroska.WALKS.clear()
    h = matroska.head(path, enough)
    data, cid = open(path, "rb").read(), b"\x1f\x43\xb6\x75"
    third = data.index(cid, data.index(cid, data.index(cid) + 1) + 1)
    assert h["error"] == f"the walk of the first Clusters stops at byte {third}" and not h["whole"] and not matroska.WALKS, h
    whole = matroska.frames(path)
    assert list(h["tracks"][1]["time"]) == list(whole["tracks"][1]["time"])[:4] == [0, 50 * NS, 100 * NS, 150 * NS]
    assert matroska.head(path, enough) is whole and len(matroska.WALKS) == 1
    second = data.index(cid, data.index(cid) + 1)
    monkeypatch.setattr(matroska, "HEAD_MAX", second - hook.arr_decide.segment_start(data)[0])
    matroska.WALKS.clear()
    assert len(matroska.head(path, enough)["tracks"][1]["time"]) == 2   # the first Cluster ends at the bound
    monkeypatch.undo()
    bad = bytearray(data)
    bad[second + 29] = 0   # the id of the first block of the second Cluster, after its Timestamp
    open(tmp_path / "d.mkv", "wb").write(bad)
    h, whole = matroska.head(str(tmp_path / "d.mkv"), lambda *a: False), matroska.frames(str(tmp_path / "d.mkv"))
    assert h["error"] == whole["error"] and h["read_on"] is None and whole["read_on"] and not h["whole"], (h, whole)
    assert [list(t["time"]) for t in h["tracks"]] == [list(t["time"]) for t in whole["tracks"]] == [[0], [0, 50 * NS]]
    info, tracks = el(0x1549A966, uint(0x2AD7B1, NS)), el(0x1654AE6B, b"".join(entries))
    clusters = b"".join(el(0x1F43B675, uint(0xE7, k * 100) + b"".join(kids())) for k in range(4))
    one, two = len(seekhead((0, 0))), len(seekhead((0, 0), (0, 0)))   # a SeekHead of one entry and of two
    shapes = {"listed": seekhead((0x1549A966, two), (0x1654AE6B, two + len(info + clusters))) + info + clusters + tracks,
              "second": seekhead((0x114D9B74, one + len(info + clusters + tracks))) + info + clusters + tracks
              + seekhead((0x1549A966, one), (0x1654AE6B, one + len(info + clusters))),
              "none": seekhead((0x1549A966, one)) + info + clusters + tracks,
              "stale": seekhead((0x1654AE6B, one + len(info))) + info + el(0xEC, bytes(len(tracks))) + clusters + tracks,
              "tail": seekhead((0x1549A966, one)) + info + tracks + clusters + el(0x1254C367, b"")[:-8] + (1 << 56 | 99).to_bytes(8, "big")}
    j = {"tracks": [{"id": 0, "type": "video"}, {"id": 1, "type": "audio"}]}
    for name, segment in shapes.items():
        p = headed(tmp_path / f"{name}.mkv", segment)
        h = matroska.head(p, enough if name != "tail" else lambda *a: False)
        if name in ("none", "stale"):   # lace_guess() then takes the whole walk
            assert h is None and remux.lace_guess(p, j) == ([], None) and matroska.frames(p)["tracks"][1]["codec"] == "A_PCM/INT/LIT", name
            continue
        if name == "tail":   # a Tags whose size runs past the end, after the last Cluster
            assert h["error"] == matroska.frames(p)["error"] and matroska.frames(p)["whole"] and not h["whole"], h
            continue
        assert [t["codec"] for t in h["tracks"]] == ["V_MPEG4/ISO/AVC", "A_PCM/INT/LIT"] and h["error"].startswith("the walk of the first"), (name, h)
        assert list(h["tracks"][1]["time"]) == list(matroska.frames(p)["tracks"][1]["time"])[:4], name


def test_the_lace_read_stops_at_the_first_video_keyframe_and_n_audio_frames(tmp_path, monkeypatch):
    """remux.lace_read() holds once each audio track has n frames and the first video track a keyframe or KEY_BLOCKS
    blocks, so head() stops after the Cluster where both hold. A file with no video needs the audio frames only."""
    from arr_media_guard import remux
    plain = lambda rel: el(0xA3, bytes([0x81]) + rel.to_bytes(2, "big", signed=True) + b"\0v")   # flags 0: no keyframe
    entries = [entry(1, 1, "V_MPEG4/ISO/AVC"), entry(2, 2, "A_PCM/INT/LIT", dd=10 * NS)]
    audio = [simple(2, 0, [b"a"]), simple(2, 50, [b"a"])]
    path = mkv(tmp_path / "k.mkv", entries, [(0, [plain(0)] + audio), (100, [simple(1, 0, [b"k"])] + audio), (200, [plain(0)] + audio),
                                             (300, [plain(0)] + audio)])
    alone = mkv(tmp_path / "a.mkv", entries[1:], [(k * 100, audio) for k in range(4)])
    cid = b"\x1f\x43\xb6\x75"
    ends = lambda p: (lambda d: [k for k in range(len(d)) if d.startswith(cid, k)][1:] + [len(d)])(open(p, "rb").read())   # where each Cluster ends
    stop = lambda p, n: (matroska.WALKS.clear(), (matroska.head(p, lambda b, e, blocks: remux.lace_read(b, e, blocks, n)) or {})["error"])[1]
    at = lambda p, k: f"the walk of the first Clusters stops at byte {ends(p)[k]}"
    assert [stop(path, n) for n in (2, 4, 6, 9)] == [at(path, 1), at(path, 1), at(path, 2), None]
    assert stop(alone, 4) == at(alone, 1)
    monkeypatch.setattr(remux, "KEY_BLOCKS", 1)
    assert stop(path, 2) == at(path, 0)


def test_codec_frame_keeps_the_codecs_length_whatever_the_default_duration(tmp_path):
    """A tool can store a DefaultDuration that differs from the length the codec defines. codec_frame keeps the codec's
    length for AAC, AC-3, E-AC-3, DTS and MPEG audio. It comes from the AAC config or the header of the first frame,
    laced or not. Other codecs, video and a zlib-packed track give none."""
    ac3 = bytes.fromhex("0b77") + b"\0\0" + bytes([0 << 6 | 20, 8 << 3])   # 48 kHz, bsid 8
    eac3 = bytes.fromhex("0b77") + b"\0\0" + bytes([1 << 6 | 1 << 4, 16 << 3])   # 44.1 kHz, 2 blocks
    path = mkv(tmp_path / "c.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 2, "A_AAC", 20 * NS, AAC_LC_48K), entry(3, 2, "A_AC3", 30 * NS),
                                    entry(4, 2, "A_EAC3", 30 * NS), entry(5, 2, "A_DTS", 11 * NS), entry(6, 2, "A_MPEG/L3", 25 * NS),
                                    entry(7, 2, "A_OPUS", 20 * NS), entry(8, 2, "A_FLAC", 20 * NS),
                                    el(0xAE, uint(0xD7, 9) + uint(0x83, 2) + el(0x86, b"A_AC3") + el(0x6D80, el(0x6240, el(0x5034, uint(0x4254, 0)))))],
               [(0, [simple(1, 0, [b"v0"]), simple(2, 0, [b"a"]), simple(3, 0, [ac3]), simple(4, 0, [eac3, eac3], "xiph"), simple(5, 0, [dts()]),
                     simple(6, 0, [bytes.fromhex("fffb90") + bytes(13)]), simple(7, 0, [b"\xfc\0"]), simple(8, 0, [b"f"]), simple(9, 0, [ac3])])])
    got = {t["number"]: t["codec_frame"] for t in matroska.frames(path)["tracks"]}
    assert got == {1: None, 2: pytest.approx(1024e9 / 48000), 3: 32 * NS, 4: pytest.approx(512e9 / 44100), 5: pytest.approx(DTS_FRAME),
                   6: pytest.approx(1152e9 / 44100), 7: None, 8: None, 9: None}, got


@pytest.mark.parametrize("lacing", ["xiph", "ebml", "fixed"])
def test_a_lace_gives_the_size_of_each_frame(lacing):
    """Each frame of a lace gets its size, an empty frame too. A size that runs past the end of the block raises
    ValueError, which the walk takes as damage."""
    frames = [b"a" * 600, b"b" * 600, b"c" * 600] if lacing == "fixed" else [b"a" * 600, b"b" * 3, b"", b"c" * 255, b"d" * 70]
    data = block(1, 0, frames, lacing)
    assert matroska.lace_sizes(data, 4, len(data), data[3] & 0x06) == ([len(f) for f in frames], len(data) - sum(map(len, frames)))
    if lacing != "fixed":
        sizes = data[:data.index(b"a" * 600)]   # the block header, the frame count and the sizes, with no frame
        for cut in range(5, len(sizes)):
            with pytest.raises(ValueError):
                matroska.lace_sizes(sizes[:cut], 4, cut, data[3] & 0x06)


def test_a_variable_size_integer_ends_by_its_end():
    assert matroska.vint(b"\x81\x40\x05", 0, 3) == (1, 1) and matroska.vint(b"\x81\x40\x05", 1, 3) == (2, 5)
    for p, end in ((1, 2), (3, 3), (0, 0)):
        with pytest.raises(ValueError):
            matroska.vint(b"\x81\x40\x05", p, end)


@pytest.mark.parametrize("cut", ["in a Cluster at a block boundary", "after a whole Cluster", "a zero run between two Clusters"])
def test_the_walk_names_a_cut_file_and_a_zero_run_between_clusters(tmp_path, cut):
    """A file cut at a block boundary holds a Cluster that runs past its end. A file cut after a whole Cluster holds a
    Segment that does. A zero run between two Clusters stands where blocks were. The walk keeps the blocks before each
    and names it in error, so the proof reads no part of a file."""
    path = mkv(tmp_path / "e.mkv", [entry(1, 2, "A_AC3", 32 * NS)],
               [(0, [simple(1, 0, [b"a"]), simple(1, 40, [b"b"])]), (100, [simple(1, 0, [b"c"]), simple(1, 40, [b"d"])]), (200, [simple(1, 0, [b"e"])])])
    with open(path, "rb") as f:
        data = f.read()
    t = matroska.frames(path)["tracks"][0]
    second, third = (data.rindex(b"\xe7", 0, t["place"][k]) - 12 for k in (2, 4))   # the Clusters of blocks c and e
    data, want, error = {"in a Cluster at a block boundary": (data[:t["place"][3]], [0, 40, 100], "the Cluster at byte"),
                         "after a whole Cluster": (data[:third], [0, 40, 100, 140], "the Segment runs"),
                         "a zero run between two Clusters": (data[:second] + bytes(third - second) + data[third:], [0, 40],
                                                             f"a run of zero bytes at byte {second}")}[cut]
    path = tmp_path / "cut.mkv"   # a new name: the walk of e.mkv stays cached at its size and mtime
    path.write_bytes(data)
    got = matroska.frames(str(path))
    assert got["error"].startswith(error) and list(got["tracks"][0]["time"]) == [x * NS for x in want], got
    with pytest.raises(RuntimeError, match="^the block walk of cut.mkv failed: "):
        hook.stored_places(str(path), [{"index": 0, "codec_type": "audio"}])


def test_two_matroska_files_may_differ_by_their_rounding():
    """Only the new file rounds a subtitle's stored time. Each block of the original rounds its time to a tick, and a
    copy that laces in other places rounds again. A time counts from a first time, which a coarser new file rounds
    too, so each bound gets half the difference of the scales then. Each bound gets 1 us for the comparison in whole
    microseconds."""
    assert hook.mkv_slack(NS, NS, "audio") == pytest.approx(0.001501) and hook.mkv_slack(NS, NS, "subtitle") == pytest.approx(0.000501)
    assert hook.mkv_slack(100000, NS, "video") == pytest.approx(0.001051) and hook.mkv_slack(100000, NS, "subtitle") == pytest.approx(0.000951)
    assert hook.mkv_slack(NS, 100000, "video") == pytest.approx(0.001051)   # a finer new file rounds no first time more


def test_paired_times_drop_an_unknown_time_and_keep_the_file_order():
    """Two Matroska files hold the same frames in the same order. A pair with a time that is not known drops out and
    counts. Two frames that trade their times move, though the sorted times match."""
    slack = hook.mkv_slack(NS, NS, "audio")
    x, y = [0.0, None, 0.042, 0.064], [0.0, 0.021, 0.042, 0.064]
    assert hook.times_fault("audio 1", "aac", x, y, slack=slack, paired=True) == (None, {"worst_ms": 0.0, "unknown": 1})
    why, check = hook.times_fault("audio 1", "aac", [0.0, 0.021, 0.042], [0.0, 0.042, 0.021], slack=slack, paired=True)
    assert why.startswith("the MKV would play the audio 21 ms out of sync") and why.endswith(". 2 of 3 frames moved"), why
    assert hook.times_fault("audio 1", "aac", [0.0, 0.021, 0.042], [0.0, 0.042, 0.021])[0] is None   # sorted, as ffmpeg's reads pair
    assert hook.times_fault("audio 1", "aac", [0.0, 0.0016], [0.0, 0.0], slack=slack, paired=True)[0]   # 1.6 ms is past the rounding
    assert hook.times_fault("audio 1", "aac", [0.0, 0.0015], [0.0, 0.0], slack=slack, paired=True)[0] is None


@pytest.mark.parametrize("length, fault", [(0.0107, None), (None, "the MKV would play the audio 11 ms out of sync"), (0.005, "the MKV")])
def test_a_shared_time_may_move_the_frame_the_file_defines(length, fault):
    """Two packets of the original share a time, and the new file spaces them one frame apart. Between two Matroska
    files the frame is the one the original defines, and a file that defines none allows no move. A median step of
    the original never counts."""
    x = [0.0, 0.011, 0.021, 0.021, 0.043, 0.053]
    y = [0.0, 0.011, 0.021, 0.032, 0.043, 0.053]
    why, check = hook.times_fault("audio 1", "dts", x, y, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=length,
                                  overlaps=[1, 0.0107, 0.021])
    assert (why or "").startswith(fault or "") and bool(why) == bool(fault), why
    if fault:
        assert why.endswith("1 of 6 frames moved. The original stores 1 block that starts before the block before it ends, up to 11 ms, "
                            "the first at 0.021 s"), why


def test_stored_places_pair_the_tracks_with_ffprobes_streams(tmp_path, monkeypatch):
    """ffmpeg makes a stream of each video, audio and subtitle TrackEntry with a codec, in file order. A cover picture is
    no TrackEntry. A frame with no data is no packet, and an unknown time reads NaN. Another set of tracks raises."""
    path = mkv(tmp_path / "e.mkv", [entry(1, 1, "V_MPEG4/ISO/AVC", 40 * NS), entry(2, 2, "A_VORBIS"), entry(3, 17, "S_TEXT/UTF8")],
               [(0, [simple(1, 0, [b"v"]), simple(2, 0, [b"ab", b"cd"], "xiph"), simple(3, 5, [b""]), simple(3, 9, [b"hi"])])])
    streams = [{"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "subtitle"},
               {"index": 3, "codec_type": "video", "disposition": {"attached_pic": 1}}]
    got = hook.stored_places(path, streams)
    assert list(got[0][0]) == [0.0] and got[1][0][0] == 0.0 and got[1][0][1] != got[1][0][1] and list(got[2][0]) == [0.009], got
    assert got[1][3] == {"scale": NS, "frame": None, "overlaps": None, "lapped": [], "unknown": 1, "unended": False} and got[2][3]["unended"] is True
    sub = [{"index": 0, "codec_type": "subtitle"}]
    for k, (kids, unended) in enumerate((([simple(1, 1, [b"a"]), group(1, 5, b"b", 3)], False), ([group(1, 1, b"a", 3), simple(1, 5, [b"b"])], True))):
        one = mkv(tmp_path / f"s{k}.mkv", [entry(1, 17, "S_TEXT/UTF8")], [(0, kids)])   # only the last block in time order counts
        assert hook.stored_places(one, sub)[0][3]["unended"] is unended
    with pytest.raises(RuntimeError, match="finds the tracks"):
        hook.stored_places(path, streams[1:])
    with pytest.raises(RuntimeError, match="^the block walk of gone.mkv failed"):
        hook.stored_places(str(tmp_path / "gone.mkv"), streams)


L = 0.010667   # one DTS frame
LACE = [0.0, L, 2 * L]   # a lace of 3 frames of the original


@pytest.mark.parametrize("case, new, lapped, passes", [
    ("an overlap closed later", LACE + [3 * L, 4 * L], [0.030], True),
    ("an overlap moved earlier", LACE + [0.025, 0.025 + L], [0.030], False),
    ("an overlap moved later with a gap", LACE + [0.040, 0.040 + L], [0.030], False),
    ("a later move with no overlap before it", LACE + [3 * L, 4 * L], [], False),
])
def test_an_overlap_may_close_by_a_contiguous_later_move(case, new, lapped, passes):
    """The original's next block starts at 30 ms, 2 ms before its lace ends. mkvmerge plays the audio on: each frame
    one frame after the one before it, later than the time the original defines. That passes, and the check counts the
    frames, the worst move and the first time. An earlier move, or a later move that leaves a gap, refuses. The same
    move refuses when the walk found no overlapping block there, as a slow drift moves the audio."""
    old = LACE + [0.030, 0.030 + L]
    why, check = hook.times_fault("audio 1", "dts", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L, lapped=lapped)
    assert (why is None) is passes, (why, check)
    if passes:
        assert check["contiguous"] == {"frames": 2, "worst_ms": 2.0, "first": 0.03}, check


@pytest.mark.parametrize("past", [-0.01, 0.01])
def test_a_contiguous_move_may_reach_overlap_move(past):
    """A lace of 30 frames, and the original's next block starts a whole overlap before it ends. mkvmerge plays the
    audio on. A player measurement covered such moves up to 150 ms, so a move up to OVERLAP_MOVE passes. A longer one
    refuses, and the refusal names the count, the worst move, the first time and the original's overlapping blocks."""
    lap = hook.OVERLAP_MOVE + past   # the overlap, and so the move of the two frames of the next block
    old, new = [k * L for k in range(30)] + [30 * L - lap, 31 * L - lap], [k * L for k in range(32)]
    why, check = hook.times_fault("audio 1", "dts", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L,
                                  overlaps=[1, lap, old[30]], lapped=[old[30]])
    if past < 0:
        assert why is None and check["contiguous"]["frames"] == 2, (why, check)
        return
    ms = round(lap * 1000)
    assert why == (f"the MKV would play the audio {ms} ms out of sync with the video, at the start. Detail: audio 1 (dts) moved {ms} ms, "
                   f"first at {old[30]:.3f} s. 2 of 32 frames moved. The original stores 1 block that starts before the block before it "
                   f"ends, up to {ms} ms, the first at {old[30]:.3f} s") and "contiguous" not in check, (why, check)


def test_a_frame_that_kept_its_time_ends_the_overlap_before_it():
    """The block at 30 ms overlaps, and mkvmerge plays on to it. The block at 60 ms then keeps its time within the
    slack. A later move from there needs an overlapping block of its own, so the frame after it refuses."""
    old = LACE + [0.030, 0.030 + L, 0.060, 0.060 + L]
    new = LACE + [3 * L, 4 * L, 0.0614, 0.0614 + L + 0.001]
    why, check = hook.times_fault("audio 1", "dts", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L, lapped=[0.030])
    assert why.endswith("1 of 7 frames moved") and check["contiguous"]["frames"] == 2, (why, check)


def test_a_refusal_keeps_the_count_of_the_frames_that_played_on():
    """Two frames close the overlap, and a later frame moves 10 ms with no chain. The refusal names that frame, and the
    check still counts the two that played on."""
    old, new = LACE + [0.030, 0.030 + L, 0.060], LACE + [3 * L, 4 * L, 0.070]
    why, check = hook.times_fault("audio 1", "dts", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L, lapped=[0.030])
    assert why.endswith("1 of 6 frames moved") and check["contiguous"]["frames"] == 2, (why, check)


def test_a_gap_stays_a_gap():
    """The original's next block starts 10 ms after its lace ends. A new file that closes the gap moves those frames
    earlier, and that refuses."""
    old = LACE + [0.042, 0.042 + L]
    why, _ = hook.times_fault("audio 1", "dts", old, LACE + [3 * L, 4 * L], (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L)
    assert why.startswith("the MKV would play the audio 10 ms out of sync"), why
    assert hook.times_fault("audio 1", "dts", old, old, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=L)[0] is None


@pytest.mark.parametrize("case", ["video", "no frame length", "not paired"])
def test_only_paired_audio_with_a_frame_length_may_close_an_overlap(case):
    """The rule needs the frame length the original defines, and it holds for audio of two Matroska files only."""
    old, new = LACE + [0.030, 0.030 + L], LACE + [3 * L, 4 * L]
    kw = dict(video=None if case == "video" else (0.0, 0.0), slack=hook.mkv_slack(NS, NS, "audio"), paired=case != "not paired",
              length=None if case == "no frame length" else L, lapped=[0.030])
    assert hook.times_fault("audio 1", "dts", old, new, **kw)[0], case


def test_the_first_frame_has_no_frame_before_it():
    """Two packets of the original share a time, so every move counts from the video's start. The first frame moved
    10 ms, and the last frame in the file sits one frame before it. That last frame is no frame before the first one,
    so the first frame does not count as contiguous."""
    why, check = hook.times_fault("audio 1", "dts", [0.0, 0.0, 0.003], [0.010, 0.021, 0.005], (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"),
                                  paired=True, length=0.005, lapped=[0.0, 0.003])
    assert why and "contiguous" not in check, (why, check)


def planned(tmp_path, blocks, dd=20 * NS, codec="A_AAC"):
    """(planned_times() in ms of the audio track of a made-up file, the walk's track). blocks holds the time in ms of
    each one-frame block, or (time, frames) for a lace."""
    kids = [simple(1, x, [b"a"]) if isinstance(x, int) else simple(1, x[0], [b"a"] * x[1], "fixed") for x in blocks]
    r = matroska.frames(mkv(tmp_path / "p.mkv", [entry(1, 2, codec, dd)], [(0, kids)]))
    got = hook.planned_times(r["tracks"][0], r["scale"])
    return (None if got is None else [v / NS for v in got]), r["tracks"][0]


def proved_plan(t, plan):
    """times_fault() of the frames of t against plan, both in ms, as prove() pairs a new file that stores the plan."""
    return hook.times_fault("audio 1", "aac", [x / 1e9 for x in t["time"]], [x / 1000 for x in plan], (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"),
                            paired=True, length=t["frame"] / 1e9, lapped=[x / 1e9 for x in t["lapped"]])


@pytest.mark.parametrize("blocks, want, moved", [
    ([0, 20, 40, 50, 70, 90, 120, 140], [0, 20, 40, 60, 80, 100, 120, 140], 3),   # 10 ms overlap, a 10 ms gap brings the time back
    ([0, 20, 40, 50, 70, 110], [0, 20, 40, 60, 80, 110], 2),                       # a larger gap stays, less the move
    ([0, 20, (30, 3), 100], [0, 20, 40, 60, 80, 100], 3),                         # the frames of a lace play on too
    ([0, 20, 37, 57], [0, 20, 40, 60], 2),                                         # 3 ms, past the rounding
    ([0, 20, 38, 58], [0, 20, 38, 58], 0),                                         # 2 ms, within the rounding
    ([0, 20, 40, 40, 60], [0, 20, 40, 60, 80], 2),                                 # a shared time plays on too
    ([0, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 15, 35], [0, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 15, 35], 0),
])
def test_a_plan_keeps_each_time_and_plays_on_past_an_overlap_up_to_close_move(tmp_path, blocks, want, moved):
    """Each frame keeps its stored or default time. Past a block that starts before the block before it ends, the
    frames play on one frame after the one before, until the original's own time comes back. A frame that shares its
    time with the one before it plays on too. A move within the rounding of the two files stays. The proof counts it
    as a frame that kept its time, so the frames after it would follow no overlap. A move past CLOSE_MOVE (245 ms
    here) stays too. The proof passes every plan, and counts the moved frames as contiguous."""
    got, t = planned(tmp_path, blocks)
    assert got == want, got
    why, check = proved_plan(t, got)
    assert why is None and (check.get("contiguous") or {}).get("frames", 0) == moved, (why, check)


def test_a_plan_steps_back_in_time_only_where_it_keeps_an_overlap_past_close_move(tmp_path):
    """Made-up tracks with overlaps of 1 to 260 ms, gaps, blocks that share a time and laces. A plan never puts a frame
    before the frame before it. The one exception is an overlap that a move of CLOSE_MOVE does not close. That frame
    keeps its stored time, as the original holds it."""
    seen = {"shared": 0, "kept": 0}
    for seed in range(60):
        rnd = random.Random(seed)
        dd = rnd.choice([10666667, 21333333, 32000000])
        t, blocks = 0.0, []
        for _ in range(rnd.randint(40, 160)):
            ev = rnd.random()
            if ev < 0.06:
                t -= rnd.choice([rnd.uniform(1, 20), rnd.uniform(20, 120), rnd.uniform(190, 260)]) * NS   # an overlap
            elif ev < 0.1:
                t += rnd.uniform(1, 60) * NS   # a gap
            elif ev < 0.16 and blocks:
                t = (blocks[-1] if isinstance(blocks[-1], int) else blocks[-1][0]) * NS   # the time of the block before
            tk, m = max(0, round(t / NS)), rnd.choice([1, 1, 2, 4])
            blocks.append(tk if m == 1 else (tk, m))
            t = tk * NS + m * dd
        (tmp_path / str(seed)).mkdir()   # the walk keeps its last walks by path, size and mtime
        got, tr = planned(tmp_path / str(seed), blocks, dd=dd)
        x = [v / NS for v in tr["time"]]
        seen["shared"] += sum(a == b for a, b in zip(x, x[1:]))
        for k in range(1, len(got)):
            if got[k] < got[k - 1]:
                assert got[k] == x[k] and got[k - 1] + dd / NS - x[k] > hook.CLOSE_MOVE * 1000 - 0.5, (seed, k, got[k - 1], got[k], x[k])
                seen["kept"] += 1
    assert seen["shared"] and seen["kept"], seen


@pytest.mark.parametrize("case, new, passes", [
    ("plays on", [0.0, 0.020, 0.040, 0.060, 0.080], True),
    ("half a frame", [0.0, 0.020, 0.040, 0.050, 0.070], False),
    ("leaves a gap", [0.0, 0.020, 0.040, 0.065, 0.085], False),
])
def test_a_frame_that_shares_its_time_goes_on_with_the_chain(case, new, passes):
    """The original's block at 40 ms starts where the block before it starts. A new file that plays on from there
    moves it one frame and the frame after it too. That passes as a chain, and the check counts both frames. A move of
    half a frame, or one that leaves a gap, refuses."""
    old = [0.0, 0.020, 0.040, 0.040, 0.060]
    why, check = hook.times_fault("audio 1", "aac", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=0.020, lapped=[0.040])
    assert (why is None) is passes, (why, check)
    if passes:
        assert check["contiguous"] == {"frames": 2, "worst_ms": 20.0, "first": 0.04}, check


def test_a_shared_frame_off_the_chain_counts_as_a_frame_that_kept_its_time():
    """The shared frame at 40 ms moves 15 ms, within one frame, and off the chain. It counts as a frame that kept its
    time, as before. The frames after it move 15 ms in whole frames from it, and no block of the original after it
    overlaps. So they refuse."""
    old, new = [0.0, 0.020, 0.040, 0.040, 0.060, 0.080], [0.0, 0.020, 0.040, 0.055, 0.075, 0.095]
    why, check = hook.times_fault("audio 1", "aac", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=0.020, lapped=[0.040])
    assert why and why.endswith("2 of 6 frames moved") and "contiguous" not in check, (why, check)


def test_a_shared_frame_that_kept_its_time_ends_the_chain():
    """The block at 30 ms overlaps, and two frames play on. The frame at 79 ms then moves 1 ms, within the slack, so
    it kept its time, though it shares that time with the frame after it. A later move from there needs an overlapping
    block of its own, so the last frame refuses."""
    old = [0.0, 0.020, 0.030, 0.050, 0.079, 0.079, 0.099]
    new = [0.0, 0.020, 0.040, 0.060, 0.080, 0.100, 0.120]
    why, check = hook.times_fault("audio 1", "aac", old, new, (0.0, 0.0), hook.mkv_slack(NS, NS, "audio"), paired=True, length=0.020, lapped=[0.030])
    assert why and why.endswith("1 of 7 frames moved") and check["contiguous"]["frames"] == 2, (why, check)


@pytest.mark.parametrize("close", [0.0, float("inf")])
def test_close_move_picks_each_policy(tmp_path, monkeypatch, close):
    """CLOSE_MOVE at 0 keeps every time. At infinity every overlap closes, as mkvmerge closes it on its own, and the
    proof refuses a move past OVERLAP_MOVE. The block at 35 ms starts 245 ms before the block before it ends."""
    monkeypatch.setattr(hook, "CLOSE_MOVE", close)
    for blocks in ([0, 20, 40, 50, 70], [0, 20, 240, 260, 35, 55]):
        got, t = planned(tmp_path, blocks)
        if close == 0:
            assert got == blocks and proved_plan(t, got)[0] is None, got
        elif blocks[-1] == 70:
            assert got == [0, 20, 40, 60, 80] and proved_plan(t, got)[0] is None, got
        else:
            assert got == [0, 20, 240, 260, 280, 300], got
            assert proved_plan(t, got)[0].startswith("the MKV would play the audio 245 ms out of sync"), proved_plan(t, got)


@pytest.mark.parametrize("case", ["pcm", "unknown", "empty", "before 0"])
def test_no_plan_where_a_frame_cannot_be_timed(tmp_path, case):
    """mkvmerge cuts PCM into other frames. A Vorbis lace with no DefaultDuration has frames with no known time. A frame
    with no data is no frame in the proof. A time before 0 does not go into a timestamps file. None of them get a plan."""
    if case == "pcm":
        assert planned(tmp_path, [0, 20, 40], codec="A_PCM/INT/LIT")[0] is None
    elif case == "unknown":
        assert planned(tmp_path, [0, (20, 3)], dd=None, codec="A_VORBIS")[0] is None
    elif case == "empty":
        r = matroska.frames(mkv(tmp_path / "e.mkv", [entry(1, 2, "A_AAC", 20 * NS)], [(0, [simple(1, 0, [b"a"]), simple(1, 20, [b""])])]))
        assert hook.planned_times(r["tracks"][0], r["scale"]) is None
    else:
        r = matroska.frames(mkv(tmp_path / "n.mkv", [entry(1, 2, "A_AAC", 20 * NS)], [(0, [simple(1, -20, [b"a"]), simple(1, 0, [b"a"])])]))
        assert hook.planned_times(r["tracks"][0], r["scale"]) is None


def test_a_block_the_walk_finds_no_overlap_at_keeps_its_time(tmp_path):
    """The block at 40 ms lasts 10 ms by its BlockDuration, so the block at 50 ms starts as it ends. The frame length
    would put it at 60 ms, but the proof finds no overlap there and would refuse that move."""
    kids = [simple(1, 0, [b"a"]), simple(1, 20, [b"a"]), group(1, 40, b"a", 10), simple(1, 50, [b"a"]), simple(1, 70, [b"a"])]
    r = matroska.frames(mkv(tmp_path / "g.mkv", [entry(1, 2, "A_AAC", 20 * NS)], [(0, kids)]))
    assert [v / NS for v in hook.planned_times(r["tracks"][0], r["scale"])] == [0, 20, 40, 50, 70]


def test_a_track_with_no_frame_length_keeps_each_time(tmp_path):
    """Vorbis defines no frame length in a frame header, so its frames cannot play on, and each keeps its time. Each
    block lasts 20 ms by its BlockDuration, so the walk finds the overlap at 30 ms."""
    kids = [group(1, x, b"a", 20) for x in (0, 20, 30, 50)]
    r = matroska.frames(mkv(tmp_path / "v.mkv", [entry(1, 2, "A_VORBIS")], [(0, kids)]))
    t = r["tracks"][0]
    assert t["frame"] is None and list(t["lapped"]) == [30 * NS], t
    assert [v / NS for v in hook.planned_times(t, r["scale"])] == [0, 20, 30, 50]


def test_the_new_files_rounding_keeps_a_planned_move_within_close_move(tmp_path):
    """The original stores 100 us ticks, and the new file rounds each planned time half up to 1 ms. A frame planned at
    320.6 ms, 199.7 ms after its own time, would store 321 ms, a move of 200.1 ms, which the proof refuses. So a move
    plays on only up to CLOSE_MOVE less half a tick, here 199.5 ms, and a larger one keeps every time."""
    def plan(late):   # 15 blocks 20 ms apart, one at 300.6 ms, then two that start late ticks before 320.6 ms
        ticks = [200 * k for k in range(15)] + [3006, 3206 - late, 3406 - late]
        r = matroska.frames(mkv(tmp_path / f"c{late}.mkv", [entry(1, 2, "A_AAC", 20 * NS)], [(0, [simple(1, x, [b"a"]) for x in ticks])], scale=100000))
        return [v / 1e5 for v in hook.planned_times(r["tracks"][0], r["scale"])], r["tracks"][0]
    got, t = plan(1997)
    assert got[-3:] == [3006, 1209, 1409], got
    rounded = [(v * 1e5 + NS // 2) // NS * NS / 1e9 for v in got[:-2] + [3206, 3406]]   # the plan of a move up to CLOSE_MOVE
    why = hook.times_fault("audio 1", "aac", [x / 1e9 for x in t["time"]], rounded, (0.0, 0.0), hook.mkv_slack(100000, NS, "audio"), paired=True,
                           length=0.02, lapped=[x / 1e9 for x in t["lapped"]])[0]
    assert why.startswith("the MKV would play the audio 200 ms out of sync"), why
    assert plan(1995)[0][-3:] == [3006, 3206, 3406]   # 199.5 ms plays on


def args_of(path, lag=None):
    """The plan of planned_args() for the one audio track of the made-up file path, as mkvmerge -J would list it."""
    return hook.planned_args(str(path), {"tracks": [{"id": 0, "properties": {"number": 1}}]}, [0], str(path.parent), lag)[1][1][1]


def clustered(ticks):
    """Clusters of a one-frame block of track 1 at each time of ticks, in that order, a new Cluster each 30000 ticks."""
    out = []
    for x in ticks:
        if not out or out[-1][0] != x // 30000 * 30000:
            out.append((x // 30000 * 30000, []))
        out[-1][1].append(simple(1, x - out[-1][0], [b"a"]))
    return out


@pytest.mark.parametrize("case", ["earliest time moves", "never returns"])
def test_a_plan_the_proof_would_refuse_keeps_each_time(tmp_path, case):
    """planned_args() checks each plan as prove() would check the new file, rounded to 1 ms. When the proof would
    refuse it, every frame keeps its time. The frame at 0 ms is the earliest, and the plan moves it to 80 ms, so the
    first time of the new file moves 40 ms. An AAC run that never returns starts at 21 ms, after a first block stored at
    60 ms. The plan plays it on for 5999 frames, 60 ms late to the end, so the earliest frame moves too."""
    scale, dd = NS, 20 * NS if case == "earliest time moves" else 21333333
    ticks = {"earliest time moves": [40, 60, 0, 20], "never returns": [60] + [round(k * 64 / 3) for k in range(1, 6000)]}[case]
    path = tmp_path / "r.mkv"
    r = matroska.frames(mkv(path, [entry(1, 2, "A_AAC", dd)], clustered(ticks), scale=scale))
    t = r["tracks"][0]
    plan = hook.planned_times(t, r["scale"])
    rounded = [(v + NS // 2) // NS * NS / 1e9 for v in plan]
    assert hook.times_fault("audio 1", "aac", [x / 1e9 for x in t["time"]], rounded, (0.0, 0.0), hook.mkv_slack(scale, NS, "audio"), paired=True,
                            length=dd / 1e9, lapped=[x / 1e9 for x in t["lapped"]])[0], "the proof refuses the plan"
    assert args_of(path) == list(t["time"]) != plan


@pytest.mark.parametrize("at, want", [(424, 85066667), (425, 85166667)])
def test_a_plan_the_proof_passes_keeps_its_moves(tmp_path, at, want):
    """An original of 100 us ticks has a frame at 42.4 or 42.5 ms that the new file stores at 42 or 43 ms. The next
    frame plays on to 85.067 or 85.167 ms, stored at 85 ms, 0.333 or 0.667 ms off the frame length. The slack of 1.051
    ms takes both, as the new file also rounds the first time, see proof.mkv_slack(). The proof passes each plan, so
    planned_args() keeps it."""
    path = tmp_path / "p.mkv"
    r = matroska.frames(mkv(path, [entry(1, 2, "A_AAC", 42666667)], clustered([0, at, 802]), scale=100000))
    assert args_of(path) == hook.planned_times(r["tracks"][0], r["scale"]) == [0, at * 100000, want]


def test_a_first_time_off_the_grid_of_the_new_file_keeps_every_time():
    """An original of 100 us ticks starts at 63.4 ms, and each frame of 42.667 ms sits on that grid. The new file rounds
    each time to 1 ms, the first one too. A time counts from the first time of its file, so a frame moves up to 0.5 ms
    plus the 0.4 ms of the first time, 0.7 ms here. The slack takes half the difference of the two scales more, 1.051
    ms, so a copy that keeps every time passes (fuzz seed 1569)."""
    x = [(634 + round(k * 426.66667)) * 100000 for k in range(200)]
    new = [(v + NS // 2) // NS * NS for v in x]
    why, check = hook.times_fault("audio 1", "aac", [v / 1e9 for v in x], [v / 1e9 for v in new], (0.0, 0.0), hook.mkv_slack(100000, NS, "audio"),
                                  paired=True, length=0.042666667)
    assert why is None and check["worst_ms"] == 0.7, (why, check)


@pytest.mark.parametrize("video, n, kept", [(True, 120, False), (False, 120, False), (False, 40, False)])
def test_planned_args_checks_a_plan_at_the_tick_of_the_new_file(tmp_path, video, n, kept):
    """An original of 20833 ns ticks plays on its AAC frames past a 50 ms overlap at frame 10. Where a video track
    stays, the new file rounds to 1 ms, and the slack is 0.522 ms. A new file of 48 kHz audio alone rounds to 20832 ns,
    see mux_tick(), and the slack is 0.032 ms. The proof counts the frame length of 21.333 ms as a float, so it passes
    the runs of 110 and 30 frames at both ticks. Rounded to 1 ms, the stored times of 20833 ns would move up to 0.5 ms,
    past the slack of the finer tick. The options give mkvmerge the finer tick."""
    path = tmp_path / "f.mkv"
    r = matroska.frames(mkv(path, [entry(1, 2, "A_AAC", 21333333)], clustered([round(k * 21333333 / 20833) - (2400 if k >= 10 else 0) for k in range(n)]),
                            scale=20833))
    t = r["tracks"][0]
    j = {"tracks": [{"id": 0, "type": "audio", "properties": {"number": 1, "audio_sampling_frequency": 48000}},
                    *([{"id": 1, "type": "video", "properties": {"number": 2}}] if video else [])]}
    args, plans = hook.planned_args(str(path), j, [0, 1] if video else [0], str(tmp_path))
    plan = hook.planned_times(t, r["scale"])
    assert plan != list(t["time"]) and plans[1][1] == (list(t["time"]) if kept else plan)
    assert args[:-2] == ([] if video else ["--timestamp-scale", "20832"]) and args[-2] == "--timestamps"


@pytest.mark.parametrize("codec, frame, plan", [("A_MPEG/L3", bytes.fromhex("fffb90") + bytes(400), True), ("A_MPEG/L2", bytes.fromhex("fffd94") + bytes(400), True),
                                                ("A_DTS", dts(), False), ("A_OPUS", bytes([0xFC]), False)], ids=["mp3", "mp2", "dts", "opus"])
def test_a_lace_with_no_default_duration_gets_a_plan_only_for_mpeg_audio(tmp_path, codec, frame, plan):
    """mkvmerge 102 times the frames of an MP2 or MP3 lace with no DefaultDuration wrong, by up to 144 ms. It keeps the
    lace times of AC-3, E-AC-3, DTS and Opus. So only an MPEG audio lace gets a plan of its bitstream times."""
    path = mkv(tmp_path / "l.mkv", [entry(1, 2, codec)], [(0, [simple(1, 0, [frame] * 3, "xiph"), simple(1, 200, [frame] * 3, "xiph")])])
    assert matroska.BITSTREAM in matroska.frames(path)["tracks"][0]["tag"]
    got = hook.planned_args(path, {"tracks": [{"id": 0, "properties": {"number": 1}}]}, [0], str(tmp_path))[1]
    assert (1 in got) == plan, got


@pytest.mark.parametrize("tracks, ids, tick", [
    ([("video", None), ("audio", 48000)], [0, 1], None), ([("video", None), ("audio", 48000)], [1], 20832),
    ([("audio", 44100), ("audio", 48000), ("subtitles", None)], [0, 1, 2], 20832), ([("audio", 44100), ("audio", 96000)], [0], 22674),
    ([("audio", 8000)], [0], 124999), ([("audio", 192000)], [0], 5207), ([("audio", None), ("subtitles", None)], [0, 1], NS),
    ([("audio", 2000000)], [0], 1000), ([("audio", 50)], [0], 10000000)])
def test_mux_tick_takes_the_timestamp_scale_of_mkvmerge(tracks, ids, tick):
    """mkvmerge 102 gives a new file with a video track 1 ms (None here). A file with audio and no video gets one sample
    of its highest audio rate, less 1 ns, cut to whole ns (measured at 8, 44.1, 48, 96 and 192 kHz). A track that the
    remux leaves out counts for nothing. A file with no audio rate gets 1 ms. mkvmerge takes 1000 to 10000000 ns."""
    j = {"tracks": [{"id": i, "type": kind, "properties": {"number": i + 1, **({"audio_sampling_frequency": rate} if rate else {})}}
                    for i, (kind, rate) in enumerate(tracks)]}
    assert hook.mux_tick(j, ids) == tick


def test_a_header_repair_refuses_a_plan_that_keeps_an_overlap_over_lag_slack(tmp_path):
    """The block at 200 ms starts 1020 ms before the block before it ends, past CLOSE_MOVE, so every frame keeps its
    time. It then sits 1000 ms behind the frame before it, which LAG_SLACK allows. One at 199 ms sits 1001 ms behind,
    and the plan refuses with the original's overlap. A Vorbis block with no BlockDuration has no known end, so the walk
    finds no overlap there. Its drop of 1100 ms raises nothing, and header_checks() decides after mkvmerge."""
    path = tmp_path / "v.mkv"
    mkv(path, [entry(1, 2, "A_VORBIS")], [(0, [simple(1, x, [b"a"]) for x in [*range(0, 1201, 20), 100]])])
    assert args_of(path, hook.LAG_SLACK)[-1] == 100 * NS
    for at in (200, 199):
        path = tmp_path / f"l{at}.mkv"
        mkv(path, [entry(1, 2, "A_AAC", 20 * NS)], [(0, [simple(1, x, [b"a"]) for x in [*range(0, 1201, 20), at, at + 20]])])
        if at == 200:
            assert args_of(path, hook.LAG_SLACK)[-2:] == [200 * NS, 220 * NS]
            continue
        with pytest.raises(RuntimeError) as ex:
            args_of(path, hook.LAG_SLACK)
        assert str(ex.value) == ("audio track 0 of the original stores 1 block that starts before the block before it ends, up to 1021 ms, the first "
                                 "at 0.199 s. The new file would keep that overlap, and the audio would sit up to 1.0 s behind the other streams")
