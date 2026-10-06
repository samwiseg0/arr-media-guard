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


class Cut(list):
    """The cues of a track whose read was cut short: it stopped at CUE_MAX or PICTURE_MAX blocks, or after READ_WALL
    seconds, or FULL_WALL for full_read(). So the track goes on past its last cue here. A window
    past that cue holds none of the track's cues and could pair its words only with cues far away by chance. So no check
    judges such a window, see before_cut(). A remux rewrites every cue of a track, and a plan of new times or text
    holds only the cues read. So a Cut track gets no plan of its own cues: no block, live, flash or garbled fix, see
    sub_dense(), flash_check() and garbled_tracks(). A whole-track fix still moves every cue. Nor does it time another
    track, see sub_reference()."""


def cut_end(cues):
    """The end of the last cue of cues when its read was cut short, see Cut, else None."""
    return max(e for _, e, _ in cues) if isinstance(cues, Cut) else None


def before_cut(heard, cues):
    """The windows of heard, lid.py's, that end by cut_end() of cues. Every window when the read of cues was whole."""
    end = cut_end(cues)
    return heard if end is None else [w for w in heard if w["at"] + w.get("secs", subsync.WINDOW) <= end]


def subtitle_cues(path, j, want, full=False):
    """{track position: [(start, end, text)] in seconds, see timed()} of every cue of the text subtitle tracks at the
    positions in want, for the subtitle match check (docs/design.md, "Subtitle match"). SubRip, ASS, SSA and WebVTT
    tracks. A track whose Cues list CUE_MAX blocks or more gives a Cut, as the read stopped there, and so does a read
    that took READ_WALL seconds. A block that gives no cue still counts. With full, a track the Cues do not index comes
    from full_read(). A track the read cannot take gets no entry."""
    capped = set()
    got = subtitle_blocks(path, j, want, config.SUB_CODECS, config.CUE_MAX, timed, lambda why: None, capped)
    got = {p: Cut(c) if p in capped else c for p, c in got.items() if c}
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
    failure gives no cues and why. A track keeps CUE_MAX cues at most, PICTURE_MAX for pictures, and a read stops after
    FULL_WALL seconds. A track cut short either way gives a Cut, and a stop gives why too."""
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
            until, late = time.monotonic() + config.FULL_WALL, False
            while fds:   # read every pipe as it fills, so ffmpeg never waits on a full one
                if (left := until - time.monotonic()) <= 0:   # a slow share: the read stops here, see Cut
                    late = True
                    proc.kill()
                    break
                for fd, _ in poll.poll(left * 1000):
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
            if proc.returncode and not late:
                raise RuntimeError(f"ffmpeg exited {proc.returncode}: {config.mask(err.decode('utf-8', 'replace').strip())[-200:]}")
            for p, codec in take.items():
                fmt = FULL_OUT[codec][2]
                data = bytes(keep[p]) if fmt == "sup" or not late else bytes(keep[p][:keep[p].rfind(b"\n") + 1])   # whole lines only
                c, cap = full_cues(data, fmt), config.PICTURE_MAX if fmt in ("sup", "framecrc") else config.CUE_MAX
                out["cues"][p] = Cut(c[:cap]) if late or len(c) >= cap else c
            if late:
                out["why"] = f"the read stopped after {config.FULL_WALL} s, so each track holds only the cues read"
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
    gets every cue. A track whose read was cut short gets no plan, see Cut. A track with an entry that has no
    CueDuration is read too. With full, a track the Cues do not
    index comes from full_read(). stop() true before a read leaves that track and the rest unread. SubRip, ASS, SSA
    and WebVTT tracks. sides are sidecar_subs() entries, read in their file order."""
    codecs, out = sub_codecs(j), {}
    lens = cue_lengths(path, j, {p for p, c in codecs.items() if c in config.SUB_CODECS}, full)
    for p in sorted((p for p, ds in lens.items() if ds and (None in ds or statistics.median(ds) < subsync.FLASH)), key=lambda p: int(p[1:])):
        if stop and stop():
            break
        cues = subtitle_cues(path, j, {p}, full).get(p) or []
        plan = remux.flash_plan(cues, codecs[p] in ("S_TEXT/ASS", "S_TEXT/SSA")) if len(cues) == len(lens[p]) and not isinstance(cues, Cut) else None
        out.update({p: plan} if plan else {})
    for s in sides:
        plan = remux.flash_plan(side_cues(s))
        out.update({s["name"]: plan} if plan else {})
    return out


def side_cues(s):
    """[(start, end, text)] in seconds of the cues of a sidecar_subs() entry s in file order, see proof.srt_blocks(). A
    plan of new times pairs with the cues of the file in this order, see remux.set_ends(). A file that does not read
    gives []."""
    try:
        with open(s["path"], "rb") as f:
            return [(a / 1000, b / 1000, t) for a, b, t in proof.srt_blocks(f.read().decode(proof.PY_CHARSET[s["charset"]], errors="replace"))]
    except OSError:
        return []


