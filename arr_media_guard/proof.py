# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The packet proof of a conversion and the damage checks of its original."""
import array, collections, concurrent.futures, contextlib, fractions, hashlib, heapq, itertools, json, os, re, shutil, subprocess, threading, uuid

from . import checks, cli, config, content, convert, decide, ff, matroska, mp4, runner, subsync, ts


PROOF_RATE = 10e6         # bytes a second a proof read must reach at least, else it times out
SYNC = 0.05               # seconds a stream's start may move against the first video stream's
TS_WRAP = (1 << 33) / 90000   # seconds a TS PTS counts before it wraps
OVERLAP_MOVE = 0.2        # seconds an audio frame may move later where the original's blocks overlap, see times_fault().
                          # The player measurement of such a remux covered moves up to 150 ms.
CLOSE_MOVE = OVERLAP_MOVE  # seconds a remux moves audio frames later, at most, to close such an overlap, see remux.planned_times().
                          # The owner picked this on 2026-10-21. With OVERLAP_MOVE 0, every frame keeps its time. With
                          # float("inf") here, every overlap closes, as mkvmerge closes it on its own, and the proof refuses a
                          # move past OVERLAP_MOVE.
TIME_SLACK = 0.002        # seconds a packet's time may move against its stream's start when ffmpeg reads the original, see
                          # mkv_slack() for two Matroska files
LAG_SLACK = 1.0           # seconds a stream may sit further behind the other streams in the new file than in the original, see lags().
                          # A header repair, remux.header_fault(), takes it as an absolute bound.
END_LOSS = 3              # video packets the new file may lack at its end, a loss of up to 3 frames that is accepted
JUNK_MAX = 16             # audio packets the ends of a stream may lose as junk and cut frames, see ends_lost()
LEAD_LOSS = 0.1           # seconds of the original an audio stream may lose at its start, whatever the packets hold
JUNK_HEADERS = (b"RIFF",)  # a stray container header that an audio stream may hold as a packet of its own
JUNK_HEADER_MAX = 128     # bytes such a header packet holds at most
SIDECAR_FLAGS = {"forced": "--forced-display-flag", "hi": "--hearing-impaired-flag", "sdh": "--hearing-impaired-flag",
                 "cc": "--hearing-impaired-flag"}
# Where mkvmerge changes packets on purpose, per ffprobe format and codec, "*" for any format: (the bitstream filter for
# the original's packets, the one for the new file's), so both hash the same bytes. mkvmerge strips the ADTS header of
# AAC, drops the access unit delimiters and filler data of H.264, puts the HEVC parameter sets in front of each keyframe
# and drops its delimiters, and stores H.264 from a transport stream with length prefixes. Filler carries no picture.
# filter_units rewrites every unit it parses, so it runs on both sides. The parameter sets it takes out of HEVC
# packets stay in the codec private data of both files. mkvmerge keeps MPEG-4 packed B-frames packed, so MPEG-4 part 2
# needs no filter. PCM has no frames, and mkvmerge cuts it into other packets, so both sides are cut again into packets
# of 4,096 samples ("pcm_*").
NO_AUD = {"h264": "filter_units=remove_types=9|12", "hevc": "filter_units=remove_types=32-35"}
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
    whose decode reads as a language it writes wins, see decide.reads_as(). The codepages that write named, the
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
    r = checks.run_bounded(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path], 120, use="proof.ff_streams",
                           capture_output=True, text=True, errors="replace")
    d = ff.loads(r, "{}") if r.returncode == 0 else {}
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


# A line of a subtitle stream's decoder, as ffmpeg 7 names it: "[sist#0:2/mov_text @ 0x..] [dec:mov_text @ 0x..] Error
# decoding subtitles: Invalid data found when processing input". Timed text that is not UTF-8 prints it, and its read
# exits 0, or 69 when most cues fail. The video and the audio still read, so packet_hashes() never counts it as damage.
SUB_DECODER = re.compile(r"\[sist#")


class ReadFailed(RuntimeError):
    """ffmpeg did not read the file at path cleanly, see packet_hashes()."""
    def __init__(self, path, text):
        super().__init__(text)
        self.path = path


def bad_lines(stderr):
    """The lines of the stderr of an ffmpeg read with -v error that show a failed read. ffmpeg decodes a few frames of
    each stream while it opens a file, and a decoder may complain there: "[mp3float @ ..] Header missing", "[h264 @ ..]
    decode_slice_header error". ffprobe says the same, and the packets still read. Any other line is a failed read."""
    return [line for line in stderr.splitlines() if line.strip() and not re.match(r"\[(\w+) @ 0x[0-9a-f]+\]|\s+Last message repeated", line)
            or re.match(r"\[(matroska|mov|avi|mpegts|asf|mpeg|flv|webm|ipod|mp4|srt|framemd5|in#|out#|filter_units|aac_adtstoasc|"
                        r"h264_mp4toannexb|pcm_rechunk)\b", line)]


SPU_TICK = 1024 / 90000   # seconds of one unit of the delay of a VobSub control sequence


def spu_masked(b):
    """(the VobSub packet b with the delay of each control sequence set to 0, [those delays in SPU_TICK units]). A
    packet holds its size, the offset of its first control sequence, the image, then the sequences. Each one starts
    with its 2-byte delay, then the offset of the next one, its own for the last. The last one stops the cue. mkvmerge
    sets the stop delay from the block's duration, so a ratio or a cue moved before 0 changes it, see prove()."""
    b, at, seen, delays = bytearray(b), int.from_bytes(b[2:4], "big"), set(), []
    while at + 4 <= len(b) and at not in seen:
        seen.add(at)
        delays.append(int.from_bytes(b[at:at + 2], "big"))
        b[at:at + 2] = b"\0\0"
        at = int.from_bytes(b[at + 2:at + 4], "big")
    return bytes(b), delays


def packet_hashes(path, maps, bsf, texts, folder, timeout, raw=False, opts=(), nul=(), crlf=(), end_nul=(), spu=()):
    """One read of path through ffmpeg with -c copy, -copyinkf and -copyts. No stream is decoded, only timed text becomes
    SubRip. -copyinkf keeps the frames before the first keyframe, which a copy drops by default in both files.
    With raw True the text streams are SubRip already and their packets are written as they are, because ffmpeg's
    SubRip decoder drops a cue whose text starts with a line break. raw may also hold the stream indexes to write so.
    maps lists ffprobe stream indexes. bsf maps a stream index to the bitstream filter its packets go through first.
    opts are input options, such as -ignore_editlist 1. nul lists the stream indexes of maps of ASS or SSA streams
    whose packets change as the text round trip of remux.ended_track() changes them, before their md5 counts. Each
    packet loses one NUL byte at its very end, if it holds one. HandBrake ends each ASS event so, and mkvmerge drops
    that byte. A packet of one byte keeps it. Its ReadOrder becomes its rank, see read_ranks(). The same read writes
    the packets of each such stream as they
    are, and their bytes must hash to ffmpeg's md5s. end_nul lists ASS or SSA streams of maps whose packets only lose
    that NUL byte, as mkvmerge drops it from a track it copies, see remux.resub(). spu lists VobSub streams of maps
    whose packets count as spu_masked() gives them, their delays in spu. crlf lists the stream indexes of
    maps whose packets count each CR LF as one LF before their md5 counts, in the same way. mkvmerge may store a line
    break of a SubRip cue either way, see remux.ended_track().
    Returns ({stream index: count (packets with data), empty (packets without), bytes, digest (sha256 over their md5s),
    start and end in seconds, times (the time of each packet with data, in file order), ends (its time plus its
    duration), md5s (their md5s, 16 bytes each) and sizes, for a stream of nul or end_nul nul_cut and read_order, the
    counts of packets that lost a NUL and took another ReadOrder, and for a stream of crlf
    raw_md5s, the md5s of its packets as they are}, {stream index in texts: its SubRip text}). A timeout raises, and an
    ffmpeg error raises ReadFailed. A read whose only errors come from a subtitle decoder raises RuntimeError, see
    SUB_DECODER."""
    out, argv = os.path.join(folder, uuid.uuid4().hex), ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-copyts", *opts, "-i", path]
    srt = {i: os.path.join(folder, f"{uuid.uuid4().hex}.srt") for i in texts}
    if maps:
        argv += [a for i in maps for a in ("-map", f"0:{i}")] + ["-c", "copy", "-copyinkf"]   # frames before the first keyframe too
        argv += [a for k, i in enumerate(maps) if bsf.get(i) for a in (f"-bsf:{k}", bsf[i])] + ["-f", "framemd5", out]
    raw_of = {i: os.path.join(folder, f"{uuid.uuid4().hex}.bin") for i in (*nul, *end_nul, *spu, *crlf) if i in maps}   # those packets as they are
    for i, f in raw_of.items():
        argv += ["-map", f"0:{i}", "-c", "copy", "-copyinkf", "-f", "data", f]
    for i, f in srt.items():
        argv += ["-map", f"0:{i}", "-c:s", "copy" if raw is True or i in (raw or ()) else "srt", "-f", "srt", f]
    r = ff.run("proof.packet_hashes", argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    bad = bad_lines(r.stderr)
    real = [line for line in bad if not SUB_DECODER.match(line)]
    if bad and not real:   # only the text of a subtitle failed to decode, which shows no damage, see DAMAGE
        streams = sorted({int(n) for line in bad for n in re.findall(r"sist#\d+:(\d+)", line)})
        raise RuntimeError(f"ffmpeg could not read the text of subtitle stream {', '.join(map(str, streams))} of {os.path.basename(path)}")
    if r.returncode or bad:
        raise ReadFailed(path, f"ffmpeg did not read {os.path.basename(path)} cleanly: {config.mask(chr(10).join(real) or r.stderr.strip())[-200:]}")
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
                                               **(dict(nul_cut=0, read_order=0) if maps[k] in data and maps[k] in (*nul, *end_nul) else {}),
                                               **({"raw_md5s": bytearray()} if maps[k] in data and maps[k] in crlf else {}),
                                               **({"spu": []} if maps[k] in data and maps[k] in spu else {})})
                if size and maps[k] in data:   # the packet as it is, then as the text round trip gives it
                    b, at[maps[k]] = data[maps[k]][at[maps[k]]:at[maps[k]] + size], at[maps[k]] + size
                    if hashlib.md5(b).hexdigest() != p[5]:
                        raise ReadFailed(path, f"the packets ffmpeg wrote of stream {maps[k]} differ from the ones it hashed")
                    if maps[k] in spu:
                        c, delays = spu_masked(b)
                        p[5] = hashlib.md5(c).hexdigest()
                        s["spu"].append(delays)
                    if maps[k] in end_nul:
                        c = b[:-1] if size > 1 and b.endswith(b"\0") else b
                        s["nul_cut"] += len(c) < size
                        size, p[5] = len(c), hashlib.md5(c).hexdigest()
                    if maps[k] in nul:
                        c = b[:-1] if size > 1 and b.endswith(b"\0") else b
                        s["nul_cut"] += len(c) < size
                        if maps[k] in rank:
                            old, _, rest = c.partition(b",")
                            new = b"%d" % rank[maps[k]][int(old)]
                            c, s["read_order"] = new + b"," + rest, s["read_order"] + (new != old)
                        size, p[5] = len(c), hashlib.md5(c).hexdigest()
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


