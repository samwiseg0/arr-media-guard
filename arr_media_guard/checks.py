# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The checks of one file: the probes, the header check, broken audio, corrupt video and the spoken language."""
import bisect, collections, contextlib, fcntl, json, os, re, select, signal, subprocess, threading, time

from . import config, content, decide, ff, matroska, mp4, proof, store, subsync, subtitles, ts


def run_bounded(argv, timeout, use=None, **kw):
    """ff.run() of argv for use with timeout cut to the job's time left. A cut raises OutOfTime. A caller takes a
    TimeoutExpired for a slow read and goes on. An ffmpeg or ffprobe argv needs its use, see ff.READS."""
    try:
        return ff.run(use, argv, timeout=config.DEADLINE.bound(timeout), **kw)
    except subprocess.TimeoutExpired:
        config.DEADLINE.check()
        raise


def mkvmerge(path):
    """mkvmerge -J of path, with walk_frames() added."""
    r = run_bounded(["mkvmerge", "-J", path], 120, capture_output=True, text=True, errors="replace")
    if r.returncode not in (0, 1):   # 1 means warnings, and the JSON is still complete. A signal gives a negative code.
        text = (r.stdout + r.stderr).strip()[-300:]
        raise RuntimeError(f"mkvmerge exited {r.returncode}" + (f": {text}" if text else ""))
    return walk_frames(path, json.loads(r.stdout))


def walk_frames(path, j):
    """j, mkvmerge -J of the Matroska file path, with walk_frames added to each subtitle track whose NUMBER_OF_FRAMES
    tag is stale: the count of its blocks from the walk, see decide.events_per_minute(). A tag is stale when its
    _STATISTICS_WRITING_APP is not the file's writing app. The walk counts only when it read every Cluster, and it
    runs only when read_time() allows a whole read. Otherwise the track gets no count."""
    c = (j.get("container") or {}).get("properties") or {}
    app = c.get("writing_application") or ""
    stale = [p for t in j.get("tracks") or [] if t.get("type") == "subtitles" and str((p := t.get("properties") or {}).get("tag_number_of_frames") or "").isdigit()
             and p.get("tag__statistics_writing_app", app) != app]
    if not stale or (j.get("container") or {}).get("type") != "Matroska" or read_time(path, "the subtitle count")[1]:
        return j
    try:
        r = matroska.frames(path)
    except (OSError, ValueError):
        return j
    count = {t["number"]: len(t["time"]) for t in r["tracks"]} if r["whole"] else {}
    for p in stale:
        if p.get("number") in count:
            p["walk_frames"] = count[p["number"]]
    return j


def sample(path, start, index, window=decide.SAMPLE_SECS):
    """Decode 20 seconds of one audio track with volumedetect. Read-only, so a timeout may kill it."""
    try:
        r = run_bounded(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-ss", f"{start:.1f}", "-i", path,
                         "-t", f"{window:.1f}", "-map", f"0:a:{index}", "-vn", "-sn", "-dn", "-af", "volumedetect", "-f", "null", "-"],
                        120, use="checks.sample", capture_output=True, text=True, errors="replace")
        got = decide.parse_sample(r.stderr, r.returncode)
    except subprocess.TimeoutExpired:
        got = dict(decide.parse_sample("", 1))
    return dict(got, at=round(start), window=window)


def ffprobe_audio(path):
    """The audio stream indexes ffprobe sees, or None when ffprobe fails."""
    r = run_bounded(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                    120, use="checks.ffprobe_audio", capture_output=True, text=True, errors="replace")
    return r.stdout.split() if r.returncode == 0 else None


def check_audio_ffprobe(path, runtime=0):
    """check_audio() for a container mkvmerge cannot read: ffprobe alone finds the audio, and ffmpeg samples its first
    stream. With one tool there is no second opinion, so every fault is a doubt and never certain. One exception: when
    ffprobe fails too and the file starts with no known container signature, no tool can read it, and that is certain."""
    try:   # ffprobe_audio() is None only when ffprobe ran and failed on the file. A missing ffprobe says nothing about it.
        seen = ffprobe_audio(path)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return None, [f"ffprobe did not run: {type(ex).__name__}"], []
    dur = ffprobe_duration(path) or 0
    if seen is None and not known_start(path):   # a broken file can start with random bytes
        return "no tool can read the file, and it does not start like any known video format", [], []
    if not seen:
        return None, ["the file cannot be read properly, and " + ("no audio track was found" if seen == [] else "a second tool failed too")], []
    if dur < 60:
        return None, [], []
    samples = [sample(path, dur * f, 0, min(decide.SAMPLE_SECS, dur * (1 - f))) for f in decide.SAMPLE_AT]
    certain, doubts = decide.audio_verdict({}, 0, samples, runtime)
    return None, doubts + ([f"{certain}, but this is unconfirmed, because the file cannot be read properly"] if certain else []), samples


def check_audio(path, j, edits, runtime=0):
    """(certain fault or None, [doubts], samples) for the audio track that plays after the flag edits.

    runtime is the listed runtime in minutes, the cross-check for a cut file. A certain fault also needs ffprobe to
    count as many audio streams as mkvmerge lists tracks. Otherwise the sampled ffmpeg stream may not be the track
    that plays, and the fault only alerts. A container mkvmerge cannot read goes to check_audio_ffprobe().
    audio_span() places the samples, and audio_more() runs the checks after them.
    """
    ts = decide.classify(j); au = [t for t in ts if t["kind"] == "a"]
    c = j.get("container") or {}
    if c.get("recognized") is False or c.get("supported") is False:   # mkvmerge cannot read ASF/WMV
        return check_audio_ffprobe(path, runtime)
    if not au:   # mkvmerge skips some tracks, so ffprobe has to agree before this is certain
        seen = ffprobe_audio(path)
        if seen == []:
            return decide.audio_verdict(j, None, []) + ([],)
        return None, ["one tool finds no audio track, but " + ("another finds one" if seen else "a second tool failed")], []
    a = decide.default_audio(ts, edits); index = next(i for i, t in enumerate(au) if t is a)
    dur = audio_span(path, j, index)
    if dur < 60:   # too short for three samples. audio_more() decodes the whole track.
        certain, doubts, samples = audio_more(path, j, index, [], None, [], dur)
    else:
        samples = [sample(path, dur * f, index, min(decide.SAMPLE_SECS, dur * (1 - f))) for f in decide.SAMPLE_AT]
        certain, doubts = decide.audio_verdict(j, index, samples, runtime)
        certain, doubts, samples = audio_more(path, j, index, samples, certain, doubts, dur)
    if certain:
        seen = ffprobe_audio(path)
        if seen is None or len(seen) != len(au):
            return None, doubts + [f"{certain}, but this is unconfirmed, because two tools count {'no' if seen is None else len(seen)} and "
                                   f"{len(au)} audio tracks, so the track checked may not be the one that plays"], samples
    return certain, doubts, samples


def known_start(path):
    """Whether the file starts with a known container signature, see decide.SIGNATURES. A disc image starts with
    32 KiB of zeros, so it always counts as known."""
    if path.lower().endswith((".iso", ".img")):
        return True
    with open(path, "rb") as f:
        return decide.media_signature(f.read(512))


def audio_span(path, j, index):
    """The seconds the three audio samples cover: where the playing audio track or the main video ends, whichever is
    later. Each own end comes from a DURATION tag mkvmerge wrote for this file, as in video_seconds(). Without either,
    the container duration, else ffprobe's: mkvmerge gives none for AVI, MP4 and TS. One SubRip event can set the
    Segment duration to hours, and samples past the real end read nothing."""
    p = decide.mkvmerge_tags(j, decide.playing(j, index))
    ends = [x for x in (decide.tag_seconds(p["tag_duration"]) if p else None, video_seconds(j)) if x]
    return max(ends) if ends else decide.duration(j) or ffprobe_duration(path) or 0


AUDIO_MORE = ("kind", "decoded", "end", "audio", "video", "video_gap", "held", "hole", "resyncs", "credits", "took", "error")   # log fields of audio_more()
AUDIO_RESERVE = config.VIDEO_RESERVE + 3 * decide.VIDEO_TIMEOUT + config.ZERO_SECS   # seconds of the job's time limit an extra audio
                      # read leaves: the zero probe, then three video windows of up to VIDEO_TIMEOUT each, then VIDEO_RESERVE
AUDIO_MAX = 1800      # seconds an extra audio read may take with no time limit, as in a scan. A large remux may need most of it.
READ_RATE = 100e6     # bytes a second the NAS gives a whole-file read. It stays low, so the time estimate errs long.


def read_time(path, what):
    """(seconds an extra audio read of the whole file may take, or None, and why it is skipped). The job's time limit
    less AUDIO_RESERVE, else AUDIO_MAX. A read that cannot finish at READ_RATE is skipped, and a scan reads it later."""
    left = config.DEADLINE.left()
    t = left - AUDIO_RESERVE if left else AUDIO_MAX
    try:
        size = os.path.getsize(path)
    except OSError as ex:   # the app replaced or deleted the file during the check
        return None, f"{what} skipped: the file cannot be read, {type(ex).__name__}"
    if size / READ_RATE > t or t < 10:
        return None, f"{what} skipped: {size / 1e9:.1f} GB needs about {size / READ_RATE:.0f} s, and {max(t, 0):.0f} s are left"
    return t, None


