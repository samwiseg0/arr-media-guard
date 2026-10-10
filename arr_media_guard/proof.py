# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The packet proof of a conversion and the damage checks of its original."""
import array, concurrent.futures, contextlib, hashlib, itertools, json, os, re, shutil, subprocess, uuid

from . import checks, cli, config, content, convert, decide, runner, subsync


PROOF_RATE = 10e6         # bytes a second a proof read must reach at least, else it times out
SYNC = 0.05               # seconds a stream's start may move against the first video stream's
TIME_SLACK = 0.002        # seconds a packet's time may move against its stream's start: Matroska keeps milliseconds
END_LOSS = 3              # video packets the new file may lack at its end, a loss of up to 3 frames that is accepted
JUNK_MAX = 16             # audio packets the ends of a stream may lose as junk and cut frames, see ends_lost()
JUNK_HEADERS = (b"RIFF",)  # a stray container header that an audio stream may hold as a packet of its own
JUNK_HEADER_MAX = 128     # bytes such a header packet holds at most
SIDECAR_FLAGS = {"forced": "--forced-display-flag", "hi": "--hearing-impaired-flag", "sdh": "--hearing-impaired-flag",
                 "cc": "--hearing-impaired-flag"}
# Where mkvmerge changes packets on purpose, per ffprobe format and codec, "*" for any format: (the bitstream filter for
# the original's packets, the one for the new file's), so both hash the same bytes. mkvmerge strips the ADTS header of
# AAC, drops the access unit delimiters of H.264, puts the HEVC parameter sets in front of each keyframe and drops
# its delimiters, and stores H.264 from a transport stream with length prefixes. filter_units rewrites every unit it
# parses, so it runs on both sides. The parameter sets it takes out of HEVC packets stay in the codec private data of
# both files. mkvmerge keeps MPEG-4 packed B-frames packed, so MPEG-4 part 2 needs no filter. PCM has no frames, and
# mkvmerge cuts it into other packets, so both sides are cut again into packets of 4,096 samples ("pcm_*").
NO_AUD = {"h264": "filter_units=remove_types=9", "hevc": "filter_units=remove_types=32-35"}
PROOF_BSF = {("mpegts", "aac"): ("aac_adtstoasc", None),
             ("mpegts", "h264"): (NO_AUD["h264"], "h264_mp4toannexb," + NO_AUD["h264"]),
             ("*", "h264"): (NO_AUD["h264"], NO_AUD["h264"]), ("*", "hevc"): (NO_AUD["hevc"], NO_AUD["hevc"]),
             ("*", "pcm_*"): ("pcm_rechunk=n=4096", "pcm_rechunk=n=4096")}
TIMED_TEXT = ("mov_text",)   # timed text mkvmerge turns into SubRip. The proof compares the text.
CAPTIONS = ("eia_608",)      # CEA-608 caption tracks (c608), which mkvmerge drops. They become SubRip.
CC_NAME = "English (CC)"     # the name of the SubRip track a caption track becomes
# a sidecar charset as mkvmerge and Python name it, the codepages of decide.CODEPAGES too
PY_CHARSET = {"UTF-16": "utf-16", "UTF-8": "utf-8-sig", **{cp: cp for cp in decide.CODEPAGES}}


def sidecar_text(raw, named=None):
    """(charset, text) of the bytes raw of a sidecar, the charset as mkvmerge names it (docs/design.md, "Subtitle text").
    It is UTF-16 by its BOM, and UTF-8 when the bytes decode as UTF-8. Else the first codepage of decide.CODEPAGES
    whose decode reads as a language it writes wins, see text_language(). The codepages that write named, the
    language code of the sidecar's name, go first. When no decode reads, the first of those codepages whose decode is
    plausible wins, see decide.plausible(), as for a short text or a language with no word list. Serbian in Latin
    letters then takes cp1250, and in Cyrillic cp1251. Else the bytes decode as cp1252, as they did before any codepage
    was read."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "UTF-16", raw.decode("utf-16", errors="replace")
    try:
        return "UTF-8", raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    code = named and (named if len(named) == 3 else checks.langs()[0].get(named.split("-")[0]))   # the 639-2 code of en or pt-br
    own = [cp for cp in decide.CODEPAGES if code and decide.lang_key(code) in decide.writes(cp)]
    for cp in own + [cp for cp in decide.CODEPAGES if cp not in own]:
        try:
            text = raw.decode(cp)
        except UnicodeDecodeError:
            continue
        if decide.reads_as([c[2] for c in srt_cues(text)], cp):
            return cp, text
    for cp in own:
        try:
            text = raw.decode(cp)
        except UnicodeDecodeError:
            continue
        if decide.plausible(" ".join(c[2] for c in srt_cues(text))):
            return cp, text
    return "cp1252", raw.decode("cp1252", errors="replace")


def sidecar_subs(path):
    """The .srt files beside path whose name starts with its base name, as {path, name, named, lang, flags, charset,
    end, ordered, read}. The first part after the base name is the language when it reads as a code (Movie.en.srt,
    Movie.eng.srt), and named holds it, else None. Else the language is the one its text reads as, and und when it reads as none, as for Movie.srt
    and Movie.1.srt. forced, hi, sdh and cc set a flag (Movie.en.forced.srt, Movie.en.hi.srt). The charset is that of
    sidecar_text(). end is where its last cue ends, in seconds. ordered says each cue starts no earlier than the one
    before, else mkvmerge warns. read is text_language() of its first cues, and cues is srt_cues() of its text."""
    folder, base = os.path.split(os.path.splitext(os.path.abspath(path))[0])
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith(base + ".") and n.lower().endswith(".srt"))
    except OSError:
        return []
    out = []
    for n in names:
        tags = [t for t in n[len(base) + 1:-4].lower().split(".") if t]
        named = tags[0] if tags and tags[0] not in SIDECAR_FLAGS and re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", tags[0]) else None
        with open(os.path.join(folder, n), "rb") as f:
            charset, text = sidecar_text(f.read(), named)
        cues, starts = srt_cues(text), [m.groups()[:4] for m in map(decide.SRT_TIME.match, text.splitlines()) if m]
        starts = [(int(h), int(m), int(s), int(f.ljust(3, "0")[:3])) for h, m, s, f in starts]
        read = decide.text_language(c[2] for c in cues)
        out.append(dict(path=os.path.join(folder, n), name=n, named=named, lang=named or read[0] or "und", flags=sorted({SIDECAR_FLAGS[t] for t in tags if t in SIDECAR_FLAGS}),
                        charset=charset, end=max((c[1] for c in cues), default=0) / 1000, ordered=starts == sorted(starts), read=read, cues=cues))
    return out


def srt_blocks(text, sep=" "):
    """[[start ms, end ms, text]] of the cues of a SubRip text in file order, each with its lines joined by sep. A
    block with no timing line is more text of the cue before it, as mkvmerge reads it."""
    ms = lambda h, m, s, f: int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(f.ljust(3, "0")[:3])
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip("\n\ufeff")):
        lines = block.split("\n")
        k = next((i for i, line in enumerate(lines[:2]) if decide.SRT_TIME.match(line)), None)
        if k is None:
            if cues:
                cues[-1][2] += sep + sep.join(lines)
            continue
        g = decide.SRT_TIME.match(lines[k]).groups()
        cues.append([ms(*g[:4]), ms(*g[4:8]), sep.join(lines[k + 1:])])
    return cues


def srt_cues(text, shift=0):
    """[(start ms, end ms, text)] of a SubRip text, sorted, for the proof. The text drops its tags and folds its
    whitespace, and an empty cue drops, see srt_blocks() and clean_cues(). shift is added to every time, in ms."""
    return clean_cues(srt_blocks(text), shift)


def clean_cues(blocks, shift=0):
    """srt_cues() of the cues of srt_blocks()."""
    return sorted((a + shift, b + shift, clean_text(t)) for a, b, t in blocks if clean_text(t))


def clean_text(t):
    """The text of a cue with no tags and its whitespace folded, as srt_cues() compares it."""
    # A tag holds no space: "July</i><font>." is "July.". ffmpeg writes a run of spaces in timed text as ASS hard spaces
    # (\h) and line breaks as \N, some of them only on one side.
    return " ".join(re.sub(r"\\[hNn]", " ", re.sub(r"<[^>]*>|\{\\[^}]*\}", "", t)).split())


def zero_cues(a, b):
    """(the cues b with each zero-length cue of a put back, the start in seconds of each one put back). a and b are
    srt_cues() of the original and the new file. An MP4 timed-text sample may start and end at the same time, beside
    another cue at that start. The MP4 never shows it, and mkvmerge gives it a length. It passes when the new cue has
    the same start and text and ends at or before the next later start. Cues pair by start and text, because the sort
    can put the two cues of one start the other way round. cue_fault() then checks every other cue."""
    b, back = list(b), []
    for s, e, text in a:
        if s != e:
            continue
        later = next((c[0] for c in a if c[0] > s), None)
        if later is None:
            continue
        k = next((i for i, c in enumerate(b) if abs(c[0] - s) <= 2 and c[2] == text and c[0] != c[1] and c[1] <= later + 2), None)
        if k is not None:
            b[k] = (s, e, text)
            back.append(s / 1000)
    return sorted(b), back


def cue_fault(what, a, b, last_end=True):
    """Why the cue lists a and b of srt_cues() differ, or None. A time may move by 2 ms, the rounding of two timescales.
    last_end False lets the end of the last cue differ: mkvmerge ends the last MP4 timed-text sample early, where the
    MP4 lets it run to the end of the track."""
    if len(a) != len(b):
        return f"{what} holds {len(b)} cues in the new file, {len(a)} in the original"
    for n, (x, y) in enumerate(zip(a, b), 1):
        if x[2] != y[2]:
            return f"{what} differs at {content.hms(x[0] / 1000)}: {x[2][:60]!r} against {y[2][:60]!r}"
        if abs(x[0] - y[0]) > 2 or (abs(x[1] - y[1]) > 2 and (last_end or n < len(a))):
            return (f"{what} times the cue {x[2][:40]!r} {y[0] / 1000:.3f} to {y[1] / 1000:.3f} s in the new file, "
                    f"{x[0] / 1000:.3f} to {x[1] / 1000:.3f} s in the original")
    return None


def ff_streams(path):
    """(ffprobe's format name, its streams, the format duration in seconds) of path. Raises when ffprobe reads no stream."""
    r = checks.run_bounded(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path], 120, capture_output=True, text=True,
                    errors="replace")
    d = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
    if not d.get("streams"):
        raise RuntimeError(f"ffprobe read no stream: {config.mask(r.stderr.strip())[-200:]}")
    f = d.get("format") or {}
    return f.get("format_name") or "", d["streams"], float(f.get("duration") or 0)


