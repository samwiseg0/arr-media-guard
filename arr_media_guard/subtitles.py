# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The subtitle match check and the reads of the subtitle tracks."""
import contextlib, fractions, json, os, re, resource, select, statistics, subprocess, tempfile, time, zlib

from . import checks, cli, config, content, decide, logs, proof, remux, runner, store, subsync, vault


def subtitle_ends(path, j, timeout, limit=None, lines=None):
    """{mkvmerge track id: where its last subtitle packet ends, the pts plus the duration} from ffprobe's demux of the
    whole file, or None when ffprobe fails or the timeout ends it. lines gets {track id: [its packets, the packets that
    start at or after limit]}, one packet a SubRip line. ffprobe's k-th subtitle stream is mkvmerge's k-th
    subtitle track. A subtitle event with a bogus duration sets the Segment duration, and a remux keeps it."""
    ids = [t["id"] for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    if timeout < 10 or not ids:
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index:packet=stream_index,pts_time,duration_time",
                            "-of", "json", path], capture_output=True, text=True, errors="replace", timeout=timeout)
        d = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError):
        return None
    order = [x.get("index") for x in d.get("streams") or []]
    if len(order) != len(ids):
        return None
    ends, count = dict.fromkeys(ids, 0.0), {i: [0, 0] for i in ids}
    for pk in d.get("packets") or []:
        with contextlib.suppress(KeyError, ValueError, TypeError):
            i, pts = ids[order.index(pk["stream_index"])], float(pk["pts_time"])
            ends[i] = max(ends[i], pts + float(pk.get("duration_time") or 0))
            count[i][0] += 1
            count[i][1] += limit is not None and pts >= limit
    if lines is not None:
        lines.update(count)
    return {i: round(e, 3) for i, e in ends.items()}


def subtitle_read(path, j, want):
    """{track position: text_language() of its text} for the text subtitle tracks of a Matroska file at the positions in
    want (docs/design.md, "Subtitle text"). j is its mkvmerge -J probe. It stops at text_language()'s verdict or after
    TEXT_CUES blocks, see subtitle_blocks(). A track the read cannot take gets no entry, or a reason. A failure keeps
    what was read. The job's time limit passes."""
    return subtitle_blocks(path, j, want, config.TEXT_CODECS, config.TEXT_CUES, lambda blocks: decide.text_language(t for _, _, t in blocks),
                           lambda why: (None, 0.0, why))


def timed(blocks):
    """[(start, end, text)] of subtitle_blocks(). A cue with no BlockDuration ends at the next cue, 5 seconds at most."""
    got = [b for b in blocks if b[0] is not None]
    return [(s, s + d if d is not None else min(s + 5, got[k + 1][0] if k + 1 < len(got) else s + 5), text) for k, (s, d, text) in enumerate(got)]


def subtitle_cues(path, j, want, full=False):
    """{track position: [(start, end, text)] in seconds, see timed()} of every cue of the text subtitle tracks at the
    positions in want, for the subtitle match check (docs/design.md, "Subtitle match"). SubRip, ASS, SSA and WebVTT
    tracks. With full, a track the Cues do not index comes from full_read(). A track the read cannot take gets no entry."""
    got = {p: c for p, c in subtitle_blocks(path, j, want, config.SUB_CODECS, config.CUE_MAX, timed, lambda why: None).items() if c}
    if full and set(want) - set(got):
        got.update({p: c for p, c in full_read(path, j)["cues"].items() if p in set(want) - set(got) and c})
    return got


def sub_codecs(j):
    """{subtitle position: its codec id} of an mkvmerge -J probe."""
    return {f"s{n}": (t.get("properties") or {}).get("codec_id") for n, t in enumerate((t for t in j.get("tracks") or [] if t.get("type") == "subtitles"), 1)}


def cue_entries(f, numbers, cap):
    """(the file offset of the Segment data or None, decide.cue_blocks() of the track numbers) of the open
    Matroska file f, from its Cues. A second SeekHead may list the Cues."""
    b = f.read(config.HEADER_READ); seg = decide.segment_start(b)
    if not seg:
        return None, {}
    ds, seek = seg[0], decide.front_seeks(b, seg[0])
    for pos in [] if decide.CUES in seek else seek.get(decide.SEEKHEAD, []):
        f.seek(ds + pos); c = f.read(config.HEADER_READ); e = decide.element(c, 0)
        if e and e[0] == decide.SEEKHEAD and e[2] is not None:
            decide.seek_entries(c, e[1], e[1] + e[2], seek)
    for pos in seek.get(decide.CUES, [])[:1]:
        f.seek(ds + pos); e = decide.element(f.read(12), 0)
        if e and e[0] == decide.CUES and e[2] is not None and e[2] <= config.CUES_MAX:
            f.seek(ds + pos + e[1])
            return ds, decide.cue_blocks(f.read(e[2]), numbers, cap)
    return ds, {}


def cue_lengths(path, j, want, full=False):
    """{track position: [the CueDuration of each cue entry, in seconds, or None]} of the subtitle tracks at the
    positions in want, from the Cues alone, for the flash check. mkvmerge and ffmpeg write one per subtitle block. An
    entry with no CueDuration gives None, so the check reads the blocks. A track with no entry gets no list, but with
    full a track the Cues do not index gets the durations of full_read(). A failed read gives {}."""
    number = {f"s{n}": (t.get("properties") or {}).get("number") for n, t in enumerate((t for t in j.get("tracks") or [] if t.get("type") == "subtitles"), 1)}
    scale = ((j.get("container") or {}).get("properties") or {}).get("timestamp_scale") or 1000000
    try:
        with open(path, "rb", buffering=0) as f:
            got = cue_entries(f, {number[p] for p in want if number.get(p)}, config.CUE_MAX)[1]
    except OSError:
        return {}
    out = {p: [d * scale / 1e9 if d is not None else None for _, _, d in got[number[p]]] for p in want if got.get(number.get(p))}
    if full and any(not got.get(number.get(p)) for p in want):
        out.update({p: [e - s for s, e, _ in c] for p, c in full_read(path, j)["cues"].items() if p in want and not got.get(number.get(p))})
    return out


FULL = {}   # (path, size, mtime_ns) -> full_read() of that file, so a run reads a file whole at most once
FULL_OUT = {"subrip": ("-c", "copy", "srt"), "ass": ("-c", "copy", "ass"), "ssa": ("-c", "copy", "ass"), "webvtt": ("-c:s", "srt", "srt"),
            "hdmv_pgs_subtitle": ("-c", "copy", "sup"), "dvd_subtitle": ("-c", "copy", "framecrc")}   # ffmpeg codec: its codec option and format


def cue_less(path, j):
    """The positions of the subtitle tracks of path that no cue entry indexes. Old mkvmerge versions wrote none for
    subtitles. A file that does not read as Matroska names none."""
    number = {f"s{n}": (t.get("properties") or {}).get("number") for n, t in enumerate((t for t in j.get("tracks") or [] if t.get("type") == "subtitles"), 1)}
    try:
        with open(path, "rb", buffering=0) as f:
            ds, got = cue_entries(f, {n for n in number.values() if n}, 1)
    except OSError:
        return set()
    return {p for p, n in number.items() if n not in got} if ds is not None else set()


