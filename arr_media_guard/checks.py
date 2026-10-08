# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The checks of one file: the probes, the header check, broken audio, corrupt video and the spoken language."""
import contextlib, fcntl, json, os, re, select, signal, subprocess, tempfile, threading, time

from . import config, content, decide, store, subsync, subtitles


def run_bounded(argv, timeout, **kw):
    """subprocess.run() with timeout cut to the job's time left. A cut raises OutOfTime. A caller takes a TimeoutExpired
    for a slow read and goes on."""
    try:
        return subprocess.run(argv, timeout=config.DEADLINE.bound(timeout), **kw)
    except subprocess.TimeoutExpired:
        config.DEADLINE.check()
        raise


def mkvmerge(path):
    r = run_bounded(["mkvmerge", "-J", path], 120, capture_output=True, text=True, errors="replace")
    if r.returncode > 1:   # 1 means warnings, and the JSON is still complete
        raise RuntimeError("mkvmerge: " + (r.stdout + r.stderr).strip()[-300:])
    return json.loads(r.stdout)


def sample(path, start, index, window=decide.SAMPLE_SECS):
    """Decode 20 seconds of one audio track with volumedetect. Read-only, so a timeout may kill it."""
    try:
        r = run_bounded(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-ss", f"{start:.1f}", "-i", path,
                         "-t", f"{window:.1f}", "-map", f"0:a:{index}", "-vn", "-sn", "-dn", "-af", "volumedetect", "-f", "null", "-"],
                        120, capture_output=True, text=True, errors="replace")
        got = decide.parse_sample(r.stderr, r.returncode)
    except subprocess.TimeoutExpired:
        got = dict(decide.parse_sample("", 1))
    return dict(got, at=round(start), window=window)


def ffprobe_audio(path):
    """The audio stream indexes ffprobe sees, or None when ffprobe fails."""
    r = run_bounded(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                    120, capture_output=True, text=True, errors="replace")
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


AUDIO_MORE = ("kind", "decoded", "end", "audio", "video", "video_gap", "held", "hole", "credits", "took", "error")   # log fields of audio_more()
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
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-progress", "pipe:1", "-i", path,
                            "-map", f"0:a:{index}", "-vn", "-sn", "-dn", "-af",
                            "aformat=sample_rates=48000:channel_layouts=mono,asetpts=PTS-STARTPTS,volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {"kind": "full", "ran": False, "error": f"ffmpeg did not finish: {type(ex).__name__}"}
    got = decide.parse_sample(r.stderr, r.returncode)
    end = re.findall(r"out_time_us=(\d+)", r.stdout)
    return {"kind": "full", "ran": got["ran"], "errors": got["errors"], "decoded": round(sum(map(int, re.findall(r"n_samples: (\d+)", r.stderr))) / 48000, 1),
            "end": round(int(end[-1]) / 1e6, 1) if end else None, "took": round(time.monotonic() - t, 1)}