def secs(t, unit=""):
    """A time of a packet in seconds as a refusal names it, with unit, or "no time" when the read gave none."""
    return "no time" if t is None else f"{t:.3f}{unit}"


def media_streams(streams):
    """The video, audio and subtitle streams a conversion carries: every one but a cover picture."""
    return [s for s in streams if s.get("codec_type") in ("video", "audio", "subtitle") and not (s.get("disposition") or {}).get("attached_pic")]


class ReadFailed(RuntimeError):
    """ffmpeg did not read the file at path cleanly, see packet_hashes()."""
    def __init__(self, path, text):
        super().__init__(text)
        self.path = path


def packet_hashes(path, maps, bsf, texts, folder, timeout, raw=False, opts=(), nul=(), crlf=()):
    """One read of path through ffmpeg with -c copy, -copyinkf and -copyts. No stream is decoded, only timed text becomes
    SubRip. -copyinkf keeps the frames before the first keyframe, which a copy drops by default in both files.
    With raw True the text streams are SubRip already and their packets are written as they are, because ffmpeg's
    SubRip decoder drops a cue whose text starts with a line break. raw may also hold the stream indexes to write so.
    maps lists ffprobe stream indexes. bsf maps a stream index to the bitstream filter its packets go through first.
    opts are input options, such as -ignore_editlist 1. nul lists the stream indexes of maps of ASS or SSA streams
    whose packets change as the text round trip of remux.ended_track() changes them, before their md5 counts. Each
    packet loses one NUL byte at its very end, if it holds one. HandBrake ends each ASS event so, and mkvmerge drops
    that byte. A packet of one byte keeps it. Its ReadOrder becomes its rank, see read_ranks(), and its text loses the
    spaces and tabs at its end, as mkvmerge drops them. The same read writes the packets of each such stream as they
    are, and their bytes must hash to ffmpeg's md5s. crlf lists the stream indexes of
    maps whose packets count each CR LF as one LF before their md5 counts, in the same way. mkvmerge may store a line
    break of a SubRip cue either way, see remux.ended_track().
    Returns ({stream index: count (packets with data), empty (packets without), bytes, digest (sha256 over their md5s),
    start and end in seconds, times (the time of each packet with data, in file order), ends (its time plus its
    duration), md5s (their md5s, 16 bytes each) and sizes, for a stream of nul nul_cut, read_order and space_cut, the
    counts of packets that lost a NUL, took another ReadOrder and lost spaces at their end, and for a stream of crlf
    raw_md5s, the md5s of its packets as they are}, {stream index in texts: its SubRip text}). A timeout raises, and an
    ffmpeg error raises ReadFailed."""
    out, argv = os.path.join(folder, uuid.uuid4().hex), ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-copyts", *opts, "-i", path]
    srt = {i: os.path.join(folder, f"{uuid.uuid4().hex}.srt") for i in texts}
    if maps:
        argv += [a for i in maps for a in ("-map", f"0:{i}")] + ["-c", "copy", "-copyinkf"]   # frames before the first keyframe too
        argv += [a for k, i in enumerate(maps) if bsf.get(i) for a in (f"-bsf:{k}", bsf[i])] + ["-f", "framemd5", out]
    raw_of = {i: os.path.join(folder, f"{uuid.uuid4().hex}.bin") for i in (*nul, *crlf) if i in maps}   # the packets of a nul or crlf stream, as they are
    for i, f in raw_of.items():
        argv += ["-map", f"0:{i}", "-c", "copy", "-copyinkf", "-f", "data", f]
    for i, f in srt.items():
        argv += ["-map", f"0:{i}", "-c:s", "copy" if raw is True or i in (raw or ()) else "srt", "-f", "srt", f]
    r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    # ffmpeg decodes a few frames of each stream while it opens a file, and a decoder may complain there: "[mp3float @ ..]
    # Header missing", "[h264 @ ..] decode_slice_header error". ffprobe says the
    # same, and the packets still read. Any other line is a failed read.
    bad = [line for line in r.stderr.splitlines() if line.strip() and not re.match(r"\[(\w+) @ 0x[0-9a-f]+\]|\s+Last message repeated", line)
           or re.match(r"\[(matroska|mov|avi|mpegts|asf|mpeg|flv|webm|ipod|mp4|srt|framemd5|in#|out#|filter_units|aac_adtstoasc|"
                       r"h264_mp4toannexb|pcm_rechunk)\b", line)]
    if r.returncode or bad:
        raise ReadFailed(path, f"ffmpeg did not read {os.path.basename(path)} cleanly: {config.mask(chr(10).join(bad) or r.stderr.strip())[-200:]}")
    tb, stats, data, at = {}, {}, {}, {}
    for i, f in raw_of.items():
        with open(f, "rb") as fh:
            data[i], at[i] = fh.read(), 0
    rank = read_ranks(out, maps, {i: b for i, b in data.items() if i in nul}) if nul and maps else {}
    with open(out) if maps else contextlib.nullcontext([]) as lines:
        for line in lines:
            if line.startswith("#tb "):
                k, frac = line[4:].split(":")
                tb[int(k)] = int(frac.split("/")[0]) / int(frac.split("/")[1])
            elif line[:1].isdigit():
                p = [x.strip() for x in line.split(",")]
                k, dts, pts, dur, size = int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])
                pts = pts if abs(pts) < 1 << 62 else dts   # AVI video has a decode time only
                s = stats.setdefault(maps[k], {"count": 0, "empty": 0, "bytes": 0, "digest": hashlib.sha256(), "rest": hashlib.sha256(),
                                               "start": None, "end": None, "times": [], "ends": [], "md5s": bytearray(), "sizes": array.array("L"),
                                               **(dict(nul_cut=0, read_order=0, space_cut=0) if maps[k] in data and maps[k] in nul else {}),
                                               **({"raw_md5s": bytearray()} if maps[k] in data and maps[k] in crlf else {})})
                if size and maps[k] in data:   # the packet as it is, then as the text round trip gives it
                    b, at[maps[k]] = data[maps[k]][at[maps[k]]:at[maps[k]] + size], at[maps[k]] + size
                    if hashlib.md5(b).hexdigest() != p[5]:
                        raise ReadFailed(path, f"the packets ffmpeg wrote of stream {maps[k]} differ from the ones it hashed")
                    if maps[k] in nul:
                        c = b[:-1] if size > 1 and b.endswith(b"\0") else b
                        s["nul_cut"] += len(c) < size
                        if maps[k] in rank:
                            old, _, rest = c.partition(b",")
                            new = b"%d" % rank[maps[k]][int(old)]
                            c, s["read_order"] = new + b"," + rest, s["read_order"] + (new != old)
                        cut = c.rstrip(b" \t") or c
                        s["space_cut"] += len(cut) < len(c)
                        size, p[5] = len(cut), hashlib.md5(cut).hexdigest()
                    if maps[k] in crlf:
                        s["raw_md5s"] += bytes.fromhex(p[5])
                        lf = b.replace(b"\r\n", b"\n")
                        size, p[5] = len(lf), hashlib.md5(lf).hexdigest()
                if size:
                    s["but_last"], s["last"], s["last_pts"] = s["digest"].copy(), size, round(pts * tb[k], 3) if abs(pts) < 1 << 62 else None
                    if s["count"]:   # rest: every packet but the first, but_ends: every one but the first and the last
                        s["but_ends"] = s["rest"].copy(); s["rest"].update(p[5].encode())
                    else:
                        s["first"], s["first_pts"] = size, s["last_pts"]
                    s["count"] += 1; s["bytes"] += size; s["digest"].update(p[5].encode())
                    s["times"].append(pts * tb[k] if abs(pts) < 1 << 62 else None)
                    s["ends"].append((pts + dur) * tb[k] if abs(pts) < 1 << 62 else None)
                    s["md5s"] += bytes.fromhex(p[5]); s["sizes"].append(size)
                else:
                    s["empty"] += 1
                if abs(pts) < 1 << 62:   # AV_NOPTS_VALUE has no time
                    s["start"] = min(pts * tb[k], s["start"] if s["start"] is not None else float("inf"))
                    s["end"] = max(s["end"] or 0, (pts + dur) * tb[k])
    if any(at[i] != len(b) for i, b in data.items()):
        raise ReadFailed(path, "ffmpeg wrote more packet bytes of a stream than it hashed")
    for s in stats.values():
        s.update(digest=s["digest"].hexdigest(), start=round(s["start"] or 0, 3), end=round(s["end"] or 0, 3))
        s["but_last"] = s["but_last"].hexdigest() if "but_last" in s else None
        s["but_first"], s["but_ends"] = s.pop("rest").hexdigest(), s["but_ends"].hexdigest() if "but_ends" in s else None
    texts = {}
    for i, f in srt.items():
        with open(f, encoding="utf-8", errors="replace") as fh:
            texts[i] = fh.read()
    return stats, texts