def subtitle_blocks(path, j, want, codecs, cap, use, failed, capped=None):
    """{track position: use(its blocks)} for the tracks of codecs at the positions in want. mkvmerge and ffmpeg index
    every subtitle block in the Cues, so the read takes the Cues and then each block by its index entry, one small read
    each. It never reads a Cluster in full, so the cost does not grow with the file size. use gets an iterator of
    (start seconds, duration seconds or None, text) over at most cap blocks, read one at a time while use asks. A
    picture track gives the frame's bytes for its text, and a PGS block only its first bytes, see picture_cues(). A
    track with a content encoding other than zlib, and a track the Cues do not index get no entry, or failed(why). A
    failure keeps what was read. The job's time limit passes. A track's read stops after READ_WALL seconds. The set
    capped gets the position of each track whose read stopped there, or whose Cues list cap blocks or more."""
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
            clusters, late = {}, set()
            if ds is None:
                return out

            def blocks(number, codec, packed):   # the cues of one track, read one block at a time while use() asks
                head = codec == "S_HDMV/PGS"   # a PGS block can hold a large picture, and its first bytes say all
                until = time.monotonic() + config.READ_WALL
                for i, (cp, rp, _) in enumerate(cues[number]):
                    config.DEADLINE.check()   # up to cap small reads, each a wait on a cold NFS file
                    if i and time.monotonic() > until:   # a slow share: the read stops here, see Cut
                        late.add(number)
                        return
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
                        # An ASS event holds 8 fields before its text. HandBrake ends each event with a NUL byte, and mkvextract
                        # drops it. So the text of a cue leaves it out, and a plan pairs with the extracted text, see remux.set_ends().
                        yield start, times[1] * scale / 1e9 if times and times[1] is not None else None, \
                            text.split(",", 8)[-1].removesuffix("\0") if codec in ("S_TEXT/ASS", "S_TEXT/SSA") else text
            for number, (pos, codec, packed) in tracks.items():
                out[pos] = use(blocks(number, codec, packed)) if number in cues else failed("the Cues index none of its blocks")
                if capped is not None and (number in late or len(cues.get(number) or ()) >= cap):
                    capped.add(pos)
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
    full_read(). No picture cue lasts over subsync.SPAN seconds. A track the read cannot take gets no entry. A read cut
    short gives a Cut, as in subtitle_cues()."""
    vob, capped = {t for t, c in sub_codecs(j).items() if c == "S_VOBSUB"}, set()
    got = subtitle_blocks(path, j, want & vob, ("S_VOBSUB",), config.PICTURE_MAX,
                          lambda b: timed((s, spu_stop(spu) or d, "") for s, d, spu in b), lambda why: None, capped)
    got.update(subtitle_blocks(path, j, want - vob, ("S_HDMV/PGS",), config.PICTURE_MAX, lambda b: pgs_shows(sorted(b, key=lambda x: x[0] or 0)),
                               lambda why: None, capped))
    got = {p: Cut(c) if p in capped else c for p, c in got.items() if c}
    if full and set(want) - set(got):
        got.update({p: c for p, c in full_read(path, j)["cues"].items() if p in set(want) - set(got) and c})
    return {p: (Cut if isinstance(c, Cut) else list)((s, min(e, s + subsync.SPAN), t) for s, e, t in c) for p, c in got.items()}


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


SUB_SOURCES = ("hook", "deep_analysis", "recheck")   # an import and the jobs of the background queue follow SUBTITLES


def sub_on(source, sub_check=False):
    """The subtitle match check runs: on an import, in the deep analysis and in a recheck unless SUBTITLES is off, and
    in a backfill only with --sub-check or --sub-time, whatever SUBTITLES says."""
    return config.CFG.subtitles != "off" if source in SUB_SOURCES else sub_check


def sub_fixes(source):
    """Whether the subtitle check may change the file: remove a track, retime, lengthen cues, move a sidecar or turn a
    flag off. An import, the deep analysis and a recheck do at SUBTITLES fix or deep. --sub-check and --sub-time do with
    --apply."""
    return source not in SUB_SOURCES or config.CFG.subtitles in ("fix", "deep")


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


def sub_verdicts(path, j, items, starts=None, line=True, deep=False, streams=None):
    """{key: result} of the subtitle match check (docs/design.md, "Subtitle match") for items {key: (language, ffmpeg
    audio index, [(start, end, text)] in seconds)}. The items of one audio track share its windows, picked from all
    their cues by subsync.windows(), and one hearing. When a window hears under subsync.MIN_WORDS words and
    the other does not, lid.py hears a longer window in the same part of the file, in the same process. Later
    hearings follow what the check asks for: windows where a far drift puts the speech when too few windows heard
    enough, then windows where a matched window's offset puts the speech of a late track that runs past the end, a
    longer window around a window with too few matched cues for a fix, and a middle window to confirm a ratio. Only the
    first hearing names a mismatch. A file whose windows hear enough pays for one hearing. result is subsync.check()
    with "audio", "starts" ([window starts, seconds] of each hearing) and the hearings' facts: "cached", "reused",
    "cpu", "took", "profile". starts {audio index: that list} forces the hearings of an earlier check, so a check after
    a conversion reads the words carried over. A missing install, too little time, a timeout or an error gives unknown
    and never fails the job. The job's time limit passes.

    With line, a fix that subsync.needs_line() names needs one more hearing: a window at a third and one at two
    thirds of the file, which must both sit on the fitted line, see subsync.on_line(). Else the times stay, and
    with deep, an import that queues a deep analysis, the deep analysis judges the fix with its sweep. Those windows never change the verdict.
    With line, a fix must also raise the share of the word check's matched cues in their spans, see subsync.timing().
    --sub-time and the deep analysis pass line False, as their sweep judges a fix, see sweep_confirms() and sub_sweep().

    streams is where the video and the audio end, from the header probe of the caller. Without it, the hearing at a
    matched window's offset probes the header again, see checks.header_of()."""
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
        check = lambda: {k: subsync.check(before_cut(heard, items[k][2]), items[k][2], lang, dur, gain=line) for k in keys}   # see Cut
        res = {} if why else check()
        before, done = res, set()   # the verdicts of the first hearing, and the later hearings and windows made
        for p in (plan or [])[1:] if not why else ():
            if p[3:] != ["line"]:   # the windows on the line never join the fit, and the same fix picks them again below
                listen(*p[:3])
                res = check()
        for _ in range(5) if not (why or plan) else ():   # ext, drift, hint and ext leave a fifth for the middle window
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
            elif len(halves) < 2 and "hint" not in done and (hint := max(
                    ((w["overlap"] * w["words"], w["at"] + subsync.WINDOW / 2, w["offset"]) for w in seen
                     if w["words"] >= subsync.MIN_WORDS and w["overlap"] >= subsync.MATCH), default=None)) \
                    and hint[2] > 0 and max(c[1] for c in cues) > (end := streams or (checks.header_of(path, j) or {}).get("streams")
                                                                   or checks.audio_span(path, j, idx)):
                # A window that counts toward a match says where the cues sit, and the other half still has no window.
                # Subtitles of another cut can sit minutes late at ratio 1, where no far ratio puts a window. Such a
                # track runs past the end of the audio and the video. The header probe reads where they end in the
                # last clusters. The late track sets the header duration, and a file that ffmpeg wrote has no DURATION
                # tag, so audio_span() gives the header duration there. On such a file the probe reads every subtitle
                # event, see subtitle_ends(). So the caller passes the end it read, and the probe runs again here only
                # without it. A late track whose tail is in time ends before the video and the audio end. A fix through
                # this window would move that tail early, so that track gets no hearing here.
                # Hear where the offset puts the speech of that half's densest cues, at ratio 1 and at each far
                # ratio, before the audio ends. One such hearing at most.
                done.add("hint")
                ws = [a for a in subsync.drift([s for s in first if (s < dur / 2) not in halves], hint[1:], ((1,),) + subsync.FAR)
                      if a + subsync.WINDOW <= end and all(abs(a - w["at"]) >= subsync.WINDOW / 2 for w in heard)]
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
                if missed or not subsync.on_line(before_cut(heard, items[k][2]), items[k][2], lang, t["fix"], list(zip(ws, alt))):
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


def sub_match(path, j, d, starts=None, sides=None, known=(), items=None, full=False, line=True, deep=False, streams=None):
    """{subtitle position or sidecar name: sub_verdicts() result} for the tracks of sub_targets(), with their cues read
    by subtitle_cues(), and the sidecars sides of mkv_sidecars(). {} when nothing qualifies or the file runs under
    SUB_MIN_SECONDS. So a file with no such track or sidecar costs nothing. known holds the item's original languages,
    and a mismatch that sub_hold() rules out is unknown. items is sub_items() when the caller read them already, and
    full reads a track the Cues do not index, see full_read(). streams is the stream end of the header probe, see
    sub_verdicts()."""
    sides = sides or {}
    if not (sub_targets(j, d) or sides) or decide.duration(j) < config.SUB_MIN_SECONDS or not checks.lid_ready():
        return {}
    items = items or sub_items(path, j, d, sides, full)
    return sub_held(sub_verdicts(path, j, items, starts, line, deep, streams), items, d["tracks"], known)


def ref_sidecars(path, d):
    """{name: sidecar_subs() entry} of the .srt files beside a Matroska file that the word check leaves out and the
    reference timing of --sub-time takes: not forced, and not in mkv_sidecars()."""
    words = mkv_sidecars(path, d)
    return {s["name"]: s for s in proof.sidecar_subs(path) if s["name"] not in words and proof.SIDECAR_FLAGS["forced"] not in s["flags"]}


def sub_reference(path, j, d, sync, items, others, sweeps=None, full=False, stop=None, report=True, blocks=None):
    """({subtitle position or sidecar name: subsync.reference() result, with its codec, language and role},
    {reference: "fixed", "in time" or "clean sweep"}) of the reference timing (docs/design.md, "Subtitle match"). It
    takes the subtitles the word check does not read: a text or picture track in a SUB_ROLES role that sub_targets()
    leaves out, and the sidecars others of ref_sidecars(). A reference is a track or sidecar of sync, the word check,
    with a match whose times are in time or fixed. A match with too few anchors for a fix is one too when its rows of
    sweeps are clean, see subsync.clean(). A track whose read was cut short is none, see Cut: its fit would rest on
    its first part alone. Its cues come from items, sub_items(), and move into audio time first:
    by its fix, or by its measured offset when it has none, the timing's for an in-time match, sweep_offset() for a
    clean sweep. blocks {key: subsync.blocks() blocks} are the blocks dense hearing found, and a cue of the reference in
    one moves by its shift too, as remux.time_plan() moves it. A block left in place sits off the rest, so the slices of
    a right track would disagree and its fix would stay out.

    full reads a track the Cues do not index from the whole file, see full_read(). Once stop() is true, the time
    limit less SUB_RESERVE in an import, no track is read or fit, and each one left is "deferred" to the deep
    analysis. The deep analysis passes deep_waits(), which stops it for an import instead. With no reference nothing is
    read, and report False gives ({}, {}) then."""
    refs, basis, dur = {}, {}, decide.duration(j)
    for k, r in sync.items():
        t = r.get("timing") or {}
        basis[k] = "fixed" if t.get("fix") else "in time" if t.get("why") == "in time" else \
            "clean sweep" if ("few" in t or t.get("swept")) and subsync.clean((sweeps or {}).get(k) or [], dur) else None
        if r["verdict"] == "match" and basis[k] and k in items and not isinstance(items[k][2], Cut):
            # The reference in audio time: moved by its fix, else by its own measured offset, which may reach MIN_SHIFT
            # while it is in time. So the target is judged, and moved, against the audio.
            f = t.get("fix") or {"rate": "1/1", "offset": (t.get("offset") or 0.0) if basis[k] == "in time" else sweep_offset((sweeps or {}).get(k))}
            late = lambda a: subsync.shift_of(blk, a) if (blk := subsync.mover((blocks or {}).get(k), a)) else 0.0   # a block's cues move by its shift
            refs[k] = [(subsync.moved(a * 1000, f) / 1000 - late(a), subsync.moved(b * 1000, f) / 1000 - late(a), x) for a, b, x in items[k][2]]
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