def full_read(path, j):
    """{"cues": {track position: [(start, end, text)] in seconds}, "tracks", "took", "cpu", "why"} of the subtitle tracks
    of path that no cue entry indexes, from one ffmpeg read of the whole file at nice 19 and idle I/O (docs/design.md,
    "Subtitle match"). A film takes minutes, so only --sub-check and --sub-time ask for it, and an import never does.
    SubRip and ASS keep their text, WebVTT becomes SubRip, a PGS track gives its display sets, see pgs_shows(), and a
    VobSub track its packets with their durations. ffmpeg writes each track into its own pipe, and the read keeps only
    the cue times and text as they arrive. A PGS track keeps only its PCS segments, see sup_pcs(). So the read writes
    no file and needs no free space. The result is kept for the file as it is, so a run reads a file whole once. A
    failure gives no cues and why."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key in FULL:
        return FULL[key]
    out = {"cues": {}, "tracks": [], "took": 0.0, "cpu": 0.0, "why": None}
    lost, ends, proc = cue_less(path, j), {}, None
    try:
        streams = [x for x in proof.ff_streams(path)[1] if x.get("codec_type") == "subtitle"] if lost else []   # the k-th is mkvmerge's k-th subtitle track
        take = {f"s{k}": x["codec_name"] for k, x in enumerate(streams, 1) if f"s{k}" in lost and x.get("codec_name") in FULL_OUT}
        if take:
            argv = ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-copyts", "-i", path]
            for p, codec in take.items():
                ends[p] = list(os.pipe())   # [read end, write end or None once closed]
                opt, value, fmt = FULL_OUT[codec]
                argv += ["-map", f"0:s:{int(p[1:]) - 1}", opt, value, "-f", fmt, f"pipe:{ends[p][1]}"]
            t0, c0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        pass_fds=[w for _, w in ends.values()])
            finally:   # only ffmpeg holds the write ends, so each pipe ends when ffmpeg closes its output
                for e in ends.values():
                    os.close(e[1]); e[1] = None
            keep, part, err = {p: bytearray() for p in take}, {p: bytearray() for p in take}, b""
            fds, poll = {r: p for p, (r, _) in ends.items()} | {proc.stderr.fileno(): None}, select.poll()
            for fd in fds:
                poll.register(fd, select.POLLIN)
            while fds:   # read every pipe as it fills, so ffmpeg never waits on a full one
                for fd, _ in poll.poll():
                    chunk, p = os.read(fd, 1 << 16), fds[fd]
                    if not chunk:
                        poll.unregister(fd); del fds[fd]
                    elif p is None:
                        err = (err + chunk)[-4096:]
                    elif FULL_OUT[take[p]][2] != "sup":
                        keep[p] += chunk
                    else:
                        part[p] += chunk
                        keep[p] += sup_pcs(part[p])
            proc.wait()
            c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            out.update(tracks=sorted(take), took=round(time.monotonic() - t0, 1), cpu=round(c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime, 1))
            if proc.returncode:
                raise RuntimeError(f"ffmpeg exited {proc.returncode}: {config.mask(err.decode('utf-8', 'replace').strip())[-200:]}")
            out["cues"] = {p: full_cues(bytes(keep[p]), FULL_OUT[codec][2]) for p, codec in take.items()}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as ex:
        out.update(cues={}, why=config.mask(f"the whole-file read failed: {type(ex).__name__}: {ex}")[:200])
    finally:
        if proc and proc.poll() is None:   # the job's time limit or an interrupt: no ffmpeg outlives the read
            proc.kill(); proc.wait()
        if proc:
            proc.stderr.close()
        for fd in (fd for e in ends.values() for fd in e if fd is not None):
            os.close(fd)
    while len(FULL) >= 8:   # the files a backfill's workers read now, never the whole library
        FULL.pop(next(iter(FULL)))
    FULL[key] = out
    return out


def sup_pcs(buf):
    """The PCS segments among the whole segments at the start of buf, a bytearray of ffmpeg's sup stream. The whole
    segments leave buf, and the part segment at its end stays for the next bytes. A segment is "PG", PTS and DTS, type,
    size and data. pgs_shows() reads only the PCS, and the pictures of a film can take GBs. A lost sync raises
    ValueError, so buf never grows without end."""
    k, pcs = 0, bytearray()
    while buf[k:k + 2] == b"PG" and k + 13 + int.from_bytes(buf[k + 11:k + 13], "big") <= len(buf):
        end = k + 13 + int.from_bytes(buf[k + 11:k + 13], "big")
        pcs += buf[k:end] if buf[k + 10] == 0x16 else b""
        k = end
    del buf[:k]
    if not b"PG".startswith(buf[:2]):
        raise ValueError("a PGS track's sup stream lost its sync")
    return bytes(pcs)


def full_cues(data, fmt):
    """[(start, end, text)] in seconds of one subtitle track that ffmpeg wrote as fmt for full_read()."""
    if fmt == "srt":
        return [(a / 1000, b / 1000, t) for a, b, t in proof.srt_blocks(data.decode("utf-8", "replace"))]
    if fmt == "ass":
        clock = lambda t: sum(float(x) * m for x, m in zip(t.split(":"), (3600, 60, 1)))
        return sorted((clock(m[1]), clock(m[2]), m[3]) for m in re.finditer(r"(?m)^Dialogue:\s*[^,]*,([^,]*),([^,]*),(?:[^,]*,){6}(.*?)\r?$",
                                                                             data.decode("utf-8", "replace")))
    if fmt == "sup":   # segments: "PG", PTS and DTS at 90 kHz, type, size, data. pgs_shows() takes the PCS of each display set.
        sets, k = [], 0
        while data[k:k + 2] == b"PG" and k + 13 <= len(data):
            size = int.from_bytes(data[k + 11:k + 13], "big")
            sets.append((int.from_bytes(data[k + 2:k + 6], "big") / 90000, None, data[k + 10:k + 13 + size]))
            k += 13 + size
        return pgs_shows(sets)
    tb, got = 0.001, []   # framecrc: "#tb 0: 1/1000", then stream, dts, pts, duration, size, crc
    for line in data.decode("ascii", "replace").splitlines():
        if line.startswith("#tb "):
            a, b = line.split(":")[1].strip().split("/")
            tb = int(a) / int(b)
        elif line[:1].isdigit():
            f = [x.strip() for x in line.split(",")]
            got.append((int(f[2]) * tb, int(f[3]) * tb or None, ""))
    return timed(got)


def flash_check(path, j, sides, full=False, stop=None):
    """{subtitle position or sidecar name: flash_plan()} of the text tracks and .srt sidecars whose cues flash
    (docs/design.md, "Subtitle match"). Any language and any role, forced too. The Cues give the duration of each
    block, so only a track whose median entry lasts under subsync.FLASH is read in full, and only when the read
    gets every cue. A track with an entry that has no CueDuration is read too. With full, a track the Cues do not
    index comes from full_read(). stop() true before a read leaves that track and the rest unread. SubRip, ASS, SSA
    and WebVTT tracks. sides are sidecar_subs() entries, read in their file order."""
    codecs, out = sub_codecs(j), {}
    lens = cue_lengths(path, j, {p for p, c in codecs.items() if c in config.SUB_CODECS}, full)
    for p in sorted((p for p, ds in lens.items() if ds and (None in ds or statistics.median(ds) < subsync.FLASH)), key=lambda p: int(p[1:])):
        if stop and stop():
            break
        cues = subtitle_cues(path, j, {p}, full).get(p) or []
        plan = remux.flash_plan(cues, codecs[p] in ("S_TEXT/ASS", "S_TEXT/SSA")) if len(cues) == len(lens[p]) else None
        out.update({p: plan} if plan else {})
    for s in sides:
        with contextlib.suppress(OSError):
            with open(s["path"], "rb") as f:
                plan = remux.flash_plan([(a / 1000, b / 1000, t) for a, b, t in proof.srt_blocks(f.read().decode(proof.PY_CHARSET[s["charset"]], errors="replace"))])
            out.update({s["name"]: plan} if plan else {})
    return out


def subtitle_blocks(path, j, want, codecs, cap, use, failed):
    """{track position: use(its blocks)} for the tracks of codecs at the positions in want. mkvmerge and ffmpeg index
    every subtitle block in the Cues, so the read takes the Cues and then each block by its index entry, one small read
    each. It never reads a Cluster in full, so the cost does not grow with the file size. use gets an iterator of
    (start seconds, duration seconds or None, text) over at most cap blocks, read one at a time while use asks. A
    picture track gives the frame's bytes for its text, and a PGS block only its first bytes, see picture_cues(). A
    track with a content encoding other than zlib, and a track the Cues do not index get no entry, or failed(why). A
    failure keeps what was read. The job's time limit passes."""
    tracks, n, out = {}, 0, {}
    for t in j.get("tracks") or []:
        p = t.get("properties") or {}
        if t.get("type") != "subtitles":
            continue
        n += 1
        if f"s{n}" in want and p.get("codec_id") in codecs and p.get("content_encoding_algorithms") in (None, "0") and p.get("number"):
            tracks[p["number"]] = (f"s{n}", p["codec_id"], p.get("content_encoding_algorithms") == "0")
    if not tracks:
        return out
    scale = ((j.get("container") or {}).get("properties") or {}).get("timestamp_scale") or 1000000   # ns a tick
    try:
        with open(path, "rb", buffering=0) as f:   # unbuffered, so each read takes only the bytes it asks for
            ds, cues = cue_entries(f, set(tracks), cap)
            clusters = {}
            if ds is None:
                return out

            def blocks(number, codec, packed):   # the cues of one track, read one block at a time while use() asks
                head = codec == "S_HDMV/PGS"   # a PGS block can hold a large picture, and its first bytes say all
                for i, (cp, rp, _) in enumerate(cues[number]):
                    config.DEADLINE.check()   # up to cap small reads, each a wait on a cold NFS file
                    if i % config.TEXT_AHEAD == 0:   # start the reads of the next blocks at once, so a cold NFS read waits about once a batch
                        for c, r, _ in cues[number][i:i + config.TEXT_AHEAD]:   # the Cluster header and its Timestamp, then the block
                            os.posix_fadvise(f.fileno(), ds + c, 48, os.POSIX_FADV_WILLNEED)
                            os.posix_fadvise(f.fileno(), ds + c + 5 + r, 1031, os.POSIX_FADV_WILLNEED)
                    if cp not in clusters:   # (data start, Timestamp). The Timestamp comes first, after ffmpeg's CRC-32.
                        f.seek(ds + cp); h = f.read(48); e = decide.element(h, 0)
                        kids = decide.children(h, e[1], len(h)) if e and e[0] == decide.CLUSTER else ()
                        ts = next((int.from_bytes(h[d:d + n], "big") for i, d, n in kids if i == decide.TIMESTAMP), None)
                        clusters[cp] = (e[1], ts) if e and e[0] == decide.CLUSTER else None
                    if clusters[cp] is None:
                        continue
                    f.seek(ds + cp + clusters[cp][0] + rp); blk = f.read(1024); e = decide.element(blk, 0)
                    if not head and e and e[2] is not None and len(blk) < e[1] + e[2] <= config.TEXT_BLOCK:
                        blk += f.read(e[1] + e[2] - len(blk))
                    frame, times = decide.block_head(blk, number) if head else (decide.block_frame(blk, number), decide.block_times(blk))
                    if frame is not None:
                        data = zlib.decompressobj().decompress(frame) if packed else frame   # a cut PGS block gives its first bytes
                        start = (clusters[cp][1] + times[0]) * scale / 1e9 if times and clusters[cp][1] is not None else None
                        text = data if codec in config.PICTURE_CODECS else data.decode("utf-8", "replace")
                        yield start, times[1] * scale / 1e9 if times and times[1] is not None else None, \
                            text.split(",", 8)[-1] if codec in ("S_TEXT/ASS", "S_TEXT/SSA") else text   # an ASS event holds 8 fields before its text
            for number, (pos, codec, packed) in tracks.items():
                out[pos] = use(blocks(number, codec, packed)) if number in cues else failed("the Cues index none of its blocks")
    except content.OutOfTime:
        raise
    except Exception as ex:   # the text is one signal among others, so a failed read never stops a job
        out.update({p: failed(config.mask(f"the read failed: {type(ex).__name__}: {ex}")[:200]) for p, _, _ in tracks.values() if p not in out})
    return out