def read_ranks(md5, maps, data):
    """{stream index: {ReadOrder: its rank}} of each ASS or SSA stream of data, {stream index: its packets as they
    are}, whose ReadOrders are unique integers. md5 is the framemd5 file of the read of maps that gives each packet's
    size. ReadOrder is the first field of each packet. mkvextract writes the events in ReadOrder order, and mkvmerge
    numbers the lines it reads from 0, so the text round trip of remux.ended_track() gives each event the rank of its
    ReadOrder. HandBrake may skip a ReadOrder, so its events then take new ones. A stream whose ReadOrders repeat, or
    hold anything but digits, gets no entry, and its packets must keep their ReadOrders."""
    sizes = {i: [] for i in data}
    with open(md5) as f:
        for line in f:
            if line[:1].isdigit():
                p = line.split(",")
                if maps[int(p[0])] in sizes and int(p[4]):
                    sizes[maps[int(p[0])]].append(int(p[4]))
    ranks = {}
    for i, n in sizes.items():
        ends = list(itertools.accumulate(n))
        heads = [data[i][e - k:e].partition(b",") for e, k in zip(ends, n)]
        orders = [int(h) for h, comma, _ in heads if comma and h.isdigit()]
        if len(orders) == len(heads) and len(set(orders)) == len(orders):
            ranks[i] = {x: r for r, x in enumerate(sorted(orders))}
    return ranks


def packet_text(srt, md5):
    """Why the SubRip file srt does not hold the packets that the framemd5 file md5 lists, byte for byte, or None.
    ffmpeg wrote both from one read of one stream, see remux.resub(). Each cue of srt is a number, a time line, the
    packet's bytes and a blank line. Each packet's bytes must hash to its md5, in file order, and no byte may be left."""
    with open(srt, "rb") as f:
        data = f.read()
    with open(md5) as f:
        packets = [(int(p[4]), p[5]) for p in (line.split(",") for line in f if line[:1].isdigit())]
    pos = 0
    for k, (size, digest) in enumerate(packets, 1):
        m = re.compile(rb"\d+\n[^\n]* --> [^\n]*\n").match(data, pos)
        if not m:
            return f"cue {k} has no time line"
        body, pos = data[m.end():m.end() + size], m.end() + size + 2
        if hashlib.md5(body).hexdigest() != digest.strip() or data[pos - 2:pos] != b"\n\n":
            return f"cue {k} differs from its packet"
    return f"{len(data) - pos} bytes follow the last packet" if pos != len(data) else None