def event_text(path, md5, raw, codec):
    """Why the file path, which mkvextract wrote of an ASS, SSA or WebVTT track, does not hold the bytes of each packet
    that one ffmpeg read wrote into the framemd5 file md5 and, as they are, into the file raw, or None, see
    remux.resub(). Each packet's bytes must hash to its md5, and no byte may be left. mkvextract writes each event
    again with its times, so the file must hold the text of every packet, byte for byte: the fields of an ASS or SSA
    event after its Layer, and the payload of a WebVTT cue. A line break counts as LF."""
    with open(md5) as f:
        sizes = [(int(p[4]), p[5].strip()) for p in (line.split(",") for line in f if line[:1].isdigit())]
    with open(raw, "rb") as f:
        data = f.read()
    packets, pos = [], 0
    for k, (size, digest) in enumerate(sizes, 1):
        packets.append(data[pos:pos + size])
        pos += size
        if hashlib.md5(packets[-1]).hexdigest() != digest:
            return f"packet {k} differs from its md5"
    if pos != len(data):
        return f"{len(data) - pos} bytes follow the last packet"
    with open(path, "rb") as f:
        text = f.read().replace(b"\r\n", b"\n")
    if codec == "S_TEXT/WEBVTT":
        want, have = [b.replace(b"\r\n", b"\n") for b in packets], []
        for block in re.split(rb"\n\n+", text):
            lines = block.strip(b"\n").split(b"\n")
            k = next((n for n, line in enumerate(lines[:2]) if b" --> " in line), None)
            have += [] if k is None else [b"\n".join(lines[k + 1:])]
    else:   # a packet is "ReadOrder,Layer,Style,...,Text", and a line "Dialogue: Layer,Start,End,Style,...,Text"
        want = [b.split(b",", 2)[-1].rstrip(b"\0") for b in packets]
        have = [line.split(b",", 3)[-1] for line in text.split(b"\n") if line.startswith(b"Dialogue:")]
    return None if sorted(want) == sorted(have) else "the extracted text does not hold the bytes of each packet"


def timed_packets(path, indexes, streams=None):
    """{stream index: [(start, end in seconds, the bytes of its text)]} of the MP4 timed-text streams indexes of path.
    streams is ff_streams()' list, read here when it is None. Its id names each stream's track_ID. The times are the sample tables' as the edit
    list presents them, see mp4.track(), and a sample the edit list cuts gives no entry. Each sample's bytes are read
    at its byte in the file, so no text is decoded. Each sample holds a 2-byte length, the text in that many bytes,
    then style boxes, which go, as mkvmerge drops them. A sample with no text gives no entry. A read that fails raises
    RuntimeError, and so does a sample shorter than its length."""
    name = os.path.basename(path)
    r = mp4.samples(path)
    if r["error"]:
        raise RuntimeError(f"the sample tables of {name} do not read: {r['error']}")
    streams = ff_streams(path)[1] if streams is None else streams
    tracks, ids, out = {t["id"]: t for t in r["tracks"]}, {s["index"]: int(s.get("id") or "0", 16) for s in streams}, {}
    with open(path, "rb") as f:
        for i in indexes:
            if (t := tracks.get(ids.get(i))) is None:
                raise RuntimeError(f"ffprobe names no track of the moov of {name} for stream {i}")
            out[i] = []
            for k in range(len(t["time"])):
                if t["tag"][k] == mp4.CUT or not t["size"][k]:
                    continue
                f.seek(t["place"][k])
                b = f.read(t["size"][k])
                n = int.from_bytes(b[:2], "big")
                if len(b) < 2 + n:
                    raise RuntimeError(f"a timed-text sample of stream {i} at {t['time'][k] / 1e9:.3f} s holds {len(b)} bytes, less than its text")
                if n:
                    out[i].append((t["time"][k] / 1e9, (t["time"][k] + t["dur"][k]) / 1e9, b[2:2 + n]))
    return out