def pgs_shows(sets):
    """[(start, end, "")] of PGS display sets [(start, None, PCS bytes)] in time order. A set whose composition holds an
    object shows a cue, and the next set that is not a palette update ends it. A palette update only changes colours,
    as in a fade. The last cue ends 5 seconds after it starts."""
    out, since = [], None
    for s, _, pcs in sets:   # a PCS: type 0x16, its size, then width, height, rate, number, state, palette update, palette, objects
        if s is None or len(pcs) < 14 or pcs[0] != 0x16 or (pcs[11] and pcs[13] and since is not None):
            continue
        if since is not None:
            out.append((since, s, ""))
        since = s if pcs[13] else None
    return out + ([(since, since + 5, "")] if since is not None else [])


def picture_cues(path, j, want, full=False):
    """{track position: [(start, end, "")] in seconds} of the PGS and VobSub tracks at the positions in want, for the
    reference timing of --sub-time (docs/design.md, "Subtitle match"). A PGS cue comes from its display sets, see
    pgs_shows(). A VobSub packet is one cue that ends at its stop command, see spu_stop(), else at its
    BlockDuration, else as a text cue ends, see timed(). With full, a track the Cues do not index comes from
    full_read(). No picture cue lasts over subsync.SPAN seconds. A track the read cannot take gets no entry."""
    vob = {t for t, c in sub_codecs(j).items() if c == "S_VOBSUB"}
    got = subtitle_blocks(path, j, want & vob, ("S_VOBSUB",), config.PICTURE_MAX,
                          lambda b: timed((s, spu_stop(spu) or d, "") for s, d, spu in b), lambda why: None)
    got.update(subtitle_blocks(path, j, want - vob, ("S_HDMV/PGS",), config.PICTURE_MAX, lambda b: pgs_shows(sorted(b, key=lambda x: x[0] or 0)),
                               lambda why: None))
    got = {p: c for p, c in got.items() if c}
    if full and set(want) - set(got):
        got.update({p: c for p, c in full_read(path, j)["cues"].items() if p in set(want) - set(got) and c})
    return {p: [(s, min(e, s + subsync.SPAN), t) for s, e, t in c] for p, c in got.items()}