def prove(src, tmp, subs, folder, captions=None, dropped=(), retimed=None, absolute=False, ended=None, timed=None, recoded=None):
    """(the refusal, or None; the proof per stream). The refusal is (stream, why): why the new file tmp is not a lossless
    copy of src with the sidecars subs, and the ffprobe index of the stream of src it refuses for its packet count or
    its packet data, else None. No stream is ever decoded.

    The k-th video, audio and subtitle stream of src pairs with the k-th of its kind in tmp, and each sidecar with one
    of the subtitle streams after them. A cover picture is no stream. A pair must keep its codec, its count of packets
    with data and a sha256 over the md5 of each packet, read with -c copy. Where mkvmerge changes packets on purpose, the
    original's packets go through the same bitstream filter first, PROOF_BSF. A packet with no data (an AVI drop frame)
    holds nothing, so it does not count. Timed text, and a sidecar, must keep every cue and its text, see srt_cues().
    Each stream must start where it started against the first video stream, within SYNC, and the video and the audio
    must end within 1 s of where they ended. Every packet must also keep its time against its stream's first packet,
    see times_fault(). Those checks run for every stream, whatever passed its data.

    Some differences pass. mkvmerge drops a cut last frame, the stream's last packet, when every
    other packet matches. The proof entry names it in dropped, with its time and size. An audio stream may also lose a
    cut first frame, alone or with its cut last frame. The entry names it in
    dropped_first, and the stream's start is then its second packet. mkvmerge may instead trim that fragment. A
    smaller first MP3 or MP2 packet passes when every other packet matches and the second packet keeps its time.
    The entry names both sizes in trimmed_first. The stream may also lose its cut last frame then, when the new packet
    0 is the tail of the old one after zeros or a stray header, see junk_head(). The new file may also hold one
    packet more, the sample an MP4's edit list cuts off. A read with -ignore_editlist must then match it, see
    edit_list_sample(), and the entry names it in edit_list. The time check leaves that one packet out. When the time
    check fails on a Matroska tmp, ffprobe reads the stored times of that stream again, see stored_times(), and only
    that second check decides. The entry names it in times.reread.

    A few more packets may go at the ends when every other packet matches in order, see ends_lost(): up to END_LOSS
    video packets at the end, and junk and cut frames at the ends of an audio stream. The entry names them in
    dropped_start and dropped_end. An HEVC packet 0 may also hold units of the codec header more, see header_units(),
    named in header_units. A zero-length timed-text cue may get a length, see zero_cues(), named in zero_length. An
    audio packet that shares its time with a neighbour may move one frame, see times_fault(). captions maps the stream index of
    a CEA-608 track to the SubRip file convert_captions() wrote. The track pairs with a subtitle stream after the kept
    ones, and its text must match.

    dropped holds the stream indexes of src that the remux left out on purpose, a subtitle that does not match the
    audio. retimed maps the k-th subtitle stream of src, from 0, to the fix of subsync.timing() that the remux
    applied: its packets must match, and its times are compared as the fix moves them, a time before 0 at 0. absolute
    holds each stream to its own start time too, within SYNC, for a remux that must move no stream. The other checks
    compare each start with the video's, so a remux that moved every stream would pass them. ended maps the k-th
    subtitle stream of src to the planned end of each of its packets, in seconds, from the flash fix: its packets and
    starts must match as before, and each end must be the planned one, moved by its fix when it is retimed too. timed
    maps the k-th subtitle stream of src to [(start, end)] of each of its packets in seconds, from a time plan of
    remux.time_plan(): its packets must match, and each start and end must be the planned one within TIME_SLACK. A
    stream in ended or timed whose original holds a cue with no duration is refused: its end is not known, and the
    plan's end for it is made up. An ASS stream in ended or timed comes back through mkvextract and mkvmerge, which
    drop the NUL byte HandBrake puts at the very end of each event. So each packet of the original that ends in a NUL
    is compared without that one byte, see packet_hashes(), and the entry counts those packets in nul_cut. mkvmerge
    also numbers the events from 0 in ReadOrder order and drops the spaces and tabs at the end of each event. So each
    packet of the original is compared with the rank of its ReadOrder in its place, when its stream's ReadOrders are
    unique integers, see read_ranks(), and without those spaces. The entry counts the packets whose ReadOrder changed
    in read_order and those that lost spaces in space_cut. A SubRip
    stream in ended or timed comes back through the same round trip, and mkvmerge may store each line break of a cue as
    LF or CRLF. So each packet of both files is compared with each CR LF as one LF, and the entry counts the packets
    whose line breaks changed in crlf. Any other difference of a byte still refuses. recoded maps the k-th subtitle
    stream of src, a SubRip track, to a function that gives the cues of the new text from srt_blocks() of the original's
    text, see remux.resub(). The proof compares text then: the new stream must hold those cues, and they must keep the
    original's count and times."""
    fa, sa, _ = ff_streams(src)
    fb, sb, _ = ff_streams(tmp)
    a, b, fam = [s for s in media_streams(sa) if s["index"] not in dropped], media_streams(sb), fa.split(",")[0]
    by_sub = lambda m: {s["index"]: m[k] for k, s in enumerate(s for s in a if s["codec_type"] == "subtitle") if k in m}
    moved, lengthened, planned, recoded = by_sub(retimed or {}), by_sub(ended or {}), by_sub(timed or {}), by_sub(recoded or {})
    nul = [s["index"] for s in a if s["codec_name"] in ("ass", "ssa") and (s["index"] in lengthened or s["index"] in planned)]   # see packet_hashes()
    crlf = [s["index"] for s in a if s["codec_name"] == "subrip" and (s["index"] in lengthened or s["index"] in planned)]
    pairs, proof = [], []
    captions = captions or {}
    for kind in ("video", "audio", "subtitle"):
        x, y = [s for s in a if s["codec_type"] == kind and s["index"] not in captions], [s for s in b if s["codec_type"] == kind]
        cc = [s for s in a if s["codec_type"] == kind and s["index"] in captions]   # after the kept tracks, as convert_cmd() orders them
        want = len(x) + len(cc) + (len(subs) if kind == "subtitle" else 0)
        if len(y) != want:
            return (None, f"the new file holds {len(y)} {kind} streams, not {want}"), proof
        pairs += [(s, t, None) for s, t in zip(x, y)] + [(s, t, captions[s["index"]]) for s, t in zip(cc, y[len(x):])]
        pairs += [(None, t, sc) for t, sc in zip(y[len(x) + len(cc):], subs)] if kind == "subtitle" else []
    for s, t, sc in pairs:
        if s and not sc and s["codec_name"] not in TIMED_TEXT and s["codec_name"] != t["codec_name"]:
            return (None, f"stream {s['index']} changed its codec from {s['codec_name']} to {t['codec_name']}"), proof
    packets = [(s, t) for s, t, sc in pairs if s and not sc and s["codec_name"] not in TIMED_TEXT and s["index"] not in recoded]
    text = [(s, t) for s, t, sc in pairs if s and not sc and (s["codec_name"] in TIMED_TEXT or s["index"] in recoded)]
    rule = lambda c: proof_bsf(fam, c)
    bsf = {s["index"]: rule(s["codec_name"]) for s, _ in packets if rule(s["codec_name"])}
    timeout = max(600, os.path.getsize(src) / PROOF_RATE)
    pool = concurrent.futures.ThreadPoolExecutor(2)   # both reads at once: the proof then takes about as long as the remux
    try:
        a_ = pool.submit(packet_hashes, src, [s["index"] for s, _ in packets], {i: f[0] for i, f in bsf.items() if f[0]},
                         [s["index"] for s, _ in text], folder, timeout, raw=set(recoded), **({"nul": nul} if nul else {}),
                         **({"crlf": crlf} if crlf else {}))
        b_ = pool.submit(packet_hashes, tmp, [t["index"] for _, t in packets], {t["index"]: bsf[s["index"]][1] for s, t in packets
                                                                                 if bsf.get(s["index"], ("", ""))[1]},
                         [t["index"] for _, t in text] + [t["index"] for s, t, sc in pairs if sc], folder, timeout, raw=True,
                         **({"crlf": [t["index"] for s, t in packets if s["index"] in crlf]} if crlf else {}))
        (ha, ta), (hb, tb) = a_.result(), b_.result()
    except BaseException:   # a stop or a failed read: no ffmpeg of the proof reads on
        cli.kill_children()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    none = {"count": 0, "empty": 0, "digest": "", "start": 0, "end": 0}
    video = lambda h, side: min(((h.get(p[side]["index"]) or none)["start"] for p in packets if p[0]["codec_type"] == "video"), default=0)
    va, vb, fault, stream = video(ha, 0), video(hb, 1), None, None
    for s, t in packets:
        x, y, name = ha.get(s["index"]) or none, hb.get(t["index"]) or none, f"{s['codec_type']} {s['index']}"
        f = bsf.get(s["index"]) or (None, None)
        proof.append(dict(stream=name, codec=s["codec_name"], method="packets" + (f", original through {f[0]}" if f[0] else "")
                          + (f", new file through {f[1]}" if f[1] else ""),
                          count=x["count"], **({"empty": x["empty"]} if x["empty"] else {}), **{c: x[c] for c in ("nul_cut", "read_order", "space_cut") if x.get(c)},
                          hash=x["digest"][:16],
                          match=(x["count"], x["digest"]) == (y["count"], y["digest"]), start=[round(x["start"] - va, 3), round(y["start"] - vb, 3)]))
        if proof[-1]["match"] and (n := sum(x["raw_md5s"][k:k + 16] != y["raw_md5s"][k:k + 16] for k in range(0, len(x.get("raw_md5s") or b""), 16))):
            proof[-1]["crlf"] = n   # packets whose line breaks changed between LF and CRLF, and nothing else
        xt, yt, xs, why, ycut, bad = x.get("times") or [], y.get("times") or [], x["start"], None, slice(None), None   # bad: refused for its packets
        if s["index"] in moved:   # the times the fix gives, as mkvmerge --sync wrote them
            fx = moved[s["index"]]
            to = lambda t: max(0, subsync.moved(t * 1000, fx)) / 1000
            xt, xs = [None if t is None else to(t) for t in xt], to(xs)
            proof[-1].update(retimed=fx, start=[round(xs - va, 3), proof[-1]["start"][1]])
        if s["index"] in planned:   # the times of the plan
            xt = [a for a, _ in planned[s["index"]]]
            xs = min(xt, default=xs)
            proof[-1]["start"][0] = round(xs - va, 3)
        if x["count"] == y["count"] + 1 and x.get("but_last") == y["digest"]:   # mkvmerge drops a cut last frame, and that passes
            proof[-1].update(dropped={"pts": x.get("last_pts"), "size": x["last"]})
            xt = xt[:-1]
        elif s["codec_type"] == "audio" and x["count"] - y["count"] in (1, 2) and \
                x.get("but_first" if x["count"] == y["count"] + 1 else "but_ends") == y["digest"]:   # a cut first audio frame too
            proof[-1].update(dropped_first={"pts": x.get("first_pts"), "size": x["first"]})
            if x["count"] == y["count"] + 2:
                proof[-1].update(dropped={"pts": x.get("last_pts"), "size": x["last"]})
            xt = xt[1:len(xt) - (x["count"] - y["count"] - 1)]
            xs = xt[0] if xt and xt[0] is not None else xs   # the stream now starts at its second packet
            proof[-1]["start"][0] = round(xs - va, 3)
        elif x["count"] + 1 == y["count"] and y.get("but_last") == x["digest"] and fam == "mov" and \
                edit_list_sample(src, s["index"], f[0], y, folder, timeout):   # the sample the MP4's edit list cuts off
            proof[-1].update(count=y["count"], edit_list={"pts": y.get("last_pts"), "size": y["last"]})
            ycut = slice(None, -1)
            yt = yt[ycut]
        elif x["count"] > y["count"] and (lost := ends_lost(src, x, y, s, f[0], timeout)):   # a few packets at the ends
            a, b, gone = lost
            proof[-1].update(**({"dropped_start": gone[:a]} if a else {}), **({"dropped_end": gone[a:]} if b else {}))
            xt = xt[a:len(xt) - b]
            if a:   # the stream now starts at its first kept packet
                xs = xt[0] if xt and xt[0] is not None else xs
                proof[-1]["start"][0] = round(xs - va, 3)
        elif s["codec_name"] == "hevc" and x["count"] == y["count"] and x["digest"] != y["digest"] and \
                x.get("but_first") == y.get("but_first") and (units := header_units(src, tmp, s["index"], t["index"], f, timeout)):
            proof[-1]["header_units"] = units   # mkvmerge copies units of the codec header into packet 0
        elif s["codec_name"] in ("mp3", "mp2") and x["count"] - y["count"] in (0, 1) and y["count"] > 1 and x["digest"] != y["digest"] and \
                x.get("but_first" if x["count"] == y["count"] else "but_ends") == y.get("but_first") and \
                (y.get("first") or 0) < (x.get("first") or 0) and \
                (x["count"] == y["count"] or (junk := junk_head(src, tmp, s["index"], t["index"], f, timeout))):   # a trimmed cut first frame
            proof[-1].update(trimmed_first={"pts": x.get("first_pts"), "size": [x["first"], y["first"]]})
            if x["count"] > y["count"]:   # junk inside packet 0, and a cut last frame mkvmerge drops
                proof[-1]["trimmed_first"]["junk"] = junk
                proof[-1].update(dropped={"pts": x.get("last_pts"), "size": x["last"]})
                xt = xt[:-1]
            # mkvmerge re-times the stream from the first frame it keeps. Junk longer than one frame would shift every
            # later packet, and the first packets still start together, so the second packet must keep its time.
            if None in (xt[1:2] or [None]) + (yt[1:2] or [None]):
                why = f"stream {name} ({s['codec_name']}) lost a trimmed first frame, and its second packet has no time"
            elif abs((xt[1] - va) - (yt[1] - vb)) > SYNC:
                why = (f"stream {name} ({s['codec_name']}) lost a trimmed first frame, and its second packet moved "
                       f"{((yt[1] - vb) - (xt[1] - va)) * 1000:+.0f} ms")
        elif x["count"] != y["count"]:
            why, bad = f"stream {name} ({s['codec_name']}) holds {y['count']} packets in the new file, {x['count']} in the original", s["index"]
        elif x["digest"] != y["digest"]:
            why, bad = f"the packet data of stream {name} ({s['codec_name']}) differ", s["index"]
        if not why and abs((xs - va) - (y["start"] - vb)) > SYNC:   # every stream, the exceptions included
            why = f"stream {name} starts {y['start'] - vb:+.3f} s from the video in the new file, {xs - va:+.3f} s in the original"
        if not why and absolute and abs(xs - y["start"]) > SYNC:
            why = f"stream {name} starts at {y['start']:.3f} s in the new file, {xs:.3f} s in the original"
        if not why and (s["index"] in lengthened or s["index"] in planned) and \
                (n := sum(t is not None and e == t for t, e in zip(x.get("times") or [], x.get("ends") or []))):   # no BlockDuration
            why = f"stream {name} holds {n} cue{'s' if n > 1 else ''} with no duration in the original, so {'their ends are' if n > 1 else 'its end is'} not known"
        if not why and s["index"] in lengthened:   # the flash fix: only the ends change, as planned
            fx = moved.get(s["index"])
            want = [max(0, subsync.moved(e * 1000, fx)) / 1000 if fx else e for e in lengthened[s["index"]]]
            cue = next((k for k, (e, g) in enumerate(zip(want, y.get("ends") or [])) if g is None or abs(e - g) > TIME_SLACK), None)
            if len(want) != len(y.get("ends") or []) or cue is not None:
                why = (f"stream {name} ends {len(y.get('ends') or [])} cues, and the plan {len(want)}" if cue is None else
                       f"stream {name} ends cue {cue + 1} at {secs(y['ends'][cue], ' s')}, and the plan at {want[cue]:.3f} s")
            proof[-1]["ended"] = len(want)
        if not why and s["index"] in planned:   # a time plan: each start and end as planned
            want, got = planned[s["index"]], list(zip(y.get("times") or [], y.get("ends") or []))
            cue = next((k for k, ((a, e), (p, q)) in enumerate(zip(want, got)) if None in (p, q) or abs(a - p) > TIME_SLACK or abs(e - q) > TIME_SLACK), None)
            if len(want) != len(got) or cue is not None:
                why = (f"stream {name} holds {len(got)} timed cues, and the plan {len(want)}" if cue is None else
                       f"stream {name} times cue {cue + 1} {secs(got[cue][0])} to {secs(got[cue][1], ' s')}, and the plan {want[cue][0]:.3f} to {want[cue][1]:.3f} s")
            proof[-1]["timed"] = len(want)
        if not why and fam == "avi" and s["codec_type"] == "audio":
            proof[-1]["times"] = {"checked": False, "why": "AVI keeps no audio times"}
        elif not why:
            video = (va, vb) if s["codec_type"] == "audio" else None   # packets that share a time, see times_fault()
            why, proof[-1]["times"] = times_fault(name, s["codec_name"], xt, yt, video)
            if why and fb.startswith("matroska"):   # ffmpeg's read may move a few stored times, see stored_times()
                why, proof[-1]["times"]["reread"] = times_fault(name, s["codec_name"], xt, stored_times(tmp, t["index"], timeout)[ycut], video)
        proof[-1]["match"] = not why
        if why and not fault:
            fault, stream = why, bad
    for s, t in text:
        what, cb = f"the {s['codec_name']} stream {s['index']}", srt_cues(tb[t["index"]], -round(vb * 1000))
        if s["index"] in recoded:   # the new text, with the original's times
            old = srt_blocks(ta[s["index"]])
            ca, was = clean_cues(recoded[s["index"]](old), -round(va * 1000)), clean_cues(old, -round(va * 1000))
            why = cue_fault(what, ca, cb) or cue_fault(f"the new text of {what}", [(a, b, "") for a, b, _ in was], [(a, b, "") for a, b, _ in ca])
            proof.append(dict(stream=f"s{s['index']}", codec=s["codec_name"], method="recoded text", count=len(ca), match=not why))
        else:
            ca = srt_cues(ta[s["index"]], -round(va * 1000))
            cb, zero = zero_cues(ca, cb)
            why = cue_fault(what, ca, cb, last_end=False)
            proof.append(dict(stream=f"s{s['index']}", codec=s["codec_name"], method="text", count=len(ca), match=not why,
                              **({"zero_length": zero} if zero else {})))
        fault = fault or why
    for s, t, sc in pairs:
        if sc:
            with open(sc.get("mux") or sc["path"], "rb") as f:
                ca = srt_cues(f.read().decode(PY_CHARSET[sc["charset"]], errors="replace"))
            what = f"the captions of stream {s['index']}" if s else f"the sidecar {sc['name']}"
            why = cue_fault(what, ca, srt_cues(tb[t["index"]]))
            proof.append(dict(stream=f"{s['codec_type']} {s['index']}" if s else sc["name"], codec=s["codec_name"] if s else "subrip",
                              method="caption text" if s else "sidecar text", count=len(ca), match=not why))
            fault = fault or why
    # The video and the audio must end where they ended. A subtitle may run on, so the container duration does not count.
    end = lambda h, side, v0: max(((h.get(p[side]["index"]) or none)["end"] - v0 for p in packets if p[0]["codec_type"] in ("video", "audio")),
                                  default=0)
    ea, eb = end(ha, 0, va), end(hb, 1, vb)
    if abs(eb - ea) > decide.REPAIR_END:
        fault = fault or f"the video and the audio end at {eb:.3f} s in the new file, {ea:.3f} s in the original"
    return ((stream, fault) if fault else None), proof