LAYOUT_READ = {}   # (path, size, mtime_ns, track position) -> the cues sub_layout() read of a track, see layout_cues()


def layout_cues(path, p):
    """The cues sub_layout() read of track position p of path as it is now, or None."""
    st = os.stat(path)
    return LAYOUT_READ.get((path, st.st_size, st.st_mtime_ns, p))


def sub_layout(path, j, d, timed_by, others, stop=None, deep=False):
    """({subtitle position or sidecar name: its sub_reference() result with "layout": subsync.layout()}, the facts of
    the speech read) of the speech layout check (docs/design.md, "Incorrect subtitle identification"). It takes a text
    track in a SUB_ROLES role and a sidecar of ref_sidecars() whose language no main audio track speaks, when the
    reference timing found no fit for it. A layout mismatch alerts and keeps the verdict, so nothing moves. With
    subsync.LAYOUT_ACTION "remove" it makes the verdict a mismatch, so the track leaves the file and the sidecar moves
    aside as after the word check. The decision log holds the lift of every judged track under "layout". A fit gets its
    times from subsync.layout_fix(), so a track off by one shift gets a fix, and a track at different offsets in
    different parts gets none and alerts. A fix may keep runs of lines at the file's ends where they are, see
    subsync.shifted(). Such a fix needs a plan of every cue, so a WebVTT track gets none and alerts. A cut read gets no
    fix at all, as the fix would move the lines past the read unseen. The cues read stay in LAYOUT_READ for the plan.
    A fix stands only when the speech onsets confirm it, see subsync.layout_onsets(). The onsets are read once for the file, in the parts of subsync.onset_parts(), and only when
    some track has a fix. With subsync.LAYOUT_FIX "alert" a fix then only alerts: "would" holds it, and "unfixed" its
    offset. The spans of speech come from one read of the whole audio track that plays, see lid.speech(). A
    track whose read was cut short is judged up to its last cue read, as a file that ends there, see Cut. With stop()
    true before a read, nothing more is read, and the deep analysis checks the file later. With deep, the deep analysis,
    the speech read stops between two of its chunks when an import job waits, and Yielded rises."""
    audio, codecs = sub_audio(d["tracks"], d["edits"]), sub_codecs(j)
    apart = lambda lang: lang not in decide.UNTAGGED and decide.lang_key(lang) not in audio   # no main audio track speaks it
    open_ = lambda k: (timed_by.get(k) or {}).get("verdict", "unknown") in ("unknown", "weak")   # no reference fits it
    tracks = {t["pos"]: t for t in d["tracks"] if t["kind"] == "s" and t["role"] in config.SUB_ROLES and codecs.get(t["pos"]) in config.SUB_CODECS
              and apart(t["lang"]) and open_(t["pos"])}
    sides = {n: s for n, s in others.items() if apart(side_code(s)) and open_(n)}
    play = decide.default_audio(d["tracks"], d["edits"])
    if not audio or not (tracks or sides) or decide.duration(j) < subsync.LAYOUT_MIN or (stop and stop()):   # no verdict could come
        return {}, {}
    idx = [t for t in d["tracks"] if t["kind"] == "a"].index(play)
    got = checks.lid_speech(path, idx, j, config.SPEECH_TIMEOUT, (None, store.path()) if deep else None)
    if got.get("yielded"):
        raise Yielded("an import waits, during the speech read")
    facts = {"audio": idx, **{k: got[k] for k in ("cached", "took", "cpu") if k in got}}
    if not isinstance(got.get("spans"), list):
        return {}, dict(facts, why=config.mask(str(got.get("why") or "no spans"))[:200])
    cues = subtitle_cues(path, j, set(tracks), full=True) if tracks else {}
    LAYOUT_READ.clear()   # the plan of a partial shift takes these cues, so the remux never reads them again
    st = os.stat(path)
    LAYOUT_READ.update({(path, st.st_size, st.st_mtime_ns, p): c for p, c in cues.items()})
    todo = {p: (cues.get(p) or [], t["lang"], codecs[p], t["role"]) for p, t in tracks.items()}
    todo.update({n: ([(a / 1000, b / 1000, x) for a, b, x in s["cues"]], side_code(s), "srt",
                      "sdh" if {proof.SIDECAR_FLAGS["hi"], proof.SIDECAR_FLAGS["sdh"]} & set(s["flags"]) else "full") for n, s in sides.items()})
    out, heard, dur = {}, None, decide.duration(j)
    for k, (cs, lang, codec, role) in todo.items():
        end = cut_end(cs)   # no speech past a cut read counts, see Cut
        spans = got["spans"] if end is None else [(a, min(b, end)) for a, b in got["spans"] if a < end]
        upto = dur if end is None else min(dur, end)
        lay = subsync.layout(cs, spans, upto)
        timing = subsync.layout_fix(cs, spans, upto, lay)
        if (timing or {}).get("fix") and end is not None:   # a fix would move the lines past the read too, unseen
            timing = {"fix": None, "unfixed": timing["fix"]["offset"], "why": f'{timing["why"]}, but the read of its lines stopped part way, so the times stay'}
        if (timing or {}).get("keep") and codec == "S_TEXT/WEBVTT":   # a partial shift needs every cue in a new plan
            timing = {"fix": None, "unfixed": timing["fix"]["offset"], "why": f'{timing["why"]}, but a WebVTT track is never rewritten, so the times stay'}
        if (timing or {}).get("fix"):   # the second clock, read once for the file
            if heard is None and not (stop and stop()):
                facts["onsets"] = {"cpu": 0.0, "took": 0.0}
                heard = onsets(path, j, idx, subsync.onset_parts(dur), facts["onsets"])
            timing = subsync.layout_onsets(cs, timing, heard, subsync.onset_parts(dur), dur) if heard is not None else \
                {"fix": None, "why": f'{timing["why"]}, but the run had no time left to read the speech onsets, so the times stay'}
        if (timing or {}).get("fix") and subsync.LAYOUT_FIX != "write":   # the fix it would make alerts, see LAYOUT_FIX
            timing = dict(timing, fix=None, unfixed=timing["fix"]["offset"], would=dict(timing["fix"], **({"kept": timing["kept"]} if timing.get("kept") else {})),
                          why=f'{timing["why"]}, but a fix from the speech layout only alerts for now, so the times stay')
        r = timed_by.get(k) or {"verdict": "unknown", "why": "no track or sidecar of the file matched the audio in its words", "timing": None}
        drop = lay["verdict"] == "mismatch" and subsync.LAYOUT_ACTION == "remove"
        out[k] = dict(r, codec=codec, lang=lang, role=role, layout=lay, **({"verdict": "mismatch", "why": lay["why"]} if drop else {}),
                      **({"timing": timing} if timing else {}))
    return out, facts