def spu_stop(spu):
    """Seconds from the start of a VobSub packet to its stop command, or None. Each control sequence holds its time in
    units of 1024/90000 s, the offset of the next sequence, and commands of fixed sizes up to 0xff, as ffmpeg's dvdsub
    decoder reads them. 0x02 stops the display."""
    size = {0: 0, 1: 0, 2: 0, 3: 2, 4: 2, 5: 6, 6: 4}
    pos, seen = int.from_bytes(spu[2:4], "big"), set()
    while pos + 4 <= len(spu) and pos not in seen:
        seen.add(pos)
        date, nxt, k = int.from_bytes(spu[pos:pos + 2], "big"), int.from_bytes(spu[pos + 2:pos + 4], "big"), pos + 4
        while k < len(spu) and spu[k] in size:
            if spu[k] == 2:
                return date * 1024 / 90000
            k += 1 + size[spu[k]]
        pos = nxt
    return None


def lid_kept(path, index):
    """[(start, seconds)] of the audio the language check kept for path, see lid.kept_pcm(). [] when lid.py does not import."""
    try:
        from . import lid
    except ImportError:
        return []
    return lid.kept_pcm(os.path.join(config.CFG.state_dir, "lid.sqlite"), path, index)


def sub_on(source, sub_check=False):
    """The subtitle match check runs: on an import and in the deep analysis unless SUBTITLES is off, and in a backfill
    only with --sub-check or --sub-time, whatever SUBTITLES says."""
    return config.CFG.subtitles != "off" if source in ("hook", "deep_analysis") else sub_check


def sub_fixes(source):
    """Whether the subtitle check may change the file: remove a track, retime, lengthen cues, move a sidecar or turn a
    flag off. An import and the deep analysis do at SUBTITLES fix or deep. --sub-check and --sub-time do with --apply."""
    return source not in ("hook", "deep_analysis") or config.CFG.subtitles in ("fix", "deep")


def sub_audio(ts, edits=()):
    """{language key: the ffmpeg audio index of the main track the subtitle check hears for that language}. The track
    that plays wins when it speaks the language, else the first main track that does. ts is decide.classify()."""
    play, out = decide.default_audio(ts, edits), {}
    for i, t in enumerate(t for t in ts if t["kind"] == "a"):
        k = decide.lang_key(t["lang"])
        if t["role"] == "main" and t["lang"] not in decide.UNTAGGED and (k not in out or t is play):
            out[k] = i
    return out


def spaced(lang):
    """The language writes spaces between its words, so the check can compare them, see NO_SPACES."""
    return decide.lang_key(lang) not in {decide.lang_key(x) for x in config.NO_SPACES}


def sub_targets(j, d):
    """{subtitle position: (its language, the ffmpeg audio index it is compared with)} of the text tracks a viewer
    would use: SubRip, ASS, SSA or WebVTT, in a SUB_ROLES role, in the language of a main audio track. j is the mkvmerge
    -J probe of a Matroska file and d its decision."""
    codec, audio = sub_codecs(j), sub_audio(d["tracks"], d["edits"])
    return {t["pos"]: (t["lang"], audio[decide.lang_key(t["lang"])]) for t in d["tracks"] if t["kind"] == "s"
            and codec.get(t["pos"]) in config.SUB_CODECS and t["role"] in config.SUB_ROLES and decide.lang_key(t["lang"]) in audio and spaced(t["lang"])}


def sub_groups(items):
    """{ffmpeg audio index: (language, the cues of all its items, sorted)} of sub_verdicts() items. The items of one
    audio track share its windows, picked from all their cues, and one hearing."""
    out = {}
    for k, (lang, idx, cues) in items.items():
        out.setdefault(idx, (lang, []))[1].extend(cues)
    return {idx: (lang, sorted(cues)) for idx, (lang, cues) in out.items()}


