# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The frame reader of an MPEG transport stream (ISO/IEC 13818-1), as a .ts or a Blu-ray .m2ts file holds one
(docs/design.md, "Time reads"). frames() reads the time of every frame from the PES headers, as matroska.frames()
reads it from the blocks of a Matroska file. A PES header stores the PTS of the first access unit that starts in the
PES (13818-1 2.4.3.7). An audio PES can hold several frames, and the codec gives the length of each."""
import array, bisect, mmap, os, re, threading

from . import matroska
from .matroska import BITSTREAM, NONE, STORED, UNKNOWN

WRAP = 1 << 33   # a PTS or a DTS counts 90 kHz ticks in 33 bits
CHUNK = 1 << 18  # packets read at a time, about 48 MB

# stream_type -> (kind, codec). ISO holds the types of 13818-1 table 2-34. HDMV holds the Blu-ray types, which count
# only in a program with an "HDMV" or "HDPR" registration descriptor. ATSC holds the types of ATSC A/53 and A/52, and
# 0x82 for DTS as ffmpeg reads it.
ISO = {0x01: ("video", "mpeg2video"), 0x02: ("video", "mpeg2video"), 0x03: ("audio", "mp3"), 0x04: ("audio", "mp3"),
       0x0F: ("audio", "aac"), 0x10: ("video", "mpeg4"), 0x11: ("audio", "aac_latm"), 0x1B: ("video", "h264"),
       0x20: ("video", "h264"), 0x24: ("video", "hevc"), 0xEA: ("video", "vc1")}
HDMV = {0x80: ("audio", "pcm_bluray"), 0x81: ("audio", "ac3"), 0x82: ("audio", "dts"), 0x83: ("audio", "truehd"),
        0x84: ("audio", "eac3"), 0x85: ("audio", "dts"), 0x86: ("audio", "dts"), 0xA1: ("audio", "eac3"), 0xA2: ("audio", "dts"),
        0x90: ("subtitle", "pgs"), 0x92: ("subtitle", "hdmv_text")}
ATSC = {0x81: ("audio", "ac3"), 0x82: ("audio", "dts"), 0x87: ("audio", "eac3"), 0x8A: ("audio", "dts")}
# The codec of a private PES (stream_type 0x06) from a descriptor tag of ETSI EN 300 468, else from the registration
# descriptor (tag 5).
PRIVATE = {0x6A: ("audio", "ac3"), 0x7A: ("audio", "eac3"), 0x7B: ("audio", "dts"), 0x59: ("subtitle", "dvb_sub"),
           0x56: ("subtitle", "dvb_teletext")}
REGISTERED = {b"AC-3": ("audio", "ac3"), b"EAC3": ("audio", "eac3"), b"DTS1": ("audio", "dts"), b"DTS2": ("audio", "dts"),
              b"DTS3": ("audio", "dts")}
# The first bytes of an audio frame of each codec that frames() splits into frames. Another audio codec gets one frame
# a PES. A TrueHD access unit holds no sync word of its own, so the stream starts at a unit with a major sync (0xBA), 4
# bytes in. ffmpeg reads MLP (0xBB) in a TS as data. A LOAS frame of LATM AAC starts with the 11-bit sync word 0x2B7.
SYNC = {"aac": re.compile(rb"\xff[\xf0\xf1\xf8\xf9]"), "ac3": re.compile(rb"\x0b\x77"), "eac3": re.compile(rb"\x0b\x77"),
        "mp3": re.compile(rb"\xff[\xe2-\xff]"), "dts": re.compile(rb"\x7f\xfe\x80\x01|\x64\x58\x20\x25"),
        "truehd": re.compile(rb"(?s)....\xf8\x72\x6f\xba"), "aac_latm": re.compile(rb"\x56[\xe0-\xff]")}
HEAD = 32     # bytes of a unit's header that frames() reads at once. A LATM StreamMuxConfig can run to 22.
CORE = 0x76   # the stream_id_extension of the PES that hold the AC-3 core of a Blu-ray TrueHD PID. Its TrueHD units take 0x72.
NO_HEADER = (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF)   # the stream_ids of a PES with no optional header
AC3_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640)
# kbit/s of MPEG audio by (MPEG-1, the layer field) and the bitrate index 1 to 14 (ISO/IEC 11172-3, 13818-3)
V2_LOW = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
MPEG_KBPS = {(True, 3): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
             (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
             (True, 1): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
             (False, 3): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256), (False, 2): V2_LOW, (False, 1): V2_LOW}
# Header bytes 1 to 3 of each packet become 4 key bytes. The first holds the marker 0x80, the PUSI bit and the 5 high
# PID bits. The second holds PID bits 7 to 1, the third PID bit 0, the fourth the adaptation field control and the
# continuity counter. Only the first key byte has the high bit set, so a match of re or bytes.count always starts at a
# packet.
KEY = (bytes(0x80 | x & 0x5F for x in range(256)), bytes(x >> 1 for x in range(256)), bytes(x & 1 for x in range(256)),
       bytes(x & 0x3F for x in range(256)))


def frames(path):
    """{"packet": 188 or 192, "tracks": [Track.out of each elementary stream of the first PMT, in PMT order], "error": why
    the read stopped, or None} of the transport stream at path (docs/design.md, "Time reads"). It reads the packet
    headers through mmap, the PES header of each video and subtitle PES, and every payload byte of the audio PIDs it
    splits into frames. A thread reads the next chunk of the file ahead, so the disk reads while Python parses. A lost
    sync byte, a continuity count that skips, a PES that does not read or a file that ends inside a packet stops the
    read, and error says where. The tracks then hold the frames whose PES starts before it, so a caller never takes them
    for the whole file. Raises ValueError when the file holds no packets, PAT or PMT, and OSError when the file does not
    open."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as b:
        b.madvise(mmap.MADV_SEQUENTIAL)   # one pass from the start, so the pages read can go
        size, sync = packet_size(b)
        off = size - 188   # the byte of the sync byte in a packet. An M2TS packet starts with a 4-byte time.
        first = sync - off if sync >= off else sync + 188
        count, rest = divmod(len(b) - first, size)
        tracks, error, at, ahead, spare = None, None, None, None, bytearray(CHUNK * size)
        try:
            for p0 in range(first, first + count * size, CHUNK * size):   # strided slices of the mmap, so no chunk copy
                p1 = min(p0 + CHUNK * size, first + count * size)
                if ahead:
                    ahead.join()
                # A thread reads the next chunk into the page cache while this one parses. os.preadv releases the GIL.
                ahead = threading.Thread(target=os.preadv, args=(f.fileno(), [spare], p1), daemon=True)
                ahead.start()
                lost = re.search(rb"[^G]", b[p0 + off:p1:size])
                if lost:
                    p1 = p0 + lost.start() * size
                    error, at = f"the packet at byte {p1} holds no sync byte", p1
                key, h2 = bytearray((p1 - p0) // size * 4), b[p0 + off + 2:p1:size]
                key[0::4], key[1::4] = b[p0 + off + 1:p1:size].translate(KEY[0]), h2.translate(KEY[1])
                key[2::4], key[3::4] = h2.translate(KEY[2]), b[p0 + off + 3:p1:size].translate(KEY[3])
                if tracks is None:
                    tracks = program(b, key, p0, size, off)
                for t in tracks:
                    try:
                        t.read(b, key, p0, size, off)
                    except ValueError as ex:   # the first damage in the file counts
                        error, at = ex.args if at is None or ex.args[1] < at else (error, at)
                if error:
                    break
        finally:
            if ahead:   # the thread reads f, so it ends before f closes
                ahead.join()
        if tracks is None:
            raise ValueError("the file holds no PAT")
        if not error and rest:
            error, at = f"the file ends inside the packet at byte {first + count * size}", first + count * size
        if not error:
            for t in tracks:
                try:
                    t.close(True)
                except ValueError as ex:
                    error, at = ex.args if at is None or ex.args[1] < at else (error, at)
        if error:   # keep the frames whose PES starts before the damage
            for t in tracks:
                k = bisect.bisect_left(t.out["place"], at)
                for a in ("time", "dts", "tag", "place", "dur", "full"):
                    del t.out[a][k:]
        return {"packet": size, "tracks": [t.out for t in tracks if not t.core], "error": error}


def packet_size(b):
    """(the packet size, the byte of the first sync byte) of the transport stream b. Five sync bytes one packet apart
    mark the first packet. Raises ValueError when the first 64 KB hold none."""
    s = b.find(b"G", 0, 1 << 16)
    while s >= 0:
        for size in (188, 192):
            if all(b[s + k * size:s + k * size + 1] == b"G" for k in range(1, 5)):
                return size, s
        s = b.find(b"G", s + 1, 1 << 16)
    raise ValueError("the file holds no transport stream packets")


def packets(key, pid, start=False):
    """The key byte of each packet of pid in key, in file order, see KEY. With start, only of each packet that starts a
    PES or a section. re finds a literal key about 4 times faster than a pattern that starts with a set of bytes."""
    keys = [bytes([x | pid >> 8, pid >> 1 & 0x7F, pid & 1]) for x in ((0xC0,) if start else (0x80, 0xC0))]
    return sorted([m.start() for x in keys for m in re.finditer(re.escape(x), key)])


def payload(c, q, flags):
    """The byte of the payload in c of the packet whose sync byte is at q. flags is key byte 3 of the packet."""
    return q + 4 + (c[q + 4] + 1 if flags & 0x20 else 0)


def jump(c, q, flags):
    """True when the packet at q sets the discontinuity indicator, so its continuity count may start again."""
    return flags & 0x20 and c[q + 4] and c[q + 5] & 0x80


def section(c, key, pid, p0, size, off):
    """The first whole PSI section of pid in the chunk of c that starts at byte p0, or None."""
    data = None
    for k in packets(key, pid):
        flags = key[k + 3]
        q = p0 + (k >> 2) * size + off
        x = payload(c, q, flags)
        if not flags & 0x10 or (data is None and not key[k] & 0x40):
            continue
        if data is None:
            data, x = bytearray(), x + 1 + c[x]   # the pointer_field
        data += c[x:q + 188]
        if len(data) >= 3 and len(data) >= 3 + ((data[1] & 15) << 8 | data[2]):
            return bytes(data[:3 + ((data[1] & 15) << 8 | data[2])])
    return None


def descriptors(d):
    """{tag: data} of the descriptor loop d."""
    got, i = {}, 0
    while i + 2 <= len(d):
        got.setdefault(d[i], bytes(d[i + 2:i + 2 + d[i + 1]]))
        i += 2 + d[i + 1]
    return got


def program(c, key, p0, size, off):
    """[a Track of each elementary stream of the first program], from the PAT and that program's PMT in the chunk of c
    that starts at byte p0. Raises ValueError when the chunk holds neither."""
    pat = section(c, key, 0, p0, size, off)
    entries = [(pat[i:i + 2], (pat[i + 2] & 0x1F) << 8 | pat[i + 3]) for i in range(8, len(pat) - 7, 4)] if pat and pat[0] == 0 else []
    pmt = next((pid for number, pid in entries if number != b"\0\0"), None)   # program 0 names the NIT
    s = section(c, key, pmt, p0, size, off) if pmt is not None else None
    if not s or s[0] != 2:
        raise ValueError("the first chunk of the file holds no PAT and PMT")
    i = 12 + ((s[10] & 15) << 8 | s[11])
    bluray = descriptors(s[12:i]).get(5, b"")[:4] in (b"HDMV", b"HDPR")
    tracks, first = [], [None]
    while i + 5 <= len(s) - 4:
        stype, pid, n = s[i], (s[i + 1] & 0x1F) << 8 | s[i + 2], (s[i + 3] & 15) << 8 | s[i + 4]
        got = ISO.get(stype) or (HDMV.get(stype) if bluray else None) or ATSC.get(stype)
        if not got and stype == 6:
            d = descriptors(s[i + 5:i + 5 + n])
            got = next((PRIVATE[t] for t in d if t in PRIVATE), None) or REGISTERED.get(d.get(5, b"")[:4])
        tracks.append(Track(pid, stype, *(got or (None, None)), first))
        if bluray and stype == 0x83:   # ffmpeg reads the AC-3 core of a Blu-ray TrueHD PID as a stream of its own
            tracks.append(Track(pid, stype, "audio", "ac3", first, core=True))
            tracks[-2].out["core"] = tracks[-1].out
        i += 5 + n
    return tracks


def stamp(c, i):
    """The 33-bit PTS or DTS at byte i of c."""
    return (c[i] >> 1 & 7) << 30 | c[i + 1] << 22 | c[i + 2] >> 1 << 15 | c[i + 3] << 7 | c[i + 4] >> 1


def pes(c, q, at):
    """(PTS, DTS, the byte of the payload in c, the payload bytes the header gives or None) of the PES that starts in
    the packet whose sync byte is at q of c. at is the file byte of the packet. Raises ValueError when the header does
    not read in that packet."""
    flags = c[q + 3]
    if not flags & 0x10:
        raise ValueError(f"the PES start at byte {at} holds no payload", at)
    x, end = payload(c, q, flags), q + 188
    if x + 9 > end or c[x:x + 3] != b"\0\0\1":
        raise ValueError(f"the PES at byte {at} has no start code", at)
    length = int.from_bytes(c[x + 4:x + 6], "big")
    if c[x + 3] in NO_HEADER:
        return None, None, x + 6, length or None
    h = x + 9 + c[x + 8]
    if c[x + 6] >> 6 != 2 or h > end:
        raise ValueError(f"the PES header at byte {at} does not read", at)
    pd = c[x + 7] >> 6
    return stamp(c, x + 9) if pd & 2 else None, stamp(c, x + 14) if pd == 3 else None, h, length - 3 - c[x + 8] if length else None


def truehd_unit(h):
    """(bytes, samples, sample rate, True) of the TrueHD access unit whose header starts h, or None. The first 12 bits
    after the check nibble give its length in 16-bit words. A unit with a major sync names the rate code. The code
    gives 40 samples at 48 or 44.1 kHz, each doubled by its low 3 bits, as ffmpeg's mlp_parse.c reads it. Another
    unit gives None for samples and rate, so the stream's last rate counts."""
    n = ((h[0] << 8 | h[1]) & 0xFFF) * 2
    if n < 4 or len(h) < 10:
        return None
    if h[4:7] != b"\xf8\x72\x6f" or h[7] not in (0xBA, 0xBB):
        return n, None, None, True
    code = h[8] >> 4 if h[7] == 0xBA else h[9] >> 4   # TrueHD names one rate, MLP the rate of its first group
    return (n, 40 << (code & 7), (44100 if code & 8 else 48000) << (code & 7), True) if code != 15 else (n, None, None, True)


def latm_unit(h):
    """(bytes, samples, sample rate, True) of the LOAS frame of LATM AAC whose header starts h, or None (ISO/IEC 14496-3
    1.7). 13 bits after the sync word give the bytes that follow. A frame that holds its StreamMuxConfig names its
    subframes and the AudioSpecificConfig of its first layer, which matroska.aac_length() reads. Another frame, or a
    config that h cuts, gives None for samples and rate, so the stream's last config counts."""
    if len(h) < 4 or h[0] != 0x56 or h[1] >> 5 != 7:
        return None
    n = 3 + ((h[1] & 0x1F) << 8 | h[2])
    x, width, at = int.from_bytes(h[3:], "big"), 8 * (len(h) - 3), 0

    def take(k):   # the next k bits
        nonlocal at
        if at + k > width:
            raise IndexError
        at += k
        return x >> width - at & ((1 << k) - 1)
    try:
        if take(1):   # useSameStreamMux: no config in this frame
            return n, None, None, True
        version = take(1)
        if version and take(1):   # audioMuxVersionA 1 holds no config AMG reads
            return n, None, None, True
        if version:
            take(8 * (take(2) + 1))   # taraBufferFullness
        take(1); subframes = take(6); take(7)   # allStreamsSameTimeFraming, numSubFrames, numProgram and numLayer
        if version:
            take(8 * (take(2) + 1))   # ascLen
        got = matroska.aac_length(take(64).to_bytes(8, "big"))   # every bit aac_length() may read
    except IndexError:
        return n, None, None, True
    return (n, got[0] * (subframes + 1), got[1], True) if got else (n, None, None, True)


def unit(h):
    """(bytes, samples, sample rate, new) of the audio unit whose header starts h, or None when h holds no header or
    too few bytes. samples and rate are None when the header gives no length. new is False for a unit that belongs to
    the frame before it: an E-AC-3 dependent substream (ATSC A/52 annex E) or a DTS-HD extension substream (ETSI TS 102
    114 annex E). Each unit reads as matroska.frame_length() reads its samples."""
    if len(h) < 10:
        return None
    if h[:2] == b"\x0b\x77":
        bsid, got = h[5] >> 3, matroska.frame_length("A_AC3", h) or (None, None)
        if bsid <= 10 and h[4] >> 6 < 3 and h[4] & 63 < 38:   # AC-3. The frame size code and the rate give the size.
            rate, code = matroska.AC3_RATES[h[4] >> 6], h[4] & 63
            return 2 * (AC3_KBPS[code >> 1] * 96000 // rate + (rate == 44100 and code & 1)), *got, True
        if 10 < bsid <= 16:   # E-AC-3. frmsiz gives the size, and strmtyp 1 marks a dependent substream.
            return 2 * ((h[2] & 7) << 8 | h[3]) + 2, *got, h[2] >> 6 != 1
        return None
    if h[:4] == b"\x7f\xfe\x80\x01":   # DTS core. FSIZE gives the size.
        return (int.from_bytes(h[4:10], "big") >> 20 & 0x3FFF) + 1, *(matroska.frame_length("A_DTS", h) or (None, None)), True
    if h[:4] == b"\x64\x58\x20\x25":   # DTS-HD extension substream. nuExtSSFsize of 16 or 20 bits gives the size.
        v = int.from_bytes(h[4:10], "big")
        return (v >> 5 & 0xFFFFF if v >> 37 & 1 else v >> 13 & 0xFFFF) + 1, None, None, False
    if h[0] == 0xFF and h[1] & 0xF6 == 0xF0:   # ADTS. frame_length gives the size, and each raw data block holds 1024 samples.
        size, idx = (h[3] & 3) << 11 | h[4] << 3 | h[5] >> 5, h[2] >> 2 & 15
        return (size, 1024 * ((h[6] & 3) + 1), matroska.AAC_RATES[idx] if idx < 13 else None, True) if size >= 7 else None
    got = matroska.frame_length("A_MPEG/L2", h)
    if got and 0 < h[2] >> 4 < 15:   # MPEG audio. The bitrate, the rate and the padding bit give the size.
        layer, pad = h[1] >> 1 & 3, h[2] >> 1 & 1
        kbps = MPEG_KBPS[h[1] >> 3 & 3 == 3, layer][(h[2] >> 4) - 1]
        return ((12000 * kbps // got[1] + pad) * 4 if layer == 3 else got[0] * 125 * kbps // got[1] + pad), *got, True
    return None


def extension(c, x, end):
    """The stream_id_extension of the PES whose header starts at byte x of c and ends by byte end, or None (ISO/IEC
    13818-1 2.4.3.7, PES_extension_flag_2). Only a PES of stream_id 0xFD carries one."""
    f = c[x + 7]
    if c[x + 3] != 0xFD or not f & 1:
        return None
    i = x + 9 + (0, 0, 5, 10)[f >> 6] + 6 * (f >> 5 & 1) + 3 * (f >> 4 & 1) + (f >> 3 & 1) + (f >> 2 & 1) + 2 * (f >> 1 & 1)
    e = c[i] if i < end else 0
    i += 1 + 16 * (e >> 7)   # the flags, then the private data
    i += 1 + c[i] if e & 0x40 and i < end else 0   # the pack header
    i += 2 * (e >> 5 & 1) + 2 * (e >> 4 & 1)   # the sequence counter and the P-STD buffer
    return c[i + 1] & 0x7F if e & 1 and i + 1 < end and not c[i + 1] & 0x80 else None


class Track:
    """The frames of one PID. out holds them as matroska.track_frames() does: each list one item per frame in file
    order. time and dts are ns (NONE when not known), tag an index of matroska.TAGS, place the byte of the packet where
    the frame's PES starts, dur the ns of an audio frame from its codec (NONE when not known), full 1 when the frame
    holds data. frame is the ns of the first audio frame the codec gives a length, else None. split says the audio
    splits into frames by its codec, else each PES is one frame. junk counts the runs of audio bytes that start no
    frame. A broadcast splice can leave one. Each run belongs to the frame before it, as ffmpeg's parsers keep it.
    ffmpeg's LATM parser puts a run in the frame after it instead. So a LATM frame after a run takes the PTS of the PES
    where the run starts (lead). The first frame after such bytes at the stream start does the same.

    A video, subtitle or other PES is one frame, stored at its PTS. An audio PES of a codec in SYNC holds whole frames
    and parts of frames. The first frame that starts in a PES is stored at its PTS. Each later frame is bitstream at the
    time of the frame before plus that frame's codec length, and so is each frame of a PES with no PTS. A frame after
    one with no time or no length is unknown. A TrueHD or LATM frame with no rate of its own takes the last one the
    stream named, see truehd_unit() and latm_unit(). A TrueHD PID leaves out the PES of its AC-3 core (CORE). ffmpeg
    reads the core of a Blu-ray TrueHD PID as a stream of its own, so that PID gets one more Track, with core True. It
    keeps only the PES of the core, and its out is the "core" of the TrueHD Track's out.

    A PID whose stream type names no codec takes its codec from the first bytes of its first PES, as ffmpeg probes such
    a stream. TrueHD with no Blu-ray registration and LATM in a private PES are such PIDs. They must hold a TrueHD major sync or
    a LOAS sync word. Any other PID of no codec gives no frames."""

    def __init__(self, pid, stream_type, kind, codec, first, core=False):
        self.out = {"pid": pid, "stream_type": stream_type, "kind": kind, "codec": codec, "time": array.array("q"),
                    "dts": array.array("q"), "tag": bytearray(), "place": array.array("q"), "dur": array.array("q"),
                    "full": bytearray(), "frame": None, "pes_no_pts": 0, "junk": 0, "split": kind == "audio" and codec in SYNC}
        self.split, self.core = self.out["split"], core
        self.body = bytes([0x80 | pid >> 8, pid >> 1 & 0x7F, pid & 1])   # the key of a packet that starts nothing
        self.void = re.compile(re.escape(self.body) + rb"[\x00-\x0f\x20-\x2f]")   # one with no payload
        self.first, self.prev, self.cc, self.between = first, None, None, 0
        self.pes, self.parts, self.skip, self.carry, self.held = None, None, 0, b"", None
        self.synced, self.ext_only, self.anchor, self.at = False, False, None, 0.0
        self.last, self.probed, self.lead = (None, None), kind is not None, None   # the stream's last rate, see unit()
        self.codec(kind, codec)

    def codec(self, kind, codec):
        """Set the kind and the codec of the PID, and whether its PES split into frames."""
        self.out.update(kind=kind, codec=codec, split=kind == "audio" and codec in SYNC)
        self.split = self.out["split"]

    def probe(self, c, key, p0, size, off):
        """Name the codec of a PID whose stream type names none, see the class. Only the first PES that starts in the
        chunk of c at byte p0 counts."""
        for k in packets(key, self.out["pid"], True)[:1]:
            q = p0 + (k >> 2) * size + off
            try:
                x = pes(c, q, q - off)[2]
            except ValueError:
                x = q + 188
            data = bytes(c[x:q + 188])
            self.probed = True
            if SYNC["truehd"].match(data):
                self.codec("audio", "truehd")
            elif SYNC["aac_latm"].match(data) and (latm_unit(data[:HEAD]) or (0, None))[1]:   # a LOAS frame with its config
                self.codec("audio", "aac_latm")

    def unit(self, h):
        """unit() of the header h, or truehd_unit() or latm_unit() for those codecs, with the stream's last rate for a
        frame that names none."""
        codec = self.out["codec"]
        if codec not in ("truehd", "aac_latm"):
            return unit(h)
        u = (truehd_unit if codec == "truehd" else latm_unit)(h)
        if u and u[1]:
            self.last = u[1:3]
        return u and (u[0], *self.last, True)

    def clock(self, pts, dts):
        """(PTS, DTS) in ns, each None when absent. The PTS takes the value closest to the PTS before it in the track, or
        to the first PTS of the file, so a wrap at 2^33 ticks adds 2^33 ticks. The DTS takes the value closest to the
        PTS."""
        if pts is None:
            return None, None
        if self.first[0] is None:
            self.first[0] = pts
        prev = self.first[0] if self.prev is None else self.prev
        self.prev = pts = pts + (prev - pts + WRAP // 2) // WRAP * WRAP
        dts = None if dts is None else dts + (pts - dts + WRAP // 2) // WRAP * WRAP
        return (pts * 100000 + 4) // 9, None if dts is None else (dts * 100000 + 4) // 9

    def add(self, place, pts, dts, tag, dur, full):
        """Add one frame to out. A time of None adds NONE."""
        o = self.out
        o["time"].append(NONE if pts is None else pts)
        o["dts"].append(NONE if dts is None else dts)
        o["tag"].append(tag)
        o["place"].append(place)
        o["dur"].append(dur)
        o["full"].append(full)

    def read(self, c, key, p0, size, off):
        """Read the packets of the PID in the chunk of c that starts at byte p0. key is the key of the chunk, see KEY."""
        if not self.probed:
            self.probe(c, key, p0, size, off)
        if self.out["kind"] is None:
            return
        if self.split:
            return self.read_audio(c, key, p0, size, off)
        void, last = [m.start() for m in self.void.finditer(key)], 0
        for k in packets(key, self.out["pid"], True):
            at = p0 + (k >> 2) * size
            q = at + off
            self.count(key, void, last, k)
            if self.cc is not None and (self.cc + 1 + self.between) & 15 != key[k + 3] & 15 and not jump(c, q, key[k + 3]):
                lost = self.out["place"][-1]   # the PES before this one lost a packet, so its frame goes too
                raise ValueError(f"the PES at byte {lost} of PID {self.out['pid']} lost a packet: the continuity count skips", lost)
            self.cc, self.between, last = key[k + 3] & 15, 0, k
            pts, dts, x, _ = pes(c, q, at)
            self.out["pes_no_pts"] += pts is None
            self.add(at, *self.clock(pts, dts), UNKNOWN if pts is None else STORED, NONE, x < q + 188)
        self.count(key, void, last, len(key))

    def count(self, key, void, a, b):
        """Add the packets of the PID with a payload that start nothing between key bytes a and b. Any such packet makes
        the frame before it hold data."""
        n = key.count(self.body, a, b) - (bisect.bisect_left(void, b) - bisect.bisect_left(void, a))
        self.between += n
        if n and self.out["full"]:
            self.out["full"][-1] = 1

    def read_audio(self, c, key, p0, size, off):
        """Read each packet of the PID in the chunk of c that starts at byte p0, and gather the payload of each PES."""
        pid, start = self.out["pid"], p0 + off
        for k in packets(key, pid):
            flags, parts = key[k + 3], self.parts
            # Most packets hold only payload, start no PES and count one on. They take this short path.
            if flags & 0x30 == 0x10 and key[k] < 0xC0 and parts is not None and flags & 15 == (self.cc + 1) & 15:
                q = start + (k >> 2) * size
                self.cc = flags & 15
                parts.append(c[q + 4:q + 188])
                continue
            if not flags & 0x10:
                continue
            at = p0 + (k >> 2) * size
            q = at + off
            if self.cc is not None and (self.cc + 1) & 15 != flags & 15 and not jump(c, q, flags):
                raise ValueError(f"the packet at byte {at} of PID {pid} breaks the continuity count", at)
            self.cc, x = flags & 15, payload(c, q, flags)
            if key[k] & 0x40:
                self.close()
                head, (pts, dts, x, length) = x, pes(c, q, at)
                self.pes, self.parts = (at, pts, dts, length), []
                if (self.core or self.out["codec"] == "truehd") and (extension(c, head, x) == CORE) != self.core:   # see the class
                    self.parts = None
                    continue
            if x > q + 188:
                raise ValueError(f"the adaptation field of the packet at byte {at} runs past it", at)
            if self.parts is not None:
                self.parts.append(c[x:q + 188])

    def close(self, final=False):
        """Split the PES read so far into frames. final ends the stream, so a unit cut by the end of the file reads. A
        stream whose last PES was left out still reads the unit it carries, see CORE."""
        if self.parts is None:
            if final and self.carry:
                self.feed(b"", *self.held, True)
            return
        (place, pts, dts, length), data = self.pes, b"".join(self.parts)
        self.parts = None
        if length is not None and length != len(data):
            raise ValueError(f"the PES at byte {place} holds {len(data)} payload bytes, and its header says {length}", place)
        self.out["pes_no_pts"] += pts is None
        self.feed(data, place, *self.clock(pts, dts), final)

    def feed(self, data, place, pts, dts, final):
        """Split the payload data of the PES at byte place into frames. A unit that starts in a PES and runs past it ends
        in the next PES, and belongs to the PES where it starts. The PTS of a PES times the first frame that starts in
        it. carry holds the first bytes of a unit whose header the PES before cut, held the [place, PTS, DTS] of its
        PES. skip counts the bytes of the last unit still to come."""
        if self.skip and self.skip >= len(data):   # no unit starts in this PES
            self.skip -= len(data)
            return
        data, base, pos = self.carry + data, len(self.carry), self.skip
        self.carry, self.skip, mine = b"", 0, [place, pts, dts]
        latm = self.out["codec"] == "aac_latm"
        if not self.synced:   # the stream may start inside a frame, so frames start at the first sync word
            m = SYNC[self.out["codec"]].search(data, pos)
            if latm and (not m or m.start() > pos):
                self.lead = self.lead or mine
            if not m:
                return
            pos, self.synced = m.start(), True
        while pos < len(data):
            info = self.held if pos < base else mine
            h = data[pos:pos + HEAD]
            if len(h) < HEAD and not final:
                self.carry, self.held = data[pos:], info
                return
            u = self.unit(h)
            if u is None:   # bytes that start no frame belong to the frame before, as ffmpeg's parser keeps them
                if len(h) < HEAD:   # the end of the stream cut a header
                    return
                self.out["junk"] += 1
                self.lead = self.lead or (info if latm else None)   # see the class
                m = SYNC[self.out["codec"]].search(data, pos + 1)
                if not m:
                    self.synced = False
                    return
                pos = m.start()
                continue
            n, samples, rate, new = u
            if not self.out["time"] and not new and self.out["codec"] == "dts":
                self.ext_only = True   # DTS Express holds extension substreams and no core
            if new or self.ext_only:
                info, self.lead = self.lead or info, None
                self.frame(info, samples, rate)
                info[1] = info[2] = None   # the PTS times only the first frame that starts in the PES
                if self.out["codec"] == "mp3":
                    self.out["codec"] = ("mp3", "mp3", "mp2", "mp1")[h[1] >> 1 & 3]
            pos += n
        self.skip = pos - len(data)

    def frame(self, info, samples, rate):
        """Add the audio frame that starts in the PES info, [place, PTS, DTS], with its codec length."""
        length = samples * 1e9 / rate if samples and rate else None
        if info[1] is not None:
            self.anchor, self.at = info[1], 0.0
        time = NONE if self.anchor is None else self.anchor + round(self.at)
        tag = STORED if info[1] is not None else UNKNOWN if self.anchor is None else BITSTREAM
        self.add(info[0], time, info[2], tag, round(length) if length else NONE, 1)
        self.out["frame"] = self.out["frame"] or length
        self.at += length or 0.0
        if not length:
            self.anchor = None