def swept_before(rec, ex):
    """rec of a pass that followed Replan ex, with the sweep facts of the pass that raised it added to its own, the
    dense hearing's too. The second pass finds the words in the cache, so its facts alone would hide what the first
    pass heard."""
    f, g = (ex.args[1] if len(ex.args) > 1 else None), rec.get("sweep_facts")
    add = lambda g, f: dict(g, cpu=round(g["cpu"] + f["cpu"], 1), took=round(g["took"] + f["took"], 1), runs=g.get("runs", 0) + f.get("runs", 0),
                            cached=g.get("cached", 0) + f.get("cached", 0), failed=g["failed"] + [x for x in f["failed"] if x not in g["failed"]])
    if f and g:
        rec["sweep_facts"] = dict(add(g, f), **({"dense": add(g["dense"], f["dense"])} if "dense" in f and "dense" in g else {}))
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
    minute of the file gets one window of subsync.WINDOW seconds at its densest cues, which sweep_hear() hears. deep
    is the deep analysis, which yields to an import job in the queue. The rows change no time, and sub_dense() hears the
    parts where they sit off. facts holds the CPU and wall seconds and why a hearing heard nothing. A track whose read
    was cut short gets no row past its last cue, see Cut, and facts holds that cue's end in "cut".

    A word-check fix that passes sweep_confirms() must also put more of the sweep's matched cues in their spans than
    the cues have as they are, see subsync.shares(). Else the times stay with "unfixed", as at an import, see
    subsync.timing(). The sweep's windows are many more than the word check's.

    The sweep also judges the times of each track that matched and was read whole, when the word check left them as
    they are or its fix fails sweep_confirms() or the share, see subsync.sweep_fit(). Its fix, or its "in time" in
    place of a fix that the sweep did not confirm, goes into sync as the track's timing, with the word check's why in
    "word_check", and the rows then sit against that fix. The fix must pass sweep_confirms(), and no fix and no "in time" stands on rows
    that subsync.live() reads as live captions. A fix of the sweep keeps the word check's timing in "word_timing", and
    waits for dense hearing of the whole file at it, see sub_dense() and checked_fix(). An "in time" in place of steps
    or an offset the word check could not fix waits in the timing's "in_time" for dense hearing, see held_in_time().
    Else the timing keeps the word check's result, and "sweep_fit" says why, with the sweep's numbers in "sweep"."""
    dur, out, facts = decide.duration(j), {}, {"cpu": 0.0, "took": 0.0, "failed": [], "runs": 0, "cached": 0}
    for idx, (lang, cues) in sub_groups(items).items():
        stop = decide.STOPWORDS.get(lang, frozenset())
        ts = subsync.word_times(cues, stop)
        starts = [w for m in range(int(dur // 60) + 1) for w in subsync.windows(cues, dur, stop, parts=((m * 60 / dur, min(1.0, (m + 1) * 60 / dur)),), ts=ts)]
        heard = sweep_hear(path, idx, j, lang, starts, facts, deep, "sweep")
        for k, x in items.items():
            if x[1] != idx:
                continue
            h, r = before_cut(heard, x[2]), sync.get(k) or {}
            t = r.get("timing") or {}
            out[k] = subsync.sweep(h, x[2], lang, t)
            if r.get("verdict") != "match":
                continue
            if t.get("fix") and not sweep_confirms(out[k]):   # the rows confirm the word check's fix, so its share in spans decides
                cs, _, pairs = subsync.sweep_pairs(h, x[2], lang)
                was, now = subsync.shares(pairs, subsync.unflashed(cs), t["fix"])
                if now > was:
                    sync[k] = dict(r, timing=dict(t, spans=[round(was, 3), round(now, 3)]))
                    continue
                t = {"fix": None, "unfixed": t["fix"]["offset"], "spans": [round(was, 3), round(now, 3)],
                     "why": f'{t["why"]}, but the sweep puts {now:.0%} of its matched cues in their spans after the fix and {was:.0%} as they are, so the times stay'}
                r = sync[k] = dict(r, timing=t)
                out[k] = [dict(w, off=w["offset"]) for w in out[k]]   # no line is fitted now
            if isinstance(x[2], Cut):
                continue
            got = subsync.sweep_fit(h, x[2], lang, dur, plain=t.get("why") != "in time")
            rows = subsync.sweep(h, x[2], lang, got) if got["fix"] else [dict(w, off=w["offset"]) for w in out[k]]   # against no fix
            why = sweep_confirms(rows) if got["fix"] else None if got["why"] == "in time" else got["why"]
            why = why or ("the sweep reads the track as live captions" if subsync.live(rows) else None)
            if why:
                sync[k] = dict(r, timing=dict(t, sweep_fit=why, **({"sweep": got["sweep"]} if "sweep" in got else {})))
            elif not got["fix"] and (t.get("piecewise") or "unfixed" in t):   # dense hearing judges the windows off first, see held_in_time()
                sync[k] = dict(r, timing=dict(t, in_time=dict(got, word_check=t.get("why"))))
            elif got["fix"] or t.get("fix"):   # a fix waits for dense hearing of the whole file at it, see sub_dense()
                sync[k], out[k] = dict(r, timing=dict(got, word_check=t.get("why"), **({"word_timing": t} if got["fix"] else {}))), rows
    cut = {k: round(e, 1) for k, x in items.items() if (e := cut_end(x[2])) is not None}
    return out, dict(facts, cut=cut) if cut else facts


def held_in_time(sync, found):
    """sync with each "in time" that sub_sweep() held in a timing's "in_time" settled, after dense hearing gave found,
    sub_dense()'s. The word check's steps or offset can come from a short block that one sweep window hears or none, so
    the sweep alone never clears them. The "in time" stands when dense hearing judged each word-check window that sat
    MIN_SHIFT or more off, see subsync.check(): the window lies in a part that is in line, or its cues lie in a block that
    dense hearing moved. Else the word check's result stands, and its "sweep_fit" names the windows left."""
    for k, r in sync.items():
        t = r.get("timing") or {}
        if "in_time" not in t:
            continue
        got = found.get(k) or {}
        place = lambda w: w["at"] + w.get("secs", subsync.WINDOW) / 2   # audio time, and place + late its cues' time
        judged = lambda w: any(p.get("in_line") and p["lo"] <= place(w) <= p["hi"] for p in got.get("parts") or ()) or \
            any(b["from"] <= place(w) + w["late"] < b["to"] for b in got.get("blocks") or ())
        left = [w for w in r.get("windows") or () if w.get("late") is not None and abs(w["late"]) >= subsync.MIN_SHIFT and not judged(w)]
        if not left:
            sync[k] = dict(r, timing=t["in_time"])
        else:
            at = ", ".join(f"{place(w) // 60:.0f}:{place(w) % 60:04.1f}" for w in left)
            sync[k] = dict(r, timing={**{x: v for x, v in t.items() if x != "in_time"}, "sweep": t["in_time"].get("sweep"),
                                      "sweep_fit": f"the sweep puts the cues in time, but dense hearing did not find them in time at {at}"})
    return sync


def checked_fix(sync, found, sweeps):
    """sync with each fix of the sweep checked by dense hearing of the whole file at that fix, see sub_dense(). The fix
    stands, with the blocks that dense hearing moved, when no long part is left off, see subsync.left_off(). A long part
    left off is a step that the fix moved the wrong way, and a hearing that stopped part way checked nothing past that.
    Then the word check's result stands, the track's blocks go, and its rows sit off the audio again. A word-check fix
    the sweep did not confirm stays unconfirmed. A word check that would not alert gets "unfixed": None, so the times
    that stay off alert. A short part left off in the middle of the file keeps the fix and alerts nothing. found and
    sweeps change in place too."""
    for k, b in list(found.items()):
        t = (sync.get(k) or {}).get("timing") or {}
        if not b.get("whole") or not (b.get("left") or b.get("unheard")):
            continue
        w = dict(t["word_timing"])
        if w.get("fix"):   # it failed the sweep, see sub_sweep()
            w = {"fix": None, "unconfirmed": w["fix"], "swept": True, "why": f'{w["why"]}, but the sweep did not confirm it'}
        alerts = (w.get("piecewise") and round(max(w["offsets"]) - min(w["offsets"]), 2) >= config.STEP_ALERT) or "unfixed" in w
        mmss = lambda x: f"{x // 60:.0f}:{x % 60:04.1f}"
        why = "the hearing stopped part way" if b.get("unheard") else "the lines at " + ", ".join(f"{mmss(lo)} to {mmss(hi)} sit {o:+.2f} s off" for lo, hi, o in b["left"])
        sync[k] = dict(sync[k], timing=dict(w, **({} if alerts else {"unfixed": None}), sweep=t.get("sweep"),
                                            sweep_fit=f'a fix of {t["fix"]["offset"]:+.2f} s and the ratio {t["fix"]["rate"]} by the sweep was heard in full, '
                                                      f'and {why}, so the times stay'))
        sweeps[k] = [dict(x, off=x["offset"]) for x in sweeps.get(k) or []]
        del found[k]
    return sync


def sweep_hear(path, idx, j, lang, starts, facts, deep, what):
    """The heard windows of starts, WINDOW seconds each, on audio idx, for sub_sweep() and sub_dense(). One lid.py
    process hears them with the model loaded once, two windows a Whisper run, and caches each pair. Between two runs
    it yields when an import's hearing waits for the model, and then hears the rest with its next turn. With deep it
    raises Yielded when an import job waits in the queue. It adds its cost to facts. what names the windows in the
    reason of Yielded."""
    gate, heard, left = os.path.join(config.CFG.state_dir, "lid.turn.gate"), [], list(starts)
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
            raise Yielded(f"an import waits, after {len(heard)} of {len(starts)} {what} windows")
        left = left[len(got["windows"]):]
    return heard