def packet_read(path, index):
    """One demux of the whole file, no decode. Every time counts from the file start, as ffmpeg's -ss does: audio and
    video, where each stream ends, held, the seconds from the first audio packet to the audio end less every gap over
    decide.HOLE_GAP, hole, the longest gap between two audio packets, video_gap, the longest gap between two video
    packets or the longest video packet duration, as a broken MP4 stts entry gives one frame 20,000 s, subs,
    [end, events, text] of each subtitle track, and the codec. held reads no bit rate and no rounded packet duration.
    Only running sums are kept, so a TrueHD track of 8 million packets needs no memory. A skip, a missing ffprobe, a
    failure or a timeout returns {"error": why}, never a verdict."""
    t, (timeout, skipped) = time.monotonic(), read_time(path, "the packet read")
    if skipped:
        return {"error": skipped}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time:stream=index,codec_type,codec_name,profile:"
                            "stream_disposition=attached_pic", "-of", "json", path], capture_output=True, text=True, errors="replace", timeout=min(120, timeout))
        info = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
        streams = info.get("streams") or []
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as ex:
        return {"error": f"ffprobe did not run: {type(ex).__name__}"}
    au = [x for x in streams if x.get("codec_type") == "audio"]
    vi = [x["index"] for x in streams if x.get("codec_type") == "video" and not (x.get("disposition") or {}).get("attached_pic")]
    if index >= len(au):
        return {"error": "ffprobe lists no such audio stream" if streams else "ffprobe failed"}
    try:
        p = subprocess.Popen(["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    except OSError as ex:
        return {"error": f"ffprobe did not run: {type(ex).__name__}"}
    a, v = au[index]["index"], vi[0] if vi else None
    subs = {x["index"]: [0.0, 0, x.get("codec_name") not in decide.BITMAP_SUBS] for x in streams if x.get("codec_type") == "subtitle"}
    out = dict(codec=au[index].get("codec_name"), profile=au[index].get("profile"), audio=None, hole=None, video=None, video_gap=0.0)
    timer, rc, first, afirst, gaps = threading.Timer(timeout, p.kill), None, None, None, 0.0
    timer.start()
    try:
        for line in p.stdout:
            f = line.split(",", 3)
            try:
                i, pts = int(f[0]), float(f[1])
                dur = float(f[2]) if f[2][:1].isdigit() else 0.0
            except (ValueError, IndexError):
                continue
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
        rc = p.wait()
    finally:   # the job's time limit may end the loop: ffprobe must not run on
        timer.cancel()
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
    if rc != 0:
        return {"error": f"ffprobe exited {rc}" + (f", killed after {timeout:.0f} s" if rc == -signal.SIGKILL else "")}
    try:   # an MPEG-TS file may start at any PTS
        zero = float(info["format"]["start_time"])
    except (KeyError, TypeError, ValueError):
        zero = first or 0.0
    at = lambda x: None if x is None else round(x - zero, 3)
    return dict(out, audio=at(out["audio"]), video=at(out["video"]), hole=out["hole"] and [at(x) for x in out["hole"]],
                held=None if afirst is None else round(out["audio"] - afirst - gaps, 1), video_gap=round(out["video_gap"], 1),
                subs=[[at(e), n, text] for e, n, text in subs.values()], took=round(time.monotonic() - t, 1))


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
    samples.append(dict({k: pk[k] for k in ("audio", "video", "video_gap", "held", "hole", "took")}, kind="packets", ran=True))
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


def header_probe(path, j):
    """The header check of a Matroska file (docs/design.md, "Header repair"), None for another container. j is its mkvmerge -J
    probe. It reads the start of the file and the Cues. It reads the last HEADER_TAIL bytes of the Segment only when
    those point at an issue: no usable Cues, a last video or audio cue far from the header duration, a Segment size past
    the end after the hook's own failed edit, or more than TAIL_MIN bytes past the Segment end. The stream ends come from
    the Clusters, so a file whose index is lost reads too. tail is the bytes past the Segment end when over TAIL_MIN,
    else 0. A remux drops them, but only when the video and the audio reach the header duration, so the Segment is whole.
    not_whole then says why the Segment may be cut, and check_video() makes it a doubt. Bytes past the Segment end that
    start another Matroska file block the remux too.

    Returns duration (the header's seconds), cues (True, or why none is usable), cue_end, short, failed_edit, video and
    audio (where each ends), end (where the last block of any track ends, the duration a remux writes), read (bytes),
    issue and blocked. issue says what is wrong with the header. blocked says why a remux must not run or would not
    help: the video and the audio end apart, a subtitle event runs far past them, or no stream end could be read.
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
    with open(path, "rb") as f:
        size = f.seek(0, 2); f.seek(0); b = f.read(config.HEADER_READ)
        seg, read = decide.segment_start(b), len(b)
        if not seg:
            return None
        ds, seg_size = seg
        short = None if seg_size is None else ds + seg_size - size
        seek, cues, cue_end = decide.front_seeks(b, ds), "no SeekHead lists the Cues", None
        if decide.CUES not in seek:
            for pos in seek.get(decide.SEEKHEAD, []):   # a second SeekHead, often at the end of the file, lists the Cues
                f.seek(ds + pos); c = f.read(config.HEADER_READ); read += len(c); e = decide.element(c, 0)
                if e and e[0] == decide.SEEKHEAD and e[2] is not None:
                    decide.seek_entries(c, e[1], e[1] + e[2], seek)
                elif ds + pos >= size:
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
                f.seek(ds + pos + e[1]); c = f.read(e[2]); read += len(c); last = decide.last_cues(c)
                if last:   # mkvmerge cues subtitles too, so the video and audio cues say where those end
                    cues, cue_end = True, max((secs(t) for n, t in last.items() if tracks.get(n) in ("video", "audio")), default=None)
                    break
                cues = "the Cues hold no cue point"
        failed = bool(short and short > 0) and hook_edit_failed(path)
        little = failed and short < decide.SHORT_CERTAIN   # the hook's own failed edit cut the file's end
        out = dict(duration=round(dur, 3), cues=cues, cue_end=cue_end, short=short, failed_edit=failed, video=None, audio=None, end=None,
                   read=read, issue=[], blocked=[], tail=-short if short is not None and -short > config.TAIL_MIN else 0)
        if cues is True and cue_end is not None and not off(dur, cue_end) and not little and not out["tail"]:
            return out   # the common case, read in two small parts
        kinds, ends = [k for k in ("video", "audio") if k in tracks.values()], None
        stop = size + min(0, short or 0)   # the Segment end. The bytes past it may hold the Clusters of an older copy.
        for n in config.HEADER_TAIL:   # the larger read also when a stream has no block in the smaller one
            f.seek(max(0, stop - n)); t = f.read(stop - max(0, stop - n)); out["read"] += len(t)
            ends = decide.stream_ends(t, durations)
            if (ends is not None and {tracks.get(i) for i in ends} >= set(kinds)) or n >= stop:
                break
        if out["tail"]:   # two files joined with cat: ffmpeg plays the second one too, and a remux would drop it
            f.seek(size - out["tail"])
            if f.read(4) == decide.EBML_ID.to_bytes(4, "big"):
                out["blocked"].append("the bytes past the Segment end start another Matroska file")
    if little:
        out["issue"].append(f"the Segment size promises {short} bytes past the end of the file after the hook's own edit of it failed")
    if out["tail"]:   # a shorter copy written over an older one leaves the old bytes behind
        out["issue"].append(f"the file holds {out['tail']} bytes past the end of its Matroska Segment")
    if cues is not True:
        out["issue"].append(f"no usable Cues index: {cues}")
    got = {k: [e for i, e in (ends or {}).items() if tracks.get(i) == k] for k in ("video", "audio")}
    if ends is None:
        out["blocked"].append("no stream end could be read from the last clusters")
        return out
    missing = [k for k in kinds if not got[k]]
    if missing or not kinds:
        out["blocked"].append(f"the last {len(t) >> 20} MiB hold no {missing[0]} block" if missing else "the file has no video or audio track")
        return out
    out.update(video=secs(max(got["video"])) if got["video"] else None, audio=secs(max(got["audio"])) if got["audio"] else None,
               end=secs(max(ends.values())))
    out["streams"] = streams = max(x for x in (out["video"], out["audio"]) if x)
    if out["video"] and out["audio"] and abs(out["video"] - out["audio"]) > decide.AV_APART:
        out["blocked"].append(f"the video ends at {content.hms(out['video'])} and the audio at {content.hms(out['audio'])}")
    if out["tail"] and any(off(dur, x) for x in (out["video"], out["audio"]) if x):   # the Segment itself may be cut. check_video() alerts.
        out["not_whole"] = (f"the file has {out['tail']} bytes of extra data at its end, and the video and audio stop before its stated "
                            f"length of {content.hms(dur)}, so the file may be cut off")
        out["blocked"].append(out["not_whole"])
    subs = [t for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    if subs and (off(dur, out["end"]) or off(out["end"], streams)):   # a subtitle event that starts earlier may set the duration too
        left = config.DEADLINE.left()
        lines = {}
        per = subtitles.subtitle_ends(path, j, (left - config.SUB_RESERVE) if left else config.SUB_MAX, streams, lines) if config.CFG.header_repair and size <= config.CFG.repack_max else None
        if per is None:
            out["blocked"].append("the subtitle events were not read: no time, no remux can follow, or ffprobe failed")
        else:
            out["subtitles"], out["sublines"], out["end"] = per, lines, max([out["end"]] + list(per.values()))
    out["expect"] = out["end"]   # the duration a remux writes
    if off(dur, out["end"]):
        out["issue"].insert(0, f"the header says {content.hms(dur)}, but the streams end at {content.hms(out['end'])}")
    if off(out["end"], streams):   # an event can run an hour past the film's end. mkvmerge keeps it, --split too.
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
    return out


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
    """(offsets as a share of the size where a 64 KiB read meets a zero run of ZERO_RUN or more, bytes read, stopped). An
    incomplete download leaves regions of zero bytes. The read holds an all-zero 4 KiB page, and zero_run() measures
    the run around it. An encoder that pads easy frames with zeros leaves only short runs, far under ZERO_RUN. again
    moves every read by half a step, for the second check before a re-grab. Hits closer than one read count once,
    because the reads of a file under about 18 MB overlap. stop() is asked before each read and ends the probe early.
    The reads spread over zero_span() of the file, and each offset is a share of the whole file."""
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
                    hits.append(round(page / size, 3)); last = page
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


def window(path, start, secs):
    """Decode secs seconds of the main video stream from start, parsed by decide.parse_window(). Read-only, so it
    may be killed. A seek with no usable index reads the file from the start, and a seek into zeros reads to the next
    intact cluster. So the window stops at VIDEO_MAX_READ bytes or VIDEO_TIMEOUT seconds. 0:V:0 skips cover art.
    Skipping the loop filter saves time and parses the same bitstream. A pidfd says when ffmpeg exits, before it is
    reaped. Its bytes read are the change of our own count across the reap, under REAP, so the other scan workers do
    not add to it."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-threads", "2", "-skip_loop_filter", "all",
           "-ss", f"{start:.2f}", "-t", f"{secs:.2f}", "-i", path, "-map", "0:V:0", "-an", "-sn", "-dn", "-vf", "showinfo=checksum=0",
           "-f", "null", "-"]
    t0, stopped = time.monotonic(), None
    with tempfile.TemporaryFile("w+", errors="replace", dir=config.CFG.state_dir) as err:   # a file, so a long stderr never blocks ffmpeg
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        try:
            exited = os.pidfd_open(p.pid)
            try:
                while not select.select([exited], [], [], config.WINDOW_POLL)[0]:
                    stopped = (f"ran over {decide.VIDEO_TIMEOUT} s" if time.monotonic() - t0 > decide.VIDEO_TIMEOUT else
                               f"read over {decide.VIDEO_MAX_READ >> 20} MiB" if rchar(p.pid) > decide.VIDEO_MAX_READ else None)
                    if stopped:
                        p.kill()
                        break
            finally:
                os.close(exited)
            # ponytail: the samples and probes of other workers reap without REAP. One reaped in these microseconds adds its reads.
            with REAP:
                b0 = rchar(); p.wait(); read = rchar() - b0
        finally:
            if p.poll() is None:   # the job's time limit or an interrupt: no decoder outlives the check
                p.kill(); p.wait()
        err.seek(0)
        return dict(decide.parse_window(err.read(), 1 if stopped else p.returncode), at=round(start), stopped=stopped,
                    took=round(time.monotonic() - t0, 2), read=read)


def check_video(path, dur, again=False, hp=None):
    """(certain reason or None, [doubts], log fields) for one file of dur seconds, the shape of check_audio().

    Three read-only stages, each only when the earlier ones found nothing certain. header: a Matroska file shorter
    than its Segment size lost bytes, 64 bytes read. Another container skips it. A short file whose last edit by the
    hook failed is only a doubt, see hook_edit_failed(). zeros: zero_probe(). windows: three windows at the audio
    sample positions, for a file of 60 seconds or more. Each stage logs its result, seconds and bytes read. fault is
    the class of a certain verdict (truncated, zero-filled or bad windows), which the second check before a re-grab must
    find again. again is that second check: the zero reads move by half a step and the windows go to VIDEO_AGAIN, so it
    samples other parts of the file. Under a time limit (the hook) a stage starts only when the limit holds its worst
    case plus VIDEO_RESERVE, and the zero probe stops once the reserve is reached. A skipped or stopped stage is logged
    and is never a doubt. video_check() turns any exception into no verdict.

    hp is header_probe() of the file. Its fields join the header stage. With it, a Segment size under SHORT_CERTAIN past
    the end after the hook's own failed edit is a header issue, and no doubt. The header stage then says whether a remux
    may repair the header: repairable is True when the header has an issue and every stage ran and found no sign of
    real damage. A file with no usable Cues reads from the start for each window, so a container error anywhere before
    it counts. Only a window the read cap stopped with no error on the way is no sign of damage in such a file.
    Such a window is never a doubt, but in a file with usable Cues it still blocks the repair. windows_clean says
    all three windows decoded clean, which a repair of a track with no default duration needs.
    """
    certain, doubts, fields = video_stages(path, dur, again, hp)
    h = fields["header"]
    if hp and hp["issue"]:
        z, w = fields.get("zeros") or {}, fields.get("windows") or {}
        wins = w.get("list") or []
        against = hp["blocked"] + ([certain] if certain else []) + [d for d in doubts if d not in hp["blocked"]]
        against += [decide.stopped_doubt(x) for x in wins if hp["cues"] is True and decide.read_capped(x)]   # no doubt, still no repair
        if not z or "skipped" in z:
            against.append("the zero probe did not run to its end")
        if w.get("skipped") or len(wins) < 3:   # a skip is never clean
            against.append(f"the video windows did not all run: {w.get('skipped') or 'none ran'}")
        clean = len(wins) == 3 and all(x["ran"] and not decide.bad_window(x) and not x["empty"] and not x.get("stopped") for x in wins)
        h.update(blocked=against, repairable=not against, windows_clean=clean)
    return certain, doubts, fields


def video_stages(path, dur, again, hp):
    """The three stages of check_video()."""
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
    t = time.monotonic(); hits, read, stopped = zero_probe(path, again, lambda: left() < config.VIDEO_RESERVE)
    fields["zeros"] = {"hits": hits, "took": took(t), "read": read}
    if stopped:   # a partial probe gives no verdict
        fields["zeros"]["skipped"] = f"stopped with {left():.0f} s left of the job's time limit"
        return None, doubts, fields
    windows, skipped, t = [], None, time.monotonic()
    if len(hits) < 2:
        for share in decide.VIDEO_AGAIN if again else decide.VIDEO_AT:
            skipped = "the duration is under 60 s" if dur < 60 else \
                f"{left():.0f} s left of the job's time limit" if left() < decide.VIDEO_TIMEOUT + config.VIDEO_RESERVE else None
            if skipped:
                break
            windows.append(window(path, dur * share, min(decide.VIDEO_SECS, dur * (1 - share))))
        fields["windows"] = dict(list=windows, took=took(t), read=sum(w["read"] for w in windows), **({"skipped": skipped} if skipped else {}))
    if sum(bool(w.get("nodecoder")) for w in windows) >= 2 and not any(w.get("encrypted") for w in windows) and encrypted_video(path):
        for w in windows:   # ffmpeg named no encryption, but the bytes show it
            w["encrypted"] = bool(w.get("nodecoder"))
    certain, more = decide.video_verdict(hits, windows)
    return certain, more + doubts, dict(fields, fault=certain and ("zero-filled" if len(hits) >= 2 else "bad windows"))


def video_seconds(j, hp=None):
    """Where the main video stream ends, for the video windows, or None. The real end from header_probe() when it read
    the last clusters, else the video track's DURATION tag when mkvmerge wrote it for this file. ffprobe's format
    duration is the Segment duration, which a long subtitle event can push far past the video end."""
    if hp and hp.get("video"):
        return hp["video"]
    app = (((j or {}).get("container") or {}).get("properties") or {}).get("writing_application") or ""
    p = next(((t.get("properties") or {}) for t in (j or {}).get("tracks") or [] if t.get("type") == "video"), {})
    if app.startswith("mkvmerge") and p.get("tag__statistics_writing_app", app) == app and p.get("tag_duration"):   # ffmpeg copies stale tags
        return decide.tag_seconds(p["tag_duration"])
    return None


def video_summary(certain, doubts, fields):
    """The corrupt-video verdict for the decision log: the verdict, the fault class, and each stage's result."""
    return dict(certain=certain, doubts=doubts, **fields)


def video_inputs(path, j=None, hp=None):
    """(the duration for the windows, header_probe() or None) of one file for check_video(). A .mkv file is probed with
    mkvmerge when j is not given. A probe that fails reads as none, so the zero probe and the windows still run. The
    duration is video_seconds(), else the video stream's own for a file that is not Matroska, else the format duration."""
    if path.lower().endswith(".mkv") and j is None:
        try:
            j = mkvmerge(path)
        except content.OutOfTime:
            raise
        except Exception:
            j = {}
    hp = hp or (header_of(path, j) if j else None)
    matroska = ((j or {}).get("container") or {}).get("type") == "Matroska"   # ffprobe reads no stream duration from Matroska
    return video_seconds(j, hp) or (None if matroska else video_stream_seconds(path)) or ffprobe_duration(path) or 0, hp


def video_stream_seconds(path):
    """The duration ffprobe reads for the main video stream, or None. Matroska gives none. The format duration can come
    from a stray packet of another stream."""
    r = run_bounded(["ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries", "stream=duration", "-of", "csv=p=0", path],
                    120, capture_output=True, text=True, errors="replace")
    try:
        return float(r.stdout.split()[0]) or None
    except (ValueError, IndexError):
        return None


def video_check(path, again=False, j=None, hp=None):
    """check_video() with the duration of video_seconds(), else ffprobe's. j is the file's mkvmerge -J probe and hp its
    header_probe(), each read here for a .mkv file when not given. Any exception, the job's time limit included, is no
    verdict. Its log fields then carry error and code video_check_error. So the check never costs the flag edit, and a
    second check that fails keeps the file. The time limit outside the check still stops the job."""
    try:
        dur, hp = video_inputs(path, j, hp)
        return check_video(path, dur, again, hp)
    except Exception as ex:
        return None, [], {"fault": None, "error": config.mask(f"{type(ex).__name__}: {ex}")[:200], "code": "video_check_error"}


def lid_ready():
    """The install writes LID_DIR/ready last, after the venv and the model. A half-built install has no marker."""
    return os.path.exists(os.path.join(os.path.dirname(__file__), "lid.py")) and os.path.exists(os.path.join(config.CFG.lid_dir, "ready"))


LID_ONE = threading.Lock()   # the workers of a backfill ask for the model one at a time, see lid_turn()


def lid_turn(deadline):
    """The host's one turn at the language model, the lock lid.turn in the state directory, as an open file to close after
    the hearing. None when time.monotonic() passes deadline first. A taker holds lid.turn.gate while it waits, and a
    backfill sends one worker at a time (LID_ONE). So a hook job waits for the one hearing in progress, never behind a
    queue of backfill files. lid.py still takes its own model lock, which is free by then."""
    base = os.path.join(config.CFG.state_dir, "lid.turn")
    gate, turn = open(base + ".gate", "w"), open(base, "w")
    try:
        for f in (gate, turn):
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        turn.close()
                        return None
                    select.select([], [], [], 0.2)
        return turn
    finally:
        gate.close()


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
    A run that must hear first waits for the host's turn at the model, see lid_turn(). timeout starts after that wait,
    and a job's time limit caps it then. A wait of a second or more comes back as "waited"."""
    if not lid_ready():
        return {"why": "language detection is not installed"}
    start, cached = time.monotonic(), not fresh and lid_cached(path, index, j, words)
    with contextlib.nullcontext() if cached else LID_ONE:
        left = config.DEADLINE.left()   # a hook job waits only while its time limit leaves room to hear
        turn = None if cached else lid_turn(start + (left - config.LID_RESERVE - 10 if left else float("inf")))
        waited = time.monotonic() - start
        if not cached and turn is None:
            return {"why": f"another hearing held the model for {waited:.0f} seconds", "waited": waited}
        try:
            left = config.DEADLINE.left()
            got = lid_cli(path, index, j, expect, min(timeout, left - config.LID_RESERVE) if left else timeout, fresh, keep, words, then, yield_to)
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


def lid_cli(path, index, j, expect, timeout, fresh, keep=False, words=None, then=None, yield_to=None, speech=False):
    """The lid.py process of lid_run() or lid_speech(), killed with its process group after timeout seconds."""
    argv = [os.path.join(config.CFG.lid_dir, "venv", "bin", "python"), os.path.join(os.path.dirname(__file__), "lid.py"), path,
            str(index), str(decide.duration(j)), "--cache", os.path.join(config.CFG.state_dir, "lid.sqlite"),
            "--model-dir", os.path.join(config.CFG.lid_dir, "models")]
    if words:
        argv += ["--words", words[0], *map(str, words[1]), "--secs", str(words[2] if len(words) > 2 else subsync.WINDOW)]
        argv += ["--more", *("-" if x is None else str(x) for x in words[3])] if len(words) > 3 and words[3] else []
        argv += ["--group", str(words[4])] if len(words) > 4 and words[4] else []   # the whole-file hearing, see subtitles.sweep_hear()
    elif speech:
        argv.append("--speech")
    else:
        argv += (["--fresh"] if fresh else []) + (["--keep-pcm"] if keep else []) + ["--expect", *expect] + (["--then-words", then] if then else [])
    argv += [a for opt, v in zip(("--yield-gate", "--yield-queue"), yield_to or ()) if v for a in (opt, v)]
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
    """mkvmerge's language table for decide.retag(), read once per process. Empty when mkvmerge cannot list it:
    then no two tags of a track are compared, and a language still changes on two agreeing signals."""
    if not LANGS:
        try:
            LANGS.append(decide.language_table(run_bounded(["mkvmerge", "--list-languages"], 60, capture_output=True, text=True,
                                                               errors="replace").stdout))
        except Exception:   # the table only lets retag() compare two tags, so a failure never stops a job
            LANGS.append(({}, {}))
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
    """(type, codec, language) of each track of an mkvmerge -J probe. A missing language reads as und."""
    return [(t.get("type"), t.get("codec"), (t.get("properties") or {}).get("language") or "und") for t in j.get("tracks") or []]


def ffprobe_duration(path):
    """The format duration ffprobe reads, in seconds, or None. A missing ffprobe or a hung read is None too, so no caller
    loses its stage to it (CI has no ffprobe)."""
    try:
        r = run_bounded(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                        120, capture_output=True, text=True, errors="replace")
        return float(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