def proof_bsf(fam, codec):
    """The PROOF_BSF pair of a stream of codec in a file of the ffprobe format fam, or None."""
    return PROOF_BSF.get((fam, codec)) or PROOF_BSF.get(("*", codec)) or PROOF_BSF.get(("*", codec.split("_")[0] + "_*"))


# The messages that show a conversion's original damaged, by the read that logs them: the remux (mkvmerge), the
# proof's read of the original (ffmpeg) and ffprobe's first read. Each gives one fault text, so that regrab()'s second
# check can compare it. A proof refusal alone (an edit list, the times, the cues, a packet count) stays a refusal, and
# the original stays. So does a warning alone, see invalid_audio(). ffprobe reads no stream in a file it cannot open
# either, so its message must name a data error.
DAMAGE = {"mkvmerge": (r"This audio track contains \d+ bytes of invalid data which were skipped", "part of the audio cannot be read"),
          "ffmpeg": (r"NAL unit size|Invalid data found|partial file", "parts of the file cannot be read"),
          "ffprobe": (r"ffprobe read no stream: .*?(?:Invalid data found|moov atom not found|partial file)", "no track of the file can be read")}
SKIP_EDGE = 5   # seconds from either end of the file where an mkvmerge skip is end junk, such as zero bytes after the last frame