def sub_dense(path, j, items, sweeps, sync, deep=False):
    """({subtitle position or sidecar name: subsync.blocks() result}, facts) of the dense hearing of --sub-time and the
    deep analysis (docs/design.md, "Subtitle match"). items are the sub_items() of the tracks and sidecars that
    matched, sweeps their sub_sweep() rows and sync the word check. subsync.suspects() gives the parts of a track whose
    sweep rows sit off its fitted line. The tracks of one audio track share one hearing of all their parts, in the
    subsync.dense() windows that hear each part in full. Those windows lie where the cues of the tracks are, each track
    moved to the audio by its fix. It hears and yields as sub_sweep() does. Then subsync.blocks() finds the blocks of
    each track in its own parts, with the speech onsets of the parts as a second clock, see onsets(). A track with no
    part gets no entry, and a file with no part hears nothing. facts holds the windows, the CPU and wall seconds of the
    hearing and the onsets, and why a hearing heard nothing or the onsets failed. The parts of one audio track hold
    subsync.BLOCK_HEAR seconds in all, see heard_parts().

    A part whose edge the hearing did not see asks for a stretch past it, see subsync.further(). Those stretches come
    from what BLOCK_HEAR leaves after the first hearing. A second hearing hears them all once, their onsets join the
    others, and blocks() runs again for each track that asked, with its parts and its stretches. In the deep analysis an
    import that waits goes first, before the second hearing.

    A track whose timing is a fix of the sweep, see sub_sweep(), is heard whole at that fix, past the cap, and blocks()
    takes the whole file as its one part. subsync.left_off() then names the long parts still off, see checked_fix(). Only
    the audio between the sweep's windows is new, see subsync.gaps(), and the sweep's windows come from the cache. A
    22-minute episode hears about 155 new windows, and a 2-hour film about 840.

    A track whose read was cut short gets no parts and no live moves, see Cut. A track whose sweep looks
    live-captioned, see subsync.live(), gets no parts. Dense hearing hears the whole audio track instead, past the cap,
    and subsync.live_moves() moves each of its cues to its own speech. For the choice of windows its cues move by its
    fix, then by the median "off" of its sweep rows against the fitted line, so the windows lie where the speech is.
    The other tracks of that audio track keep their parts and their blocks. A hearing that stopped part way leaves the
    cues past it where they are, and the track's "live" facts then say "fixed": False and hold the reasons in
    "failed"."""
    dur, out, facts = decide.duration(j), {}, {"windows": 0, "cpu": 0.0, "took": 0.0, "failed": [], "runs": 0, "cached": 0}
    fix = lambda k: ((sync.get(k) or {}).get("timing") or {}).get("fix")
    audio = lambda t, f: subsync.moved(t * 1000, f) / 1000 if f else t
    for idx, (lang, _) in sub_groups(items).items():
        keys = [k for k, x in items.items() if x[1] == idx and not isinstance(x[2], Cut)]   # see Cut
        lives = {k: x for k in keys if (x := subsync.live(sweeps.get(k) or []))}
        whole = [k for k in keys if k not in lives and fix(k) and "word_timing" in sync[k]["timing"]]   # a fix of the sweep, see sub_sweep()
        parts = {k: subsync.suspects(sweeps.get(k) or [], dur) for k in keys if k not in lives and k not in whole}
        if not lives and not whole and not any(parts.values()):
            continue
        union, parts = heard_parts(parts, {k: sweeps.get(k) or [] for k in parts})
        swept = sorted({r["at"] for k in whole for r in sweeps.get(k) or []})   # the sweep's windows, heard already
        parts.update({k: [(0.0, dur)] for k in whole})
        spots = [(0.0, dur)] if whole else union   # the parts of the other tracks, which alone read speech onsets, and a whole file
        union = [(0.0, dur)] if lives else [tuple(x) for x in subsync.merged(union + subsync.gaps(swept, dur))] if whole else union   # heard whole
        late = lambda k: lives[k]["off"] if k in lives else 0.0
        cues, stop = sorted((audio(a, fix(k)) - late(k), audio(b, fix(k)) - late(k), x) for k in keys for a, b, x in items[k][2]), decide.STOPWORDS.get(lang, frozenset())
        timing = lambda k: (sync.get(k) or {}).get("timing")
        starts = subsync.dense(cues, union, dur, stop)
        facts["windows"] += len(starts)
        failed = len(facts["failed"])
        heard = sweep_hear(path, idx, j, lang, starts, facts, deep, "dense")
        if whole and not lives:   # the sweep's windows come from the cache, with the clips the sweep heard
            heard = sorted(heard + sweep_hear(path, idx, j, lang, swept, facts, deep, "sweep"), key=lambda w: w["at"])
        times = onsets(path, j, idx, spots, facts) if spots and any(parts.values()) else []   # live_moves() reads no onsets
        out.update({k: subsync.live_moves(before_cut(heard, items[k][2]), items[k][2], lang, timing(k), x) for k, x in lives.items()})
        for k in lives if len(facts["failed"]) > failed else ():   # a hearing that stopped part way leaves the track not fixed
            out[k]["live"].update(fixed=False, failed=facts["failed"][failed:])
        out.update({k: subsync.blocks(before_cut(heard, items[k][2]), items[k][2], lang, timing(k), ps, onsets=times, rows=sweeps.get(k) or [])
                    for k, ps in parts.items() if ps})
        for k in whole:   # what the fix and its blocks leave off, see checked_fix()
            out[k].update(whole=True, left=subsync.left_off(heard, items[k][2], lang, timing(k), out[k], dur), **({"unheard": facts["failed"][failed:]}
                                                                                                                     if len(facts["failed"]) > failed else {}))
        more = subsync.further({k: out[k] for k, ps in parts.items() if ps and k not in whole}, union, dur)   # none past a whole track heard
        if more:
            if deep and runner.queued():
                raise Yielded("an import waits, before the second dense hearing")
            extra = subsync.unheard([tuple(x) for x in subsync.merged(x for xs in more.values() for x in xs)], union)   # heard once
            starts = subsync.dense(cues, extra, dur, stop)
            facts["windows"] += len(starts)
            heard += sweep_hear(path, idx, j, lang, starts, facts, deep, "dense")
            times = sorted(times + onsets(path, j, idx, extra, facts))
            out.update({k: subsync.blocks(before_cut(heard, items[k][2]), items[k][2], lang, timing(k), [tuple(x) for x in subsync.merged(parts[k] + xs)], onsets=times,
                                          rows=sweeps.get(k) or []) for k, xs in more.items()})
    return out, facts


ONSET_TIMEOUT = 300   # seconds the onset read of one part may take at nice 19
ONSETS = {}   # (path, size, mtime_ns, audio index, lo, hi) -> the onsets of one part, so a pass that Replan runs again reads none twice


def onsets(path, j, idx, parts, facts=None):
    """The speech onsets in parts [(lo, hi)] of ffmpeg audio track idx of path, sorted, as [(onset, seconds of silence
    before it)] in seconds of the file. An onset is where ffmpeg's silencedetect at -25 dB ends a silence of 0.3 s or
    more. A part that starts in a silence counts that silence from its own start. subsync.blocks() takes them as a
    second clock beside Whisper's word times (docs/design.md, "Subtitle match"). It counts an onset that ends a silence
    of 0.5 s or more, and checks that no other onset lies near it. A track with a centre channel is heard there,
    where the dialogue is, and another track as its mono mix. j is the mkvmerge -J probe, which gives the channel
    count. ffprobe names the layout of a track with 3 channels or more. Each part is one ffmpeg read at nice 19 and
    idle I/O. A silence that runs to the end of a part ends no speech there. facts gets the CPU and wall seconds, and
    why a read failed in "onset_why". A failure gives no onsets of that part and never raises. The onsets of each part
    read stay in ONSETS for the file as it is, so a second pass of the job reads no part again."""
    t0, c0, out, why = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN), [], []
    audio = [t for t in j.get("tracks") or [] if t.get("type") == "audio"]
    try:
        st = os.stat(path)
        key = lambda lo, hi: (path, st.st_size, st.st_mtime_ns, idx, lo, hi)
        out += [o for p in parts if key(*p) in ONSETS for o in ONSETS[key(*p)]]
        parts = [p for p in parts if key(*p) not in ONSETS]
        channels = (audio[idx].get("properties") or {}).get("audio_channels") or 0
        layout = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{idx}", "-show_entries", "stream=channel_layout", "-of",
                                 "csv=p=0", path], capture_output=True, text=True, errors="replace", timeout=60).stdout.strip() if channels > 2 and parts else ""
        centre = bool(layout) and "FC" in re.split(r"[+() ]", ffmpeg_layouts().get(layout, layout))
        for lo, hi in parts:
            r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-ss", f"{lo:.3f}", "-t",
                                f"{hi - lo:.3f}", "-i", path, "-map", f"0:a:{idx}", "-vn", "-sn", "-dn", "-af",
                                ("pan=mono|c0=FC," if centre else "aformat=channel_layouts=mono,") + "silencedetect=noise=-25dB:d=0.3",
                                "-f", "null", "/dev/null"], capture_output=True, text=True, errors="replace", timeout=ONSET_TIMEOUT)
            if r.returncode:
                why.append(f"ffmpeg exited {r.returncode} at {content.hms(lo)}: {config.mask(r.stderr.strip())[-120:]}")
                continue
            got = [(lo + float(x), float(d)) for x, d in re.findall(r"silence_end: (-?[\d.]+) \| silence_duration: ([\d.]+)", r.stderr)
                   if float(x) < hi - lo - 0.05]
            while len(ONSETS) >= 64:   # the parts of the files a backfill's workers read now
                ONSETS.pop(next(iter(ONSETS)))
            ONSETS[key(lo, hi)], out = got, out + got
    except (OSError, IndexError, ValueError, subprocess.SubprocessError) as ex:
        why.append(config.mask(f"the onset read failed: {type(ex).__name__}: {ex}")[:200])
    if facts is not None:
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        facts.update(cpu=round(facts["cpu"] + c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime, 1), took=round(facts["took"] + time.monotonic() - t0, 1),
                     **({"onset_why": why} if why else {}))
    return sorted(out)