def sub_verdicts(path, j, items, starts=None, line=True, deep=False):
    """{key: result} of the subtitle match check (docs/design.md, "Subtitle match") for items {key: (language, ffmpeg
    audio index, [(start, end, text)] in seconds)}. The items of one audio track share its windows, picked from all
    their cues by subsync.windows(), and one hearing. When a window hears under subsync.MIN_WORDS words and
    the other does not, lid.py hears a longer window in the same part of the file, in the same process. Later
    hearings follow what the check asks for: windows where a far drift puts the speech when too few windows heard
    enough, a longer window around a window with too few matched cues for a fix, and a middle window to confirm a
    ratio. Only the first hearing names a mismatch. A file whose windows hear enough pays for one hearing. result is subsync.check() with "audio", "starts" ([window starts, seconds] of each hearing) and the
    hearings' facts: "cached", "reused", "cpu", "took", "profile". starts {audio index: that list} forces the hearings
    of an earlier check, so a check after a conversion reads the words carried over. A missing install, too little
    time, a timeout or an error gives unknown and never fails the job. The job's time limit passes.

    With line, a fix that subsync.needs_line() names needs one more hearing: a window at a third and one at two
    thirds of the file, which must both sit on the fitted line, see subsync.on_line(). Else the times stay, and
    with deep, an import that queues a deep analysis, the deep analysis judges the fix with its sweep. Those windows never change the verdict. --sub-time and the
    deep analysis pass line False, as their sweep confirms a fix, see sweep_confirms()."""
    dur, out = decide.duration(j), {}
    left = config.DEADLINE.left()   # None when no time limit runs, as in a backfill
    budget, spent = (min(config.SUB_TIMEOUT, left - config.LID_RESERVE) if left else config.SUB_TIMEOUT), [0.0]
    for idx, (lang, cues) in sub_groups(items).items():
        keys, heard, runs = [k for k, x in items.items() if x[1] == idx], [], []
        stop, plan = decide.STOPWORDS.get(lang, frozenset()), (starts or {}).get(idx)

        def listen(ws, secs=subsync.WINDOW, more=None, tag=None):   # one hearing, or why none
            if budget - spent[0] < 10:
                return f"no time left of the {max(budget, 0):.0f} seconds the check may take"
            t0 = time.monotonic()
            got = checks.lid_run(path, idx, j, (), budget - spent[0], words=(lang, ws, secs, more))
            spent[0] += time.monotonic() - t0 - got.get("waited", 0)
            if not got.get("windows"):
                return config.mask(str(got.get("why") or "no words"))[:200]
            heard.extend(got["windows"]); runs.append(dict(got, starts=[ws, secs] + ([more, tag] if tag else [])))   # a replay skips the line
            return None
        first = plan[0][0] if plan else subsync.windows(cues, dur, stop, lid_kept(path, idx))
        more = None if plan else subsync.windows(cues, dur, stop, secs=subsync.THIRD, taken=first) if first else None
        why = listen(first, more=more) if first else "a part of the file holds no cue"
        check = lambda: {k: subsync.check(heard, items[k][2], lang, dur) for k in keys}
        res = {} if why else check()
        before, done = res, set()   # the verdicts of the first hearing, and the later hearings and windows made
        for p in (plan or [])[1:] if not why else ():
            if p[3:] != ["line"]:   # the windows on the line never join the fit, and the same fix picks them again below
                listen(*p[:3])
                res = check()
        for _ in range(4) if not (why or plan) else ():
            ts = [r.get("timing") or {} for r in res.values()]
            poor = {a for t in ts for a in t.get("few", ())}   # windows with too few cues whose first words matched
            seen = max((r["windows"] for r in res.values()), key=len, default=[])   # the windows the check took
            halves = {w["at"] < dur / 2 for w in seen if w["words"] >= subsync.MIN_WORDS and w["at"] not in poor}
            ext = sorted(a for a in poor - done if any(w["at"] == a and w.get("secs", subsync.WINDOW) < subsync.THIRD for w in heard))
            if ext:
                # A window with too few cues whose first words matched: hear a window of THIRD seconds around it.
                done.update(ext)
                step = ([round(max(0.0, a - (subsync.THIRD - subsync.WINDOW) / 2), 1) for a in ext], subsync.THIRD, None)
            elif len(halves) < 2 and "drift" not in done:
                # A half of the file with no window that heard enough. A drift far from 1 moves the speech of the
                # densest cues away from a window at cue time, so hear where each far ratio puts it. A window whose
                # words matched says where the cues sit, see subsync.drift().
                done.add("drift")
                hint = max(((w["overlap"] * w["words"], w["at"] + subsync.WINDOW / 2, w["offset"]) for w in seen
                            if w["offset"] is not None), default=(0,))
                ws = [a for a in subsync.drift([s for s in first if (s < dur / 2) not in halves], hint[1:] if hint[0] >= 3 else None)
                      if all(abs(a - w["at"]) >= subsync.WINDOW / 2 for w in heard)]
                step = (ws, subsync.WINDOW, None)
            elif (fix := next((t["confirm"] for t in ts if t.get("confirm")), None)) and "middle" not in done:
                # A ratio waits for a middle window near the centre between the others, see subsync.middle(). Its
                # densest cues lie at cue times, and the fix to confirm says where their speech is in the audio, many
                # seconds away for a 25 fps track. When it hears too little, as on a chant that Whisper drops, a window
                # of THIRD seconds elsewhere in that part is heard in the same process.
                done.add("middle")
                part = subsync.middle(fix, dur)
                mid = subsync.windows(cues, dur, stop, parts=part)
                alt = subsync.windows(cues, dur, stop, secs=subsync.THIRD, taken=mid, parts=part) if mid else []
                at, alt = ([round(max(0.0, subsync.moved(s * 1000, fix) / 1000), 1) for s in ws if s is not None] for ws in (mid, alt))
                step = (at, subsync.WINDOW, alt or None)
            else:
                break
            if not step[0] or listen(*step):
                break
            res = check()
        small = [k for k, r in res.items() if line and not why and r["verdict"] == "match" and subsync.needs_line((r.get("timing") or {}).get("fix"), dur)]
        if small:   # a small or ratio fix: two more windows must sit on its line, see the docstring
            f0 = res[small[0]]["timing"]["fix"]
            taken = [float(fractions.Fraction(f0["rate"])) * w["at"] + f0["offset"] for w in heard]   # the windows heard so far, in cue time
            ws = subsync.windows(cues, dur, stop, taken=taken, parts=subsync.LINE_PARTS)
            alt = subsync.windows(cues, dur, stop, secs=subsync.THIRD, taken=taken + [s for s in ws if s is not None],
                                      parts=subsync.LINE_PARTS) if ws else []
            audio = lambda s: None if s is None else round(max(0.0, subsync.moved(s * 1000, f0) / 1000), 1)   # where the fix puts its speech
            ws, alt = [audio(s) for s in ws], [audio(s) for s in alt] or [None] * len(ws)
            missed = "no cue lies free at a third or at two thirds of the file" if len(ws) != len(subsync.LINE_PARTS) or None in ws else \
                listen(ws, more=alt, tag="line")
            for k in small:
                t = res[k]["timing"]
                if missed or not subsync.on_line(heard, items[k][2], lang, t["fix"], list(zip(ws, alt))):
                    res[k] = dict(res[k], timing={"fix": None, "unconfirmed": t["fix"], "line": ws, "why": (
                        f'{t["why"]}, but {"the windows at a third and two thirds of the file do not sit on its line" if not missed else missed}, so '
                        + ("the deep analysis judges it" if deep else "the times stay"))})
                else:
                    res[k] = dict(res[k], timing=dict(t, line=ws, why=f'{t["why"]}, and the windows at a third and two thirds of the file sit on its line'))
        for k, r in res.items():   # only the first hearing's windows sit where the cues are dense, so only they name a mismatch
            if r["verdict"] == "mismatch" and before[k]["verdict"] != "mismatch":
                res[k] = dict(r, verdict="unknown", why=f'{r["why"]}, but only in windows heard after the first, which can sit where no cue is')
        facts = {"audio": idx, "starts": [r["starts"] for r in runs] or ([[first, subsync.WINDOW]] if first else []),
                 "cached": all(r.get("cached") for r in runs), "reused": sum(r.get("reused") or 0 for r in runs),
                 **{k: round(sum(r.get(k) or 0 for r in runs), 2) for k in ("cpu", "took", "waited") if any(k in r for r in runs)},
                 **({"profile": [r["profile"] for r in runs if r.get("profile")]} if any(r.get("profile") for r in runs) else {})}
        for k in keys:
            out[k] = dict(verdict="unknown", why=why, windows=[], timing=None, **facts) if why else dict(res[k], **facts)
    return out


def sub_hold(ts, known, lang):
    """Why a mismatch of a subtitle in lang cannot count, or None (docs/design.md, "Subtitle match"). A right subtitle
    can translate other speech than the audio it is compared with, such as the Japanese original beside an English
    dub. A dub script and a translation use other words, so such a track can read as a mismatch. known holds the
    item's original languages as 639-2 codes, from the app or TMDB. When it names one, the mismatch counts unless the
    original is another language than the subtitle, so an English original with a Spanish dub keeps the check. When
    no original is known, another main audio language in the file stands in for it. ts is decide.classify()."""
    key = decide.lang_key(lang)
    orig = {decide.lang_key(x): x for x in sorted(known or ()) if x and x not in decide.UNTAGGED}
    if orig:
        return None if key in orig else f"the original language is {', '.join(sorted(orig.values()))}, and the subtitle may translate it"
    other = sorted({t["lang"] for t in ts if t["kind"] == "a" and t["role"] == "main" and t["lang"] not in decide.UNTAGGED
                    and decide.lang_key(t["lang"]) != key})
    return f"no original language is known, and the file also carries {', '.join(other)} audio, which the subtitle may translate" \
        if other else None


def sub_held(res, items, ts, known):
    """res of sub_verdicts() with each mismatch that sub_hold() rules out turned into unknown, its why extended."""
    for k, r in res.items():
        why = r["verdict"] == "mismatch" and sub_hold(ts, known, items[k][0])
        if why:
            r.update(verdict="unknown", why=f'{r["why"]}, but {why}', held=True)
    return res


def side_code(s):
    """The 639-2 code of a sidecar's language: its name may give en, and the tracks give eng."""
    base = s["lang"].split("-")[0]
    return checks.langs()[0].get(base) or base


def mkv_sidecars(path, d):
    """{name: sidecar_subs() entry} of the .srt files beside a Matroska file that the check takes: in the language of a
    main audio track and not forced. A program such as Bazarr writes its downloads there."""
    audio = sub_audio(d["tracks"], d["edits"])
    return {s["name"]: s for s in proof.sidecar_subs(path) if decide.lang_key(side_code(s)) in audio and proof.SIDECAR_FLAGS["forced"] not in s["flags"]
            and spaced(side_code(s))}