def damage_of(read, text):
    """{"read", "fault", "line"} when text, the output of that read of the original, holds its DAMAGE message, else
    None. line is the message from its start, without the path before it."""
    m = re.search(DAMAGE[read][0], text or "")
    return {"read": read, "fault": DAMAGE[read][1], "line": config.mask(text[m.start():].splitlines()[0])[:200]} if m else None


def invalid_audio(text, j, streams, dur):
    """{ffprobe index of an audio stream: the warning} for each mkvmerge warning in text that skipped invalid data in
    that audio track more than SKIP_EDGE seconds from either end of the file, dur seconds long. mkvmerge names the
    track by its id in j. The k-th audio track of j is the k-th audio stream of streams, as prove() pairs them. An
    unknown dur counts nothing."""
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "audio"]
    ff = [s["index"] for s in media_streams(streams) if s.get("codec_type") == "audio"]
    out = {}
    for m in re.finditer(rf"track (\d+): ({DAMAGE['mkvmerge'][0]} before timestamp (\d+):(\d+):(\d+(?:\.\d+)?)[^\n]*)", text or ""):
        at, k = int(m[3]) * 3600 + int(m[4]) * 60 + float(m[5]), ids.index(int(m[1])) if int(m[1]) in ids else len(ff)
        if k < len(ff) and dur and SKIP_EDGE < at < dur - SKIP_EDGE:   # no known dur: no skip is away from the ends
            out.setdefault(ff[k], config.mask(m[2])[:200])
    return out