def timed_srt(cues, shift=0.0):
    """The SubRip bytes of the cues of timed_packets(), each time moved by shift seconds and kept to the millisecond.
    The number lines, the time lines and the blank lines are ASCII. Each cue holds the bytes of its packet's text as
    they are, see remux.keep_broken()."""
    stamp = lambda t: (lambda v: b"%02d:%02d:%02d,%03d" % (v // 3600000, v // 60000 % 60, v // 1000 % 60, v % 1000))(max(0, round((t + shift) * 1000)))
    return b"".join(b"%d\n%s --> %s\n%s\n\n" % (k, stamp(a), stamp(e), t) for k, (a, e, t) in enumerate(cues, 1))


def utf8(b):
    """Whether the bytes b are valid UTF-8."""
    try:
        b.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def packet_places(path, folder, timeout, clean=False):
    """{stream index: (times, places, durations)} of path from one ffprobe read, each an array per packet with data,
    in file order: the time and the duration in seconds (NaN for none) and the file position (-1 for none). prove()
    reads a Matroska file with stored_places() instead, because ffmpeg estimates the times of the frames of a lace.
    Matroska stores no decode times, so ffmpeg guesses them from the display times. A frame stored far ahead of the frames it
    displays after breaks that guess. ffmpeg's muxer then logs "Non-monotonic DTS", which -v error hides, and moves a
    few packet times of packet_hashes(). The same muxer moves a time that a file stores lower than the time before it
    in the file. ffprobe only demuxes, so it reads the times the file stores. A packet with no display time takes its
    decode time: a VC-1 track in VfW mode stores decode times, and ffprobe reads no display time there. The lines are
    read as they come, into arrays, so a film of a million packets holds about 24 MB. A timeout or an ffprobe error
    raises. A decoder message of the stream probe may show, as in packet_hashes(), so only the exit code counts. With
    clean, any other line of stderr raises too, see bad_lines(). ffprobe reads past damage that mkvmerge drops, and it
    names the damage there. A packet with side data ends its line with one more field. stderr goes to a file in
    folder."""
    argv = ["ionice", "-c3", "nice", "-n", "19", "ffprobe", "-v", "error", "-show_entries",
            "packet=stream_index,pts_time,dts_time,duration_time,size,pos", "-of", "csv=p=0", path]
    out, nan, late = {}, float("nan"), []
    with open(os.path.join(folder, f"{uuid.uuid4().hex}.err"), "w+") as err, \
            ff.popen("proof.packet_places", argv, stdout=subprocess.PIPE, stderr=err, text=True, errors="replace") as r:
        timer = threading.Timer(timeout, lambda: late.append(r.kill()))
        timer.start()
        try:
            for line in r.stdout:
                p = line.split(",")
                if len(p) >= 6 and p[4] not in ("0", "N/A"):
                    times, places, durations = out.get(int(p[0])) or out.setdefault(int(p[0]), (array.array("d"), array.array("q"), array.array("d")))
                    t = p[1] if p[1] != "N/A" else p[2]
                    times.append(nan if t == "N/A" else float(t))
                    places.append(-1 if p[5].strip() == "N/A" else int(p[5]))
                    durations.append(nan if p[3] == "N/A" else float(p[3]))
            code = r.wait()
        finally:
            timer.cancel()
        if late:
            raise subprocess.TimeoutExpired(argv, timeout)
        err.seek(0)
        text = err.read()
        bad = bad_lines(text) if clean and not code else []
        if code or bad:
            raise RuntimeError(f"ffprobe did not read the packets of {os.path.basename(path)}: {config.mask((bad[0] if bad else text).strip())[-200:]}")
    return out


STORED = ("matroska", "mov", "mpegts")   # the ffprobe formats whose times a reader of AMG gives, see stored_places()


def stored_places(path, streams, fam="matroska", folder=None, timeout=600, whole_ok=False):
    """{stream index: (times, places, durations, info)} of path from the reader of its container, in the shape of
    packet_places(): the time and the duration in seconds of each frame with data (NaN when it is not known) and its
    byte, in file order (docs/design.md, "Time reads"). fam is ffprobe's format name: Matroska reads with
    matroska.frames(), MP4 and QuickTime ("mov") with mp4.samples(), MPEG-TS and M2TS with ts.frames(). info holds the
    tick of the file's times in ns (scale), the seconds of one frame or None (frame), [count, worst s, first s] of the
    blocks that start before the block before them ends (overlaps), or None, the stored time in seconds of each such
    block (lapped), the count of frames whose time is not known (unknown), whether the last frame in time order has no
    duration (unended), and exact, True when the container stores exact times that a Matroska copy rounds, see
    mkv_slack(). An MP4 also gives shift, see start_plan(). An empty Matroska frame is a block too, so a frame with data
    before it has an end. PCM has no frames. Both files cut it again into packets of
    4096 samples (PROOF_BSF), and ffmpeg's pcm_rechunk times each one at the stored time of its block plus its sample
    offset. So prove() keeps those times for PCM, and only the info counts. Raises RuntimeError when the read fails,
    or when it finds other tracks than ffprobe. With whole_ok, a Matroska walk that stopped after it read every
    Cluster whole counts, see matroska.frames()."""
    if fam.startswith("mov"):
        return mp4_places(path, streams, folder, timeout)
    if fam.startswith("mpegts"):
        return ts_places(path, streams)
    try:
        r = matroska.frames(path)
        if r["error"] and not (whole_ok and r.get("whole")):
            raise ValueError(r["error"])
    except (OSError, ValueError) as ex:
        raise RuntimeError(f"the block walk of {os.path.basename(path)} failed: {ex}") from ex
    kinds = {1: "video", 2: "audio", 17: "subtitle"}
    tracks = [t for t in r["tracks"] if t["type"] in kinds and t["codec"]]
    media = sorted(media_streams(streams), key=lambda s: s["index"])
    if [kinds[t["type"]] for t in tracks] != [s["codec_type"] for s in media]:
        raise RuntimeError(f"the block walk of {os.path.basename(path)} finds the tracks {[kinds[t['type']] for t in tracks]}, "
                           f"and ffprobe the streams {[s['codec_type'] for s in media]}")
    sec = lambda v: float("nan") if v == matroska.NONE else v / 1e9
    out = {}
    for s, t in zip(media, tracks):
        keep = [k for k, f in enumerate(t["full"]) if f]
        o = t["overlaps"]
        out[s["index"]] = (array.array("d", (sec(t["time"][k]) for k in keep)), array.array("q", (t["place"][k] for k in keep)),
                           array.array("d", (sec(t["dur"][k]) for k in keep)),
                           {"scale": r["scale"], "frame": t["frame"] and t["frame"] / 1e9, "overlaps": [o[0], o[1] / 1e9, o[2] / 1e9] if o[0] else None,
                            "lapped": [sec(v) for v in t["lapped"]],
                            "unknown": sum(t["tag"][k] == matroska.UNKNOWN for k in keep),
                            "unended": t["type"] == 17 and bool(keep) and (last := max(range(len(t["time"])), key=lambda k: (t["time"][k], k))) in keep
                            and t["dur"][last] == matroska.NONE})
    return out


def mp4_places(path, streams, folder, timeout):
    """stored_places() of the MP4 or QuickTime file path. ffprobe names each stream's track_ID in its id. Each packet
    of ffprobe's read pairs with the sample that starts at its byte, so ffmpeg's own edit-list choices (the samples
    it keeps before an edit, the one it cuts at the end) never move the pairing. A time is the sample's presentation
    time, see mp4.track(). A sample the edit list cuts keeps its time on the edit's line, and info counts it in cut."""
    name = os.path.basename(path)
    try:
        r = mp4.samples(path)
    except Exception as ex:   # a reader that fails, as on a cut box header
        r = {"error": str(ex)}
    if r["error"]:
        raise RuntimeError(f"the sample tables of {name} do not read: {r['error']}")
    tracks = {t["id"]: t for t in r["tracks"]}
    pos, out = packet_places(path, folder, timeout), {}
    for s in media_streams(streams):
        t = tracks.get(int(s.get("id") or "0", 16))
        if t is None:
            raise RuntimeError(f"ffprobe names track {s.get('id')} for stream {s['index']} of {name}, and the moov holds no such track")
        at = {p: k for k, p in enumerate(t["place"]) if t["full"][k]}
        places = (pos.get(s["index"]) or ((), array.array("q"), ()))[1] if not (s.get("codec_name") or "").startswith("pcm_") else ()   # see PCM above
        ks = [at.get(p) for p in places]
        if None in ks:
            raise RuntimeError(f"ffprobe reads a packet of stream {s['index']} of {name} at byte {places[ks.index(None)]}, where no sample starts")
        out[s["index"]] = (array.array("d", (t["time"][k] / 1e9 for k in ks)), array.array("q", places), array.array("d", (t["dur"][k] / 1e9 for k in ks)),
                           {"scale": 1e9 / t["timescale"], "frame": t["frame"] and t["frame"] / 1e9, "overlaps": None, "unknown": 0,
                            "unended": False, "exact": True, "cut": sum(t["tag"][k] == mp4.CUT for k in ks), "shift": t["shift"]})
    return out


def ts_places(path, streams):
    """stored_places() of the MPEG-TS or M2TS file path. ffprobe names each stream's PID in its id. The k-th frame with
    data of a PID pairs with the k-th packet of its stream, see ts.frames(). Every audio codec of the reader splits its
    PES into frames, TrueHD and LATM AAC too. PCM gets its entry, see PCM in stored_places(). A PID the reader names no
    codec gives no frames, and the proof refuses its count. The AC-3 stream of a Blu-ray TrueHD PID takes the frames of
    its core, see ts.CORE."""
    name = os.path.basename(path)
    try:
        r = ts.frames(path)
    except (OSError, ValueError) as ex:
        raise RuntimeError(f"the PES walk of {name} failed: {ex}") from ex
    if r["error"]:
        raise RuntimeError(f"the PES walk of {name} failed: {r['error']}")
    tracks, out = {t["pid"]: t for t in r["tracks"]}, {}
    sec = lambda v: float("nan") if v == matroska.NONE else v / 1e9
    for s in media_streams(streams):
        t = tracks.get(int(s.get("id") or "0", 16))
        if t is None:
            raise RuntimeError(f"ffprobe names PID {s.get('id')} for stream {s['index']} of {name}, and the PMT lists no such PID")
        t = t["core"] if s.get("codec_name") == "ac3" and t.get("core") else t
        keep = [k for k, f in enumerate(t["full"]) if f]
        out[s["index"]] = (array.array("d", (sec(t["time"][k]) for k in keep)), array.array("q", (t["place"][k] for k in keep)),
                           array.array("d", (sec(t["dur"][k]) for k in keep)),
                           {"scale": 1e9 / 90000, "frame": t["frame"] and t["frame"] / 1e9, "overlaps": None,
                            "unknown": sum(t["tag"][k] == matroska.UNKNOWN for k in keep), "unended": False, "exact": True})
    return out


def mkv_slack(scale_a, scale_b, kind, exact=False):
    """The seconds a frame time of a Matroska file may move in a Matroska copy, the one bound of the time check for two
    Matroska files (docs/design.md, "Frame times"). scale_a and scale_b are their TimestampScales in ns. A subtitle
    keeps its stored time, and only the new file rounds it: scale_b / 2. An audio or a video block of the original
    rounds its time to a tick, so the frames of a lace and the block after it can sit up to scale_a apart from the
    DefaultDuration spacing. A copy that laces in other places times a frame from another block of the original, and
    rounds again: scale_a + scale_b / 2. A time counts from the time of the stream's first frame or the video's, and a
    new file with a coarser scale rounds that time too. So both bounds get max(0, scale_b - scale_a) / 2 more, and the
    start check of prove() takes the same bound. All bounds get 1 us more for the grid of us().

    exact is True for an original of another container that stores exact times (MP4 ticks of its timescale, TS ticks
    of 90 kHz). mkvmerge moves such a file by its earliest time, which is not on the new file's grid, and rounds each
    moved time to a tick. A time and the time it counts from (the stream's first, or the video's) each round, so a
    move between them reaches scale_b. The check of a stream's start uses the same bound, see prove()."""
    if exact:
        return (scale_b + 1000) / 1e9
    return ((scale_b / 2 if kind == "subtitle" else scale_a + scale_b / 2) + max(0, scale_b - scale_a) / 2 + 1000) / 1e9


def lags(places):
    """{stream index: its worst lag in seconds} of the packet_places() of a file. A packet's lag is how far its time
    trails the latest time of the packets before it in the file. A player reads the file in order, so a packet that
    sits minutes late never plays at its time. B-frames give the video a lag of a few frames in any file. Each stream
    is in file order already, so the streams merge by position one packet at a time."""
    runs = [filter(lambda r: r[0] >= 0 and r[2] == r[2], zip(ps, itertools.repeat(i), times)) for i, (times, ps, *_) in places.items()]
    worst, top = {}, float("-inf")
    for _, i, t in heapq.merge(*runs):
        top = max(top, t)
        worst[i] = max(worst.get(i, 0.0), top - t)
    return worst


def start_plan(shift):
    """The ms of mkvmerge's --sync that gives a track ffmpeg's MP4 start rule back, for a rule that moves it shift ns
    earlier, see mp4.ffmpeg_lead(). mkvmerge takes whole ms and drops a frame it would time before 0, so the move
    rounds toward 0, and the track lands up to 1 ms later than ffmpeg plays it, under one tick of the new file."""
    return -(shift // 1000000)


def lone_cores(j, streams):
    """The ffprobe indexes of the streams of checks.truehd_cores() that mkvmerge does not read. mkvmerge 102 writes the
    core as an AC-3 track of that PID only when it finds core frames. j is the mkvmerge -J probe of the file, which
    numbers each track of a TS by its PID."""
    if (j.get("container") or {}).get("type") != "MPEG transport stream":
        return set()
    read = {(t.get("properties") or {}).get("number") for t in j.get("tracks") or [] if t.get("codec") == "AC-3"}
    return {s["index"] for s in checks.truehd_cores(media_streams(streams)) if int(s["id"], 16) not in read}


def prove(src, tmp, subs, folder, captions=None, dropped=(), retimed=None, absolute=False, ended=None, timed=None, recoded=None, synced=None,
          cores=()):
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
    dropped_first, and the stream's start is then its second packet, which must keep its time within TIME_SLACK.
    mkvmerge may instead trim that fragment. A smaller first MP3 or MP2 packet passes when every other packet matches
    and the second packet keeps its time.
    The entry names both sizes in trimmed_first. The stream may also lose its cut last frame then, when the new packet
    0 is the tail of the old one after zeros or a stray header, see junk_head(). The new file may also hold one
    packet more, the sample an MP4's edit list cuts off. A read with -ignore_editlist must then match it, see
    edit_list_sample(), and the entry names it in edit_list. The time check leaves that one packet out. When src and
    tmp are both Matroska, the times and ends of every stream are the ones its blocks store or RFC 9559 defines, see
    stored_places() (docs/design.md, "Frame times"). ffmpeg's read moves a time that a file stores lower than the
    time before it in the file, and it estimates the times of the frames of a lace. A walk of the blocks reads
    neither. The frames pair in file order, and each time may move mkv_slack(), the rounding of the two files. A
    subtitle's stored time is measured against the video's start, or against 0 with absolute. ffmpeg moves a time
    that goes back 2 ms or less with no warning, see remux.moved_tracks(), and such a move refuses here. mkvmerge times
    the TrueHD frames of a copy again, 0.83 ms each, up to 1 ms off the times of the source. Both times are right. Each
    file rounds the same 1/1200 s frame grid to whole ms from its own start phase, so the two can differ by 1 ms, the
    bound mkv_slack() gives. A frame whose time the file does not define drops out of that check, and the entry counts
    it. When the walk and ffmpeg count other frames of a stream, the proof refuses. When the time check fails on a
    Matroska tmp of another container, the times of that stream as its blocks store them decide. The entry names it in
    times.reread.

    A few more packets may go at the ends when every other packet matches in order, see ends_lost(): up to END_LOSS
    video packets at the end, junk and cut frames at the ends of an audio stream, and an audio lead of at most
    LEAD_LOSS seconds, whatever it holds. The entry names them in dropped_start and dropped_end. The first kept packet
    after a lead must keep its time within TIME_SLACK. An HEVC or VC-1 packet 0 may also hold units of the codec header more, see header_units(), named in header_units. A zero-length timed-text cue may get a length, see zero_cues(), named in zero_length. An
    audio packet that shares its time with a neighbour may move one frame, see times_fault(). captions maps the stream index of
    a CEA-608 track to the SubRip file convert_captions() wrote. The track pairs with a subtitle stream after the kept
    ones, and its text must match.

    dropped holds the stream indexes of src that the remux left out on purpose, a subtitle that does not match the
    audio. cores holds the stream indexes of src of lone_cores(), which mkvmerge does not write. They pair with no
    stream of tmp, and the proof refuses one that holds a frame. retimed maps the k-th subtitle stream of src, from 0,
    to the fix of subsync.timing() that the remux applied: its packets must match, and its times are compared as the
    fix moves them, a time before 0 at 0. Each cue with a duration must keep its length as the fix moves its start and its end, within TIME_SLACK. A cue the fix
    moves before 0 runs from 0 to its moved end, and 1 ms when that end lies before 0, see remux.moved_tracks(). absolute
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
    also numbers the events from 0 in ReadOrder order. So each packet of the original is compared with the rank of
    its ReadOrder in its place, when its stream's ReadOrders are unique integers, see read_ranks(). The entry counts
    the packets whose ReadOrder changed in read_order. The spaces at the end of an event stay, see
    remux.KEEP_PACKETS. mkvmerge drops that NUL byte from an ASS or SSA track it
    copies too, so each packet of every other ASS or SSA stream is compared without it, and nothing else. mkvmerge sets
    the delay of the stop command inside each packet of a VobSub stream in retimed from the cue's new length. So those
    packets are compared with their delays at 0, see spu_masked(), and each delay of the new file must be the old one
    over the rate, within one SPU_TICK. The stop delay of a cue the fix moved before 0 must be its length from 0 to the
    original's end moved by the fix. mkvmerge
    writes each line break of a SubRip packet as CRLF, and a source may store LF. So each packet of a SubRip stream is
    compared with each CR LF as one LF in both files, and the entry counts the packets whose line breaks changed in
    crlf. Any other difference of a byte still refuses. recoded maps the k-th subtitle
    stream of src, a SubRip track, to a function that gives the cues of the new text from srt_blocks() of the original's
    text, see remux.resub(). The proof compares text then: the new stream must hold those cues, and they must keep the
    original's count and times."""
    fa, sa, _ = ff_streams(src)
    fb, sb, _ = ff_streams(tmp)
    if fb.startswith("matroska") and (layout := checks.layout_of(tmp)):   # a player may not find the tracks, see checks.layout_fault()
        return (None, f"the header layout of the new file breaks the Matroska rules: {layout}"), []
    a, b, fam = [s for s in media_streams(sa) if s["index"] not in dropped and s["index"] not in cores], media_streams(sb), fa.split(",")[0]
    if unknown := next((s for s in (*a, *b) if not s.get("codec_name")), None):   # as ffprobe 7.1 reads a WebVTT track in Matroska
        side = "original" if unknown in a else "new file"
        return (None, f"ffprobe names no codec for stream {unknown['index']} of the {side}, so the proof cannot read it"), []
    by_sub = lambda m: {s["index"]: m[k] for k, s in enumerate(s for s in a if s["codec_type"] == "subtitle") if k in m}
    moved, lengthened, planned, recoded = by_sub(retimed or {}), by_sub(ended or {}), by_sub(timed or {}), by_sub(recoded or {})
    nul = [s["index"] for s in a if s["codec_name"] in ("ass", "ssa") and (s["index"] in lengthened or s["index"] in planned)]   # see packet_hashes()
    end_nul = [s["index"] for s in a if s["codec_name"] in ("ass", "ssa") and s["index"] not in nul]
    spu = [s["index"] for s in a if s.get("codec_name") == "dvd_subtitle" and s["index"] in moved]
    crlf = [s["index"] for s in a if s["codec_name"] == "subrip"]
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
    pool = concurrent.futures.ThreadPoolExecutor(4)   # every read at once: the proof then takes about as long as the remux
    try:
        read = lambda p, f, ss: pool.submit(stored_places, p, ss) if f.startswith("matroska") else \
            pool.submit(stored_places, p, ss, f, folder, timeout) if f.startswith(STORED) else pool.submit(packet_places, p, folder, timeout)
        pa_, pb_ = read(src, fa, sa), read(tmp, fb, sb)
        a_ = pool.submit(packet_hashes, src, [s["index"] for s, _ in packets], {i: f[0] for i, f in bsf.items() if f[0]},
                         [s["index"] for s, _ in text], folder, timeout, raw=set(recoded), **({"nul": nul} if nul else {}),
                         **({"end_nul": end_nul} if end_nul else {}), **({"spu": spu} if spu else {}),
                         **({"crlf": crlf} if crlf else {}))
        b_ = pool.submit(packet_hashes, tmp, [t["index"] for _, t in packets], {t["index"]: bsf[s["index"]][1] for s, t in packets
                                                                                 if bsf.get(s["index"], ("", ""))[1]},
                         [t["index"] for _, t in text] + [t["index"] for s, t, sc in pairs if sc], folder, timeout, raw=True,
                         **({"crlf": [t["index"] for s, t in packets if s["index"] in crlf]} if crlf else {}),
                         **({"spu": [t["index"] for s, t in packets if s["index"] in spu]} if spu else {}))
        (ha, ta), (hb, tb), pa, pb = a_.result(), b_.result(), pa_.result(), pb_.result()
    except BaseException:   # a stop or a failed read: no ffmpeg of the proof reads on
        cli.kill_children()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for i in sorted(cores):   # see lone_cores()
        if n := len((pa.get(i) or ((),))[0]):
            pid = next(s.get("id") for s in sa if s["index"] == i)
            main = next(s["index"] for s in sa if s.get("codec_name") == "truehd" and s.get("id") == pid)
            return (i, f"stream {i} holds {n} frames of the AC-3 core of TrueHD stream {main}, and mkvmerge left it out"), proof
    stored = fa.startswith(STORED) and fb.startswith(STORED)   # each time as the file stores or its spec defines it, see stored_places()
    codecs = ({s["index"]: s.get("codec_name") or "" for s in sa}, {s["index"]: s.get("codec_name") or "" for s in sb})
    for h, places, codec, f in ((ha, pa, codecs[0], fa), (hb, pb, codecs[1], fb)) if stored else ():
        lift, kept = 0.0, []   # seconds from ffmpeg's clock of a TS to the reader's, which take a PTS wrap apart
        for i, x in h.items():
            if (got := places.get(i)) is None or codec.get(i, "").startswith("pcm_"):   # see PCM in stored_places()
                kept.append(x)
                x["stored"], x["info"] = (True, got[3] if len(got) > 3 else {}) if got else (False, {})
                continue
            if len(got[0]) != len(x.get("times") or []):   # the reader and ffmpeg count other frames
                x["walked"] = len(got[0])
                kept.append(x)
                continue
            if f.startswith("mpegts") and (pair := next(((a, b) for a, b in zip(got[0], x["times"]) if a == a and b is not None), None)):
                lift = round((pair[0] - pair[1]) / TS_WRAP) * TS_WRAP
            x["times"] = [None if t != t else t for t in got[0]]
            x["ends"] = [None if t is None else t + (d if d == d else 0) for t, d in zip(x["times"], got[2])]   # no duration: at its time
            x["stored"], x["info"] = True, got[3] if len(got) > 3 else {}
            shift = x["info"].get("shift", 0) if h is ha else 0
            if h is ha and (shift or i in (synced or {})):   # ffmpeg's MP4 start rule, see start_plan()
                if (synced or {}).get(i, 0) != start_plan(shift):
                    x["sync_fault"] = ((synced or {}).get(i, 0), start_plan(shift))
                else:   # the exact move the conversion asked of mkvmerge
                    r = (shift + start_plan(shift) * 1000000) / 1e9
                    x["times"], x["ends"] = [None if t is None else t + r for t in x["times"]], [None if e is None else e + r for e in x["ends"]]
            known = [(t, e) for t, e in zip(x["times"], x["ends"]) if t is not None]   # start and end as the reader gives them
            if known:
                x["start"], x["end"] = min(t for t, _ in known), max(e for _, e in known)
        for x in kept if lift else ():   # a PCM stream keeps ffmpeg's times, and they move to the reader's clock
            x["times"], x["ends"] = [None if t is None else t + lift for t in x["times"]], [None if e is None else e + lift for e in x["ends"]]
            x["start"], x["end"] = x["start"] + lift, x["end"] + lift
    none = {"count": 0, "empty": 0, "digest": "", "start": 0, "end": 0}
    video = lambda h, side: min(((h.get(p[side]["index"]) or none)["start"] for p in packets if p[0]["codec_type"] == "video"), default=0)
    va, vb, fault, stream = video(ha, 0), video(hb, 1), None, None
    for s, t in packets:
        x, y, name = ha.get(s["index"]) or none, hb.get(t["index"]) or none, f"{s['codec_type']} {s['index']}"
        f = bsf.get(s["index"]) or (None, None)
        proof.append(dict(stream=name, codec=s["codec_name"], method="packets" + (f", original through {f[0]}" if f[0] else "")
                          + (f", new file through {f[1]}" if f[1] else ""),
                          count=x["count"], **({"empty": x["empty"]} if x["empty"] else {}), **{c: x[c] for c in ("nul_cut", "read_order") if x.get(c)},
                          hash=x["digest"][:16],
                          match=(x["count"], x["digest"]) == (y["count"], y["digest"]), start=[round(x["start"] - va, 3), round(y["start"] - vb, 3)]))
        if proof[-1]["match"] and (n := sum(x["raw_md5s"][k:k + 16] != y["raw_md5s"][k:k + 16] for k in range(0, len(x.get("raw_md5s") or b""), 16))):
            proof[-1]["crlf"] = n   # packets whose line breaks changed between LF and CRLF, and nothing else
        xt, yt, xs, why, ycut, bad = x.get("times") or [], y.get("times") or [], x["start"], None, slice(None), None   # bad: refused for its packets
        lead = None   # what the new file lost at the start, a cut first frame or a short lead, see ends_lost()
        both = x.get("stored") and y.get("stored")   # stored times in both files: one bound for every time and the start
        ia, ib = (x["info"], y["info"]) if both else ({}, {})
        slack = mkv_slack(ia.get("scale", 1e6), ib.get("scale", 1e6), s["codec_type"], ia.get("exact")) if both else None
        sync = slack if both else SYNC   # a start counts from the video's start, which the slack holds, see mkv_slack()
        if s["index"] in moved:   # the times the fix gives, see remux.moved_tracks()
            fx = moved[s["index"]]
            to = lambda t: max(0.0, (t - float(fx["offset"])) / float(fractions.Fraction(fx["rate"])))   # unrounded, see mkv_slack()
            xt, xs = [None if t is None else to(t) for t in xt], to(xs)
            proof[-1].update(retimed=fx, start=[round(xs - va, 3), proof[-1]["start"][1]])
        if s["index"] in planned:   # the times of the plan
            xt = [a for a, _ in planned[s["index"]]]
            xs = min(xt, default=xs)
            proof[-1]["start"][0] = round(xs - va, 3)
        if s["codec_type"] != "subtitle" and x["count"] == y["count"] + 1 and x.get("but_last") == y["digest"]:   # mkvmerge drops a cut last frame
            proof[-1].update(dropped={"pts": x.get("last_pts"), "size": x["last"]})
            xt = xt[:-1]
        elif s["codec_type"] == "audio" and x["count"] - y["count"] in (1, 2) and \
                x.get("but_first" if x["count"] == y["count"] + 1 else "but_ends") == y["digest"]:   # a cut first audio frame too
            proof[-1].update(dropped_first={"pts": x.get("first_pts"), "size": x["first"]})
            lead = "its cut first frame"   # mkvmerge drops it after an empty edit, and a --sync from its time is a frame off
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
            lead = f"a lead of {a} packets" if a and gone[0]["kind"] == "lead" else None
            xt = xt[a:len(xt) - b]
            if a:   # the stream now starts at its first kept packet
                xs = xt[0] if xt and xt[0] is not None else xs
                proof[-1]["start"][0] = round(xs - va, 3)
        elif s["codec_name"] in ("hevc", "vc1") and x["count"] == y["count"] and x["digest"] != y["digest"] and \
                x.get("but_first") == y.get("but_first") and (units := header_units(src, tmp, s["index"], t["index"], f, timeout, s["codec_name"])):
            proof[-1]["header_units"] = units   # mkvmerge copies units of the codec header into packet 0
        elif s["codec_name"] == "hevc" and x["count"] == y["count"] and x["digest"] != y["digest"] and \
                (dv := rpu_moves(src, tmp, s["index"], t["index"], timeout)):   # an old mkvmerge put each RPU in the next packet
            proof[-1]["rpu_moved"] = dv[1]
            why, bad = (f"stream {name} (hevc) holds its Dolby Vision RPUs in another way than mkvmerge moves them: {dv[0]}", s["index"]) \
                if dv[0] else (None, None)
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
            elif abs((xt[1] - va) - (yt[1] - vb)) > sync:
                why = (f"stream {name} ({s['codec_name']}) lost a trimmed first frame, and its second packet moved "
                       f"{((yt[1] - vb) - (xt[1] - va)) * 1000:+.0f} ms")
        elif x["count"] != y["count"]:
            why, bad = f"stream {name} ({s['codec_name']}) holds {y['count']} packets in the new file, {x['count']} in the original", s["index"]
        elif x["digest"] != y["digest"]:
            why, bad = f"the packet data of stream {name} ({s['codec_name']}) differ", s["index"]
        if not why and (walked := next((f"the {side}" for side, z in (("original", x), ("new file", y)) if "walked" in z), None)):
            z, f = (x, fa) if walked == "the original" else (y, fb)
            how = "block walk" if f.startswith("matroska") else "sample table" if f.startswith("mov") else "PES walk"
            why = f"the {how} of {walked} finds {z['walked']} frames of stream {name} ({s['codec_name']}), and ffmpeg {z['count']}"
        if not why and x.get("sync_fault"):
            why = (f"stream {name} moves {x['sync_fault'][0]:+d} ms in the conversion, and ffmpeg's start rule of the MP4 needs "
                   f"{x['sync_fault'][1]:+d} ms")
        moved_by = abs(us((xs - va) - (y["start"] - vb)))   # the start of every stream, the exceptions included
        if not why and (moved_by > us(SYNC) or not lead and moved_by > us(sync)):
            why = f"stream {name} starts {y['start'] - vb:+.3f} s from the video in the new file, {xs - va:+.3f} s in the original"
        if not why and lead and moved_by > us(slack or TIME_SLACK):   # mkvmerge can time the rest of an AVI's VBR MP3 early
            why = f"stream {name} lost {lead}, and its first kept packet moved {((y['start'] - vb) - (xs - va)) * 1000:+.0f} ms"
        if not why and absolute and abs(us(xs - y["start"])) > us(sync):
            why = f"stream {name} starts at {y['start']:.3f} s in the new file, {xs:.3f} s in the original"
        if not why and (s["index"] in lengthened or s["index"] in planned) and x.get("stored"):   # RFC 9559: a cue ends at the next block
            if x["info"].get("unended"):
                why = f"the last cue of stream {name} has no duration in the original and no block after it, so its end is not known"
        elif not why and (s["index"] in lengthened or s["index"] in planned) and \
                (n := sum(t is not None and e == t for t, e in zip(x.get("times") or [], x.get("ends") or []))):   # ffmpeg reads no BlockDuration so
            why = f"stream {name} holds {n} cue{'s' if n > 1 else ''} with no duration in the original, so {'their ends are' if n > 1 else 'its end is'} not known"
        if not why and s["index"] in lengthened:   # the flash fix: only the ends change, as planned
            fx = moved.get(s["index"])
            want = [max(0, subsync.moved(e * 1000, fx)) / 1000 if fx else e for e in lengthened[s["index"]]]
            cue = next((k for k, (e, g) in enumerate(zip(want, y.get("ends") or [])) if g is None or abs(us(e - g)) > us(TIME_SLACK)), None)
            if len(want) != len(y.get("ends") or []) or cue is not None:
                why = (f"stream {name} ends {len(y.get('ends') or [])} cues, and the plan {len(want)}" if cue is None else
                       f"stream {name} ends cue {cue + 1} at {secs(y['ends'][cue], ' s')}, and the plan at {want[cue]:.3f} s")
            proof[-1]["ended"] = len(want)
        if not why and s["index"] in moved and s["index"] not in lengthened:   # each cue's length as the fix moves its start and end
            fx = moved[s["index"]]
            to = lambda t: max(0, subsync.moved(t * 1000, fx)) / 1000
            cue = next((k for k, (a0, e0, a1, e1) in enumerate(zip(x.get("times") or [], x.get("ends") or [], y.get("times") or [], y.get("ends") or []))
                        if None not in (a0, e0) and e0 > a0 and (None in (a1, e1) or abs(us((e1 - a1) - (max(0.001, to(e0)) - to(a0)))) > us(TIME_SLACK))), None)
            if cue is not None:
                why = f"stream {name} ({s['codec_name']}) ends cue {cue + 1} at another time than the fix gives"
        if not why and s["index"] in spu:   # the stop delay of each cue, see spu_masked()
            fx, cue = moved[s["index"]], None
            rate = float(fractions.Fraction(fx["rate"]))
            for k, (old, new, t0, e0) in enumerate(zip(x.get("spu") or [], y.get("spu") or [], x.get("times") or [], x.get("ends") or [])):
                want = [d / rate for d in old]
                if None not in (t0, e0) and subsync.moved(t0 * 1000, fx) < 0:   # its new length, from 0 to its moved end
                    want[-1] = max(0.001, subsync.moved(e0 * 1000, fx) / 1000) / SPU_TICK
                if len(new) != len(want) or any(abs(d - w) > 1 for d, w in zip(new, want)):
                    cue = k
                    break
            if cue is not None or len(x.get("spu") or []) != len(y.get("spu") or []):
                why = f"stream {name} ({s['codec_name']}) stops cue {(cue or 0) + 1} at another time than the fix gives"
        if not why and s["index"] in planned:   # a time plan: each start and end as planned
            want, got = planned[s["index"]], list(zip(y.get("times") or [], y.get("ends") or []))
            cue = next((k for k, ((a, e), (p, q)) in enumerate(zip(want, got)) if None in (p, q) or max(abs(us(a - p)), abs(us(e - q))) > us(TIME_SLACK)), None)
            if len(want) != len(got) or cue is not None:
                why = (f"stream {name} holds {len(got)} timed cues, and the plan {len(want)}" if cue is None else
                       f"stream {name} times cue {cue + 1} {secs(got[cue][0])} to {secs(got[cue][1], ' s')}, and the plan {want[cue][0]:.3f} to {want[cue][1]:.3f} s")
            proof[-1]["timed"] = len(want)
        if not why and fam == "avi" and s["codec_type"] == "audio":
            proof[-1]["times"] = {"checked": False, "why": "AVI keeps no audio times"}
        elif not why:
            video = (va, vb) if s["codec_type"] == "audio" else None   # packets that share a time, see times_fault()
            if both:   # stored times in both files: a muxer that moved a time past the rounding refuses
                why, proof[-1]["times"] = times_fault(name, s["codec_name"], xt, yt, video, sync if s["codec_type"] == "subtitle" and not absolute else slack,
                                                      ((0, 0) if absolute else (va, vb)) if s["codec_type"] == "subtitle" else None, paired=True,
                                                      length=ia.get("frame"), overlaps=ia.get("overlaps"), lapped=ia.get("lapped") or ())
            else:
                why, proof[-1]["times"] = times_fault(name, s["codec_name"], xt, yt, video)
            if why and fb.startswith("matroska") and not stored:   # ffmpeg's muxer may move a few times of its read, see packet_places()
                got = [None if v != v else v for v in (pb.get(t["index"]) or [[]])[0]]
                why, proof[-1]["times"]["reread"] = times_fault(name, s["codec_name"], xt, got[ycut], video)
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
    la, lb = lags(pa), lags(pb)   # each stream sits in the file near its time, as in the original, see lags()
    for (s, t), row in zip(packets + text + [(s, t) for s, t, sc in pairs if sc], proof):   # the rows in the order they were added
        row["lag"] = [round(la.get(s["index"], 0.0), 3) if s else 0.0, round(lb.get(t["index"], 0.0), 3)]
        if row["lag"][1] > row["lag"][0] + LAG_SLACK:
            row["match"] = False
            fault = fault or f"stream {row['stream']} sits up to {row['lag'][1]:.1f} s behind the other streams in the new file, {row['lag'][0]:.1f} s in the original"
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
DAMAGE = {"mkvmerge": (r"This audio track contains \d+ bytes? of invalid data which (?:were|was) skipped", "part of the audio cannot be read"),
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
    track by its id in j. The k-th audio track of j is the k-th audio stream of streams but lone_cores(), as prove()
    pairs them. An unknown dur counts nothing."""
    ids, lone = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "audio"], lone_cores(j, streams)
    ff = [s["index"] for s in media_streams(streams) if s.get("codec_type") == "audio" and s["index"] not in lone]
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
    writes nothing: mkvmerge remuxes into /dev/null with the conversion's --sync, and ffmpeg reads the streams the way
    prove() reads them. A subtitle decoder's error is no damage there either, see SUB_DECODER. j is the mkvmerge -J
    probe of path. mkvmerge must skip invalid data in the same audio stream
    away from the ends again. The proof's refusal stands from the conversion. Any other failure of the read is no
    damage."""
    read = damage["read"]
    try:
        fam, streams, _ = ff_streams(path)
        if read == "mkvmerge":
            r = ff.run("convert.convert", convert.convert_cmd(path, os.devnull, j, [], sync=convert.avi_sync(path, j, streams)), capture_output=True, text=True,
                               errors="replace")
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


def us(t):
    """The time t in seconds as whole microseconds. A time is ticks times the time base, a float, so two times on the
    millisecond grid can differ by 2 ms and one float bit. TIME_SLACK compares on these."""
    return round(t * 1e6)


def times_fault(name, codec, xt, yt, video=None, slack=TIME_SLACK, zero=None, paired=False, length=None, overlaps=None, lapped=()):
    """(why the packet times of a stream moved in the new file, or None; the check for the proof entry). xt and yt are
    the times of the packets with data, in file order. The one packet an exception left out is already gone. Each list
    is sorted and taken against its own first time. Each pair may differ by slack, TIME_SLACK by default, in whole
    microseconds, see us(). zero, (a time of the original, one of the new file), takes the place of the first times.
    The lists are sorted because an AVI video stream has decode times only, and mkvmerge writes presentation times.
    With B-frames the order changes, and the set stays. mkvmerge makes three changes on purpose, and all pass. It
    starts the file at 0. It rounds each time to the millisecond of Matroska's timestamp scale. It writes presentation
    times for decode times. An AVI keeps no audio times at all. ffmpeg counts them from the bytes and the header's byte
    rate. mkvmerge counts them from the samples. The two drift apart over the length of a file. A player plays the
    audio on, as mkvmerge counts it. So prove() leaves AVI audio out of this check and keeps its start and its data.
    In a TS whose audio times go back 0.3 s at 2 minutes, mkvmerge makes the audio continuous. The new file then plays
    0.3 s late from there, and that fails here.

    paired is True for two Matroska files, whose times come from stored_places(). Their packets matched in order, so
    each pair is the same frame, in file order. No list is sorted then. A pair with a time that is not known (None)
    drops out of the check. The entry counts it in unknown. slack is then mkv_slack().

    video is (the original's video start, the new file's) for an audio stream. A second check runs when the check
    fails and packets of the original share a time with a neighbour. It measures every time against the video's start,
    as the start check does. A packet that shares a time may move one frame, and slack more for the rounding. Every
    other packet may move slack. For paired files one frame is length, the seconds of a frame the original defines.
    When the original defines none, a packet that shares a time may move slack only. For other files one frame is the
    median step of the original. An MP4 can give its first two AAC packets one time, and mkvmerge spaces them one
    frame apart. The check then names the shared packets in shared.

    An audio frame of paired files may also move later than its defined time. That passes when the new file plays on
    in whole frames from the last frame that kept its time. Its new time is then that frame's new time plus one frame
    (length) for each frame between, within slack. Where the original's blocks overlap, mkvmerge makes the
    audio contiguous so. A player then plays it closer to the picture. So the chain must hold a block of the original
    after the frame that kept its time. That block starts before the block before it ends. lapped holds the stored
    times of those blocks. The move may reach OVERLAP_MOVE at most. An earlier move refuses, so a gap of the original
    never closes. A later move that is not contiguous refuses, and so does a chain with no such block. So a slow drift
    never passes. A frame that shares a time with a neighbour and moves more than slack may go on with the chain by the
    same rule. When it does not, it counts as a frame that kept its time. The check counts such frames in contiguous, with the worst move and the first time. The refusal says
    where the new file plays out of sync, see out_of_sync(). It takes the moves of the check that refused. For paired
    files it also names the count of moved frames. It names overlaps too, see stored_places(). That is [count, worst s,
    first s] of the original's blocks that start before the block before them ends."""
    if len(xt) != len(yt) or (not paired and (None in xt or None in yt)):
        return (f"stream {name} ({codec}) has packets with no time", {"checked": False}) if len(xt) == len(yt) else \
            (f"stream {name} ({codec}) holds {len(yt)} timed packets in the new file, {len(xt)} in the original", {"checked": False})
    known = [k for k, (p, q) in enumerate(zip(xt, yt)) if p is not None and q is not None]
    a, b, slack = [us(xt[k]) for k in known], [us(yt[k]) for k in known], us(slack)
    if not paired:
        a, b = sorted(a), sorted(b)
    za, zb = (us(zero[0]), us(zero[1])) if zero else (min(a), min(b)) if a else (0, 0)
    worst, at = 0, None
    for p, q in zip(a, b):
        d = abs((p - za) - (q - zb))
        if d > worst:
            worst, at = d, p - za
    check = {"worst_ms": round(worst / 1000, 1), **({"at": round(at / 1e6, 3)} if at is not None and worst > slack else {}),
             **({"unknown": len(xt) - len(known)} if len(known) < len(xt) else {})}
    same = {j for i in range(1, len(a)) if a[i] == a[i - 1] for j in (i - 1, i)} if worst > slack and video else set()
    if same and len(set(a)) > 1:
        steps = sorted(q - p for p, q in zip(a, a[1:]) if q > p) if not paired else []
        zero = (us(video[0]), us(video[1]))
        frame, moves = us(length or 0) if paired else steps[len(steps) // 2], [abs((q - zero[1]) - (p - zero[0])) for p, q in zip(a, b)]
        check["shared"] = {"packets": len(same), "frame_ms": round(frame / 1000, 1), "worst_ms": round(max(moves) / 1000, 1)}
        if all(d <= (frame if i in same else 0) + slack for i, d in enumerate(moves)):
            return None, check
    if worst > slack:
        if "shared" not in check:   # the moves of the first check, each against its stream's first time
            moves, frame, same, zero = [abs((q - zb) - (p - za)) for p, q in zip(a, b)], 0, set(), (za, zb)
        # Each frame may move slack, a frame that shares a time one frame more. An audio frame of paired files may also
        # move later when the new file plays on in whole frames from the last frame that kept its time (the anchor),
        # after an overlap of the original's blocks, see "Overlapping blocks".
        out = [i for i, d in enumerate(moves) if d > (frame if i in same else 0) + slack]
        step, anchor, closed, moved = (length or 0) * 1e6 if paired and video else 0, None, [], set(out)   # us, kept as a float
        lap, cap, seen = {us(t) for t in lapped}, us(OVERLAP_MOVE), False   # seen: an overlapping block since the anchor
        for i in range(len(a)) if step and out else ():
            seen = (i in moved or i in same and moves[i] > slack) and (seen or a[i] in lap)   # a shared frame may go on with the chain
            if anchor is not None and seen and moves[i] <= cap and b[i] - zero[1] > a[i] - zero[0] and \
                    abs(b[i] - b[anchor] - (i - anchor) * step) <= slack:
                closed.append(i)
            elif i not in moved:
                anchor, seen = i, False
        if closed:
            check["contiguous"] = {"frames": len(closed), "worst_ms": round(max(moves[i] for i in closed) / 1000, 1),
                                   "first": round(a[closed[0]] / 1e6 - zero[0] / 1e6 + zero[1] / 1e6, 3)}
            out = sorted(moved - set(closed))
        if not out:
            return None, check
        sec = lambda v: [x / 1e6 for x in v]
        why = out_of_sync(name, codec, sec(a), sec(b), out, sec(moves), same, sec(zero))
        if paired:
            why += f". {len(out)} of {len(a)} frames moved"
            one = overlaps and overlaps[0] == 1
            why += (f". The original stores {overlaps[0]} block{'' if one else 's'} that start{'s' if one else ''} before the block before "
                    f"{'it' if one else 'them'} ends, up to {overlaps[1] * 1000:.0f} ms, the first at {overlaps[2]:.3f} s") if overlaps else ""
        return why, check
    return None, check


# Per stream kind: how the new file plays it, and what it plays against, see out_of_sync()
SYNC_WORDS = {"audio": ("play the audio", "the video"), "video": ("show the video", "the audio"), "subtitle": ("show the subtitles", "the video")}


def out_of_sync(name, codec, a, b, out, moves, same, zero):
    """The refusal of times_fault(): when the new file would play stream name out of sync, then one technical line. a
    and b hold the sorted times of the original and the new file, out the packets that moved more than they may, moves
    how far each one moved, same the packets that share a time in the original, and zero the time in each file that
    the moves count from. The span takes its times from b. mkvmerge starts the new file at 0, so b holds the times a
    player shows. The span runs from the first moved packet to the last one, or to the end when the last packet moved
    too. Moved packets with in-sync packets between them name the number of places. A span inside one second is "at"
    that second. The detail names the first moved packet with a time of its own, else the first moved packet. Its time
    is where that packet belongs in the new file. It is the packet's time in the original, moved from zero there to
    zero in the new file."""
    (verb, other), ms = SYNC_WORDS[name.split()[0]], f"{max(moves[i] for i in out) * 1000:.0f} ms"
    first, last = (decide.clock(max(0, b[i])) for i in (out[0], out[-1]))
    start, places = "the start" if first == "0:00" else first, 1 + sum(j > i + 1 for i, j in zip(out, out[1:]))
    span = f"at {start}" if first == last else f"in {places} places between {start} and {last}" if places > 1 else \
        f"from {start} to the end" if out[-1] == len(b) - 1 else f"from {start} to {last}"
    k = next((i for i in out if i not in same), out[0])
    return f"the MKV would {verb} {ms} out of sync with {other}, {span}. Detail: {name} ({codec}) moved {ms}, first at {a[k] - zero[0] + zero[1]:.3f} s"


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
    r = ff.run("proof.packet_data", ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{index}", "-c", "copy",
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
    those first packets shows. A cut frame is kind "cut". An audio stream may also lose a short lead, kind "lead",
    whatever its packets hold: the lost packets before the first kept one span at most LEAD_LOSS seconds, from the
    time of packet 0 to the time of the first kept packet. An AVI can start its MP3 stream so, with a silent frame,
    a few stray bytes and cut frames that mkvmerge drops. prove() holds the first kept packet of a lead to its time
    within TIME_SLACK. mkvmerge can drop the lead of an AVI MP3 stream whose header times each chunk as one frame
    (VBR) and play the rest early, and an AVI keeps no audio times for the time check. After the other kinds, prove()
    holds the stream's start within SYNC."""
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
        junk = all(sizes[i] <= JUNK_HEADER_MAX for i in heads)
        if heads and junk and data is None:   # one read of the most packets any split can lose at the start
            data = packet_data(src, s["index"], bsf, n - m, timeout)
        start = lambda i: sum(sizes[:i])
        junk = junk and all(data[start(i):start(i) + sizes[i]].startswith(JUNK_HEADERS) for i in heads)
        short = a < len(times) and None not in (times[0], times[a]) and times[a] - times[0] <= LEAD_LOSS
        if not junk and not short:
            continue
        kind = lambda i, cut: "lead" if not junk and i < a else "zero" if zero(i) else "header" if i in heads else cut
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


def extradata(path, index, timeout):
    """The codec header of stream index of path, from ffprobe's dump of it, or b"" when ffprobe gives none."""
    r = ff.run("proof.extradata", ["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries", "stream=extradata", "-show_data", "-of", "json",
                        path], capture_output=True, text=True, errors="replace", timeout=timeout)
    dump = ((ff.loads(r, "{}").get("streams") or [{}])[0].get("extradata") or "") if r.returncode == 0 else ""
    return bytes.fromhex("".join("".join(line[10:49].split()) for line in dump.splitlines() if line.strip()))


def hvcc_units(path, index, timeout):
    """The NAL units in the HEVC codec header (hvcC) of stream index of path."""
    raw, units = extradata(path, index, timeout), []
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


RPU = 62   # the NAL type of a Dolby Vision RPU, the frame's metadata, in an HEVC stream


def hevc_packets(path, index, timeout):
    """Each packet with data of the HEVC stream index of path, in file order, as (its pts, its dts, whether it is a
    keyframe, [its NAL units]). The parameter sets and access unit delimiters (types 32 to 35) go, as the proof's filter
    takes them out. ffprobe gives the times, flags and sizes, and ffmpeg -c copy the bytes, which the length size of
    the codec header splits. Never filter_units: it writes Annex B, whose start code length follows a unit's place in
    its packet. It reads the stream as it goes, so a whole film never sits in memory. A failed read raises."""
    head = extradata(path, index, timeout)
    if len(head) < 23 or head[0] != 1:
        raise RuntimeError(f"stream {index} of {os.path.basename(path)} has no HEVC codec header")
    n = (head[21] & 3) + 1
    r = ff.run("proof.hevc_packets", ["ionice", "-c3", "nice", "-n", "19", "ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
                        "packet=pts,dts,size,flags", "-of", "csv=p=0", path], capture_output=True, text=True, errors="replace", timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ffprobe did not read the packets of {os.path.basename(path)}: {config.mask(r.stderr.strip())[-200:]}")
    p = ff.popen("proof.hevc_packets.data", ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{index}", "-c", "copy",
                          "-copyinkf", "-f", "data", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        for row in r.stdout.splitlines():
            pts, dts, size, flags = (row.split(",") + ["", "", "0", ""])[:4]
            if not size.isdigit() or not int(size):
                continue
            data, units, at = p.stdout.read(int(size)), [], 0
            if len(data) != int(size):
                raise RuntimeError(f"ffmpeg read fewer bytes of stream {index} of {os.path.basename(path)} than ffprobe")
            while at < len(data):
                size_of = int.from_bytes(data[at:at + n], "big")
                unit, at = data[at + n:at + n + size_of], at + n + size_of
                if not size_of or len(unit) != size_of:
                    raise RuntimeError(f"a packet of stream {index} of {os.path.basename(path)} holds a cut NAL unit")
                if not 32 <= (unit[0] >> 1) & 63 <= 35:
                    units.append(unit)
            yield pts, dts, "K" in flags, units
        if p.stdout.read(1):
            raise RuntimeError(f"ffmpeg read more bytes of stream {index} of {os.path.basename(path)} than ffprobe")
    finally:
        timer.cancel()
        p.kill()
        p.wait()


def rpu_fault(a, b):
    """(why the packets b of a new file are not the packets a of the original with each Dolby Vision RPU moved, or
    None; the RPUs that moved; the RPUs seen). a and b give hevc_packets() rows. mkvmerge reads an RPU at the start of
    packet k + 1, where an old mkvmerge put it, as the end of packet k, the frame it describes. So each new packet must
    be the old one as it was, or the old one without its leading RPU and with the leading RPU of the next old packet at
    its end. The times and keyframe flags of each packet stay, and the NAL units joined across the packets stay the
    same, byte for byte, so no RPU is lost or doubled. It walks both reads once and holds two packets of each."""
    a, moved, seen, k = iter(a), 0, 0, 0
    left, right = collections.deque(), collections.deque()   # the joined units of each file not yet compared
    old = next(a, None)
    for k, new in enumerate(b):
        if old is None:
            return f"the new file holds packet {k + 1}, and the original {k}", moved, seen
        nxt = next(a, None)
        seen += sum((u[0] >> 1) & 63 == RPU for u in old[3])
        if old[:3] != new[:3]:
            return f"packet {k + 1} changed its time or keyframe flag", moved, seen
        lead = old[3][:1] if old[3] and (old[3][0][0] >> 1) & 63 == RPU else []
        after = nxt[3][:1] if nxt and nxt[3] and (nxt[3][0][0] >> 1) & 63 == RPU else []
        if new[3] != old[3] and new[3] != old[3][len(lead):] + after:
            return f"packet {k + 1} holds other NAL units", moved, seen
        moved += new[3] != old[3] and bool(after)
        left.extend(old[3]); right.extend(new[3])
        while left and right:
            if left.popleft() != right.popleft():
                return f"the NAL units differ by packet {k + 1}", moved, seen
        old = nxt
    if old is not None or left or right:
        return f"the new file holds {k + 1} packets or fewer units than the original", moved, seen
    return None, moved, seen


def rpu_moves(src, tmp, i, k, timeout):
    """None when no Dolby Vision RPU (NAL 62) shows in stream i of src or stream k of tmp, else (why the new packets
    are not the old ones with the RPUs moved, or None; the RPUs that moved), see rpu_fault(). prove() asks only for an
    HEVC stream whose packet digests differ, so no other file pays for the read. A read that fails gives None too, and
    the packets refuse as they differ."""
    try:
        why, moved, seen = rpu_fault(hevc_packets(src, i, timeout), hevc_packets(tmp, k, timeout))
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None
    return (why, moved) if seen or moved else None


def header_units(src, tmp, i, k, bsf, timeout, codec):
    """The hex of each unit that packet 0 of stream k of tmp holds more than packet 0 of stream i of src, when each
    one is a unit of the original's codec header, byte for byte, and packet 0 of src is in the new packet 0 in order.
    Else None. mkvmerge copies the units of an HEVC header into packet 0, and bsf removes only the parameter sets. For
    VC-1 (codec "vc1") the units are the sequence header and the entry point of the header. mkvmerge puts the entry
    point of a header that ffmpeg padded with a byte in front of the frame of packet 0. prove() reads one packet of
    each file, and only when every other packet matches."""
    old = annexb_units(packet_data(src, i, bsf[0], 1, timeout))
    new = annexb_units(packet_data(tmp, k, bsf[1], 1, timeout))
    header = hvcc_units(src, i, timeout) if codec == "hevc" else [u for u in annexb_units(extradata(src, i, timeout)) if u[:1] in (b"\x0e", b"\x0f")]
    left, more = iter(old), []
    want = next(left, None)
    for u in new:
        if u == want:
            want = next(left, None)
        elif u in header:
            more.append(u.hex()[:64])
        else:
            return None
    return more if old and want is None and more else None