def sub_items(path, j, d, sides, full=False):
    """The sub_verdicts() items of a Matroska file: the tracks of sub_targets() with their cues read by
    subtitle_cues(), with full as there, and the sidecars sides of mkv_sidecars()."""
    targets, audio = sub_targets(j, d), sub_audio(d["tracks"], d["edits"])
    cues = subtitle_cues(path, j, set(targets), full) if targets else {}
    items = {p: (lang, idx, cues.get(p) or []) for p, (lang, idx) in targets.items()}
    items.update({n: (side_code(s), audio[decide.lang_key(side_code(s))], [(a / 1000, b / 1000, t) for a, b, t in s["cues"]])
                  for n, s in sides.items()})
    return items


def sub_jobs(path, j, d, sides):
    """A file of the subtitle check's hearings for lid.jobs(), written into STATE_DIR, or None when nothing would be
    heard. A language check runs them with the model it loaded, see hear(). The caller removes the file."""
    items = sub_items(path, j, d, sides) if decide.duration(j) >= config.SUB_MIN_SECONDS else {}
    if not items:
        return None
    fd, name = tempfile.mkstemp(prefix="subjobs-", suffix=".json", dir=config.CFG.state_dir)
    with os.fdopen(fd, "w") as f:
        json.dump([{"index": idx, "lang": lang, "cues": cues, "duration": decide.duration(j)} for idx, (lang, cues) in sub_groups(items).items()], f)
    return name


def sub_match(path, j, d, starts=None, sides=None, known=(), items=None, full=False, line=True, deep=False):
    """{subtitle position or sidecar name: sub_verdicts() result} for the tracks of sub_targets(), with their cues read
    by subtitle_cues(), and the sidecars sides of mkv_sidecars(). {} when nothing qualifies or the file runs under
    SUB_MIN_SECONDS. So a file with no such track or sidecar costs nothing. known holds the item's original languages,
    and a mismatch that sub_hold() rules out is unknown. items is sub_items() when the caller read them already, and
    full reads a track the Cues do not index, see full_read()."""
    sides = sides or {}
    if not (sub_targets(j, d) or sides) or decide.duration(j) < config.SUB_MIN_SECONDS or not checks.lid_ready():
        return {}
    items = items or sub_items(path, j, d, sides, full)
    return sub_held(sub_verdicts(path, j, items, starts, line, deep), items, d["tracks"], known)


def ref_sidecars(path, d):
    """{name: sidecar_subs() entry} of the .srt files beside a Matroska file that the word check leaves out and the
    reference timing of --sub-time takes: not forced, and not in mkv_sidecars()."""
    words = mkv_sidecars(path, d)
    return {s["name"]: s for s in proof.sidecar_subs(path) if s["name"] not in words and proof.SIDECAR_FLAGS["forced"] not in s["flags"]}


def sub_reference(path, j, d, sync, items, others, sweeps=None, full=False, stop=None, report=True):
    """({subtitle position or sidecar name: subsync.reference() result, with its codec, language and role},
    {reference: "fixed", "in time" or "clean sweep"}) of the reference timing (docs/design.md, "Subtitle match"). It
    takes the subtitles the word check does not read: a text or picture track in a SUB_ROLES role that sub_targets()
    leaves out, and the sidecars others of ref_sidecars(). A reference is a track or sidecar of sync, the word check,
    with a match whose times are in time or fixed. A match with too few anchors for a fix is one too when its rows of
    sweeps are clean, see subsync.clean(). Its cues come from items, sub_items(), and move into audio time first:
    by its fix, or by its measured offset when it has none, the timing's for an in-time match, sweep_offset() for a
    clean sweep.

    full reads a track the Cues do not index from the whole file, see full_read(). Once stop() is true, the time
    limit less SUB_RESERVE in an import, no track is read or fit, and each one left is "deferred" to the deep
    analysis. The deep analysis passes deep_waits(), which stops it for an import instead. With no reference nothing is
    read, and report False gives ({}, {}) then."""
    refs, basis, dur = {}, {}, decide.duration(j)
    for k, r in sync.items():
        t = r.get("timing") or {}
        basis[k] = "fixed" if t.get("fix") else "in time" if t.get("why") == "in time" else \
            "clean sweep" if ("few" in t or t.get("swept")) and subsync.clean((sweeps or {}).get(k) or [], dur) else None
        if r["verdict"] == "match" and basis[k] and k in items:
            # The reference in audio time: moved by its fix, else by its own measured offset, which may reach MIN_SHIFT
            # while it is in time. So the target is judged, and moved, against the audio.
            f = t.get("fix") or {"rate": "1/1", "offset": (t.get("offset") or 0.0) if basis[k] == "in time" else sweep_offset((sweeps or {}).get(k))}
            refs[k] = [(subsync.moved(a * 1000, f) / 1000, subsync.moved(b * 1000, f) / 1000, x) for a, b, x in items[k][2]]
    if not (refs or report):
        return {}, {}
    codecs, targets = sub_codecs(j), sub_targets(j, d)
    tracks = {t["pos"]: t for t in d["tracks"] if t["kind"] == "s" and t["role"] in config.SUB_ROLES and t["pos"] not in targets
              and codecs.get(t["pos"]) in config.SUB_CODECS + config.PICTURE_CODECS}
    late = lambda: bool(stop and stop())   # once true it stays true: the deadline has passed, or deep_waits() raised
    none = lambda: "no track or sidecar of the file matched the audio in its words with its times right or fixed" if not refs else \
        f"the read got no cues: {full_read(path, j)['why'] or 'the whole-file read found none'}" if full else \
        "the Cues index none of its blocks, and an import never reads the whole file"
    out, later = {}, {"verdict": "deferred", "why": "the import had no time left to read or fit it, so the deep analysis times it", "timing": None}
    for p, t in sorted(tracks.items(), key=lambda x: int(x[0][1:])):   # one track at a time, each read and each fit after the stop test
        cues = None if not refs or late() else (subtitle_cues if codecs[p] in config.SUB_CODECS else picture_cues)(path, j, {p}, full).get(p)
        r = later if refs and late() else subsync.reference(cues, refs, dur) if cues else {"verdict": "unknown", "why": none(), "timing": None}
        out[p] = dict(r, codec=codecs[p], lang=t["lang"], role=t["role"])
    for n, s in others.items():
        role = "sdh" if {proof.SIDECAR_FLAGS["hi"], proof.SIDECAR_FLAGS["sdh"]} & set(s["flags"]) else "full"
        r = later if refs and late() else subsync.reference([(a / 1000, b / 1000) for a, b, _ in s["cues"]], refs, dur)
        out[n] = dict(r, codec="srt", lang=side_code(s), role=role)
    return out, {k: basis[k] for k in refs}


def swept_before(rec, ex):
    """rec of a pass that followed Replan ex, with the sweep facts of the pass that raised it added to its own. The
    second pass finds the words in the cache, so its facts alone would hide what the first pass heard."""
    f, g = (ex.args[1] if len(ex.args) > 1 else None), rec.get("sweep_facts")
    if f and g:
        rec["sweep_facts"] = dict(g, cpu=round(g["cpu"] + f["cpu"], 1), took=round(g["took"] + f["took"], 1), runs=g.get("runs", 0) + f.get("runs", 0),
                                  cached=g.get("cached", 0) + f.get("cached", 0), failed=g["failed"] + [x for x in f["failed"] if x not in g["failed"]])
    return rec