LAYOUTS = {}   # ffmpeg -layouts, see ffmpeg_layouts()


def ffmpeg_layouts():
    """{layout name: its channels joined by +} of ffmpeg -layouts, as "5.1(side)": "FL+FR+FC+LFE+SL+SR". ffprobe names a
    track's layout by these names. A process keeps the first read that gives them, and a failed read gives {}."""
    if not LAYOUTS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            text = subprocess.run(["ffmpeg", "-hide_banner", "-layouts"], capture_output=True, text=True, errors="replace", timeout=30).stdout
            LAYOUTS.update(re.findall(r"(?m)^(\S+)\s+([A-Z][A-Z0-9]*(?:\+[A-Z][A-Z0-9]*)+)\s*$", text or ""))
    return LAYOUTS


def heard_parts(parts, rows):
    """(the parts dense hearing hears, {key: its parts}) for parts {key: subsync.suspects()} of the tracks of one audio
    track, whose sweep rows are rows {key: rows}. suspects() holds the parts of one track to BLOCK_HEAR seconds, and the
    parts of tracks that differ could hear more together. So the parts merge, and they hold BLOCK_HEAR seconds in all.
    The suspect rows are those of subsync.suspect_rows(), each track's against its own lean. The part with the row
    farthest off comes first, as in suspects(). A part that does not fit in what is left keeps the stretch that fits,
    see subsync.centred(). With under WINDOW seconds left it keeps none. A track's part keeps what of it is heard, and a
    part with nothing heard gives (lo, lo), which blocks() names as the cap."""
    sus = {}
    for rs in rows.values():
        for t, off in subsync.suspect_rows(rs).items():   # the tracks share their windows, so a time can be off in several
            sus[t] = max(off, sus.get(t, 0.0))
    far = lambda p: -max((o for t, o in sus.items() if p[0] <= t < p[1]), default=0.0)
    union, left = [], subsync.BLOCK_HEAR
    for lo, hi in sorted(subsync.merged(p for ps in parts.values() for p in ps if p[1] > p[0]), key=far):
        if hi - lo > left:
            lo, hi = (round(x, 1) for x in subsync.centred(lo, hi, sus, left))
        union.append((lo, hi))
        left -= hi - lo
    heard = lambda p: next(((max(p[0], a), min(p[1], b)) for a, b in union if max(p[0], a) < min(p[1], b)), (p[0], p[0]))
    return sorted(p for p in union if p[1] > p[0]), {k: [heard(p) for p in ps] for k, ps in parts.items()}


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


# The checks a saved subtitle result records, each with its version (docs/development.md, "When a change can fix old
# files"). Raise a check's version with every change to it. fixes says which older saved results the change of a
# version can fix: {version: the findings of those results, see sub_found(), or None for every older result}. A list
# names a result when any one of its words is among the result's findings of that check. Those results are stale. The
# next --sub-check checks such a file again, and RECHECK_ON_UPDATE queues a recheck of it. A version with no entry, as
# for a change of speed or wording, keeps every older result. tests/test_arr_media_guard.py checks each entry against
# SUB_FINDINGS and the version.
SUB_CHECKS = {
    "subtitle_match": {"version": 2, "fixes": {2: ["off", "steps"]}},   # the word check of a subtitle in an audio language,
                                                       # and its fix. Version 2 reads a roll-up caption by its new line, and
                                                       # the deep analysis fits a drift from its sweep. Both can fix or clear
                                                       # times that version 1 left off or in steps.
    "reference_timing": {"version": 1, "fixes": {}},   # a subtitle timed against one whose words matched
    "foreign_timing": {"version": 1, "fixes": {}},     # Foreign subtitle timing and Incorrect subtitle identification. A
                                                       # version 2 that fixes the subtitles 1 left off adds 2: ["off"].
    "garbled_repair": {"version": 1, "fixes": {}},
    "flash": {"version": 2, "fixes": {2: ["fix"]}},    # lines that flash by too fast to read. Version 2 writes the new
                                                       # ends of a HandBrake ASS track, whose events end in a NUL byte.
                                                       # Version 1 failed there, and its result says fix.
    "block_timing": {"version": 4, "fixes": {3: ["fix"], 4: ["live", "off"]}},   # Subtitle block timing and Live caption
                                                       # timing. Version 2 changes only the ends of live captions. A recheck
                                                       # of a file 1 moved finds its lines on their speech and moves none
                                                       # again. Version 3 writes the plan of a HandBrake ASS track, as flash
                                                       # 2. A result with fix gets a recheck, a written plan too, which then
                                                       # moves nothing. Version 4 reads a straight drift as a drift, never as
                                                       # live captions, and the sweep of a drift or a roll-up caption track
                                                       # sits on its line.
}
SUB_FINDINGS = ("mismatch", "unknown", "fix", "off", "steps", "unfixable", "cut", "live", "unread")   # the words of sub_found()
SUB_RUNS = {"import": ("subtitle_match", "reference_timing", "flash")}   # the checks each run of process() makes
SUB_RUNS["sub_check"] = SUB_RUNS["import"] + ("foreign_timing", "garbled_repair")
SUB_RUNS["sub_time"] = SUB_RUNS["deep"] = SUB_RUNS["sub_check"] + ("block_timing",)


def sub_found(rec):
    """{check of SUB_CHECKS: its findings} of the run of rec, sorted words. Each subtitle gives the words of its result:
    mismatch (it does not belong to the audio), unknown (no verdict), fix (new times or a repair, made or not), off
    (times off by one amount that stay), steps (times off by different amounts in parts of the file), unfixable
    (garbled text with no repair), cut (its read stopped part way) and live (live captions). Foreign subtitle timing
    gives unread when the read of the speech failed, so it judged nothing. A clean check gives []."""
    def words(verdict, t):
        return {w for w, on in (("mismatch", verdict == "mismatch"), ("unknown", verdict not in ("match", "fit", "mismatch")), ("fix", t.get("fix")),
                                ("off", "unfixed" in t or "unconfirmed" in t), ("steps", t.get("piecewise"))) if on}
    timed = (rec.get("subtime") or {}).values()
    out = {"subtitle_match": [words(r.get("verdict"), r.get("timing") or {}) for r in (rec.get("subcheck") or {}).values()],
           "reference_timing": [words(r.get("verdict"), r.get("timing") or {}) for r in timed if "layout" not in r],
           "foreign_timing": [words(r["layout"].get("verdict"), r.get("timing") or {}) for r in timed if "layout" in r]
           + ([{"unread"}] if "why" in (rec.get("speech") or {}) else []),
           "garbled_repair": [{"cut" if g.get("capped") else "fix" if g.get("repair") else "unfixable"} for g in (rec.get("garbled") or {}).values()],
           "flash": [{"off" if f.get("report_only") else "fix"} for f in (rec.get("flash") or {}).values()],
           "block_timing": [{w for w, on in (("fix", b.get("blocks")), ("live", b.get("live"))) if on} for b in (rec.get("blocks") or {}).values()]
           + [{"off"} for rows in (rec.get("sweep") or {}).values() if cli.sweep_alerts(rows)]}
    return {k: sorted(set().union(*v)) for k, v in out.items()}