def audio_damage(text, j, streams, dur, fault):
    """damage_of() for mkvmerge: its warning of invalid audio data counts only when the proof then refused that same
    audio stream for its packet count or its packet data, see invalid_audio(). The refusal is fault, see prove()."""
    index, why = fault
    line = invalid_audio(text, j, streams, dur).get(index)
    return {"read": "mkvmerge", "fault": DAMAGE["mkvmerge"][1], "line": line, "stream": index, "refusal": why[:200]} if line else None


def source_read(damage, path, j):
    """The read of a conversion's original that showed damage, run again from scratch on path, as damage_of() it. It
    writes nothing: mkvmerge remuxes into /dev/null, and ffmpeg reads the streams the way prove() reads them. j is the
    mkvmerge -J probe of path. mkvmerge must skip invalid data in the same audio stream away from the ends again. The
    proof's refusal stands from the conversion. Any other failure of the read is no damage."""
    read = damage["read"]
    try:
        fam, streams, _ = ff_streams(path)
        if read == "mkvmerge":
            r = subprocess.run(convert.convert_cmd(path, os.devnull, j, []), capture_output=True, text=True, errors="replace")
            dur = decide.duration(j) or checks.ffprobe_duration(path) or 0
            line = invalid_audio(r.stdout + r.stderr, j, streams, dur).get(damage["stream"])
            return dict(damage, line=line) if line else None
        if read == "ffmpeg":
            media = [s for s in media_streams(streams) if s.get("codec_name") not in CAPTIONS]
            text = [s["index"] for s in media if s["codec_name"] in TIMED_TEXT]
            maps = [s["index"] for s in media if s["index"] not in text]
            bsf = {s["index"]: f[0] for s in media if s["index"] in maps and (f := proof_bsf(fam.split(",")[0], s["codec_name"])) and f[0]}
            folder = runner.work_dir("damage")
            try:
                packet_hashes(path, maps, bsf, text, folder, max(600, os.path.getsize(path) / PROOF_RATE))
            finally:
                shutil.rmtree(folder, ignore_errors=True)
    except Exception as ex:
        return None if read == "mkvmerge" else damage_of(read, str(ex))
    return None


def damage_probe(damage, j):
    """probe() of regrab() for a damaged source, see source_read(). It judges only the job's own file: another file of
    the download shows its damage in its own conversion, and joins the unit then."""
    def probe(path, f, fresh=False):
        if f is not None:
            return None, {"skipped": "only its own conversion reads it for damage"}
        config.DEADLINE.stop()   # a read of the whole file, as the remux, has no time limit
        d = source_read(damage, path, j)
        return d and d["fault"], {"damage": d}
    return probe


def times_fault(name, codec, xt, yt, video=None):
    """(why the packet times of a stream moved in the new file, or None; the check for the proof entry). xt and yt are
    the times of the packets with data in file order, the one packet an exception left out already gone. Each list is
    sorted and taken against its own first time, and each pair may differ by TIME_SLACK. Sorted, because an AVI video
    stream has decode times only and mkvmerge writes presentation times: with B-frames the order changes, never the
    set. mkvmerge makes three changes on purpose, and all pass: it starts the file at 0, it rounds each time to the
    millisecond of Matroska's timestamp scale, and it writes presentation times for decode times. convert_cmd() turns
    lacing off: a lace stores one time for several audio frames, and ffmpeg then reads its frames up to 2 ms off. An AVI
    keeps no audio times at all: ffmpeg counts them from the bytes and the header's byte rate, mkvmerge from the samples,
    and the two drift apart over the length of a file. A player plays the audio on, as mkvmerge
    counts it, so prove() leaves AVI audio out of this check and keeps its start and its data. A TS
    whose audio times go back 0.3 s at 2 minutes plays 0.3 s late in the new file from there, because mkvmerge makes
    the audio continuous. That fails here.

    video is (the original's video start, the new file's) for an audio stream. When the check fails and packets of the
    original share a time with a neighbour, a second check measures every time against the video's start, as the start
    check does. A packet that shares a time may move one frame, the median step of the original, and TIME_SLACK more
    for the rounding. Every other packet may move TIME_SLACK. An MP4 can give its first two AAC packets one time, and mkvmerge spaces them one frame apart.
    The check then names the shared packets in shared."""
    if len(xt) != len(yt) or None in xt or None in yt:
        return (f"stream {name} ({codec}) has packets with no time", {"checked": False}) if None in xt + yt else \
            (f"stream {name} ({codec}) holds {len(yt)} timed packets in the new file, {len(xt)} in the original", {"checked": False})
    a, b = sorted(xt), sorted(yt)
    worst, at = 0.0, None
    for p, q in zip(a, b):
        d = abs((p - a[0]) - (q - b[0]))
        if d > worst:
            worst, at = d, p - a[0]
    check = {"worst_ms": round(worst * 1000, 1), **({"at": round(at, 3)} if at is not None and worst > TIME_SLACK else {})}
    same = {j for i in range(1, len(a)) if a[i] == a[i - 1] for j in (i - 1, i)} if worst > TIME_SLACK and video else set()
    if same and len(set(a)) > 1:
        steps = sorted(q - p for p, q in zip(a, a[1:]) if q > p)
        frame, moves = steps[len(steps) // 2], [abs((q - video[1]) - (p - video[0])) for p, q in zip(a, b)]
        check["shared"] = {"packets": len(same), "frame_ms": round(frame * 1000, 1), "worst_ms": round(max(moves) * 1000, 1)}
        if all(d <= (frame if i in same else 0) + TIME_SLACK for i, d in enumerate(moves)):
            return None, check
    if worst > TIME_SLACK:
        return (f"a packet of stream {name} ({codec}) moved {worst * 1000:.0f} ms against its stream's start, {at:.3f} s into the "
                "original"), check
    return None, check


def stored_times(path, index, timeout):
    """The time of each packet with data of stream index of the Matroska file path, in file order, as ffprobe reads it.
    Matroska stores no decode times, so ffmpeg guesses them from the display times. A frame stored far ahead of the
    frames it displays after breaks that guess. ffmpeg's muxer then logs "Non-monotonic DTS", which -v error hides,
    and moves a few packet times of its read. ffprobe only demuxes, so it reads the times the file stores. prove()
    runs this only after a failed time check. A timeout or an ffprobe error raises."""
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
                        "packet=pts_time,size", "-of", "csv=p=0", path], capture_output=True, text=True, errors="replace", timeout=timeout)
    if r.returncode:   # a decoder message of the stream probe may show, as in packet_hashes(). A wrong read fails the check.
        raise RuntimeError(f"ffprobe did not read the times of {os.path.basename(path)}: {config.mask(r.stderr.strip())[-200:]}")
    rows = [line.split(",") for line in r.stdout.splitlines() if line.strip()]
    return [None if t == "N/A" else float(t) for t, size in rows if size != "0"]