def sweep_confirms(rows):
    """Why the sweep() rows of a track, fitted to its fix, do not confirm that fix, or None. The rows to trust, with an
    offset and MIN_CUES cues, must sit at least as close to the fitted line as they sat to the audio before the fix: as
    many within TOLERANCE, and at a median distance no larger. So a sweep that shows the track in time blocks a fix
    that two windows asked for, and a sweep that heard under SWEPT such windows cannot confirm one."""
    ok = [w for w in rows if w["off"] is not None and w["offset"] is not None and w["cues"] >= subsync.MIN_CUES]
    if len(ok) < subsync.SWEPT:
        return f"the sweep heard {len(ok)} windows to judge the fix by, under {subsync.SWEPT}, so the times stay"
    near = lambda k: sum(abs(w[k]) <= subsync.TOLERANCE for w in ok)
    far = lambda k: statistics.median(abs(w[k]) for w in ok)
    if near("off") < near("offset") or far("off") > far("offset"):
        return (f"the sweep puts {near('off')} of {len(ok)} windows within {subsync.TOLERANCE} s of the fix and {near('offset')} within it of "
                f"the audio as they are, so the times stay")
    return None


def sweep_offset(rows):
    """The median offset of the sweep() rows that heard MIN_WORDS words: where a clean sweep puts the track's cues."""
    got = [w["offset"] for w in rows or [] if w["words"] >= subsync.MIN_WORDS and w["offset"] is not None]
    return statistics.median(got) if got else 0.0


class Yielded(Exception):
    """The deep analysis stopped between two steps, because an import waits. Its job stays queued, and the words heard so
    far stay cached, see deep_analysis()."""