def sub_cache(path, rec, mode, app, pending):
    """Save the subtitle check of the run of rec, of mode, for path as it is now, see lid.verdict_put(): the version
    and the findings of each check the run made (SUB_RUNS), the app and the sidecars. The rows are the saved results."""
    with contextlib.suppress(ImportError):
        from . import lid
        found = sub_found(rec)
        lid.verdict_put(os.path.join(config.CFG.state_dir, "lid.sqlite"), path,
                        {"checks": {c: {"version": SUB_CHECKS[c]["version"], "found": found[c]} for c in SUB_RUNS[mode]}, "app": app, "mode": mode,
                         "sidecars": side_stats(path)}, pending)


def sub_fixed(result):
    """The checks of the saved result whose newer version can fix it, see SUB_CHECKS. Only the checks of the run that
    saved it count, see SUB_RUNS. A check that run makes now and the result lacks, as one added later, counts as
    version 0 with no findings, so only a fixes entry of None makes it stale. A result saved before 2.5.0 names no run,
    so it has none."""
    r = result if isinstance(result, dict) else {}
    have = r.get("checks") if isinstance(r.get("checks"), dict) else {}

    def stale(c):
        e = have.get(c) or {}
        return any(e.get("version", 0) < v <= SUB_CHECKS[c]["version"] and (found is None or set(found) & set(e.get("found") or []))
                   for v, found in SUB_CHECKS[c]["fixes"].items())
    return [c for c in SUB_RUNS.get(r.get("mode"), ()) if stale(c)]


def sub_cached(path):
    """The subtitle check of path as it is now is saved and asks for nothing more, so a backfill with --sub-check
    skips the file. A dry run's result that asks for an action does not count, so an apply after it still acts. A
    sidecar that came, went or changed since counts as a change, so a new download from a program such as Bazarr is checked.
    The result must come from a run that makes every check of --sub-check, with none of them stale, see sub_fixed().
    So a result of an import, or of 2.4.0 and older, does not count, and the file is checked once more."""
    try:
        from . import lid
    except ImportError:
        return False
    got = lid.verdict_get(os.path.join(config.CFG.state_dir, "lid.sqlite"), path)
    r = got[0] if got and not got[1] and isinstance(got[0], dict) else {}
    return set(SUB_RUNS["sub_check"]) <= set(SUB_RUNS.get(r.get("mode"), ())) and not sub_fixed(r) and r.get("sidecars") == side_stats(path)


def sub_stale():
    """{app: {path: the run of its saved result}} of the files whose saved result a newer check can fix, see
    sub_fixed(). Only a result of the file as it is now counts, and only one an instance of this setup made. A recheck
    repeats the checks of that run, and none deeper, see process.Ctx. It never raises."""
    try:
        from . import lid
    except ImportError:
        return {}
    out = {}
    for path, size, mtime_ns, r in lid.verdicts(os.path.join(config.CFG.state_dir, "lid.sqlite")):
        if r.get("app") in config.CFG.apps and r.get("mode") in SUB_RUNS and sub_fixed(r):
            with contextlib.suppress(OSError):
                st = os.stat(path)
                if (st.st_size, st.st_mtime_ns) == (size, mtime_ns):
                    out.setdefault(r["app"], {})[path] = r["mode"]
    return out


def raw_srt(raw):
    """(BOM, codec, text) of the bytes raw of a SubRip sidecar, so a rewrite of its time lines keeps every other byte
    as it was. A file with a UTF-16 BOM decodes as UTF-16 in its byte order. Every other file decodes as latin-1, which
    reads each byte as one character and writes it back the same. The time lines are ASCII, so the charset of the text
    never matters, and a charset the detection misses keeps its text. Raises OSError when UTF-16 text does not decode."""
    for bom, code in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "latin-1"), (b"", "latin-1")):
        if raw.startswith(bom):
            try:
                return bom, code, raw[len(bom):].decode(code, "surrogatepass")
            except UnicodeDecodeError as ex:
                raise OSError(f"its text does not decode as {code}: {ex.reason}") from None


def sidecar_fix(sides, sync, apply, app, source, ends=None, timed=None):
    """Act on the sidecars beside a Matroska file (docs/design.md, "Subtitle match"). A sidecar that does not match the
    audio moves into originals_root() as a kept original. A sidecar whose times need a fix, or whose cues flash, with
    ends {name: flash_plan()}, is written again with new times, and its original is kept the same way. A sidecar with
    blocks, with timed {name: time_plan()} of its cues in file order, is written with the times of its plan, which
    holds its fix and its flash ends. A move and a rewrite need a place to keep the original, so with
    KEEP_ORIGINALS_DAYS 0 the sidecar stays as it is. A rewrite changes only the time lines and keeps every other byte,
    see raw_srt(). Each move and rewrite gets a log line. Returns one entry per sidecar with an action: {name, action: "move", "retime" or "lengthen", why, result:
    "moved", "retimed", "left" or "dry run", kept or left}. A rewrite whose new times do not fit the text adds error,
    the error of remux.set_ends(), and left says so in plain words for the alert."""
    out, ends, timed = [], ends or {}, timed or {}
    for n, s in sorted(sides.items()):
        r, plan, times = sync.get(n) or {}, ends.get(n), timed.get(n)
        fix = (r.get("timing") or {}).get("fix")
        if r.get("verdict") != "mismatch" and not fix and not plan and not times:
            continue
        move = r.get("verdict") == "mismatch"
        e = {"name": n, "action": "move" if move else "retime" if fix or times else "lengthen",
             "why": r["why"] if move else "; ".join(x for x in (fix and r["timing"]["why"], plan and flash_why(plan),
                                                                times and remux.blocks_moved(times, fix)) if x)}
        out.append(e)
        if not apply or not sub_fixes(source):
            e.update(result="dry run") if not apply else e.update(result="left", left="SUBTITLES is set to check")
            continue
        tmp = remux.repack_tmp(s["path"])
        try:
            st = os.stat(s["path"])
            why = vault.keepable(s["path"], st) if config.CFG.keep_days else "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept"
            if why: raise OSError(why)
            if not move:   # a new file over the name, so the kept hard link holds the old text
                with open(s["path"], "rb") as f:
                    bom, code, text = raw_srt(f.read())
                try:
                    text = remux.set_ends(text, False, times or plan) if times or plan else text
                except RuntimeError as ex:   # the alert says it plainly, and the decision log keeps the error
                    e["error"] = config.mask(str(ex))[:300]
                    raise OSError(f"its new {'times' if times else 'ends'} do not fit its text") from None
                text = remux.srt_moved(text, fix) if fix and not times else text
                remux.new_tmp(tmp)
                with open(tmp, "wb") as f:
                    f.write(bom + text.encode(code, "surrogatepass"))
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


def garbled_tracks(path, j, sides, full=False):
    """{subtitle position: decide.garbled() verdict} of the SubRip tracks of the Matroska file path whose text is
    garbled (docs/design.md, "Garbled subtitle repair"). A repair reads the track back, see remux.repaired(). A
    sidecar of sides, the sidecar_subs() entries, may fill the cues the old muxer cut. It must be in the track's
    language, name and text alike. It must hold the track's cues at their times. Every other cue must equal the
    read-back. The verdict then names it in "sidecar", counts the cues it fills in "filled" and holds its cues in
    "side". "tag" is the track's language tag. The repair stops when more than decide.REPAIR_CUT of the cues stay cut,
    or when the new text fails decide.repair_fault(). With full, a track the Cues do not index comes from full_read(). A
    track the read cannot take gets no verdict. A track whose read was cut short, see Cut, is never repaired, and its
    verdict holds "capped", because the cues past the cut were never judged."""
    tags = {f"s{n}": ((t.get("properties") or {}).get("language") or "und")
            for n, t in enumerate((t for t in j.get("tracks") or [] if t.get("type") == "subtitles"), 1)}
    want = {p for p, c in sub_codecs(j).items() if c == "S_TEXT/UTF8"}
    out = {}
    for p, cues in sorted(subtitle_cues(path, j, want, full).items(), key=lambda x: int(x[0][1:])) if want else ():
        texts = [t for _, _, t in cues]
        g = decide.garbled(texts, tags[p])
        if not g:
            continue
        if isinstance(cues, Cut):
            g = dict(g, capped=True, **({"repair": False, "why": f'{g["why"]}, but only its first {len(cues)} cues were read'} if g["repair"] else {}))
        if g["repair"]:
            got, side = remux.repaired(texts, g["codepage"], tags[p]), None
            for s in sides:
                if not decide.lang_key(side_code(s)) == decide.lang_key(tags[p]) == decide.lang_key(s["read"][0]):
                    continue
                blocks = remux.side_blocks(s)
                if len(blocks) != len(cues) or any(abs(a / 1000 - c[0]) > 0.002 or abs(b / 1000 - c[1]) > 0.002 for (a, b, _), c in zip(blocks, cues)):
                    continue
                fit = remux.repaired(texts, g["codepage"], tags[p], [t for _, _, t in blocks])
                if fit and fit[1]:   # it fills a cut cue
                    got, side = fit, (s, blocks)
                    break
            new, filled, left = got
            why = (f"{left} of {len(cues)} cues were cut short" if left > decide.REPAIR_CUT * len(cues) else None) or decide.repair_fault(new, tags[p])
            if why:
                g = dict(g, repair=False, why=f'{g["why"]}, but {why}')
            elif side:
                g = dict(g, sidecar=side[0]["name"], filled=len(filled), side=[list(b) for b in side[1]])
        out[p] = dict(g, tag=tags[p])
    return out