def full_decode(path, index):
    """Decode the whole audio track index, audio only, to 48 kHz mono, so every decoded second counts the same when the
    channel layout changes. asetpts starts the output at the first frame. decoded is the seconds out of the decoder, end
    the track's own span from ffmpeg's progress. A missing ffmpeg, a skip or a timeout is ran False, never a verdict."""
    t, (timeout, skipped) = time.monotonic(), read_time(path, "the full decode")
    if skipped:
        return {"kind": "full", "ran": False, "error": skipped}
    try:
        r = ff.run("checks.full_decode", ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-progress", "pipe:1", "-i", path,
                            "-map", f"0:a:{index}", "-vn", "-sn", "-dn", "-af",
                            "aformat=sample_rates=48000:channel_layouts=mono,asetpts=PTS-STARTPTS,volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {"kind": "full", "ran": False, "error": f"ffmpeg did not finish: {type(ex).__name__}"}
    got = decide.parse_sample(r.stderr, r.returncode)
    end = re.findall(r"out_time_us=(\d+)", r.stdout)
    return {"kind": "full", "ran": got["ran"], "errors": got["errors"], "decoded": round(sum(map(int, re.findall(r"n_samples: (\d+)", r.stderr))) / 48000, 1),
            "end": round(int(end[-1]) / 1e6, 1) if end else None, "took": round(time.monotonic() - t, 1)}


def stored_frames(path, streams, fam, meta):
    """Yields (ffprobe stream index, time, duration) of each frame with data and a known time of each media stream of
    path, in ns, stream by stream in file order, from the reader of its container (docs/design.md, "Time reads"). fam
    is ffprobe's format name, one of proof.STORED. A duration that is not known is 0, as ffprobe's N/A counts. The
    streams pair as proof.stored_places() pairs them. A Matroska walk reads on past damage, see matroska.frames(), and
    meta gets the count of its resyncs. An MP4 sample that no edit presents, or whose bytes run past the end of the
    file, is left out, because ffmpeg's read stops at such a sample. A TS read that stops before the last packet
    raises, and so does a read that fails or tracks that do not pair."""
    name, media = os.path.basename(path), sorted(proof.media_streams(streams), key=lambda s: s["index"])
    shown = lambda t, k: True
    if fam.startswith("mov"):
        r, size = mp4.samples(path), os.path.getsize(path)
        if r["error"]:
            raise RuntimeError(f"the sample tables of {name} do not read: {r['error']}")
        by = {t["id"]: t for t in r["tracks"]}
        shown = lambda t, k: t["tag"][k] != mp4.CUT and t["place"][k] + t["size"][k] <= size
    elif fam.startswith("mpegts"):
        r = ts.frames(path)
        if r["error"] and not r["error"].startswith("the file ends inside the packet"):   # a cut file, where ffmpeg's read stops too
            raise RuntimeError(f"the PES read of {name} stopped: {r['error']}")
        by = {t["pid"]: t for t in r["tracks"]}
    else:
        r, kinds = matroska.frames(path), {1: "video", 2: "audio", 17: "subtitle"}
        tracks = [t for t in r["read_on"] or r["tracks"] if t["type"] in kinds and t["codec"]]
        if [kinds[t["type"]] for t in tracks] != [s["codec_type"] for s in media]:
            raise RuntimeError(f"the block walk of {name} finds the tracks {[kinds[t['type']] for t in tracks]}, "
                               f"and ffprobe the streams {[s['codec_type'] for s in media]}")
        meta["resyncs"] = len(r["resyncs"])
    pairs = [(s["index"], t) for s, t in zip(media, tracks)] if fam.startswith("matroska") else \
        [(s["index"], by.get(int(s.get("id") or "0", 16))) for s in media]
    if any(t is None for _, t in pairs):
        raise RuntimeError(f"ffprobe names a track of {name} that its reader does not find")
    for i, t in pairs:
        time_, dur, full = t["time"], t["dur"], t["full"]
        for k in range(len(time_)):
            if full[k] and time_[k] != matroska.NONE and shown(t, k):
                yield i, time_[k], max(dur[k], 0)   # NONE is negative


def packet_read(path, index):
    """One demux of the whole file, no decode. Every time counts from the file start, as ffmpeg's -ss does: audio and
    video, where each stream ends, held, the seconds from the first audio packet to the audio end less every gap over
    decide.HOLE_GAP, hole, the longest gap between two audio packets, video_gap, the longest gap between two video
    packets or the longest video packet duration, as a broken MP4 stts entry gives one frame 20,000 s, subs,
    [end, events, text] of each subtitle track, the codec, and resyncs, the count of damages a Matroska walk read on
    past. The video is the film's, see decide.main_video(). held reads no bit rate and no rounded packet duration.
    Matroska, MP4 and TS files read the times the file stores, see stored_frames(). ffprobe gives the start, the
    origin of the -ss of each sample. AVI times each frame by its index, so ffmpeg's times are the container's own
    definition, and ffprobe reads its packets, as it reads a format with no reader of AMG.
    The ffprobe read keeps only running sums, so a TrueHD track of 8 million packets needs no memory. A skip, a
    missing ffprobe, a failure or a timeout returns {"error": why}, never a verdict."""
    t, (timeout, skipped) = time.monotonic(), read_time(path, "the packet read")
    if skipped:
        return {"error": skipped}
    try:
        r = ff.run("checks.packet_read", ["ffprobe", "-v", "error", "-show_entries", "format=format_name,start_time:stream=index,id,codec_type,codec_name,profile:"
                            "stream_disposition=attached_pic", "-of", "json", path], capture_output=True, text=True, errors="replace", timeout=min(120, timeout))
        info = ff.loads(r, "{}") if r.returncode == 0 else {}
        streams = info.get("streams") or []
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as ex:
        return {"error": f"ffprobe did not run: {type(ex).__name__}"}
    au = [x for x in streams if x.get("codec_type") == "audio"]
    if index >= len(au):
        return {"error": "ffprobe lists no such audio stream" if streams else "ffprobe failed"}
    a, v = au[index]["index"], (decide.main_video(streams) or {}).get("index")
    subs = {x["index"]: [0.0, 0, x.get("codec_name") not in decide.BITMAP_SUBS] for x in streams if x.get("codec_type") == "subtitle"}
    out = dict(codec=au[index].get("codec_name"), profile=au[index].get("profile"), audio=None, hole=None, video=None, video_gap=0.0)
    first, afirst, gaps, meta = None, None, 0.0, {"resyncs": 0}

    def add(i, pts, dur):   # one packet, in file order within its stream
        nonlocal first, afirst, gaps
        first = pts if first is None else min(first, pts)
        if i == a:
            gap = pts - out["audio"] if out["audio"] is not None else 0.0
            if gap > (out["hole"][1] - out["hole"][0] if out["hole"] else 0):
                out["hole"] = [out["audio"], pts]
            gaps += gap if gap > decide.HOLE_GAP else 0
            afirst = pts if afirst is None else afirst
            out["audio"] = max(pts + dur, out["audio"] or pts)
        elif i == v:
            out["video_gap"] = max(out["video_gap"], dur, pts - out["video"] if out["video"] is not None else 0.0)   # a frame of 20,000 s too
            out["video"] = max(pts + dur, out["video"] or pts)
        elif i in subs:
            subs[i][:2] = [max(subs[i][0], pts + dur), subs[i][1] + 1]

    fam = (info.get("format") or {}).get("format_name") or ""
    if fam.startswith(proof.STORED):
        try:   # ponytail: the read_time() gate bounds the walk, which no timer stops. A thread with a deadline if a walk ever hangs.
            for k, (i, x, d) in enumerate(stored_frames(path, streams, fam, meta)):
                if not k & 0xffff:   # a film holds millions of frames
                    ff.check_stop()
                add(i, x / 1e9, d / 1e9)
        except Exception as ex:   # any reader that fails, as on a cut box header
            return {"error": config.mask(f"the frame read failed: {ex}")[:200]}
    else:
        try:
            rc = ffprobe_packets(path, timeout, add)
        except OSError as ex:
            return {"error": f"ffprobe did not run: {type(ex).__name__}"}
        if rc != 0:
            return {"error": f"ffprobe exited {rc}" + (f", killed after {timeout:.0f} s" if rc == -signal.SIGKILL else "")}
    try:   # an MPEG-TS file may start at any PTS
        zero = float(info["format"]["start_time"])
    except (KeyError, TypeError, ValueError):
        zero = first or 0.0
    at = lambda x: None if x is None else round(x - zero, 3)
    return dict(out, audio=at(out["audio"]), video=at(out["video"]), hole=out["hole"] and [at(x) for x in out["hole"]],
                held=None if afirst is None else round(out["audio"] - afirst - gaps, 1), video_gap=round(out["video_gap"], 1),
                subs=[[at(e), n, text] for e, n, text in subs.values()], resyncs=meta["resyncs"], took=round(time.monotonic() - t, 1))


def ffprobe_packets(path, timeout, add):
    """ffprobe's exit code after it read every packet of path, with add(stream index, time, duration) of each in
    seconds. The read of a format with no reader of AMG, see packet_read(). Raises OSError when ffprobe does not start."""
    p = ff.popen("checks.ffprobe_packets", ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    timer, rc = threading.Timer(timeout, p.kill), None
    timer.start()
    try:
        for line in p.stdout:
            f = line.split(",", 3)
            try:
                i, pts = int(f[0]), float(f[1])
                dur = float(f[2]) if f[2][:1].isdigit() else 0.0
            except (ValueError, IndexError):
                continue
            add(i, pts, dur)
        rc = p.wait()
    finally:   # the job's time limit may end the loop: ffprobe must not run on
        timer.cancel()
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
    ff.check_stop()   # a stop killed ffprobe, so rc counts for nothing
    return rc


def audio_more(path, j, index, samples, certain, doubts, span):
    """The checks after the three samples (docs/design.md, "Broken audio"). Returns (certain, doubts, samples). Each extra
    read adds one entry with its kind to samples, and runs only while nothing is certain.

    full: a file under decide.FULL_UNDER seconds decodes its whole playing track, see decide.full_verdict().
    packets: one demux reads every packet, and only when a rule needs it. Rule 3 needs it when every sample decoded
    nothing (decide.stops_early()). Rule 4 needs it for a constant-rate track when the tags say it holds too little,
    or when a sample lost audio: tags written at mux time miss later damage. A sample in the longest hole, kind hole,
    must then decode nothing (decide.held()). Rule 1 needs it for a lone silent late sample in a file with a text
    subtitle track, unless a DURATION tag already runs past the sample. A lone silent late sample after the last
    subtitle event is the end credits, and its doubt goes (decide.credits_silence()).
    """
    three, doubts, samples = samples, list(doubts), list(samples)
    if not certain and 0 < span < decide.FULL_UNDER:
        samples.append(full_decode(path, index))
        certain, doubt = decide.full_verdict(samples[-1])
        doubts += [doubt] if doubt else []
    tags, (_, text, sub_tag) = decide.tag_held(j, index), decide.subtitle_kinds(j)
    rule3 = len(three) == 3 and all(s.get("ran") and s["n"] == 0 for s in three)
    rule4 = decide.cbr(j, index) and (any(decide.lost(s) for s in three) or (tags is not None and tags < decide.HELD_SHARE * span))
    rule1 = decide.lone_late_silence(three) and text and (sub_tag is None or sub_tag <= three[2]["at"])
    if certain or not (rule3 or rule4 or rule1):
        return certain, doubts, samples
    pk = packet_read(path, index)
    if "error" in pk:
        samples.append({"kind": "packets", "ran": False, "error": pk["error"]})
        return certain, doubts, samples
    samples.append(dict({k: pk[k] for k in ("audio", "video", "video_gap", "held", "hole", "resyncs", "took")}, kind="packets", ran=True))
    certain = decide.stops_early(three, pk)
    doubt = None if certain else decide.held(pk)
    if doubt and pk["hole"] and pk["hole"][1] - pk["hole"][0] >= decide.HOLE_MIN:
        (h0, h1), w = pk["hole"], decide.SAMPLE_SECS
        samples.append(dict(sample(path, h0 + (h1 - h0 - w) / 2, index, w), kind="hole"))
        if decide.empty(samples[-1]):
            certain, doubt = f"{doubt}, and its {h1 - h0:.0f} s gap at {decide.clock(h0)} plays nothing", None
    doubts += [doubt] if doubt else []
    last = None if certain or not rule1 else decide.credits_silence(three, pk["subs"], span)
    if last is not None:   # the doubt audio_verdict() gave the lone silent sample
        doubts = [d for d in doubts if d != "the audio is silent at 1 of 3 places checked"]
        samples[2] = dict(samples[2], credits=round(last, 1))
    return certain, doubts, samples


def rchar(pid="self"):
    """Bytes a process has read so far (rchar in /proc/<pid>/io), 0 when it is gone. The kernel adds a reaped child's
    count to its parent's, so the change of our own count across the reap of a window is what ffmpeg read."""
    try:
        with open(f"/proc/{pid}/io") as f:
            return int(next(line for line in f if line.startswith("rchar")).split()[1])
    except (OSError, StopIteration):
        return 0


def segment_short(path):
    """Bytes a Matroska Segment promises past the end of the file: over 0 when bytes are missing, None when the file is
    not Matroska or the Segment size is unknown. A muxer writes the size when it finishes, so a short file lost bytes.
    A lost Usenet article leaves a file short, and its Cues and Tags then sit past the end."""
    with open(path, "rb") as f:
        size = f.seek(0, 2); f.seek(0); seg = decide.segment_start(f.read(64))
    return None if not seg or seg[1] is None else seg[0] + seg[1] - size   # seg[1] None: the unknown size of a live stream


def seeks(f, b, ds):
    """(seek, read, lost) of the open Matroska file f. seek is decide.front_seeks() of b, the first bytes of f, with
    the Segment data at ds. When those list no Cues, seek also gets the entries of each SeekHead they list, and of each
    SeekHead those list. mkvpropedit writes such a second SeekHead at the end of the file when it grows an element.
    Each SeekHead is read once, the front ones included, and SEEK_HEADS of them in all, so SeekHeads that list each
    other or themselves end the read. read counts the bytes it reads, and lost holds the file offset of each listed
    SeekHead that does not read as one."""
    seek, read, lost, seen, p = decide.front_seeks(b, ds), 0, [], set(), ds
    for i, d, n in decide.front(b, ds):
        if i == decide.SEEKHEAD:
            seen.add(p - ds)   # front_seeks() read it
        p = d + n
    for pos in [] if decide.CUES in seek else seek.get(decide.SEEKHEAD, []):   # the list grows as each SeekHead read lists more
        if pos in seen or len(seen) >= config.SEEK_HEADS:
            continue
        seen.add(pos)
        f.seek(ds + pos); c = f.read(config.HEADER_READ); read += len(c); e = decide.element(c, 0)
        if e and e[0] == decide.SEEKHEAD and e[2] is not None:
            decide.seek_entries(c, e[1], e[1] + e[2], seek)
        else:
            lost.append(ds + pos)
    return seek, read, lost


SEEKHEAD_MAX = 16 << 20   # bytes layout_fault() reads of one SeekHead. An old mkvmerge lists every Cluster in one, before its Chapters.
# The level-1 elements by name, for layout_fault()
TOP_NAMES = {decide.SEEKHEAD: "SeekHead", decide.INFO: "Info", decide.TRACKS: "Tracks", 0x1043A770: "Chapters", decide.CLUSTER: "Cluster",
             decide.CUES: "Cues", 0x1941A469: "Attachments", 0x1254C367: "Tags"}


def layout_fault(f, ds):
    """Why the top level of the open Matroska file f breaks RFC 9559, or None. ds is where the Segment data starts.
    Info and Tracks must sit before the first Cluster or be listed in a SeekHead before it (6.1). A second SeekHead,
    one that a front SeekHead lists, may list Clusters only (6.3). When mkvpropedit grows the Tracks of a file with
    no room behind its front SeekHead, it moves Tracks to the end, copies the SeekHead there and leaves a front one
    that lists only that copy and the Tags. MediaInfo then shows no track. Tracks at the end that the front SeekHead
    lists are valid, as Lavf writes them and as the zero padding case of the whole-file check holds them, and so is an
    old mkvmerge's second SeekHead of Clusters. The front is walked by element headers, so a large front element
    is no limit. A walk that meets no Cluster, and a listed SeekHead that does not read, give None. Each SeekHead is
    read once and whole, up to SEEKHEAD_MAX, and SEEK_HEADS of the others in all."""
    front, listed, heads, p = set(), {}, set(), ds
    for _ in range(64):   # up to the first Cluster
        f.seek(p); e = decide.element(f.read(12), 0)
        if not e or e[0] not in decide.LEVEL1 or e[2] is None:
            return None
        if e[0] == decide.CLUSTER:
            break
        front.add(e[0])
        if e[0] == decide.SEEKHEAD:
            heads.add(p - ds)
            f.seek(p); b = f.read(min(e[1] + e[2], SEEKHEAD_MAX))
            decide.seek_entries(b, e[1], len(b), listed)
        p += e[1] + e[2]
    else:
        return None
    missing = [TOP_NAMES[i] for i in (decide.INFO, decide.TRACKS) if i not in front and i not in listed]
    if missing:
        return f"{' and '.join(missing)} {'sit' if len(missing) > 1 else 'sits'} after the first Cluster, and no SeekHead before it lists {'them' if len(missing) > 1 else 'it'}"
    for pos in [x for x in listed.get(decide.SEEKHEAD, []) if x not in heads][:config.SEEK_HEADS]:
        f.seek(ds + pos); e = decide.element(f.read(12), 0)
        if not e or e[0] != decide.SEEKHEAD or e[2] is None:
            continue
        f.seek(ds + pos); b = f.read(min(e[1] + e[2], SEEKHEAD_MAX))
        other = sorted({TOP_NAMES.get(i, hex(i)) for i in decide.seek_entries(b, e[1], e[1] + e[2], {}) if i != decide.CLUSTER})
        if other:
            return f"a second SeekHead lists {', '.join(other)}, and only Clusters may be listed there"
    return None


def layout_of(path):
    """layout_fault() of the Matroska file path, None for another file or one that does not open."""
    try:
        with open(path, "rb") as f:
            seg = decide.segment_start(f.read(64))
            return layout_fault(f, seg[0]) if seg else None
    except OSError:
        return None


def header_probe(path, j):
    """The header check of a Matroska file (docs/design.md, "Header repair"), None for another container. j is its mkvmerge -J
    probe. It reads the start of the file, the Cues and the elements of the RFC 9559 check, see matroska.check(). It
    reads the last HEADER_TAIL bytes of the Segment only when those point at an issue: no usable Cues, a last video or
    audio cue far from the header duration, a Segment size past the end after the hook's own failed edit, bytes past
    the Segment end, an RFC 9559 fault, or any issue below. The stream ends come from the Clusters, so a file whose
    index is lost reads too. tail is the bytes past the Segment end when over TAIL_MIN, else 0. A remux drops them, but
    only when the video and the audio reach the header duration, so the Segment is whole. not_whole then says why the
    Segment may be cut, and check_video() makes it a doubt. Bytes past the Segment end that start another Matroska file
    block the remux too.
    edit_tail is the count of bytes past the Segment end, whatever their content, else 0. Any count is an issue (owner
    2026-10-20). mkvpropedit grows Tracks into the first Void that holds it. When none does, it writes Tracks at the end
    of the file and extends the Segment over those bytes. So a flag edit of such a file rides a remux, see
    process.edit(). edit_blocked lists why that remux must not run, by the guards of tail. A subtitle event far past the
    streams sets the stated length, so it skips the cut-off guard. It is [] when edit_tail is 0.
    rfc holds the faults of the RFC 9559 check by kind, see docs/design.md, "RFC 9559 check". The kinds are those of
    matroska.check(), padding (bytes past the Segment end, up to TAIL_MIN), tail (more), enclosed and layout. Each fault
    is an issue. notes holds the elements RFC 9559 does not define, which readers skip.
    took is the seconds of its reads, the ffprobe read of the subtitles left out.
    enclosed is the first byte of a zero run that such an edit left inside the Segment, by zero_pad()'s rule, else
    None. It is an issue. enclosed_size is the byte count of that run, else 0. The walk for it runs only when a SeekHead
    lists an element that starts past the end of the Cues. It reads 16 bytes for each element there, and a zero run up
    to ZERO_RUN.

    Returns duration (the header's seconds), cues (True, or why none is usable), cue_end, short, failed_edit, video and
    audio (where each ends), end (where the last block of any track ends, the duration a remux writes), read (bytes),
    issue and blocked. issue says what is wrong with the header. blocked says why a remux must not run or would not
    help: the video and the audio end apart, a subtitle event runs far past them, or no stream end could be read.
    first is True when the issue must be repaired before the other checks read the file: no usable Cues, a header
    duration far from the streams, a subtitle event far past them, the hook's own failed edit, or enclosed zeros.
    ffmpeg cannot read past enclosed zeros, so a subtitle remux of such a file fails. A layout or tail issue alone
    leaves it False.
    When the header is far from every block in the last clusters, or a subtitle block there runs far past the video
    and the audio, subtitle_ends() reads every subtitle event, because a long event that starts earlier sets the
    duration too. It runs only when a remux can follow. subtitles then maps each subtitle track to where it ends, and
    expect is the duration a remux writes. streams is where the video and the audio end. trim lists the SubRip tracks
    a trim cuts at streams, and unfixable the tracks of another codec that run far past it."""
    props = (j.get("container") or {}).get("properties") or {}
    scale = props.get("timestamp_scale") or 1000000
    tracks = {(t.get("properties") or {}).get("number"): t.get("type") for t in j.get("tracks") or []}
    durations = {(t.get("properties") or {}).get("number"): ((t.get("properties") or {}).get("default_duration") or 0) / scale
                 for t in j.get("tracks") or []}
    secs = lambda tick: round(tick * scale / 1e9, 3)
    dur = decide.duration(j)
    off = lambda a, b: abs(a - b) > max(decide.HEADER_OFF, decide.HEADER_SHARE * b)
    t0 = time.monotonic()
    with open(path, "rb") as f:
        size = f.seek(0, 2); f.seek(0); b = f.read(config.HEADER_READ)
        seg, read = decide.segment_start(b), len(b)
        if not seg:
            return None
        ds, seg_size = seg
        cue_data = None   # (its file offset, its bytes), for matroska.check()
        short = None if seg_size is None else ds + seg_size - size
        (seek, more, lost), cues, cue_end = seeks(f, b, ds), "no SeekHead lists the Cues", None
        read += more
        layout = layout_fault(f, ds)
        if any(x >= size for x in lost):
            cues = "the SeekHead that lists the Cues sits past the end of the file"
        for pos in seek.get(decide.CUES, []):
            f.seek(ds + pos); e = decide.element(f.read(12), 0)
            if not e or e[0] != decide.CUES or e[2] is None:
                cues = "no Cues where the SeekHead points"
            elif ds + pos + e[1] + e[2] > size:
                cues = "the Cues run past the end of the file"
            elif e[2] > config.CUES_MAX:   # there, but too large to read: the stream ends decide
                cues = True
                break
            else:
                f.seek(ds + pos + e[1]); c = f.read(e[2]); read += len(c); last, cue_data = decide.last_cues(c), (ds + pos + e[1], c)
                if last:   # mkvmerge cues subtitles too, so the video and audio cues say where those end
                    cues, cue_end = True, max((secs(t) for n, t in last.items() if tracks.get(n) in ("video", "audio")), default=None)
                    break
                cues = "the Cues hold no cue point"
        failed = bool(short and short > 0) and hook_edit_failed(path)
        little = failed and short < decide.SHORT_CERTAIN   # the hook's own failed edit cut the file's end
        edit_tail, gap = -short if short is not None and short < 0 else 0, None
        after = cues is True and pos + e[1] + e[2]   # where the Cues end in the Segment data
        if after and short is not None and short <= 0 and any(x > after for xs in seek.values() for x in xs):
            gap, more = zero_gap(f, ds + after, ds + seg_size); read += more   # an element listed past the Cues end
        found, notes, more = matroska.check(f, b, ds, size if seg_size is None else min(size, ds + seg_size), edit_tail, cue_data)
        rfc = dict(found, **({"padding" if edit_tail <= config.TAIL_MIN else "tail": [f"{edit_tail} bytes past the Segment end"]} if edit_tail else {}),
                   **({"enclosed": [f"{gap[1]} zero bytes after the Cues"]} if gap else {}), **({"layout": [layout]} if layout else {}))
        out = dict(duration=round(dur, 3), cues=cues, cue_end=cue_end, short=short, failed_edit=failed, video=None, audio=None, end=None,
                   read=read + more, issue=[], blocked=[], tail=-short if short is not None and -short > config.TAIL_MIN else 0,
                   edit_tail=edit_tail, edit_blocked=[], enclosed=gap and gap[0], enclosed_size=gap[1] if gap else 0,
                   first=bool(little or cues is not True or gap), rfc=rfc, notes=notes)
        if cues is True and cue_end is not None and not off(dur, cue_end) and not little and not rfc:
            out["took"] = round(time.monotonic() - t0, 3)
            return out   # the common case, read in a few small parts
        kinds = [k for k in ("video", "audio") if k in tracks.values()]
        # The Segment end. The bytes past it may hold the Clusters of an older copy. stream_ends() cannot walk enclosed zeros.
        stop = gap[0] if gap else size + min(0, short or 0)
        ends, t, more = tail_ends(f, stop, durations, tracks)
        out["read"] += more
        if edit_tail:   # two files joined with cat: ffmpeg plays the second one too, and a remux would drop it
            f.seek(size - edit_tail)
            if f.read(4) == decide.EBML_ID.to_bytes(4, "big"):
                joined = "the bytes past the Segment end start another Matroska file"
                out["blocked"] += [joined] if out["tail"] else []
                out["edit_blocked"].append(joined)
    out["took"] = round(time.monotonic() - t0, 3)
    if little:
        out["issue"].append(f"the Segment size promises {short} bytes past the end of the file after the hook's own edit of it failed")
    if edit_tail:   # padding, or a shorter copy written over an older one leaves the old bytes behind
        out["issue"].append(f"the file holds {edit_tail} bytes past the end of its Matroska Segment")
    if cues is not True:
        out["issue"].append(f"no usable Cues index: {cues}")
    if layout:   # a remux writes a new layout, see layout_fault()
        out["issue"].append(f"the header layout breaks the Matroska rules: {layout}")
    if gap:
        out["issue"].append(f"the Matroska Segment holds {gap[1]} zero bytes after its Cues, and no element starts there")
    if found:
        out["issue"].append(rfc_issue(found))
    got = {k: [e for i, e in (ends or {}).items() if tracks.get(i) == k] for k in ("video", "audio")}
    if ends is None:
        out["blocked"].append("no stream end could be read from the last clusters")
        return out
    missing = [k for k in kinds if not got[k]]
    if missing or not kinds:
        found = [k for k in kinds if got[k]]
        if found and len(t) < stop:   # the read starts inside the Segment, so the missing stream ends before it
            out["blocked"].append(f"the {found[0]} ends at {content.hms(secs(max(got[found[0]])))}, and the {missing[0]} ends before "
                                  f"the last {len(t) >> 20} MiB of the file")
        else:
            out["blocked"].append(f"the last {len(t) >> 20} MiB hold no {missing[0]} block" if missing else "the file has no video or audio track")
        return out
    out.update(video=secs(max(got["video"])) if got["video"] else None, audio=secs(max(got["audio"])) if got["audio"] else None,
               end=secs(max(ends.values())))
    out["streams"] = streams = max(x for x in (out["video"], out["audio"]) if x)
    if out["video"] and out["audio"] and abs(out["video"] - out["audio"]) > decide.AV_APART:
        out["blocked"].append(f"the video ends at {content.hms(out['video'])} and the audio at {content.hms(out['audio'])}")
    cut = any(off(dur, x) for x in (out["video"], out["audio"]) if x) and (
        f"the file has {edit_tail} bytes of extra data at its end, and the video and audio stop before its stated length of {content.hms(dur)}, "
        "so the file may be cut off")
    if out["tail"] and cut:   # the Segment itself may be cut. check_video() alerts.
        out["not_whole"] = cut
        out["blocked"].append(out["not_whole"])
    subs = [t for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    if subs and (off(dur, out["end"]) or off(out["end"], streams)):   # a subtitle event that starts earlier may set the duration too
        left = config.DEADLINE.left()
        lines = {}
        per = subtitles.subtitle_ends(path, j, (left - config.SUB_RESERVE) if left else config.SUB_MAX, streams, lines) if config.CFG.header_repair and size <= config.CFG.repack_max else None
        if per is None:
            out["blocked"].append("the subtitle events were not read: no time, no remux can follow, or the block walk failed")
        else:
            out["subtitles"], out["sublines"], out["end"] = per, lines, max([out["end"]] + list(per.values()))
    out["expect"] = out["end"]   # the duration a remux writes
    if off(dur, out["end"]):
        out["issue"].insert(0, f"the header says {content.hms(dur)}, but the streams end at {content.hms(out['end'])}")
        out["first"] = True
    if off(out["end"], streams):   # an event can run an hour past the film's end. mkvmerge keeps it, --split too.
        out["first"] = True
        out["issue"].append(f"a subtitle event runs to {content.hms(out['end'])}, past the video and the audio at {content.hms(streams)}")
        late = {i: e for i, e in (out.get("subtitles") or {}).items() if e > streams + decide.REPAIR_END}
        codec = {t["id"]: (t.get("properties") or {}).get("codec_id") or t.get("codec") for t in subs}
        bad = [i for i, e in late.items() if codec[i] not in decide.TEXT_SUBS and off(e, streams)]
        if bad:   # a bitmap subtitle cannot be cut without a new encode
            place = {t["id"]: f"s{n}" for n, t in enumerate(subs, 1)}   # the place decide.classify() gives it
            out["unfixable"] = [{"track": place[i], "codec": codec[i], "end": late[i], "streams": streams} for i in bad]
            out["blocked"].append(f"subtitle track {', '.join(f'{i} ({codec[i]})' for i in bad)} runs past the end, and only a SubRip track can "
                                  "be trimmed")
        elif late:   # cut every line that runs past the real end to that end, and remove a track
            text = [i for i in late if codec[i] in decide.TEXT_SUBS]   # timed for another cut
            n = lambda i: out["sublines"].get(i) or [0, 0]
            out["remove"] = sorted(i for i in text if decide.remove_track(*n(i)))
            out["trim"] = sorted(i for i in text if i not in out["remove"])
            out["expect"] = max([streams] + [e for i, e in out["subtitles"].items() if i not in text])
    if edit_tail and cut and not off(out["end"], streams):   # a subtitle event far past the streams sets the stated length
        out["edit_blocked"].append(cut)
    return out


def tail_ends(f, stop, durations, tracks):
    """(decide.stream_ends() of the last bytes before byte stop of the open Matroska file f, those bytes, the bytes read
    in all). It reads each size of config.HEADER_TAIL in turn, the larger one also when a stream has no block in the
    smaller one. tracks maps a track number to its mkvmerge type, durations to its default duration in ticks."""
    kinds, read = {k for k in ("video", "audio") if k in tracks.values()}, 0
    for n in config.HEADER_TAIL:
        f.seek(max(0, stop - n)); t = f.read(stop - max(0, stop - n)); read += len(t)
        ends = decide.stream_ends(t, durations)
        if (ends is not None and {tracks.get(i) for i in ends} >= kinds) or n >= stop:
            break
    return ends, t, read


def stored_end(path, j):
    """Seconds to the end of the last video or audio frame that path stores, for content.trusted_duration(), or None
    when no reader of AMG takes j's container type cheaply. j is mkvmerge -J of path. Matroska reads the last Clusters
    by tail_ends(), as header_probe() does: a block ends at its BlockDuration or its frames times the DefaultDuration.
    The read stops before zero padding, as stream_ends() reads no zero run: the padding after the Cues, see zero_pad(),
    or a run up to decide.ZERO_RUN that ends the Segment, as mkvpropedit leaves when it grows the Segment over a zero
    tail. MP4 and MOV read the sample tables of the moov box, and a sample counts only when an edit presents it and its bytes
    are in the file. A TS file has no cheap read, because ts.frames() reads the whole file. 0 when the read finds no
    end or fails. Only content.OutOfTime, the job's time limit, raises."""
    kind, props = (j.get("container") or {}).get("type"), (j.get("container") or {}).get("properties") or {}
    try:
        if kind == "Matroska":
            scale = props.get("timestamp_scale") or 1000000
            tracks = {(t.get("properties") or {}).get("number"): t.get("type") for t in j.get("tracks") or []}
            durations = {(t.get("properties") or {}).get("number"): ((t.get("properties") or {}).get("default_duration") or 0) / scale
                         for t in j.get("tracks") or []}
            with open(path, "rb") as f:
                size = f.seek(0, 2); f.seek(0); seg = decide.segment_start(f.read(config.HEADER_READ))
                if not seg:
                    return 0
                stop = size if seg[1] is None else min(size, seg[0] + seg[1])
                f.seek(stop - 1)
                if f.read(1) == b"\0":   # zero padding ends the Segment
                    stop -= zero_run(f, stop - 1, seg[0], stop)[0]
                ends = tail_ends(f, zero_pad(path) or stop, durations, tracks)[0] or {}
            return max((e for n, e in ends.items() if tracks.get(n) in ("video", "audio")), default=0) * scale / 1e9
        if kind == "MP4/QuickTime":
            r, size = mp4.samples(path), os.path.getsize(path)
            return max((t["time"][k] + t["dur"][k] for t in r["tracks"] if t["kind"] in ("video", "audio") for k in range(len(t["time"]))
                        if t["tag"][k] != mp4.CUT and t["place"][k] + t["size"][k] <= size), default=0) / 1e9
    except content.OutOfTime:
        raise
    except Exception:   # a reader that fails, as on a cut box header, gives no end, so the metadata checks still run
        return 0
    return None


RFC_ISSUE = "the header breaks RFC 9559: "   # the start of the issue of matroska.check()'s faults
RFC_SHOWN = 3   # faults the issue names


def rfc_issue(found):
    """The issue of the faults of matroska.check(), {kind: [text]}: RFC_ISSUE, then the first RFC_SHOWN faults."""
    texts = [x for k in matroska.KINDS for x in found.get(k, ())]
    return RFC_ISSUE + "; ".join(texts[:RFC_SHOWN]) + (f", and {len(texts) - RFC_SHOWN} more" if len(texts) > RFC_SHOWN else "")


def rfc_again(now, hp):
    """The faults of matroska.check() in the header stage now of a remux when the header stage hp of its original
    holds each of them too. mkvmerge then wrote them again. [] when there is none, or when one is new."""
    texts = lambda h: [x for k in matroska.KINDS for x in ((h or {}).get("rfc") or {}).get(k, ())]
    new = texts(now)
    return new if new and set(new) <= set(texts(hp)) else []


def padding_only(hp):
    """Whether the only issue of the header stage hp is padding. Padding is up to TAIL_MIN bytes past the Segment end,
    which no reader notices. The library pass leaves such a file alone, see docs/design.md, "RFC 9559 check"."""
    return bool(hp) and list(hp.get("rfc") or ()) == ["padding"] and len(hp["issue"]) == 1


def header_of(path, j=None):
    """header_probe() of a .mkv file, None for another file or when the probe fails. A failure never costs the video
    check. The job's time limit passes."""
    if not path.lower().endswith(".mkv"):
        return None
    try:
        return header_probe(path, j or mkvmerge(path))
    except content.OutOfTime:
        raise
    except Exception:
        return None


def zero_run(f, pos, lo=0, hi=1 << 62):
    """(bytes of the zero run that holds the zero page at pos, counted up to decide.ZERO_RUN, bytes read). It reads
    forward from the page, then back from it, 64 KiB at a time, and never outside lo to hi, see zero_span()."""
    cap, n, read, at = decide.ZERO_RUN, 0, 0, pos
    f.seek(pos)
    while n < cap:
        b = f.read(max(0, min(65536, cap - n, hi - pos - n))); read += len(b)
        z = len(b) - len(b.lstrip(b"\0")); n += z
        if z < len(b) or not b:
            break
    while n < cap and at > lo:
        step = min(65536, at - lo, cap - n)
        f.seek(at - step); b = f.read(step); read += len(b)
        z = len(b) - len(b.rstrip(b"\0")); n += z; at -= step
        if z < len(b):
            break
    return n, read


def zero_probe(path, again=False, stop=lambda: False):
    """(the first byte of each zero page where a 64 KiB read meets a zero run of ZERO_RUN or more, bytes read, stopped). An
    incomplete download leaves regions of zero bytes. The read holds an all-zero 4 KiB page, and zero_run() measures
    the run around it. An encoder that pads easy frames with zeros leaves only short runs, far under ZERO_RUN. again
    moves every read by half a step, for the second check before a re-grab. Hits closer than one read count once,
    because the reads of a file under about 18 MB overlap. stop() is asked before each read and ends the probe early.
    The reads spread over zero_span() of the file."""
    size, hits, zero, read, n, last = os.path.getsize(path), [], bytes(4096), 0, decide.ZERO_READS, None
    with open(path, "rb", buffering=0) as f:
        lo, hi = zero_span(f, size)
        for i in range(n):
            if stop():
                return hits, read, True
            at = decide.ZERO_EDGE + (1 - 2 * decide.ZERO_EDGE) * (i + (0 if again else 0.5)) / n
            pos = max(lo, (lo + int((hi - lo) * at)) // 4096 * 4096)
            f.seek(pos)
            b = f.read(max(0, min(65536, hi - pos))); read += len(b)
            page = next((pos + j for j in range(0, len(b) - 4095, 4096) if b[j:j + 4096] == zero), None)
            if page is not None and (last is None or page - last >= 65536):
                run, more = zero_run(f, page, lo, hi); read += more
                if run >= decide.ZERO_RUN:
                    hits.append(page); last = page
    return hits, read, False


def zero_span(f, size):
    """(the first byte, the end) of the part of a file the zero probe reads. For Matroska that is the first Cluster to
    the Segment end. Zeros before it sit in the Attachments (a font), and zeros past the Segment end
    are no part of the file (see header_probe()). A walk that meets no Cluster starts at the
    Segment data. Another container reads whole."""
    f.seek(0); seg = decide.segment_start(f.read(64))
    if not seg:
        return 0, size
    ds, seg_size = seg
    end, p = size if seg_size is None else min(size, ds + seg_size), ds
    for _ in range(64):   # the level-1 elements before the first Cluster: a few, plus Void elements
        f.seek(p); e = decide.element(f.read(12), 0)
        if e and e[0] == decide.CLUSTER:
            return p, end
        if not e or e[2] is None or e[0] not in decide.LEVEL1:
            break
        p += e[1] + e[2]
    return ds, end


WHOLE_CHUNK = 8 << 20   # bytes of one read of the zero scan, a whole number of 64 KiB blocks
WHOLE_RATE = 20e6       # bytes a second the read pass may take at the least: a third of the slowest NAS a tester measured, 60 MB/s
WHOLE_MIN = 600         # seconds the read pass may always take
WHOLE_ASK = 1           # seconds between two asks of stop() in the zero scan


def zero_scan(path, stop=lambda: None):
    """(the zero runs of ZERO_RUN or more as [first byte, end], bytes read) of every byte of zero_span(), the zeros
    stage of whole_check(). It tests each 64 KiB block of a grid from the span's start. A run of ZERO_RUN holds a
    whole block of the grid, and zero_run() measures the run around its first zero block. The later zero blocks of the
    run move its end, a block at a time. stop() runs every WHOLE_ASK seconds and may raise to end the scan, as
    runner.deep_waits() does. A running time limit ends it too."""
    runs, read, zero, size = [], 0, bytes(65536), os.path.getsize(path)
    with open(path, "rb", buffering=0) as f:
        lo, hi = zero_span(f, size)
        pos, asked, last = lo, time.monotonic(), None   # last: the end of the latest zero block, so a run counts once
        while pos < hi:
            if time.monotonic() - asked >= WHOLE_ASK:
                stop(); config.DEADLINE.check(); asked = time.monotonic()
            f.seek(pos); want = min(WHOLE_CHUNK, hi - pos); b = f.read(want)
            while 0 < len(b) < want and (more := f.read(want - len(b))):   # a short read would move the grid and split a run in two
                b += more
            read += len(b)
            if not b:
                break
            for i in range(0, len(b) - 65535, 65536):
                if not b.startswith(zero, i):
                    continue
                at = pos + i
                if at == last and runs and runs[-1][1] == at:   # the counted run goes on
                    runs[-1][1] = at + 65536
                elif at != last:
                    n, more = zero_run(f, at, lo, hi); read += more
                    if n >= decide.ZERO_RUN:
                        runs.append([at, at + 65536])
                last = at + 65536
            pos += len(b)
    return runs, read


def zero_again(path, runs):
    """(the runs of zero_scan() that zero_run() still measures at ZERO_RUN or more, bytes read): the second check
    before a re-grab of the whole-file check. It reads at most 2 x ZERO_RUN per run, so never the whole file again. A
    file that is gone or does not read keeps no run."""
    try:
        with open(path, "rb", buffering=0) as f:
            lo, hi = zero_span(f, os.path.getsize(path))
            got = [(r, zero_run(f, r[0], lo, hi)) for r in runs]
    except OSError:
        return [], 0
    return [r for r, (n, _) in got if n >= decide.ZERO_RUN], sum(more for _, (_, more) in got)


def zero_pad(path):
    """The first byte of the zero padding of a Matroska file, or None. Padding is one run of zero bytes after the Cues,
    inside the Segment. Only whole level-1 elements that are not Clusters lie from the Cues to the Segment end, and at
    least one follows the run. No player reads it. Some releases end with a few hundred zero bytes past their Segment.
    mkvpropedit can then write a grown element at the end of the file and extend the Segment over the zeros. The read
    pass names the first zero. None for an unknown Segment size, a Segment end past the file end, no SeekHead that
    lists the Cues, see seeks(), Cues before a Cluster, a run of ZERO_RUN or more, or an element past the Segment end."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2); f.seek(0); b = f.read(config.HEADER_READ)
            seg = decide.segment_start(b)
            cues = seg and seg[1] is not None and seeks(f, b, seg[0])[0].get(decide.CUES)
            if not cues or seg[0] + seg[1] > size:
                return None
            p = seg[0] + cues[0]
            f.seek(p); e = decide.element(f.read(16), 0)
            if not e or e[0] != decide.CUES or e[2] is None:
                return None
            run = zero_gap(f, p + e[1] + e[2], seg[0] + seg[1])[0]
    except OSError:
        return None
    return run and run[0]


def zero_gap(f, p, end):
    """((the first byte, the length) of the zero padding, or None, bytes read) of the open Matroska file f, see
    zero_pad(). p is where the Cues end, and end is the Segment end. It walks the elements from p by their headers."""
    run, read = None, 0
    for _ in range(64):   # a few elements: Tags, Chapters, Tracks, a Void
        if p >= end:
            break
        f.seek(p); h = f.read(16); read += len(h)
        if h[:1] == b"\0" and run is None:
            f.seek(p); z = f.read(min(decide.ZERO_RUN, end - p)); k = len(z) - len(z.lstrip(b"\0")); read += len(z)
            if k == len(z):   # a run of ZERO_RUN or more, or one up to the Segment end
                return None, read
            run, p = (p, k), p + k
            continue
        e = decide.element(h, 0)
        if not e or e[2] is None or e[0] not in decide.LEVEL1 or e[0] == decide.CLUSTER:
            return None, read
        p += e[1] + e[2]
    return (run if p == end else None), read


def play_times(path, pos):
    """The play time of each byte of pos in a Matroska file, in seconds: the Timestamp of the last Cluster at or before
    it that the Cues name, from one 32-byte read of that Cluster's start. So the time is never more than a few frames
    later than the byte's blocks. Reordered frames can time a block a little before its Cluster's Timestamp. A Cluster
    the Cues do not name makes the time earlier. The Clusters after the last named one are walked, a
    few at most, and each gives its own Timestamp. "start" for a byte before the first named Cluster, and "end" for
    one past the last Cluster, when the walk reaches the next level-1 element or the Segment end. When it reaches
    neither, a byte past the Clusters it read gets None, as does a byte of a Cluster whose Timestamp does not read.
    Every byte gets None in another container, with no SeekHead that lists the Cues, see seeks(), and with Cues that
    do not parse to their end, as when the last piece of a download is lost. The read pass names bytes, and an alert
    names play times, or a share of the file size for None."""
    out = [None] * len(pos)
    stamp = lambda h, e: next((int.from_bytes(h[d:d + n], "big") for i, d, n in decide.children(h, e[1], len(h)) if i == decide.TIMESTAMP), None)
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2); f.seek(0); b = f.read(config.HEADER_READ)
            seg = decide.segment_start(b)
            cues = seg and seeks(f, b, seg[0])[0].get(decide.CUES)
            scale = cues and decide.timestamp_scale(b, seg[0])
            if not scale:
                return out
            f.seek(seg[0] + cues[0]); e = decide.element(f.read(12), 0)
            if not e or e[0] != decide.CUES or e[2] is None or e[2] > config.CUES_MAX:
                return out
            f.seek(seg[0] + cues[0] + e[1]); c = f.read(e[2])
            if len(c) != e[2] or max((d + n for _, d, n in decide.children(c, 0, len(c))), default=0) != len(c):
                return out   # zeros from inside the Cues on: the Clusters they name are only a part
            named = sorted(seg[0] + p for p in decide.last_cues(c, decide.CUECLUSTER))
            if not named:
                return out
            end, p, done, walked = size if seg[1] is None else min(size, seg[0] + seg[1]), named[-1], None, []   # done: the end of the last Cluster
            for _ in range(64):   # the Clusters from the last one the Cues name: a few
                f.seek(p); h = f.read(32); e = decide.element(h, 0)
                if p >= end or (e and e[0] in decide.LEVEL1 and e[0] != decide.CLUSTER and e[2] is not None):
                    done = p
                    break
                if not e or e[0] != decide.CLUSTER or e[2] is None:   # a broken Cluster: its end is unknown
                    break
                walked.append((p, stamp(h, e)))
                p += e[1] + e[2]
            ticks, starts = dict(walked), [w for w, _ in walked]   # the Timestamp of each Cluster read
            for k, x in enumerate(pos):
                if x < named[0]:
                    out[k] = "start"
                elif done is not None and x >= done:
                    out[k] = "end"
                elif x < p:   # past p lie the Clusters the walk did not read
                    c = starts[bisect.bisect_right(starts, x) - 1] if x >= named[-1] else named[bisect.bisect_right(named, x) - 1]
                    if c not in ticks:
                        f.seek(c); h = f.read(32); e = decide.element(h, 0)
                        ticks[c] = stamp(h, e) if e and e[0] == decide.CLUSTER else None
                    out[k] = None if ticks[c] is None else round(ticks[c] * scale / 1e9, 3)
    except OSError:
        pass
    return out


COPY_KEEP = 1000   # lines of the read pass kept for their places. A healthy remux can log a NOISE line per packet.


def copy_lines(stream, got):
    """Read the read pass's stderr into got, line by line in a thread of its own, so a long stderr never blocks
    ffmpeg and never fills a disk. got takes the count of NOISE lines, the count of the other lines, and the first
    COPY_KEEP of those in "kept"."""
    for line in stream:
        if not line.strip():
            continue
        if any(n in line for n in decide.NOISE):
            got["noise"] += 1
            continue
        got["lines"] += 1
        if len(got["kept"]) < COPY_KEEP:
            got["kept"].append(line.rstrip("\n"))


PCM_FORMATS = ("matroska", "mov", "mp4")   # ffprobe's format names where the PCM rule holds, see pcm_audio()


def stream_probe(path):
    """ffprobe's format name of path and its streams with index, PID, codec and sample rate, as a dict, or None when
    ffprobe cannot read the file. ffprobe opens the file as ffmpeg -i does, so a stream it names with no sample rate
    has none in the read pass either."""
    try:
        r = run_bounded(["ffprobe", "-v", "error", "-show_entries", "stream=index,id,codec_type,sample_rate,codec_name:format=format_name",
                         "-of", "json", path], 120, use="checks.stream_probe", capture_output=True, text=True, errors="replace")
        return ff.loads(r) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def truehd_cores(streams):
    """The ffprobe streams of streams that hold the AC-3 core of a Blu-ray TrueHD PID. ffmpeg names such a core as an
    ac3 stream of its own on the PID of the TrueHD stream, see ts.CORE. It does so even when the PID holds no PES of
    the core. ffprobe names a PID only in a TS."""
    truehd = {s["id"] for s in streams if s.get("codec_name") == "truehd" and s.get("id")}
    return [s for s in streams if s.get("codec_name") == "ac3" and s.get("id") in truehd]


def pcm_audio(path, got=None):
    """Whether path is Matroska, MP4 or MOV with a PCM audio track, by ffprobe's format and codec names (pcm_s24le,
    pcm_f32le, pcm_s16be), or None when ffprobe cannot read the file. Silent PCM is zero bytes, so only the zero runs
    the read pass confirms count in such a file, see decide.whole_verdict(). In other containers the read pass named no
    byte inside a real gap, and silent PCM left no zero run, so there every run counts and this is False. got is
    stream_probe() of path when the caller holds it."""
    got = stream_probe(path) if got is None else got
    if not got:
        return None
    if not set(((got.get("format") or {}).get("format_name") or "").split(",")) & set(PCM_FORMATS):
        return False
    return any(x.get("codec_type") == "audio" and (x.get("codec_name") or "").startswith("pcm_") for x in got.get("streams") or [])


def whole_check(path, stop=lambda: None):
    """(certain reason or None, [doubts of the zero runs], [doubts of the read pass], log fields) of the whole-file
    check (docs/design.md, "Whole-file check"). Two read-only passes over the same file at once, so the NAS sends it
    about once. The read pass, ffmpeg -c copy of every stream to the null muxer, starts first in its own process.
    zero_scan() reads beside it in the job's process, and the slower reader takes the file from the page cache. In a
    file with no PCM audio, see pcm_audio(), a certain zero scan kills the read pass, which then gives nothing. In a
    file with PCM audio the read pass runs to its end, because it confirms the zero runs. It stops at WHOLE_MIN seconds
    or the time WHOLE_RATE needs, whichever is longer, and a stopped pass gives nothing. Its stderr goes through a pipe
    to copy_lines(). stop() runs as often as zero_scan() and watch() ask, and may raise, see zero_scan(). No ffmpeg
    outlives the check. The zeros field holds the runs in bytes, in "at" as shares of the size, and in "count" the runs
    that count. fault is zero-filled for a certain verdict. A byte of the read pass that starts the file's zero_pad()
    moves from pos to padding, because no player reads it. when holds the play_times() of the other bytes, and in the
    zeros field those of the first byte of each run. When the pass printed a line, tail holds the count of bytes past
    the Segment end, and tail_errors says whether they make ffmpeg read the last element again, see tail_errors().

    The read pass leaves out each stream of truehd_cores() that has no sample rate when ffmpeg opens the file. The
    null muxer cannot write a header with such a stream. The pass then printed one error and read nothing. A PID with no
    core PES gives such a core, and so does one whose core PES start after ffmpeg's probe. The TrueHD stream of the PID
    stays in the read, so the demuxer still reads every PES of the PID."""
    size, t0, info = os.path.getsize(path), time.monotonic(), stream_probe(path) or {}
    pcm, bare = pcm_audio(path, info), [s["index"] for s in truehd_cores(info.get("streams") or []) if not int(s.get("sample_rate") or 0)]
    bound = max(WHOLE_MIN, size / WHOLE_RATE)
    took = lambda t: round(time.monotonic() - t, 1)
    p = ff.popen("checks.whole_check", ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "error", "-i", path,
                          "-ignore_unknown", "-map", "0", *(a for i in bare for a in ("-map", f"-0:{i}")), "-c", "copy", "-f", "null", "-"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    got = {"kept": [], "lines": 0, "noise": 0}
    reader = threading.Thread(target=copy_lines, args=(p.stderr, got), daemon=True)
    reader.start()
    try:
        runs, read = zero_scan(path, stop)
        zeros = {"runs": runs, "at": [[round(a / size, 4), round(e / size, 4)] for a, e in runs], "read": read, "took": took(t0)}
        if runs:
            zeros["when"] = play_times(path, [a for a, _ in runs])
        if len(runs) >= 2 and not pcm:
            p.kill(); p.wait()
            read_pass, out = {"skipped": "the zero scan was certain"}, None
        else:
            stopped = watch(p, lambda: stop() or config.DEADLINE.check() or (f"ran over {bound:.0f} s" if time.monotonic() - t0 > bound else None))
            p.wait(); reader.join()
            out = None if stopped else dict(decide.parse_copy("\n".join(got["kept"]), p.returncode, size), lines=got["lines"], noise=got["noise"])
            if out and out["pos"]:
                if (pad := zero_pad(path)) in out["pos"]:
                    keep = [b for b in out["pos"] if b != pad]
                    out.update(pos=keep, at=[round(b / size, 4) for b in keep], padding=pad)
                out["when"] = play_times(path, out["pos"])
            if out and out["lines"] and (short := segment_short(path) or 0) < 0:   # bytes past the Segment end, see decide.whole_verdict()
                out.update(tail=-short, tail_errors=tail_errors(path))
            read_pass = dict(out or {"skipped": stopped}, took=took(t0), code=p.returncode)
    finally:
        if p.poll() is None:   # a stop or an error: no ffmpeg outlives the check
            p.kill(); p.wait()
        reader.join(10)
        p.stderr.close()
    ff.check_stop()   # a stop killed the read pass, so it found nothing
    certain, zero, more, zeros["count"] = decide.whole_verdict(runs, size, out, pcm, zeros.get("when"))
    return certain, zero, more, {"zeros": zeros, "ffmpeg": read_pass, "size": size, "pcm": pcm, "fault": certain and "zero-filled"}


def tail_errors(path):
    """Whether the bytes past the Segment end of the Matroska file path make ffmpeg read its last element again, the
    tail_errors fault of matroska.check(). False for another file and for a file with no such bytes."""
    with open(path, "rb") as f:
        size = f.seek(0, 2); f.seek(0); b = f.read(config.HEADER_READ)
        seg = decide.segment_start(b)
        if not seg or seg[1] is None or seg[0] + seg[1] >= size:
            return False
        return "tail_errors" in matroska.check(f, b, seg[0], seg[0] + seg[1], size - seg[0] - seg[1])[0]


def stream_gap(path, video):
    """The doubt of a file that is not Matroska whose format duration runs past its video end by more than AV_APART, or
    None. check_audio() needs mkvmerge's duration, which an MP4, AVI or MPEG-TS file lacks. One stray audio packet can set
    the duration of an MP4 far past its video end."""
    with open(path, "rb") as f:
        if f.read(4) == decide.EBML_ID.to_bytes(4, "big"):
            return None
    whole = ffprobe_duration(path)
    if video and whole and whole - video > decide.AV_APART:
        return f"the file's tracks run to {content.hms(whole)}, but the video stops at {content.hms(video)}, so another track may be broken"
    return None


def encrypted_video(path):
    """True when the bytes show an encrypted video track: a ContentEncryption element in a Matroska video TrackEntry, or an
    encv sample entry with a sinf box that holds a tenc box in the MP4 moov. video_stages() asks only after 2 windows with
    no decoder, so a codec ffmpeg does not know stays a doubt."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2); f.seek(0); seg = decide.segment_start(f.read(64))
            p = seg[0] if seg else 0
            for _ in range(64):   # the level-1 elements before the first Cluster, or the top-level MP4 boxes before the moov
                f.seek(p); h = f.read(16)
                if seg:
                    e = decide.element(h, 0)
                    if not e or e[2] is None or e[0] not in decide.LEVEL1 or e[0] == decide.CLUSTER:
                        return False
                    if e[0] == decide.TRACKS and e[2] <= config.CUES_MAX:
                        f.seek(p + e[1]); return decide.mkv_encrypted_video(f.read(e[2]))
                    p += e[1] + e[2]
                    continue
                n, kind = int.from_bytes(h[:4], "big"), h[4:8]
                n = int.from_bytes(h[8:16], "big") if n == 1 else size - p if n == 0 else n
                if n < 8 or p + n > size:
                    return False
                if kind == b"moov":
                    f.seek(p); return n <= 64 << 20 and decide.mp4_encrypted_video(f.read(n))
                p += n
    except OSError:
        pass
    return False


def hook_edit_failed(path):
    """True when the decision log holds an editing line for path with no edited line after it, so the hook's own
    mkvpropedit run failed or was killed. A failed run can leave the Segment size past the end of the file. The store
    keeps the mark of such a line for store.KEEP_DECISIONS, see logs.log()."""
    return store.get("editing", path) is not None


REAP = threading.Lock()   # window() reaps its ffmpeg under it, see there


def watch(p, why):
    """Wait for the process p to exit, and return None. why() runs every WINDOW_POLL seconds and returns a reason to
    stop p, or None. A reason kills p, and watch() returns it. A pidfd says when p exits, before it is reaped."""
    exited = os.pidfd_open(p.pid)
    try:
        while not select.select([exited], [], [], config.WINDOW_POLL)[0]:
            if stopped := why():
                p.kill()
                return stopped
    finally:
        os.close(exited)
    return None


def main_stream(path):
    """ffprobe's stream of the film in path, see decide.main_video(), with its index and duration, or None. behind is
    True when a still-image track comes before it. An ffmpeg -ss then seeks by that track's index, and the film decodes
    from a frame that is no keyframe. So window() decodes such a film through PyAV."""
    r = run_bounded(["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,duration:stream_disposition=attached_pic",
                     "-of", "json", path], 120, use="checks.main_stream", capture_output=True, text=True, errors="replace")
    try:
        streams = ff.loads(r).get("streams") or []
        v = decide.main_video(streams)
    except (ValueError, AttributeError):
        return None
    first = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)   # V:0
    return v and dict(v, behind=v is not first)


STILL_FIRST = "a still image comes before the video, and a seek goes by its index"   # why no window runs with no PyAV


def window_stream(path):
    """{"stream": the film's ffprobe index} for window() when a still image comes before the film, see main_stream().
    Else {}, and window() runs ffmpeg."""
    v = main_stream(path)
    return {"stream": v["index"]} if v and v["behind"] else {}


def window(path, start, secs, stream=None):
    """Decode secs seconds of the main video stream from start, parsed by decide.parse_window(). Read-only, so it
    may be killed. A seek with no usable index reads the file from the start, and a seek into zeros reads to the next
    intact cluster. So the window stops at VIDEO_MAX_READ bytes or VIDEO_TIMEOUT seconds. 0:V:0 skips cover art.
    Skipping the loop filter saves time and parses the same bitstream. watch() waits for the decoder. Its bytes read
    are the change of our own count across the reap, under REAP, so the other scan workers do not add to it.

    stream is the film's ffprobe index when a still image comes before the film, see main_stream(). ffmpeg's -ss
    seeks by the first video stream, so avwindow.py decodes the window instead, in the venv of language detection.
    PyAV seeks by the film's own stream, and the stderr has the shape of ffmpeg's. Its window logs via "pyav"."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-threads", "2", "-skip_loop_filter", "all",
           "-ss", f"{start:.2f}", "-t", f"{secs:.2f}", "-i", path, "-map", "0:V:0", "-an", "-sn", "-dn", "-vf", "showinfo=checksum=0",
           "-f", "null", "-"] if stream is None else \
        [os.path.join(config.CFG.lid_dir, "venv", "bin", "python"), os.path.join(os.path.dirname(__file__), "avwindow.py"), path, str(stream),
         f"{start:.2f}", f"{secs:.2f}"]
    t0, stopped = time.monotonic(), None
    with ff.Log("checks.window", config.CFG.state_dir) as err:   # a file, so a long stderr never blocks ffmpeg
        try:
            p = ff.popen("checks.window", cmd, stdout=subprocess.DEVNULL, stderr=err)
        except OSError as ex:   # the venv's Python is gone, as after an upgrade of the host's Python: the window did not run
            return dict(decide.parse_window(f"[fatal] {ex}", 1), at=round(start), stopped=None, took=round(time.monotonic() - t0, 2), read=0,
                        error=config.mask(str(ex))[:200], **({} if stream is None else {"via": "pyav"}))
        try:
            stopped = watch(p, lambda: f"ran over {decide.VIDEO_TIMEOUT} s" if time.monotonic() - t0 > decide.VIDEO_TIMEOUT else
                            f"read over {decide.VIDEO_MAX_READ >> 20} MiB" if rchar(p.pid) > decide.VIDEO_MAX_READ else None)
            # ponytail: the samples and probes of other workers reap without REAP. One reaped in these microseconds adds its reads.
            with REAP:
                b0 = rchar(); p.wait(); read = rchar() - b0
        finally:
            if p.poll() is None:   # the job's time limit or an interrupt: no decoder outlives the check
                p.kill(); p.wait()
        ff.check_stop()   # a stop killed the decoder, so the window read nothing
        return dict(decide.parse_window(err.read(), 1 if stopped else p.returncode), at=round(start), stopped=stopped,
                    took=round(time.monotonic() - t0, 2), read=read, **({} if stream is None else {"via": "pyav"}))


def check_video(path, dur, again=False, hp=None, pcm=None, by=None):
    """(certain reason or None, [doubts], log fields) for one file of dur seconds, the shape of check_audio().

    Three read-only stages, each only when the earlier ones found nothing certain. header: a Matroska file shorter
    than its Segment size lost bytes, 64 bytes read. Another container skips it. A short file whose last edit by the
    hook failed is only a doubt, see hook_edit_failed(). zeros: zero_probe(). windows: three windows at the audio
    sample positions, for a file of 60 seconds or more. Each stage logs its result, seconds and bytes read. fault is
    the class of a certain verdict (truncated, zero-filled or bad windows), which the second check before a re-grab must
    find again. again is that second check: the zero reads move by half a step and the windows go to VIDEO_AGAIN, so it
    samples other parts of the file. Under a time limit (the hook) a stage starts only when the limit holds its worst
    case plus VIDEO_RESERVE, and the zero probe stops once the reserve is reached. A skipped or stopped stage is logged
    and is never a doubt. video_check() turns any exception into no verdict. The zeros stage logs its hits as shares of
    the file size, and their play_times() in when.

    hp is header_probe() of the file. Its fields join the header stage. With it, a Segment size under SHORT_CERTAIN past
    the end after the hook's own failed edit is a header issue, and no doubt. The header stage then says whether a remux
    may repair the header: repairable is True when the header has an issue and every stage ran and found no sign of
    real damage. A file with no usable Cues reads from the start for each window, so a container error anywhere before
    it counts. Only a window the read cap stopped with no error on the way is no sign of damage in such a file.
    Such a window is never a doubt, but in a file with usable Cues it still blocks the repair. windows_clean says
    all three windows decoded clean, which a repair of a track with no default duration needs.

    pcm says the file is Matroska, MP4 or MOV with a PCM audio track, see pcm_tracks(). None asks pcm_audio(), once per
    check. Digital silence in PCM audio is zero bytes, so in such a file the zero probe's hits are logged only, and the
    windows run all the same. The whole-file check judges the zeros, because its read pass confirms each run. The hits
    still block a header repair, as they do in any other file. Another container keeps the probe's verdict.

    by is the stream option of window(), see window_stream(). None reads it from the file.

    frame_rate holds the share of the windows' frame steps that are off the film's usual frame length, and whether that
    makes the rate variable, see decide.off_rate(). A step of a whole number of frames counts as on the rate, see
    skips there. It is logged only.
    """
    pcm = pcm_audio(path) if pcm is None else pcm
    certain, doubts, fields = video_stages(path, dur, again, hp, pcm, by)
    steps = collections.Counter()
    for w in (fields.get("windows") or {}).get("list") or []:
        steps.update(w.get("steps") or {})
    if (off := decide.off_rate(steps, skips=True)) is not None:
        fields["frame_rate"] = {"off": round(off, 4), "variable": off >= decide.VFR_SHARE}
    h = fields["header"]
    if hp and hp["issue"]:
        z, w = fields.get("zeros") or {}, fields.get("windows") or {}
        wins = w.get("list") or []
        against = hp["blocked"] + ([certain] if certain else []) + [d for d in doubts if d not in hp["blocked"]]
        against += [decide.stopped_doubt(x) for x in wins if hp["cues"] is True and decide.read_capped(x)]   # no doubt, still no repair
        if not z or "skipped" in z:
            against.append("the zero probe did not run to its end")
        if pcm and z.get("hits"):   # logged only, see video_stages(), and still no repair
            against.append(f"the zero probe found blank gaps at {len(z['hits'])} places in a file with PCM audio")
        if w.get("skipped") or len(wins) < 3:   # a skip is never clean
            against.append(f"the video windows did not all run: {w.get('skipped') or 'none ran'}")
        clean = len(wins) == 3 and all(x["ran"] and not decide.bad_window(x) and not x["empty"] and not x.get("stopped") for x in wins)
        h.update(blocked=against, repairable=not against, windows_clean=clean)
    return certain, doubts, fields


def video_stages(path, dur, again, hp, pcm=False, by=None):
    """The three stages of check_video(). With pcm the zero probe's hits give no verdict, and the windows run. by is
    as in check_video()."""
    left = lambda: config.DEADLINE.left() or float("inf")   # None means no time limit, as in a scan
    took = lambda t: round(time.monotonic() - t, 2)
    fields, doubts, t = {"fault": None}, [], time.monotonic()
    if hp is None:
        short = segment_short(path)
        failed = bool(short and short > 0) and hook_edit_failed(path)
        fields["header"] = {"skipped": "not Matroska, or no Segment size"} if short is None else {"short": short, "took": took(t), "read": 64}
    else:
        short, failed = hp["short"], hp["failed_edit"]
        fields["header"] = dict(hp, issue=list(hp["issue"]), blocked=list(hp["blocked"]))
    if short is not None and short >= decide.SHORT_CERTAIN and not failed:
        return f"the file is {short / 1e6:.1f} MB smaller than it should be, so the download is incomplete", [], dict(fields, fault="truncated")
    if short and short > 0 and not (hp and failed and short < decide.SHORT_CERTAIN):   # with hp, a header issue instead
        doubts.append(f"the file may be missing {short} bytes at its end" + (", after its last flag edit failed" if failed else ""))
    if hp and hp.get("not_whole"):   # the blocked repair alone posts nothing
        doubts.append(hp["not_whole"])
    if hp is None and short is None and (gap := stream_gap(path, dur)):
        doubts.append(gap)
    if left() < config.ZERO_SECS + config.VIDEO_RESERVE:
        fields["zeros"] = {"skipped": f"{left():.0f} s left of the job's time limit"}
        return None, doubts, fields
    t = time.monotonic(); pos, read, stopped = zero_probe(path, again, lambda: left() < config.VIDEO_RESERVE)
    size = os.path.getsize(path)
    hits, when = [round(b / size, 3) for b in pos], play_times(path, pos) if pos else None   # the Cues: a few small reads
    fields["zeros"] = {"hits": hits, **({"when": when} if when else {}), "took": took(t), "read": read, **({"pcm": True} if pcm else {})}
    if stopped:   # a partial probe gives no verdict
        fields["zeros"]["skipped"] = f"stopped with {left():.0f} s left of the job's time limit"
        return None, doubts, fields
    windows, skipped, t = [], None, time.monotonic()
    if len(hits) < 2 or pcm:
        by = (window_stream(path) if by is None else by) if dur >= 60 else {}
        for share in decide.VIDEO_AGAIN if again else decide.VIDEO_AT:
            skipped = "the duration is under 60 s" if dur < 60 else \
                STILL_FIRST if by and not lid_ready() else \
                f"{left():.0f} s left of the job's time limit" if left() < decide.VIDEO_TIMEOUT + config.VIDEO_RESERVE else None
            if skipped:
                break
            windows.append(window(path, dur * share, min(decide.VIDEO_SECS, dur * (1 - share)), **by))
        fields["windows"] = dict(list=windows, took=took(t), read=sum(w["read"] for w in windows), **({"skipped": skipped} if skipped else {}))
    if sum(bool(w.get("nodecoder")) for w in windows) >= 2 and not any(w.get("encrypted") for w in windows) and encrypted_video(path):
        for w in windows:   # ffmpeg named no encryption, but the bytes show it
            w["encrypted"] = bool(w.get("nodecoder"))
    certain, more = decide.video_verdict([] if pcm else hits, windows, when=when)
    return certain, more + doubts, dict(fields, fault=certain and ("zero-filled" if len(hits) >= 2 and not pcm else "bad windows"))


def video_seconds(j, hp=None):
    """Where the main video stream ends, for the video windows, or None. The real end from header_probe() when it read
    the last clusters, else the video track's DURATION tag when mkvmerge wrote it for this file. ffprobe's format
    duration is the Segment duration, which a long subtitle event can push far past the video end."""
    if hp and hp.get("video"):
        return hp["video"]
    app = (((j or {}).get("container") or {}).get("properties") or {}).get("writing_application") or ""
    p = (decide.main_video((j or {}).get("tracks") or []) or {}).get("properties") or {}
    if app.startswith("mkvmerge") and p.get("tag__statistics_writing_app", app) == app and p.get("tag_duration"):   # ffmpeg copies stale tags
        return decide.tag_seconds(p["tag_duration"])
    return None


def video_summary(certain, doubts, fields):
    """The corrupt-video verdict for the decision log: the verdict, the fault class, and each stage's result."""
    return dict(certain=certain, doubts=doubts, **fields)


def pcm_tracks(j):
    """Whether the mkvmerge probe j of a Matroska file lists a PCM audio track, by codec_id A_PCM/.... False for a
    container outside the PCM rule, as in pcm_audio(). None for MP4 and MOV, where mkvmerge names some PCM by its
    fourcc (ipcm, fpcm, fl32, in32), and for a probe with no track. pcm_audio() then asks ffprobe."""
    tracks, kind = (j or {}).get("tracks"), (((j or {}).get("container") or {}).get("type") or "Matroska")   # no type reads as Matroska
    if not tracks or kind == "MP4/QuickTime":
        return None
    return kind == "Matroska" and any(t.get("type") == "audio" and ((t.get("properties") or {}).get("codec_id") or "").startswith("A_PCM")
                                      for t in tracks)


def video_inputs(path, j=None, hp=None):
    """(the duration for the windows, header_probe() or None, pcm_tracks(), the stream option of window() or None) of
    one file for check_video(). A .mkv file is probed with mkvmerge when j is not given. A probe that fails reads as
    none, so the zero probe and the windows still run. The duration is video_seconds(), else the video stream's own for
    a file that is not Matroska, else the format duration. A Matroska file whose probe lists one video track and no
    warning holds no still image before the film, so window() needs no ffprobe, see window_stream(). mkvmerge skips a
    track it cannot read and warns, as in an MP4."""
    if path.lower().endswith(".mkv") and j is None:
        try:
            j = mkvmerge(path)
        except content.OutOfTime:
            raise
        except Exception:
            j = {}
    hp = hp or (header_of(path, j) if j else None)
    matroska = ((j or {}).get("container") or {}).get("type") == "Matroska"   # ffprobe reads no stream duration from Matroska
    one = matroska and not j.get("warnings") and sum(t.get("type") == "video" for t in j.get("tracks") or []) == 1
    return video_seconds(j, hp) or (None if matroska else video_stream_seconds(path)) or ffprobe_duration(path) or 0, hp, pcm_tracks(j), \
        {} if one else None


def video_stream_seconds(path):
    """The duration ffprobe reads for the main video stream, see main_stream(), or None. Matroska gives none. The format
    duration can come from a stray packet of another stream."""
    try:
        return float((main_stream(path) or {}).get("duration") or 0) or None
    except ValueError:
        return None


def video_check(path, again=False, j=None, hp=None):
    """check_video() with the duration of video_seconds(), else ffprobe's. j is the file's mkvmerge -J probe and hp its
    header_probe(), each read here for a .mkv file when not given. Any exception, the job's time limit included, is no
    verdict. Its log fields then carry error and code video_check_error. So the check never costs the flag edit, and a
    second check that fails keeps the file. The time limit outside the check still stops the job."""
    try:
        dur, hp, pcm, by = video_inputs(path, j, hp)
        return check_video(path, dur, again, hp, pcm, by)
    except Exception as ex:
        return None, [], {"fault": None, "error": config.mask(f"{type(ex).__name__}: {ex}")[:200], "code": "video_check_error"}


def lid_ready():
    """The install writes LID_DIR/ready last, after the venv and the model. A half-built install has no marker."""
    return os.path.exists(os.path.join(os.path.dirname(__file__), "lid.py")) and os.path.exists(os.path.join(config.CFG.lid_dir, "ready"))


LID_ONE = threading.Lock()   # the workers of a backfill ask for the model one at a time, see lid_turn()


def lid_turn(deadline, background=False):
    """A turn at the language model: (the open lock file to close after the hearing, the turn's number). None when
    time.monotonic() passes deadline first. The host has BACKGROUND_WORKERS turns, the locks lid.turn, lid.turn.1 and so
    on in the state directory, so each background job can hear at once. A taker holds lid.turn.gate while it waits, and
    a backfill sends one worker at a time (LID_ONE). So a hook job waits for the first turn a hearing in progress gives
    up, never behind a queue of backfill files. lid.py takes the model lock of the same turn, which is free by then.
    A background taker, a job of the background queue in a job process, holds the gate shared, and only while it tries
    the turns. A sweep tests the gate shared too, see lid.waits(). So the taker waits for a free turn and makes no sweep
    yield, and it takes no turn while another taker holds the gate."""
    base = os.path.join(config.CFG.state_dir, "lid.turn")
    gate, turns, got = open(base + ".gate", "w"), [open(base + (f".{k}" if k else ""), "w") for k in range(config.CFG.background_workers)], None
    try:
        gated = False
        while got is None:
            gated = gated or flocked(gate, background)
            got = next((k for k, f in enumerate(turns) if gated and flocked(f)), None)
            if gated and background:
                fcntl.flock(gate, fcntl.LOCK_UN)
                gated = False
            if got is None:
                if time.monotonic() >= deadline:
                    return None
                select.select([], [], [], 0.2)
                ff.check_stop()
        return turns[got], got
    finally:
        gate.close()
        for k, f in enumerate(turns):
            if k != got:
                f.close()


def flocked(f, shared=False):
    """Whether this process now holds the open file f locked, exclusive or shared, with no wait."""
    try:
        fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def lid_cached(path, index, j, words=None):
    """lid.py has an answer cached for this stream of this file, so the run needs no turn at the model. words is
    (language, window starts) of the subtitle check, see lid.listen()."""
    try:
        from . import lid
        cache = os.path.join(config.CFG.state_dir, "lid.sqlite")
        if words:
            return lid.words_get(cache, path, index, lid.tag(lid.MODEL), *words[:3]) is not None
        return lid.cache_get(cache, path, index, lid.tag(lid.MODEL), decide.duration(j)) is not None
    except Exception:   # no module, no file, no cache: take the turn
        return False


def lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words=None, then=None, yield_to=None):
    """One lid.py run on ffmpeg audio stream index. Returns its JSON answer, or {"why": the reason there is none}.
    It runs in its own process group. A timeout or the job's time limit kills the whole group, so no ffmpeg cut
    outlives it. fresh hears past the cache. The hook and the subtitle hunter both call it. keep keeps the samples'
    audio for the subtitle check. words is (language, window starts, seconds of each window, a second window per start
    or None): the run then gives the words of those windows for the subtitle check instead, see lid.listen(). then
    is a file of the subtitle check's hearings that a language check runs after its own, with the model loaded, see
    lid.jobs(). yield_to is (the gate file, the state store or None) at which a hearing of many windows yields, see lid.waits().
    A run that must hear first waits for a turn at the model, see lid_turn(). timeout starts after that wait,
    and a job's time limit caps it then. A wait of a second or more comes back as "waited"."""
    if not lid_ready():
        return {"why": "language detection is not installed"}
    start, cached = time.monotonic(), not fresh and lid_cached(path, index, j, words)
    with contextlib.nullcontext() if cached else LID_ONE:
        left = config.DEADLINE.left()   # a hook job waits only while its time limit leaves room to hear
        turn = None if cached else lid_turn(start + (left - config.LID_RESERVE - 10 if left else float("inf")),
                                            (config.JOB or "").startswith("deep-analysis-"))
        waited = time.monotonic() - start
        if not cached and turn is None:
            return {"why": f"another hearing held the model for {waited:.0f} seconds", "waited": waited}
        turn, slot = turn or (None, 0)
        try:
            left = config.DEADLINE.left()
            got = lid_cli(path, index, j, expect, min(timeout, left - config.LID_RESERVE) if left else timeout, fresh, keep, words, then, yield_to,
                          slot=slot)
            return dict(got, waited=waited) if waited >= 1 else got
        finally:
            if turn:
                turn.close()


def lid_speech(path, index, j, timeout, yield_to=None):
    """The spans of speech of the whole of ffmpeg audio stream index, from one lid.py run, see lid.speech(). Silero VAD
    needs no Whisper model, so the run takes no turn at it. yield_to is (the gate file or None, the state store or None)
    at which the read yields, see lid.waits(). Returns lid.py's answer, or {"why": the reason there is none}."""
    if not lid_ready():
        return {"why": "language detection is not installed"}
    return lid_cli(path, index, j, (), timeout, False, yield_to=yield_to, speech=True)


def lid_cli(path, index, j, expect, timeout, fresh, keep=False, words=None, then=None, yield_to=None, speech=False, burn=(), slot=0):
    """The lid.py process of lid_run() or lid_speech(), killed with its process group after timeout seconds. burn holds
    the flags of a burnin.py run instead, the burned-in subtitle check, which runs in the same venv, see
    process.burn_run(). slot is the turn at the model that lid_run() holds, see lid_turn()."""
    argv = [os.path.join(config.CFG.lid_dir, "venv", "bin", "python"), os.path.join(os.path.dirname(__file__), "burnin.py" if burn else "lid.py"),
            path, str(index), str(decide.duration(j)), "--cache", os.path.join(config.CFG.state_dir, "lid.sqlite"),
            "--model-dir", os.path.join(config.CFG.lid_dir, "models")]
    if burn:
        argv += list(burn)
    elif words:
        argv += ["--words", words[0], *map(str, words[1]), "--secs", str(words[2] if len(words) > 2 else subsync.WINDOW)]
        argv += ["--more", *("-" if x is None else str(x) for x in words[3])] if len(words) > 3 and words[3] else []
        argv += ["--group", str(words[4])] if len(words) > 4 and words[4] else []   # the whole-file hearing, see subtitles.sweep_hear()
    elif speech:
        argv.append("--speech")
    else:
        argv += (["--fresh"] if fresh else []) + (["--keep-pcm"] if keep else []) + ["--expect", *expect] + (["--then-words", then] if then else [])
    argv += [a for opt, v in zip(("--yield-gate", "--yield-queue"), yield_to or ()) if v for a in (opt, v)]
    argv += ["--slot", str(slot)] if slot else []
    if (yield_to or (None, None))[1] and config.JOB:   # a job process yields only for its slot, see runner.import_waits()
        argv += ["--yield-job", config.JOB, str(config.slots()), str(config.CFG.hook_workers)]
    t0 = time.time()
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    except OSError as ex:
        return {"why": f"{type(ex).__name__}: {ex}"}
    try:
        got = json.loads(p.communicate(timeout=timeout)[0].strip().splitlines()[-1])
        return got if isinstance(got, dict) else {"why": "lid.py printed no JSON object"}
    except subprocess.TimeoutExpired:
        return {"why": f"no answer in {timeout:.0f} seconds"}
    except (ValueError, IndexError) as ex:
        return {"why": f"{type(ex).__name__}: {ex}"}
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
        with contextlib.suppress(OSError):   # the empty log that onnxruntime of this process left, never an older file of that name
            log_file = ORT_LOG.format(p.pid)
            if os.stat(log_file).st_size == 0 and os.stat(log_file).st_mtime >= t0 - 1:
                os.remove(log_file)
        ff.check_stop()   # a stop killed the helper, so its answer counts for nothing


ORT_LOG = "/tmp/mat-debug-{}.log"   # onnxruntime in the Whisper venv creates this empty file on import, always in /tmp


def hear(path, j, d, original, fresh=False, only=None, keep=False, then=None):
    """lid.py on each main audio track whose language is below AGREE, or on the main tracks at the positions in only.
    Returns ({track position: the language heard}, {track position: lid.py's answer}) for the decision and the log.
    All tracks of a file share LID_TIMEOUT of hearing, cut to the job's time left minus LID_RESERVE for the audio samples.
    A wait for the host's model does not count, see lid_run(). With too little left, detection is
    skipped and the plan goes on without it. A missing install, a timeout or an error is no answer and never fails the
    job. The time limit passes. fresh hears past the cache. keep keeps the samples' audio for the subtitle check, and
    then is the file of the subtitle check's hearings that the first hearing runs too, see lid_run()."""
    limit = config.DEADLINE.left()   # None when no time limit runs, as in a backfill
    budget = min(config.LID_TIMEOUT, limit - config.LID_RESERVE) if limit else config.LID_TIMEOUT
    heard, out, spent = {}, {}, 0.0   # spent counts the hearing, never a wait for the model, see lid_run()
    for i, t in enumerate(t for t in d["tracks"] if t["kind"] == "a"):   # i is ffmpeg's audio index, as in sample()
        if t["role"] != "main" or (t["pos"] not in only if only is not None else t["conf"] >= decide.AGREE):
            continue
        left = budget - spent
        if not lid_ready():
            got = {"why": "language detection is not installed"}
        elif left < 10:
            got = {"why": f"no time left of the {max(budget, 0):.0f} seconds detection may take"}
        else:
            t0 = time.monotonic()
            got = lid_run(path, i, j, [t["tag"], *sorted(decide.codes(original))], left, fresh, keep, then=then)
            then = None   # once: its words are cached then
            spent += time.monotonic() - t0 - got.get("waited", 0)
        out[t["pos"]] = {"lang": got.get("lang"), "prob": got.get("prob"), "why": config.mask(str(got.get("why") or ""))[:200] or None,
                         "cached": got.get("cached"), "took": got.get("took"), **({"waited": round(got["waited"])} if got.get("waited") else {})}
        if got.get("lang"):
            heard[t["pos"]] = got["lang"]
    return heard, out


LANGS = []
def langs():
    """mkvmerge's language table for decide.retag() and remux.props_fault(), read once per process. Empty when mkvmerge
    cannot list it: then a tag edit writes the 639-2 code, and a language still changes on two agreeing signals.
    An empty table is never kept, so the next call reads again. With none, props_fault() refuses a BCP 47 tag that
    mkvmerge wrote for a track with none."""
    if not LANGS:
        try:
            got = decide.language_table(run_bounded(["mkvmerge", "--list-languages"], 60, capture_output=True, text=True, errors="replace").stdout)
        except Exception:   # retag() takes only the BCP 47 form of a new tag from the table, so a failure never stops a job
            return {}, {}
        if not got[1]:
            return got
        LANGS.append(got)
    return LANGS[0]


def item_languages(original, expected):
    """The item's languages for decide.retag(): (the app's and TMDB's original languages, TMDB's spoken languages)."""
    e = expected or {}
    return decide.codes(original) | {e.get("original")} - {None}, set(e.get("spoken") or [])


def lid_carry(path, before, was=None):
    """Move lid.py's cached answers for path from its stat before an edit to its stat now. mkvpropedit changes the
    mtime but never the audio. was is the original's path of a conversion, whose words move too. A failed import of
    lid.py costs nothing, and carry() never raises."""
    try:
        from . import lid
    except ImportError:
        return
    lid.carry(path, before, os.path.join(config.CFG.state_dir, "lid.sqlite"), was)


def tmdb_ask(app, ctx, fresh=False):
    """(content.expected_languages() for the item of ctx, the time of the ask). None means TMDB is unknown. fresh asks
    through an empty scratch cache, so a second check before a re-grab never repeats the first answer."""
    cache, t0 = "tmdb-recheck" if fresh else "tmdb", time.time()
    if fresh:
        store.drop(cache)
    return content.expected_languages(config.program(app), ctx.get("ids") or {}, token=config.CFG.tmdb_token or None, cache=cache), t0


def spoken_of(expected):
    """TMDB's spoken languages for decide.decide(), a code TMDB gives with no 639-2 code included. None when unknown."""
    return None if expected is None else sorted(set(expected.get("spoken") or []) | set(expected.get("unmapped") or []))


def track_list(j):
    """(type, codec, language) of each track of an mkvmerge -J probe, see decide.track_tag(). A missing language reads as und."""
    return [(t.get("type"), t.get("codec"), decide.track_tag(t.get("properties") or {})) for t in j.get("tracks") or []]


def ffprobe_duration(path):
    """The format duration ffprobe reads, in seconds, or None. A missing ffprobe or a hung read is None too, so no caller
    loses its stage to it (CI has no ffprobe)."""
    try:
        r = run_bounded(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                        120, use="checks.ffprobe_duration", capture_output=True, text=True, errors="replace")
        return float(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
