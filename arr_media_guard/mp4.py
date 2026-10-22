# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The sample reader of MP4 and QuickTime files (ISO/IEC 14496-12, docs/design.md, "Time reads"). samples() reads the
top-level box headers and the moov box. It gives every sample of every track its byte, its size and its time as the
sample tables store it and the edit list presents it."""
import array, functools, os, struct, zlib

from . import matroska

CUT = 4                     # a tag of a sample that no edit presents, after the tags of matroska.TAGS
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex"}
HANDLERS = {b"vide": "video", b"soun": "audio", b"sbtl": "subtitle", b"text": "subtitle", b"subt": "subtitle", b"clcp": "subtitle"}


def boxes(b, start, end):
    """(type, data start, data end) of each box of b[start:end]. A size of 0 runs to end, a size of 1 has a 64-bit size
    after the type. Raises ValueError when a box runs past end."""
    p = start
    while p + 8 <= end:
        size, kind = struct.unpack_from(">I4s", b, p)
        head = 8
        if size == 1:
            size, head = struct.unpack_from(">Q", b, p + 8)[0], 16
        elif size == 0:
            size = end - p
        if size < head or p + size > end:
            raise ValueError(f"the {kind.decode('latin-1')} box at byte {p} runs past its parent")
        yield kind, p + head, p + size
        p += size


def samples(path):
    """{"tracks": [track() of each trak, in file order], "error": why the read failed, or None} of the MP4 or QuickTime
    file path. It reads the top-level box headers and the moov box, which an old QuickTime file may compress with zlib.
    A file with movie fragments, which its moov names with an mvex box, keeps its sample tables in each moof. So error
    names it and no track holds samples. Raises OSError when the file does not open."""
    st = os.stat(path)
    return read(path, st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=4)
def read(path, size, mtime_ns):
    """samples() of path at size and mtime_ns. The proof and the conversion read the same file, so the last 4 reads stay."""
    with open(path, "rb") as f:
        moov, p = None, 0
        while p + 8 <= size:
            f.seek(p)
            head = f.read(16)
            try:
                n, kind = struct.unpack_from(">I4s", head)
                n = struct.unpack_from(">Q", head, 8)[0] if n == 1 else size - p if n == 0 else n
            except struct.error:   # the file ends inside the header, as a cut file may
                return {"tracks": [], "error": f"the top-level box at byte {p} does not read"}
            if n < 8:
                return {"tracks": [], "error": f"the top-level box at byte {p} has a size of {n}"}
            if kind == b"moov":
                f.seek(p)
                moov = f.read(n)
            p += n
    if moov is None:
        return {"tracks": [], "error": "the file holds no moov box"}
    try:
        (_, ds, de), = [x for x in boxes(moov, 0, len(moov)) if x[0] == b"moov"]
        packed = {k: (d, e) for k, d, e in boxes(moov, ds, de) if k == b"cmov"}
        if packed:   # a QuickTime moov compressed with zlib: dcom names the method, cmvd holds the size and the data
            got = {k: moov[d:e] for k, d, e in boxes(moov, *packed[b"cmov"])}
            if got.get(b"dcom") != b"zlib" or b"cmvd" not in got:
                return {"tracks": [], "error": "the moov box is compressed by another method than zlib"}
            moov = zlib.decompress(got[b"cmvd"][4:])
            (_, ds, de), = [x for x in boxes(moov, 0, len(moov)) if x[0] == b"moov"]
        movie, tracks, fragmented = None, [], False
        for kind, d, e in boxes(moov, ds, de):
            if kind == b"mvhd":
                movie = struct.unpack_from(">I", moov, d + (20 if moov[d] == 1 else 12))[0]
            elif kind == b"trak":
                tracks.append(trak(moov, d, e))
            elif kind == b"mvex":
                fragmented = True
    except (ValueError, struct.error, IndexError, zlib.error) as ex:
        return {"tracks": [], "error": f"the moov box does not read: {ex}"}
    if fragmented:
        return {"tracks": [], "error": "the file holds movie fragments, whose sample tables are in each moof box"}
    if not movie:
        return {"tracks": [], "error": "the moov box holds no movie timescale"}
    out, advanced = [], True
    for t in tracks:
        # ffmpeg turns its advanced edit lists off for the whole file at the first trak with no samples (mov.c,
        # mov_read_trak()). A trak with chunks and no stts fails ffmpeg's check before that switch. A trak with sizes
        # and no stts fails track().
        advanced = advanced and bool(t["stts"] or t["chunks"])
        try:
            out.append(track(t, movie, advanced))
        except ValueError as ex:
            return {"tracks": out, "error": f"track {t['id']}: {ex}"}
    return {"tracks": out, "error": None}


def trak(b, start, end):
    """The raw tables of the trak box b[start:end]: {id, kind, codec, timescale, stts, ctts, count, one, sizes, stsc,
    chunks, edits}, each table as stored. count is the number of samples. one is the size of every sample when the
    stsz box gives one size, and sizes is then None. Else sizes holds the size of each sample."""
    t = {"id": None, "kind": None, "handler": "", "codec": "", "timescale": None, "stts": [], "ctts": [], "count": None, "one": 0, "sizes": None,
         "stsc": [], "chunks": None, "edits": None}

    def walk(s, e, parent=b""):
        for kind, d, x in boxes(b, s, e):
            if kind in CONTAINERS:
                walk(d, x, kind)
            elif kind == b"tkhd":
                t["id"] = struct.unpack_from(">I", b, d + (20 if b[d] == 1 else 12))[0]
            elif kind == b"mdhd":
                t["timescale"] = struct.unpack_from(">I", b, d + (20 if b[d] == 1 else 12))[0]
            elif kind == b"hdlr" and parent == b"mdia":   # a QuickTime minf holds a data handler too
                t["handler"] = bytes(b[d + 8:d + 12]).decode("latin-1")
                t["kind"] = HANDLERS.get(b[d + 8:d + 12])
            elif kind == b"stsd":
                t["codec"] = bytes(b[d + 12:d + 16]).decode("latin-1") if x - d >= 16 else ""
            elif kind == b"stts":
                n = struct.unpack_from(">I", b, d + 4)[0]
                t["stts"] = list(struct.iter_unpack(">II", b[d + 8:d + 8 + 8 * n]))
            elif kind == b"ctts":   # version 0 is unsigned in the spec, and writers store negative offsets in it too
                n = struct.unpack_from(">I", b, d + 4)[0]
                t["ctts"] = list(struct.iter_unpack(">Ii", b[d + 8:d + 8 + 8 * n]))
            elif kind == b"stsz":
                t["one"], t["count"] = struct.unpack_from(">II", b, d + 4)
                if not t["one"]:
                    t["sizes"] = array.array("q", struct.unpack_from(f">{t['count']}I", b, d + 12))
            elif kind == b"stz2":
                bits, n = b[d + 7], struct.unpack_from(">I", b, d + 8)[0]
                t["count"], raw = n, b[d + 12:x]
                if bits == 16:
                    t["sizes"] = array.array("q", struct.unpack_from(f">{n}H", raw))
                elif bits == 8:
                    t["sizes"] = array.array("q", raw[:n])
                else:   # 4 bits a sample, the high nibble first
                    t["sizes"] = array.array("q", [raw[k // 2] >> (4 if k % 2 == 0 else 0) & 15 for k in range(n)])
            elif kind == b"stsc":
                n = struct.unpack_from(">I", b, d + 4)[0]
                t["stsc"] = list(struct.iter_unpack(">III", b[d + 8:d + 8 + 12 * n]))
            elif kind == b"stco":
                n = struct.unpack_from(">I", b, d + 4)[0]
                t["chunks"] = struct.unpack_from(f">{n}I", b, d + 8)
            elif kind == b"co64":
                n = struct.unpack_from(">I", b, d + 4)[0]
                t["chunks"] = struct.unpack_from(f">{n}Q", b, d + 8)
            elif kind == b"elst":
                v, n = b[d], struct.unpack_from(">I", b, d + 4)[0]
                fmt = ">Qqi" if v == 1 else ">Iii"   # the media rate is a 16.16 fixed-point number
                t["edits"] = list(struct.iter_unpack(fmt, b[d + 8:d + 8 + struct.calcsize(fmt) * n]))
    walk(start, end)
    return t


def ffmpeg_lead(kind, media, mt, cts, tag, advanced=True):
    """The ticks by which ffmpeg presents a video track earlier than its edit list does, or 0. This is ffmpeg's start
    rule (libavformat mov.c, mov_fix_index(), "Offset DTS by N to make first pts zero"). When a video track has a media
    edit and its first presented frame comes after the edit's media_time, ffmpeg moves the whole track earlier by that
    gap, so the first frame plays at the edit's start. An H.264 or HEVC encoder delays the first frame by its B-frame
    reorder, and an edit list with media_time 0 leaves that delay in place. AMG's reference for an MP4 source is the
    timeline that ffmpeg-based players (Plex, mpv, Kodi) play, so the reader takes this rule. mkvmerge 102 presents the
    edit as written, so a conversion gives mkvmerge the gap back with --sync, see convert.mp4_sync(). media holds the
    track's media edits, mt the media_time of the first, cts each sample's composition time and tag its tag. advanced
    is False after a trak with no samples, see read(). ffmpeg then keeps the edit list's times, and the rule moves
    nothing."""
    if kind != "video" or not media or not advanced:
        return 0
    return max(0, min((c - mt for c, g in zip(cts, tag) if g == matroska.STORED and c >= mt), default=0))


def track(t, movie, advanced=True):
    """The track t of trak() with its samples. Each list holds one item per sample in decode order, the order of the
    sample tables: time (ns, as the edit list presents it), tag (STORED, or CUT for a sample no edit presents), place
    (its byte), dur (ns, its stts delta), size and full (1 when it holds data). dts holds each decode time in ns, on
    the same line as time. frame is the ns of one sample when every stts delta but the last is one value, else None.
    An audio track whose stts is one entry of delta 1 gets one item per chunk, as ffmpeg reads it. The item holds the
    chunk's byte, the time of its first sample, and the ticks and the bytes of all its samples.

    ISO/IEC 14496-12 8.6.6: an edit with a media_time of -1 is empty and moves the media after it later. A media edit
    presents the composition times from media_time for its segment duration, at the presentation time where it
    starts. A sample before or after that span is cut. Its time stays on the edit's line (composition time minus
    media_time, plus the edit's start), as ffmpeg and mkvmerge time an AAC priming frame. With no edit list, the
    composition time is the presentation time. An edit list with more than one media edit, or a media rate other
    than 1, raises ValueError: the samples have no one time each then.

    The times then take ffmpeg's start rule, see ffmpeg_lead(), unless advanced is False. shift holds the ns that rule
    moved every time of the track earlier, so a sample's time as the spec presents it, and as mkvmerge writes it, is
    time + shift."""
    ts, n, chunks = t["timescale"], t["count"], t["chunks"]
    if not ts or n is None or chunks is None:
        raise ValueError("the sample tables are not complete")
    if sum(c for c, _ in t["stts"]) != n:
        raise ValueError(f"the time-to-sample table counts {sum(c for c, _ in t['stts'])} samples, the size table {n}")
    if t["ctts"] and sum(c for c, _ in t["ctts"]) != n:
        raise ValueError(f"the composition offset table counts {sum(c for c, _ in t['ctts'])} samples, the size table {n}")
    # (byte, first sample, samples) of each chunk: chunk k holds the samples-per-chunk of the last stsc entry that
    # starts at or before it.
    spans, k = [], 0
    for e, (first, per, _) in enumerate(t["stsc"]):
        last = t["stsc"][e + 1][0] - 1 if e + 1 < len(t["stsc"]) else len(chunks)
        for c in range(first - 1, min(last, len(chunks))):
            if (m := min(per, n - k)) > 0:
                spans.append((chunks[c], k, m))
                k += m
    if k != n:
        raise ValueError(f"the chunk tables place {k} samples of {n}")
    # ffmpeg's rule for old uncompressed audio (libavformat mov.c, mov_build_index()): an audio track whose stts is one
    # entry of delta 1 stores one sample a tick, and ffmpeg reads each chunk as one entry.
    chunked = t["kind"] == "audio" and len(t["stts"]) == 1 and t["stts"][0][1] == 1
    if chunked:
        place, dts = array.array("q", (p for p, _, _ in spans)), array.array("q", (k for _, k, _ in spans))
        dur = array.array("q", (m for _, _, m in spans))
        size = array.array("q", (t["one"] * m if t["sizes"] is None else sum(t["sizes"][k:k + m]) for _, k, m in spans))
    else:
        size = array.array("q", [t["one"]]) * n if t["sizes"] is None else t["sizes"]
        place, dts, dur, at = array.array("q"), array.array("q"), array.array("q"), 0
        for p, k, m in spans:
            for s in size[k:k + m]:
                place.append(p)
                p += s
        for count, delta in t["stts"]:
            for _ in range(count):
                dts.append(at)
                dur.append(delta)
                at += delta
    cts = array.array("q", dts)
    if t["ctts"]:   # each entry takes the composition offset of its first sample
        runs, end = iter(t["ctts"]), 0
        for e, k in enumerate([k for _, k, _ in spans] if chunked else range(n)):
            while k >= end:
                count, off = next(runs)
                end += count
            cts[e] += off
    empty, media = 0, []
    for seg, mt, rate in t["edits"] or ():
        if mt == -1:
            if media:
                raise ValueError("an empty edit follows a media edit")
            empty += seg
        else:
            media.append((seg, mt, rate))
    if len(media) > 1:
        raise ValueError(f"the edit list holds {len(media)} media edits")
    if media and media[0][2] != 0x10000:
        raise ValueError(f"the edit list plays the media at rate {media[0][2] / 0x10000:g}")
    seg, mt = (media[0][0], media[0][1]) if media else (0, 0)
    # A sample is presented when its span [cts, cts + delta) meets the edit's span [mt, mt + seg). A segment of 0 runs
    # to the end of the media. The spans compare in exact integers: the segment counts in the movie timescale.
    shown = lambda c, d: c + d > mt and (not seg or (c - mt) * movie < seg * ts)
    tag = bytearray(matroska.STORED if shown(c, d) else CUT for c, d in zip(cts, dur))
    lead = ffmpeg_lead(t["kind"], media, mt, cts, tag, advanced)
    # time = empty / movie + (cts - mt - lead) / ts seconds, rounded to the ns once, at the end
    ns = lambda c: ((empty * ts + (c - mt - lead) * movie) * 2000000000 + movie * ts) // (2 * movie * ts)
    time = array.array("q", (ns(c) for c in cts))
    st = [(c, d) for c, d in t["stts"] if c]   # one sample's ticks when every delta but the last sample's is one value
    deltas = {d for _, d in st[:-1]} | ({st[-1][1]} if st and st[-1][0] > 1 else set())
    return {"id": t["id"], "kind": t["kind"], "handler": t["handler"], "codec": t["codec"], "timescale": ts, "edits": t["edits"],
            "shift": (lead * 2000000000 + ts) // (2 * ts),
            "time": time, "tag": tag, "place": place, "dur": array.array("q", ((d * 2000000000 + ts) // (2 * ts) for d in dur)),
            "full": bytearray(s > 0 for s in size), "size": size,
            "dts": array.array("q", (ns(d) for d in dts)), "frame": (min(deltas) * 2000000000 + ts) // (2 * ts) if len(deltas) == 1 else None}