def edit_list_sample(src, index, bsf, y, folder, timeout):
    """Whether stream index of the MP4 src holds every packet y counted, the one after the last one ffmpeg reads
    included. The MP4's edit list ends the stream before its last sample, ffmpeg's read honours that, and mkvmerge
    keeps the sample. A second read of this stream with -ignore_editlist must then
    give the new file's count and digest. It reads the whole file again, in this case only."""
    z = packet_hashes(src, [index], {index: bsf} if bsf else {}, [], folder, timeout, opts=("-ignore_editlist", "1"))[0].get(index) or {}
    return (z.get("count"), z.get("digest")) == (y["count"], y["digest"])


def packet_data(path, index, bsf, n, timeout):
    """The data of the first n packets of stream index of path, read with -c copy through bsf, joined. The caller splits
    it by the sizes of its own read. It reads only the start of the file. A timeout or an ffmpeg error raises."""
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{index}", "-c", "copy",
                        "-copyinkf", *(["-bsf:0", bsf] if bsf else []), "-frames:0", str(n), "-f", "data", "pipe:1"], capture_output=True,
                       timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ffmpeg did not read the first packets of {os.path.basename(path)}: "
                           f"{config.mask(r.stderr.decode(errors='replace').strip())[-200:]}")
    return r.stdout


def ends_lost(src, x, y, s, bsf, timeout):
    """(packets lost at the start, packets lost at the end, [{pts, size, kind}] of each) when the new file holds every
    other packet of stream s of src in order, else None. x and y are the packet_hashes() of the stream, bsf the
    original's filter. The one-packet cuts pass in prove() before this.

    A video stream may lose up to END_LOSS packets at its end, kind "lost". An audio stream may lose up to JUNK_MAX
    packets: junk at its start, zero packets at its end, and one cut frame next to the kept packets at each end. Junk
    is a packet of zero bytes, kind "zero", or a stray container header of JUNK_HEADERS, kind "header", which a read of
    those first packets shows. A cut frame is kind "cut". prove() then checks the start and the times of the kept
    packets, so audio that moved still fails."""
    xm, ym, sizes, times = x.get("md5s"), y.get("md5s"), x.get("sizes"), x.get("times") or []
    video = s["codec_type"] == "video"
    if xm is None or ym is None or s["codec_type"] not in ("video", "audio"):
        return None
    n, m = len(xm) // 16, len(ym) // 16
    if not 0 < n - m <= (END_LOSS if video else JUNK_MAX):
        return None
    zero = lambda i: xm[16 * i:16 * i + 16] == hashlib.md5(bytes(sizes[i])).digest()
    entry = lambda i, kind: {"pts": round(times[i], 3) if i < len(times) and times[i] is not None else None, "size": sizes[i], "kind": kind}
    data = None
    for a in range(1 if video else n - m + 1):
        b = n - m - a
        if memoryview(xm)[16 * a:16 * (a + m)] != ym:
            continue
        if video:
            return 0, b, [entry(i, "lost") for i in range(n - b, n)]
        lead, tail = list(range(a)), list(range(n - b, n))
        if not all(zero(i) for i in tail[1:]):   # after the cut last frame, only zero packets
            continue
        heads = [i for i in lead[:-1] if not zero(i)]   # before the cut first frame, only junk
        if any(sizes[i] > JUNK_HEADER_MAX for i in heads):
            continue
        if heads and data is None:   # one read of the most packets any split can lose at the start
            data = packet_data(src, s["index"], bsf, n - m, timeout)
        start = lambda i: sum(sizes[:i])
        if not all(data[start(i):start(i) + sizes[i]].startswith(JUNK_HEADERS) for i in heads):
            continue
        kind = lambda i, cut: "zero" if zero(i) else "header" if i in heads else cut
        return a, b, [entry(i, kind(i, "cut")) for i in lead] + [entry(i, kind(i, "cut")) for i in tail]
    return None


def junk_head(src, tmp, i, k, bsf, timeout):
    """"zero" or "header" when packet 0 of stream k of tmp is the tail of packet 0 of stream i of src, and the part
    before it is zeros or a stray header of JUNK_HEADERS of at most JUNK_HEADER_MAX bytes, else None. The first audio
    packet can hold such junk in any container, and mkvmerge keeps only the frame after it. The measured files are AVIs.
    One packet of each file is read."""
    old, new = packet_data(src, i, bsf[0], 1, timeout), packet_data(tmp, k, bsf[1], 1, timeout)
    head = old[:len(old) - len(new)]
    if not new or not head or not old.endswith(new):
        return None
    return "zero" if not head.strip(b"\0") else "header" if head.startswith(JUNK_HEADERS) and len(head) <= JUNK_HEADER_MAX else None


def annexb_units(data):
    """The NAL units of an H.264 or HEVC packet in Annex B form, without their start codes."""
    return [u.rstrip(b"\0") for u in re.split(b"\0\0\1", data)[1:]] if data.startswith((b"\0\0\1", b"\0\0\0\1")) else []


def hvcc_units(path, index, timeout):
    """The NAL units in the HEVC codec header (hvcC) of stream index of path, from ffprobe's dump of it."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries", "stream=extradata", "-show_data", "-of", "json",
                        path], capture_output=True, text=True, errors="replace", timeout=timeout)
    dump = ((json.loads(r.stdout or "{}").get("streams") or [{}])[0].get("extradata") or "") if r.returncode == 0 else ""
    raw, units = bytes.fromhex("".join("".join(line[10:49].split()) for line in dump.splitlines() if line.strip())), []
    if len(raw) < 23 or raw[0] != 1:   # configurationVersion 1, see ISO/IEC 14496-15
        return units
    pos = 23
    for _ in range(raw[22]):   # numOfArrays: the NAL type, then numNalus units, each after its 2-byte length
        count, pos = int.from_bytes(raw[pos + 1:pos + 3], "big"), pos + 3
        for _ in range(count):
            size = int.from_bytes(raw[pos:pos + 2], "big")
            units.append(raw[pos + 2:pos + 2 + size])
            pos += 2 + size
    return units


def header_units(src, tmp, i, k, bsf, timeout):
    """The hex of each NAL unit that packet 0 of stream k of tmp holds more than packet 0 of stream i of src, when each
    one is a unit of the original's HEVC codec header, byte for byte, and packet 0 of src is in the new packet 0 in
    order. Else None. mkvmerge copies the header's units into packet 0, and bsf removes only the parameter sets.
    prove() reads one packet of each file, and only when every other packet matches."""
    old = annexb_units(packet_data(src, i, bsf[0], 1, timeout))
    new = annexb_units(packet_data(tmp, k, bsf[1], 1, timeout))
    header, left, more = hvcc_units(src, i, timeout), iter(old), []
    want = next(left, None)
    for u in new:
        if u == want:
            want = next(left, None)
        elif u in header:
            more.append(u.hex()[:64])
        else:
            return None
    return more if old and want is None and more else None