def sub_sweep(path, j, items, sync, deep=False):
    """({subtitle position or sidecar name: subsync.sweep() rows}, facts) of the sweep of --sub-time and the deep
    analysis over the tracks and sidecars of the word check, items of sub_items() (docs/design.md, "Subtitle match"). Each
    minute of the file gets one window of subsync.WINDOW seconds at its densest cues. One lid.py process hears
    them with the model loaded once, two windows a Whisper run, and caches each pair like the check's own words.
    Between two runs it yields when an import's hearing waits for the model, and then hears the rest with its next
    turn. In the deep analysis it also yields to an import job in the queue, and deep raises Yielded then. It only
    reports. facts holds the CPU and wall seconds and why a hearing heard nothing."""
    dur, out, facts = decide.duration(j), {}, {"cpu": 0.0, "took": 0.0, "failed": [], "runs": 0, "cached": 0}
    gate = os.path.join(config.CFG.state_dir, "lid.turn.gate")
    for idx, (lang, cues) in sub_groups(items).items():
        stop = decide.STOPWORDS.get(lang, frozenset())
        starts = [w for m in range(int(dur // 60) + 1) for w in subsync.windows(cues, dur, stop, parts=((m * 60 / dur, min(1.0, (m + 1) * 60 / dur)),))]
        heard, left = [], list(starts)
        while left:
            got = checks.lid_run(path, idx, j, (), config.SUB_TIMEOUT * (len(left) // 2 + 1), words=(lang, left, subsync.WINDOW, None, 2),
                          yield_to=(gate, store.path() if deep else None))
            heard += got.get("windows") or []
            facts.update(cpu=round(facts["cpu"] + (got.get("cpu") or 0), 1), took=round(facts["took"] + (got.get("took") or 0), 1),
                         runs=facts["runs"] + 1, cached=facts["cached"] + bool(got.get("cached")))
            if not got.get("windows"):
                facts["failed"].append(f'audio {idx}: {config.mask(str(got.get("why") or "no words"))[:120]}')
            if not got.get("yielded") or not got.get("windows"):
                break
            if deep and runner.queued():
                raise Yielded(f"an import waits, after {len(heard)} of {len(starts)} sweep windows")
            left = left[len(got["windows"]):]
        out.update({k: subsync.sweep(heard, x[2], lang, sync.get(k, {}).get("timing")) for k, x in items.items() if x[1] == idx})
    return out, facts


def side_stats(path):
    """{name: [size, mtime_ns]} of the .srt files beside path that start with its base name."""
    folder, base = os.path.split(os.path.splitext(os.path.abspath(path))[0])
    out = {}
    with contextlib.suppress(OSError):
        for n in os.listdir(folder):
            if n.startswith(base + ".") and n.lower().endswith(".srt"):
                with contextlib.suppress(OSError):
                    st = os.stat(os.path.join(folder, n))
                    out[n] = [st.st_size, st.st_mtime_ns]
    return out


def sub_cache(path, verdicts, pending):
    """Cache the subtitle check's verdicts for path as it is now, with its sidecars, see lid.verdict_put()."""
    with contextlib.suppress(ImportError):
        from . import lid
        lid.verdict_put(os.path.join(config.CFG.state_dir, "lid.sqlite"), path, {"verdicts": verdicts, "sidecars": side_stats(path)}, pending)


def sub_cached(path):
    """The subtitle check of path as it is now is cached and asks for nothing more, so a backfill with --sub-check
    skips the file. A dry run's verdict that asks for an action does not count, so an apply after it still acts. A
    sidecar that came, went or changed since counts as a change, so a new download from a program such as Bazarr is checked."""
    try:
        from . import lid
    except ImportError:
        return False
    got = lid.verdict_get(os.path.join(config.CFG.state_dir, "lid.sqlite"), path)
    return bool(got) and not got[1] and isinstance(got[0], dict) and got[0].get("sidecars") == side_stats(path)


def sidecar_fix(sides, sync, apply, app, source, ends=None):
    """Act on the sidecars beside a Matroska file (docs/design.md, "Subtitle match"). A sidecar that does not match the
    audio moves into originals_root() as a kept original. A sidecar whose times need a fix, or whose cues flash, with
    ends {name: flash_plan()}, is written again with new times, and its original is kept the same way. Both need a
    place to keep the original, so with KEEP_ORIGINALS_DAYS 0 the sidecar stays as it is. Each move and rewrite gets a
    log line. Returns one entry per sidecar with an action: {name, action: "move", "retime" or "lengthen", why, result:
    "moved", "retimed", "left" or "dry run", kept or left}."""
    out, ends = [], ends or {}
    for n, s in sorted(sides.items()):
        r, plan = sync.get(n) or {}, ends.get(n)
        fix = (r.get("timing") or {}).get("fix")
        if r.get("verdict") != "mismatch" and not fix and not plan:
            continue
        move = r.get("verdict") == "mismatch"
        e = {"name": n, "action": "move" if move else "retime" if fix else "lengthen",
             "why": r["why"] if move else "; ".join(x for x in (fix and r["timing"]["why"], plan and flash_why(plan)) if x)}
        out.append(e)
        if not apply or not sub_fixes(source):
            e.update(result="dry run") if not apply else e.update(result="left", left="SUBTITLES is check, so the file stays as it is")
            continue
        tmp = remux.repack_tmp(s["path"])
        try:
            st = os.stat(s["path"])
            why = vault.keepable(s["path"], st) if config.CFG.keep_days else "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept"
            if why: raise OSError(why)
            if not move:   # a new file over the name, so the kept hard link holds the old text
                with open(s["path"], "rb") as f:
                    text = f.read().decode(proof.PY_CHARSET[s["charset"]], errors="replace")
                try:
                    text = remux.set_ends(text.replace("\r\n", "\n"), False, plan) if plan else text
                except RuntimeError as ex:
                    raise OSError(f"its new ends do not fit its text: {ex}") from None
                text = remux.srt_moved(text, fix) if fix else text
                remux.new_tmp(tmp)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                with contextlib.suppress(PermissionError):   # a NAS share may refuse chown. The mode stays.
                    os.chown(tmp, st.st_uid, st.st_gid)
                os.chmod(tmp, st.st_mode & 0o7777)
            e["kept"] = vault.keep_original(s["path"], e)
            os.remove(s["path"]) if move else os.replace(tmp, s["path"])
            e["result"] = "moved" if move else "retimed"
            logs.log(dict(app=app, source=source, path=s["path"], result=f"sidecar {e['result']}", kept=e["kept"], why=e["why"]))
        except OSError as ex:
            e.update(result="left", left=config.mask(str(ex))[:200])
        finally:
            remux.drop_tmp(tmp)
    if any(e.get("kept") for e in out):
        vault.prune_originals(vault.originals_root(out[0]["kept"]))
    return out


def flash_why(plan):
    """Why a flash_plan() lengthens the ends: the median cue and how many cues get a new end."""
    return (f"its median cue shows {statistics.median(o - s for s, _, o, _ in plan):.2f} s, so {sum(n != o for _, _, o, n in plan)} of "
            f"{len(plan)} cues get new ends")


def sub_findings(rec, sync, unmatched):
    """The findings of the subtitle match check, each a list of sentence codes and facts, see report.SUB_LINES: one
    submatch finding for the tracks and sidecars that do not match the audio, and one subtiming finding for the tracks
    whose times are off and stay. unmatched holds the tracks, by their place before any remux, that stayed in the file
    and lost their flags instead. A dry run also records what --apply would do with the file, see remux_block()."""
    rp, rm, wrong, late = rec.get("repack") or {}, rec.get("subremux") or {}, [], []
    plan = {} if rec.get("apply", True) else remux_block(rec)
    flags_off, by = sub_fixes(rec.get("source")), "hook" if rec.get("source") in ("hook", "deep_analysis") else "run"
    for p in rm.get("removed") or []:
        wrong.append({"code": "removed", "track": p, "why": sync[p]["why"], "by": by, "kept": rm.get("kept")})
    for p in unmatched:
        gone = p in (rm.get("remove") or []) and not rm.get("removed")   # the remux was to remove it
        wrong.append({"code": "stays", "track": p, "why": sync[p]["why"], "gone": gone, "result": rm["result"] if gone else None,
                      "kept_back": rm.get("kept_back"), "flags_off": flags_off, **plan})
    for e in rec.get("sidecars") or []:
        if e["action"] == "move":
            wrong.append({"code": "sidecar", "name": e["name"], "why": e["why"], "kept": e.get("kept"), "left": e.get("left")})
        elif e["result"] == "left":
            late.append({"code": "sidecar_left", "name": e["name"], "why": e["why"], "left": e["left"]})
    for e in rp.get("sidecars_unmatched", []):
        wrong.append({"code": "converted_sidecar", "name": e["name"], "why": e["why"], "kept": e.get("moved"), "left": e.get("left")})
    for p in [] if rp.get("tracks_kept_back") else rp.get("tracks_unmatched", []):   # a track that stayed: the new file's check says so
        wrong.append({"code": "converted_track", "track": p, "why": rp["subcheck"][p]["why"], "kept": rp.get("kept")})
    for p, r in sorted(sync.items()):
        t = r.get("timing") or {}
        if (t.get("piecewise") and round(max(t["offsets"]) - min(t["offsets"]), 2) >= config.STEP_ALERT) or "unfixed" in t:
            late.append({"code": "off", "track": p, "ref": r.get("reference"), "why": t["why"]})
    if (rm.get("fixed") or rm.get("ended")) and not rm.get("done"):
        late.append({"code": "not_retimed", "tracks": sorted({*(rm.get("fixed") or []), *(rm.get("ended") or [])}), "result": rm.get("result"),
                     "block": plan.get("block")})
    if not flags_off:
        late += [{"code": "check_times", "track": p, "why": r["timing"]["why"]} for p, r in sorted(sync.items()) if (r.get("timing") or {}).get("fix")]
        late += [{"code": "check_flash", "track": p, "median": f["median"]} for p, f in sorted((rec.get("flash") or {}).items())]
    far = [[k, w["at"], w["off"]] for k, rows in sorted((rec.get("sweep") or {}).items()) for w in rows if id(w) in cli.sweep_steps(rows)]
    if far:
        late.append({"code": "sweep", "far": far})
    return ([{"kind": "submatch", "lines": wrong}] if wrong else []) + ([{"kind": "subtiming", "lines": late}] if late else [])


def remux_block(rec):
    """What --apply would do with the file of a dry run's rec, as facts for report.block(). hardlinked says the file
    has another hard link. block is {"code", ...}, or None when the run planned no remux. code is remux, or the reason
    --apply would skip the subtitle remux, one of hardlinked, cap, space, keep_root, keep_create and keep. A skip asks
    repack_block() again for its reason. A folder that keeps no original names the uid and gid of this run, which PUID
    and PGID set in Docker. A file gone since the check gives {}."""
    path, codes = rec["path"], (rec.get("subremux") or {}).get("codes") or []
    try:
        st = os.stat(path)
        out = {"hardlinked": vault.links(st) > 1, "block": {"code": "remux"} if "would_remux_subtitles" in codes else None}
        if "subtitle_remux_skipped" not in codes:
            return out
        code, why = remux.repack_block(path, st)
        if code in ("hardlinked", "cap"):
            return dict(out, block={"code": code, "why": why})
        if code == "space":
            fs = os.statvfs(os.path.dirname(path))
            return dict(out, block={"code": code, "folder": os.path.dirname(path), "free": fs.f_bavail * fs.f_frsize / 1e9, "need": 2 * st.st_size / 1e9})
        root, folder, user = vault.originals_root(path), vault.keep_folder(path), f"uid {os.getuid()} and gid {os.getgid()}"
        if code == "keep" and not os.access(folder, os.W_OK):   # creating the folder is enough when it is below the root
            return dict(out, block={"code": "keep_root" if folder == root else "keep_create", "root": root, "user": user})
        if code == "keep":   # a keep folder on another file system with no room for a copy, or one that does not read
            return dict(out, block={"code": code, "why": vault.keepable(path, st) or why})
        return dict(out, block={"code": "remux"})   # no code: the cause is gone since the check
    except OSError:
        return {}


def renumber(m, gone):
    """m with each subtitle position key moved down past the removed positions gone, and the removed keys dropped.
    Other keys stay."""
    out = {}
    for k, v in (m or {}).items():
        if k.startswith("s") and k[1:].isdigit():
            if k in gone: continue
            k = f"s{int(k[1:]) - sum(int(g[1:]) < int(k[1:]) for g in gone)}"
        out[k] = v
    return out