def sub_findings(rec, sync, unmatched):
    """The findings of the subtitle match check, each a list of sentence codes and facts, see report.SUB_LINES: one
    submatch finding for the tracks and sidecars that do not match the audio and the tracks whose text is garbled, and
    one subtiming finding for the tracks whose times are off and stay. unmatched holds the tracks, by their place
    before any remux, that stayed in the file and lost their flags instead. A line of a subtitle that the speech layout
    check found off, or whose times it could not fix, says layout. Such a subtitle whose verdict stays, see
    subsync.LAYOUT_ACTION, gets a layout line. A dry run also records what --apply would do with the file, see
    remux_block()."""
    rp, rm, wrong, late = rec.get("repack") or {}, rec.get("subremux") or {}, [], []
    plan = {} if rec.get("apply", True) else remux_block(rec)
    flags_off, by = sub_fixes(rec.get("source")), "hook" if rec.get("source") in SUB_SOURCES else "run"
    laid = lambda k: {"layout": True} if ((sync.get(k) or {}).get("layout") or {}).get("verdict") == "mismatch" else {}   # see sub_layout()
    for p in rm.get("removed") or []:
        wrong.append({"code": "removed", "track": p, "why": sync[p]["why"], "by": by, "kept": rm.get("kept"), **laid(p)})
    for p in unmatched:
        gone = p in (rm.get("remove") or []) and not rm.get("removed")   # the remux was to remove it
        wrong.append({"code": "stays", "track": p, "why": sync[p]["why"], "gone": gone, "result": rm["result"] if gone else None,
                      "kept_back": rm.get("kept_back"), "flags_off": flags_off, **plan, **laid(p)})
    for e in rec.get("sidecars") or []:
        if e["action"] == "move":
            wrong.append({"code": "sidecar", "name": e["name"], "why": e["why"], "kept": e.get("kept"), "left": e.get("left"), **laid(e["name"])})
        elif e["result"] == "left":
            late.append({"code": "sidecar_left", "name": e["name"], "why": e["why"], "left": e["left"], "action": e["action"]})
    wrong += [{"code": "layout", "track": k} for k, r in sorted(sync.items()) if laid(k) and r["verdict"] != "mismatch"]   # see subsync.LAYOUT_ACTION
    # The tracks whose text is garbled, by their place before any remux. A track that cannot be repaired is taken out
    # with its bytes kept beside the video, or stays when a setting keeps it or its read was cut, see subtitle_checks().
    garbled, mine, out = rec.get("garbled") or {}, rm.get("recoded") or [], rm.get("stripped") or {}
    wrong += [{"code": "repaired", "tracks": mine, "kept": rm.get("kept")}] if mine and rm.get("done") else []
    wrong += [{"code": "stripped", "track": p, "name": n, "kept": rm.get("kept")} for p, n in sorted(out.items()) if rm.get("done")]
    rest = sorted(p for p, g in garbled.items() if not g["repair"] and p not in out)
    kept_back = "check" if not flags_off else "keep_days" if not config.CFG.keep_days else None
    for ps, repair, more in ((mine, True, {"result": rm.get("result"), **plan}),
                             (sorted(p for p, g in garbled.items() if g["repair"] and p not in mine and p not in (rm.get("remove") or [])), True, {}),
                             (sorted(out), False, {"result": rm.get("result"), "names": [out[p] for p in sorted(out)], **plan}),
                             ([p for p in rest if not garbled[p].get("capped")], False, {"kept_back": kept_back} if kept_back else {}),
                             ([p for p in rest if garbled[p].get("capped")], False, {})):
        if ps and not ("result" in more and rm.get("done")):   # one sentence for the tracks that share their outcome
            wrong.append({"code": "garbled", "tracks": sorted(ps, key=lambda p: int(p[1:])), "repair": repair, "flags_off": flags_off, **more})
    for e in rp.get("sidecars_unmatched", []):
        wrong.append({"code": "converted_sidecar", "name": e["name"], "why": e["why"], "kept": e.get("moved"), "left": e.get("left")})
    for p in [] if rp.get("tracks_kept_back") else rp.get("tracks_unmatched", []):   # a track that stayed: the new file's check says so
        wrong.append({"code": "converted_track", "track": p, "why": rp["subcheck"][p]["why"], "kept": rp.get("kept")})
    # A live-captioned track gets one sentence in place of the sentences of its times and its sweep, and only when its
    # lines stay out of sync: a setting kept them, or too many could not be timed, see subsync.live_moves(). A hearing
    # that stopped part way adds hearing_stopped, see sub_dense(), so the alert names that step. When an apply's remux
    # did not run, the not_retimed sentence alone says so. A dry run's live sentence says what --apply would do in
    # place of the not_retimed sentence.
    live = {k: b["live"] for k, b in (rec.get("blocks") or {}).items() if b.get("live")}
    redo = set() if rm.get("done") else {*(rm.get("fixed") or []), *(rm.get("ended") or []), *(rm.get("timed") or [])}
    said = set()
    for k, f in sorted(live.items()):
        if not (flags_off and f["fixed"]) and not (k in redo and rec.get("apply", True)):
            said.add(k)
            late.append({"code": "live", "track": k, "lag": (f.get("scan") or {}).get("lag") or (f["lags"] or [0.0])[1], "moved": f["moved"],
                         "cues": f["cues"], "left": f["left"], "flags_off": flags_off, **({"block": plan.get("block")} if k in redo else {}),
                         **({"hearing_stopped": True} if f.get("failed") else {})})
    dur = {"duration": rec["file_duration"]} if rec.get("file_duration") else {}   # a fix with a ratio says where it ends, see report.at_end()
    for p, r in sorted(sync.items()):
        t = r.get("timing") or {}
        if p in live:
            continue
        if (t.get("piecewise") and round(max(t["offsets"]) - min(t["offsets"]), 2) >= config.STEP_ALERT) or "unfixed" in t:
            laid_out = (r.get("layout") or {}).get("verdict") == "fit"   # the speech layout timed it, see sub_layout()
            late.append({"code": "off", "track": p, "ref": None if laid_out else r.get("reference"), "why": t["why"], "offsets": t.get("offsets"),
                         "unfixed": t.get("unfixed"), **({"layout": True} if laid_out else {}), **({"would": t["would"], **dur} if t.get("would") else {})})
    if redo - said:
        late.append({"code": "not_retimed", "tracks": sorted(redo - said), "result": rm.get("result"), "block": plan.get("block")})
    if not flags_off:
        late += [{"code": "check_times", "track": p, "why": r["timing"]["why"], "fix": r["timing"]["fix"], **dur,
                  **({"kept": r["timing"]["kept"]} if r["timing"].get("kept") else {})}
                 for p, r in sorted(sync.items()) if (r.get("timing") or {}).get("fix")]
        late += [{"code": "check_flash", "track": p, "median": f["median"]} for p, f in sorted((rec.get("flash") or {}).items())]
    far = [[k, w["at"], w["off"]] for k, rows in sorted((rec.get("sweep") or {}).items()) if k not in live for w in rows if id(w) in cli.sweep_alerts(rows)]
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
