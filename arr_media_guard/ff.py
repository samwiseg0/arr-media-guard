# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Every ffmpeg, ffprobe and PyAV call of AMG (docs/design.md, "Time reads"). A check or a write takes each time from
the file as it stores it, see matroska.frames(), mp4.samples() and ts.frames(). ffmpeg estimates the time of a frame
its parser splits off, and its muxers move a time that goes back. So each call names its use, and READS names the
times that use may read, with the reason.

ff.py allows only what it knows. check() parses the argv of each call. Each option, log level, ffprobe section and
field, output format, filter, bitstream filter, muxer and protocol must be on an allow list here, and each one that
gives a time must be named by the use. Anything else raises TimeRead. The output then keeps only what the use may
read. ffprobe JSON keeps the known keys of each section, and loads() gives mappings that raise on any other key. The
text channels keep their named time fields only. stderr at level info keeps the lines of the named channels and the
lines below info. pyav() gives PyAV objects that allow their known attributes and the times the use names. An audit
hook refuses a start of ffmpeg or ffprobe, or an import of PyAV, from any other module of the package, and a start of
a program that ff.py does not allow. This module needs only the standard library, so the scripts of the language-ID
venv import it too."""
import collections, contextlib, enum, fractions, json, os, re, signal, subprocess, sys, tempfile, time, types

# A use: names, the times it may read: "section.field" of ffprobe (packet.pts_time), a channel (a muxer, a filter, a
# bitstream filter or an option that gives a time, such as framemd5, showinfo, setts, progress or seek),
# "channel.field" of its output (showinfo.pts_time), or "kind.attribute" of PyAV (frame.pts). why says why that read
# may stay. readers are the functions and classes, as tests/test_time_reads.py names them, that hold a time word for
# this use and start no process: a parser of its output, or a reader of a time it passes on.
Use = collections.namedtuple("Use", "names why readers", defaults=((),))
# ffmpeg's -ss and -t cut by ffmpeg's own frame times, and a frame its parser splits off holds an estimated time
# (plan C6). A use that seeks names "seek" with this reason.
SEEK = "ffmpeg's frame times place the -ss start and the -t end, which bound the read and judge no frame time"
READS = {
    # ffprobe
    "checks.ffprobe_audio": Use((), "the indexes of the audio streams"),
    "checks.packet_read": Use(("format.start_time",), "ffprobe's format start_time, the origin of the -ss of each sample and of the verdict "
                              "times. The frame times come from the readers (plan A1)"),
    "checks.ffprobe_packets": Use(("packet.pts_time", "packet.duration_time"), "the stops-early, hole and held checks of an AVI, which times "
                                  "each frame by its index, and of a format with no reader"),
    "checks.stream_probe": Use((), "the format, the streams and their sample rates"),
    "checks.main_stream": Use(("stream.duration",), "the film's stream duration, which places a window or gates a doubt (plan A3)"),
    "checks.ffprobe_duration": Use(("format.duration",), "the format duration, which places a sample or a window, or gates a doubt (plan A3)"),
    "content.last_packet.film": Use((), "the index of the film's stream"),
    "content.last_packet": Use(("packet.pts_time", "format.start_time", "seek"), "the stored time of the film's last packet, the runtime "
                               f"check in minutes. -read_intervals seeks to the end by time. {SEEK}"),
    "convert.avi_sync": Use(("packet.pts_time", "packet.dts_time"), "an AVI times each frame by its index, so ffmpeg's first time is the "
                            "container's own definition"),
    "proof.ff_streams": Use(("stream.duration", "format.duration"), "the stream durations of a container with no reader, which "
                            "content.trusted_duration() needs two of (plan A4), and the format duration, which places a window or gates a "
                            "doubt (plan A3)"),
    "proof.packet_places": Use(("packet.pts_time", "packet.dts_time", "packet.duration_time"), "ffprobe's byte of each MP4 packet pairs it "
                               "with its sample. Its times count only for a source with no reader"),
    "proof.extradata": Use((), "the codec header"),
    "proof.hevc_packets": Use(("packet.pts", "packet.dts"), "the RPU check holds the pts, the dts and the keyframe flag of each HEVC packet "
                              "equal in two reads, and measures no time"),
    "lid.start_delay": Use(("stream.start_time", "format.start_time"), "the start of the audio stream against the file's, as the container "
                           "stores them, puts the decoded speech on the file's clock"),
    "subtitles.onsets.layout": Use((), "the channel layout of the audio stream"),
    # ffmpeg
    "checks.sample": Use(("volumedetect", "volumedetect.n_samples", "dump", "seek"), "the peak and the count of decoded samples of one "
                         "sample. The count says how much audio decodes, never where a frame sits. The output stream line of the dump "
                         f"gives the decoded rate and channels. {SEEK}", ("decide.parse_sample",)),
    "checks.full_decode": Use(("volumedetect", "volumedetect.n_samples", "progress", "progress.out_time_us"), "the decoded length of a "
                              "whole decode, which counts the samples the decoder gives, and the end of the track from the progress (plan A2)"),
    "checks.whole_check": Use((), "the read pass: its error lines"),
    "checks.window": Use(("showinfo", "showinfo.pts_time", "dump", "seek"), "showinfo's decoded frames of a picture window, which judge the "
                         "decode, never the sync. The PyAV window writes the same lines. The stream lines of the dump name an encrypted "
                         f"track. {SEEK}", ("decide.<module>",)),
    "convert.convert_captions": Use(("srt",), "the CEA-608 cues as the 608 decoder times them from the frames the file presents (-copyts)"),
    "convert.convert": Use(("matroska",), "the ffmpeg copy of a container mkvmerge cannot read (ASF), whose every packet prove() checks",
                           ("convert.convert_cmd",)),
    "convert.convert_subs": Use(("srt",), "the cues of a text subtitle, for the word check of a conversion"),
    "proof.packet_hashes": Use(("framemd5", "framemd5.dts", "framemd5.pts", "framemd5.duration", "srt"), "the packet data and counts of "
                               "every proof. Its times count only for a source with no reader (AVI, ASF), see prove()", ("proof.bad_lines",)),
    "proof.packet_data": Use((), "the bytes of the first packets of a stream"),
    "proof.hevc_packets.data": Use((), "the bytes of each packet of an HEVC stream"),
    "lid.extract": Use(("seek",), f"raw PCM. The word times count its samples from the -ss start. {SEEK}"),
    "lid.speech": Use((), "raw PCM of the whole stream. The speech spans count its samples, then move by start_delay()"),
    "subtitles.onsets": Use(("silencedetect", "silencedetect.silence_end", "silencedetect.silence_duration", "seek"), "the speech onsets of "
                            f"a window, from its decoded samples after the -ss start, for the word check. {SEEK}"),
    "subtitles.ffmpeg_layouts": Use((), "the channels of each layout name"),
    "remux.cover_images": Use(("framemd5",), "the md5 of the first packet of a cover picture, no time"),
    "remux.moved_tracks": Use(("matroska", "setts"), "each moved subtitle track, timed by setts, whose cues prove() checks. setts moves the "
                              "PTS, DTS and DURATION of each subtitle block as the file stores them"),
    "remux.resub": Use(("framemd5",), "the md5s of the packets of a garbled track and a cover picture, no time"),
    # PyAV
    "avwindow.main": Use(("container.start_time", "container.seek", "frame.pts", "packet.dts", "showinfo", "showinfo.pts_time"), "the start "
                         "and the end of a picture window's read, as ffmpeg's -ss and -t place it, and the decoded frames, which it writes "
                         "as showinfo lines for decide.parse_window() (plan A5). The seek goes by the container's index"),
    "burnin.Sampler": Use(("container.start_time", "container.seek", "packet.pts", "packet.dts", "frame.time", "index_entry.timestamp"), "the "
                          "time of a keyframe or a decoded frame that a burn-in sample shows, from the container's start, as ffmpeg's -ss "
                          "counts. It places the sample and judges no sync. The index tells a file with no cues"),
    "burnin.Ocr.read": Use((), "PyAV's swscale resizes a crop of a frame, no file"),
}

# --- the allow lists --------------------------------------------------------------------------------------------------

TOOLS = ("ffmpeg", "ffprobe")
PREFIX = ("ionice", "nice")   # the launchers that may come before ffmpeg or ffprobe, with their options
# The other programs the package starts, see audit(). None of them starts a program that its argv names. A Python
# child must run a script of the package.
OTHERS = ("mkvmerge", "mkvextract", "mkvpropedit", "logrotate")
PYTHON = re.compile(r"python[\d.]*")
LEVELS = ("quiet", "panic", "fatal", "error", "warning", "info")   # the -v levels. info needs "level+", see lines().
# The options of ffmpeg, as (the count of values, the channel a use names to pass it or None). A per-stream form, as
# -c:s or -bsf:0, counts by its name before ":". The graph options take a filter graph.
FFMPEG = {**dict.fromkeys(("-nostdin", "-hide_banner", "-nostats", "-y", "-copyts", "-copyinkf", "-vn", "-an", "-sn", "-dn", "-ignore_unknown",
                           "-layouts"), (0, None)),
          **dict.fromkeys(("-v", "-loglevel", "-i", "-f", "-map", "-c", "-codec", "-af", "-vf", "-filter", "-filter_complex", "-lavfi", "-bsf",
                           "-frames", "-ac", "-ar", "-threads", "-skip_loop_filter", "-ignore_editlist"), (1, None)),
          **dict.fromkeys(("-ss", "-t", "-to", "-sseof"), (1, "seek")), "-progress": (1, "progress"), "-debug_ts": (0, "debug_ts"),
          "-stats": (0, "stats"), "-report": (0, "report"), "-fdebug": (1, "fdebug"),
          **dict.fromkeys(("-stats_enc_pre", "-stats_enc_post", "-stats_mux_pre", "-stats_enc_pre_fmt", "-stats_enc_post_fmt",
                           "-stats_mux_pre_fmt"), (1, "stats_enc"))}
GRAPHS = ("-af", "-vf", "-filter", "-filter_complex", "-lavfi")
FFPROBE = {"-hide_banner": 0, "-show_data": 0, "-v": 1, "-loglevel": 1, "-i": 1, "-select_streams": 1, "-show_entries": 1, "-of": 1,
           "-print_format": 1, "-output_format": 1, "-read_intervals": 1}
SHOW = {"-show_streams": "stream", "-show_format": "format", "-show_packets": "packet", "-show_frames": "frame", "-show_chapters": "chapter",
        "-show_programs": "program"}   # each prints its whole section
WRITERS = ("json", "csv")   # ffprobe's output formats
# The muxers, each with the channel a use names to pass it, or None for one that gives no time. An input takes no
# format but lavfi, whose -i is a filter graph.
MUXERS = {"null": None, "data": None, "s16le": None, "framemd5": "framemd5", "framecrc": "framecrc", "framehash": "framehash", "srt": "srt",
          "matroska": "matroska"}
# The filters and bitstream filters, each as (the channel a use names to pass it or None, a pattern of the arguments
# it may take). An expression filter evaluates its argument for each frame, and print() in it logs a time. So an
# argument passes only as AMG writes it. setts takes the move of remux.moved_tracks(), with its offset and rate.
NUM = r"-?(?:\d+(?:\.\d*)?(?:e[-+]?\d+)?|inf|nan)"   # a float as repr() writes it
MOVED = lambda ts: rf"\({ts}-\({NUM}\)/TB\)\*{NUM}\*TB\*1000"
FILTERS = {"aformat": (None, r"sample_rates=48000:channel_layouts=mono|channel_layouts=mono"), "asetpts": (None, r"PTS-STARTPTS"),
           "pan": (None, r"mono\|c0=FC"), "showinfo": ("showinfo", r"(?:checksum=0)?"), "ashowinfo": ("ashowinfo", ""),
           "volumedetect": ("volumedetect", ""), "silencedetect": ("silencedetect", r"noise=-25dB:d=0\.3")}
BSFS = {"h264_mp4toannexb": (None, ""), "hevc_mp4toannexb": (None, ""), "aac_adtstoasc": (None, ""), "pcm_rechunk": (None, r"n=4096"),
        "filter_units": (None, r"remove_types=(?:9\|12|32-35)"),
        "setts": ("setts", rf"time_base=1/1000:pts=max\({MOVED('PTS')}\\,0\):dts=max\({MOVED('DTS')}\\,0\):duration=if\(lt\({MOVED('PTS')}\\,0\)"
                           rf"\\,max\(1\\,{MOVED('PTS')}\+DURATION\*{NUM}\*TB\*1000\)\\,DURATION\*{NUM}\*TB\*1000\)")}
PROTOCOL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")   # ffmpeg reads "name:" at the start of a path as a protocol
STDOUT = ("-", "pipe:", "pipe:1")
# The untimed fields of each ffprobe section that ff.py knows. A time field, as pts_time, start_time or duration, and
# every other field pass only when the use names them. codec, properties and type are mkvmerge's keys, which
# decide.main_video() asks of ffprobe's streams too.
KEYS = {"stream": frozenset(("index", "id", "codec_name", "codec_long_name", "codec_type", "codec_tag", "codec_tag_string", "profile", "level",
                             "sample_fmt", "sample_rate", "channels", "channel_layout", "bits_per_sample", "bits_per_raw_sample", "initial_padding",
                             "width", "height", "coded_width", "coded_height", "closed_captions", "film_grain", "has_b_frames", "pix_fmt",
                             "sample_aspect_ratio", "display_aspect_ratio", "color_range", "color_space", "color_transfer", "color_primaries",
                             "chroma_location", "field_order", "refs", "is_avc", "nal_length_size", "r_frame_rate", "avg_frame_rate", "time_base",
                             "extradata", "extradata_size", "extradata_hash", "disposition", "tags", "ts_id", "ts_packetsize", "divx_packed",
                             "quarter_sample", "view_ids_available", "view_pos_available", "codec", "properties", "type")),
        "format": frozenset(("filename", "nb_streams", "nb_programs", "nb_stream_groups", "format_name", "format_long_name", "size", "probe_score")),
        "packet": frozenset(("codec_type", "stream_index", "size", "pos", "flags", "data", "data_hash")),
        "stream_disposition": frozenset(("default", "dub", "original", "comment", "lyrics", "karaoke", "forced", "hearing_impaired", "visual_impaired",
                                         "clean_effects", "attached_pic", "timed_thumbnails", "non_diegetic", "captions", "descriptions",
                                         "metadata", "dependent", "still_image", "multilayer")),
        "stream_tags": frozenset(("language", "title", "filename", "mimetype", "handler_name"))}
NESTED = {("stream", "disposition"): "stream_disposition", ("stream", "tags"): "stream_tags"}   # the sections of a JSON object in a section
PLURAL = {"streams": "stream", "format": "format", "packets": "packet", "frames": "frame", "chapters": "chapter", "programs": "program"}
# The text channels of ffmpeg: (their time fields, a pattern of their other fields). A filter logs "name: value", the
# progress "name=value", and the framehash muxers have the time columns 1 to 3.
LOG = {"showinfo": (("pts", "pts_time", "duration", "duration_time"),
                    r"n|fmt|cl|sar|s|i|iskey|type|checksum|plane_checksum|mean|stdev|color_range|color_space|color_primaries|color_trc"),
       "ashowinfo": (("pts", "pts_time", "duration", "duration_time", "nb_samples"), r"n|fmt|channels|chlayout|rate|checksum|plane_checksums"),
       "volumedetect": (("n_samples",), r"mean_volume|max_volume|histogram_\d+db"),
       "silencedetect": (("silence_start", "silence_end", "silence_duration"), r"(?!)")}
PROGRESS = (("out_time_us", "out_time_ms", "out_time"), r"progress")
FRAMEHASH = ("dts", "pts", "duration")
CHANNELS = {**{k: v[0] for k, v in LOG.items()}, "progress": PROGRESS[0], **dict.fromkeys(("framemd5", "framecrc", "framehash"), FRAMEHASH)}
# The input dump at level info: a use that names "dump" keeps these lines. Its Duration, Chapter and Metadata lines
# hold times, so they go, but for the Metadata keys of DUMP_KEYS.
DUMP = re.compile(r"(?:Input #|Output #|  Stream #|Stream mapping:)")
DUMP_KEYS = re.compile(r"\s+enc_key_id\s*:")
LINE = re.compile(r"((?:\[[^\]\n]*\] )*?)\[(panic|fatal|error|warning|info|verbose|debug|trace)\] ?")   # the contexts, then the level
FIELD = re.compile(r"\s*(\w+):\s*(\[[^\]]*\]|\S*)")


class TimeRead(RuntimeError):
    """A call opens, or code reads, a time that the use does not name in READS, or something ff.py does not know."""


FAULTS = []   # (time, text) of each refusal in this process, see faults()
# A test loads this module more than once, and the audit hook of the first copy raises its own TimeRead. So each copy
# takes the TimeRead and the FAULTS of the first copy, and every copy's except clause catches each refusal.
TimeRead, FAULTS = sys.__dict__.setdefault("_amg_ff_shared", (TimeRead, FAULTS))


def refuse(why):
    """Raise TimeRead. Each refusal also goes to FAULTS and as one JSON line to the file AMG_FF_FAULTS names, which
    cli.main() sets and each child inherits. Many callers catch every exception, so logs.decision() reads them back
    and errors the decision line, see faults()."""
    FAULTS.append((time.time(), why))
    if path := os.environ.get("AMG_FF_FAULTS"):
        try:
            with open(path, "a") as f:
                f.write(json.dumps({"at": time.time(), "pid": os.getpid(), "ppid": os.getppid(), "what": why}) + "\n")
        except OSError:   # a full or gone state folder: FAULTS still holds it
            pass
    raise TimeRead(why)


TAIL = 1 << 20   # bytes of AMG_FF_FAULTS that logged() reads, from the end. A line holds about 300.


def logged(since):
    """The lines of the file AMG_FF_FAULTS names whose time is since, a time.time(), or later, of every process."""
    got = []
    if path := os.environ.get("AMG_FF_FAULTS"):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - TAIL))
                for raw in f.read().splitlines():
                    try:
                        d = json.loads(raw)
                    except ValueError:   # the cut first line, or a test's own line
                        continue
                    if isinstance(d, dict) and isinstance(d.get("at"), (int, float)) and d["at"] >= since:
                        got.append(d)
        except OSError:
            pass
    return got


def faults(since):
    """The refusals since since, a time.time(), of this process and of the processes it started. A child names this
    process as its parent in AMG_FF_FAULTS."""
    return [w for t, w in FAULTS if t >= since] + [d.get("what") for d in logged(since) if d.get("ppid") == os.getpid()]


def names(use):
    """The names READS gives use. An unknown use raises."""
    if use not in READS:
        refuse(f"the use {use!r} has no entry in ff.READS")
    return frozenset(READS[use].names)


def find(argv):
    """(name, place) of the program argv starts: argv[0], or the element after the PREFIX launchers and their options.
    (None, None) when argv holds no other program. The audit hook judges every other program, see audit()."""
    k = 0
    while k < len(argv) and os.path.basename(argv[k]) in PREFIX:
        k += 1
        while k < len(argv) and (argv[k].startswith("-") or argv[k].isdigit()):
            k += 1
    return (os.path.basename(argv[k]), k) if k < len(argv) else (None, None)


def split_graph(graph, sep=",;"):
    """The parts of an ffmpeg filter graph or bitstream filter chain. A separator inside quotes or after a backslash
    splits nothing."""
    parts, cur, quote, esc = [], "", False, False
    for ch in graph:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            cur, esc = cur + ch, True
        elif ch == "'":
            cur, quote = cur + ch, not quote
        elif ch in sep and not quote:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def chain(use, value, known, what, sep=",;"):
    """The channels of the filters of a graph, or of the bitstream filters of a chain, that give a time. An unknown
    one raises, and so does an argument outside the pattern of its entry."""
    got = set()
    for p in split_graph(value, sep):
        m = re.match(r"\s*(?:\[[^\]]*\]\s*)*([A-Za-z0-9_]*)", p)
        name = m.group(1) if m else ""
        if not name and not p.strip():
            continue
        if name not in known:
            refuse(f"{use} passes ffmpeg the {what} {name or p!r}, which ff.py does not know")
        ch, args = known[name]
        rest = p[m.end():]
        if not re.fullmatch(args, rest[1:] if rest.startswith("=") else rest):
            refuse(f"{use} passes ffmpeg the {what} {name} with the arguments {rest!r}, which ff.py does not allow")
        if ch:
            got.add(ch)
    return got


def level(use, v):
    """(level, whether each line names its level) of a -v value. A level ff.py does not know raises."""
    flags, _, lv = v.rpartition("+")
    if flags not in ("", "level") or lv not in LEVELS:
        refuse(f"{use} logs at level {v!r}, which ff.py does not allow")
    return lv, flags == "level"


def target(use, path):
    """"stdout" for an output to the pipe, else "file". A protocol, a device or a process file raises."""
    if path in STDOUT:
        return "stdout"
    if PROTOCOL.match(path) or path.startswith(("/dev/", "/proc/")) and path != "/dev/null":
        refuse(f"{use} writes to {path!r}, which ff.py does not allow")
    return "file"


def opened(use, tool, args):
    """What the args of tool open: {"tool", "got": the channels and time fields, "narrow": the ffprobe sections it
    narrows to some fields, "whole": the sections it reads whole, "json": ffprobe prints JSON, "outs": [(muxer, path)]
    of ffmpeg, "info": stderr carries the info lines of an input}. Something ff.py does not know raises."""
    ok, got, narrow, whole, outs, ins, js, pend = names(use), set(), set(), set(), [], [], False, {}
    lv, tagged, k = "info", False, 0
    while k < len(args):
        a = args[k]
        base = a.split(":")[0] if tool == "ffmpeg" else a
        if a.startswith("-") and a != "-":
            n, ch = FFMPEG.get(base, (None, None)) if tool == "ffmpeg" else (FFPROBE.get(a, 0 if a in SHOW else None), None)
            if n is None:
                refuse(f"{use} passes {tool} the option {a}, which ff.py does not know")
            if n and k + 1 >= len(args):
                refuse(f"{use} passes {tool} the option {a} with no value")
            v = args[k + 1] if n else None
            k += 1 + n
            if ch:
                got.add(ch)
            if base in ("-v", "-loglevel"):
                lv, tagged = level(use, v)
            elif base == "-i":
                ins.append((pend, v))
                pend = {}
            elif base in GRAPHS:
                got |= chain(use, v, FILTERS, "filter")
            elif base == "-bsf":
                got |= chain(use, v, BSFS, "bitstream filter", ",")
            elif a == "-progress" and v != "pipe:1":
                refuse(f"{use} writes ffmpeg's progress to {v!r}. ff.py reads it from the pipe only")
            elif a in SHOW:
                whole.add(SHOW[a])
            elif a == "-show_entries":
                for part in v.split(":"):
                    sec, eq, fields = part.partition("=")
                    sec, mine = sec.strip(), {n.partition(".")[2] for n in ok if n.startswith(sec.strip() + ".")}
                    if sec not in KEYS and not mine:
                        refuse(f"{use} reads the ffprobe section {sec!r}, which ff.py does not know")
                    if not eq:
                        whole.add(sec)
                        continue
                    narrow.add(sec)
                    for f in (f.strip() for f in fields.split(",")):
                        if f in mine:
                            got.add(f"{sec}.{f}")
                        elif f not in KEYS.get(sec, ()):
                            refuse(f"{use} reads the ffprobe field {sec}.{f}, which ff.READS does not name for it")
            elif a in ("-of", "-print_format", "-output_format"):
                if v.partition("=")[0] not in WRITERS:
                    refuse(f"{use} prints ffprobe's output as {v!r}, which ff.py does not know")
                js = v.startswith("json")
            elif a == "-read_intervals" and any(lo.strip() or hi.strip() and not hi.strip().startswith("+#")
                                                 for lo, _, hi in (p.partition("%") for p in v.split(","))):
                got.add("seek")   # a start or an end bounds the read by time. Only an end of +#count counts packets
            else:
                pend[base] = v
        elif tool == "ffprobe":
            ins.append(({}, a))
            k += 1
        else:
            outs.append((pend, a))
            pend = {}
            k += 1
    for opts, path in ins:
        fmt = opts.get("-f")
        if fmt == "lavfi":
            got |= chain(use, path, FILTERS, "filter")
        elif fmt is not None:
            refuse(f"{use} reads an input as {fmt!r}, which ff.py does not know")
        elif PROTOCOL.match(path) or path == "-":
            refuse(f"{use} reads {path!r} through a protocol, which ff.py does not allow")
    muxers = []
    for opts, path in outs:
        m = opts.get("-f")
        if m is None:
            refuse(f"{use} writes {path!r} with no -f, so ffmpeg would guess its muxer")
        if m not in MUXERS:
            refuse(f"{use} writes with the muxer {m!r}, which ff.py does not know")
        if MUXERS[m]:
            got.add(MUXERS[m])
        muxers.append((m, path, target(use, path)))
    if tool == "ffprobe":
        if len(ins) != 1:
            refuse(f"{use} gives ffprobe {len(ins)} inputs")
        if (whole or narrow) and "-of" not in args and "-print_format" not in args and "-output_format" not in args:
            refuse(f"{use} prints ffprobe's output in its default format, which ff.py does not know")
        if not js and (bad := sorted(whole - {"stream_disposition"})):
            refuse(f"{use} reads the whole {', '.join(bad)} section as text. Name its fields with -show_entries, or read JSON")
    elif sum(1 for m, _, t in muxers if t == "stdout" and m != "null") + ("progress" in got) > 1:   # the null muxer writes nothing
        refuse(f"{use} sends two outputs to the pipe")
    info = lv == "info" and bool(ins)
    if info and not tagged:
        refuse(f"{use} reads the info lines of {tool} with no level on each. Pass -loglevel level+info")
    return {"tool": tool, "got": got, "narrow": narrow, "whole": whole, "json": js, "outs": muxers, "info": info}


def check(use, argv, env=None):
    """opened() of argv after the checks of READS for use, or None for a tool that is no ffmpeg or ffprobe. env is the
    environment of the call, os.environ when None."""
    argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in argv]
    tool, k = find(argv)
    if tool not in TOOLS:
        return None
    o = opened(use, tool, argv[k + 1:])
    ok = names(use)
    if "FFREPORT" in (os.environ if env is None else env) and "report" not in ok:
        o["got"].add("report")
    if new := sorted(o["got"] - ok):
        refuse(f"{use} opens {', '.join(new)}, which ff.READS does not name for it")
    if bad := sorted(sec for sec in o["whole"] if not allowed(sec, ok)):
        refuse(f"{use} reads the ffprobe section {', '.join(bad)}, which ff.py does not know")
    for n in sorted(ok):
        sec, _, f = n.partition(".")
        if sec in o["narrow"] and n not in o["got"]:
            refuse(f"ff.READS names {n} for {use}, and the call reads no such field")
        if f and sec in CHANNELS and sec in o["got"] and f not in CHANNELS[sec]:
            refuse(f"ff.READS names {n} for {use}, and {sec} prints no such field")
    return o


def need(use, o):
    """The streams of a call, "stdout" and "stderr", whose text ff.py must filter for use."""
    out = {"stderr"} if o["info"] else set()
    if o["json"] or "progress" in o["got"] or any(t == "stdout" and m in FRAMEHASH_MUXERS and dropped(use, m) for m, _, t in o["outs"]):
        out.add("stdout")
    return out


FRAMEHASH_MUXERS = ("framemd5", "framecrc", "framehash")


def door(argv, kw):
    """Refuse a call that ff.py cannot read: a string run by a shell, and an executable other than argv[0]."""
    if isinstance(argv, (str, bytes)) or kw.get("shell") or kw.get("executable") is not None:
        refuse("ff.run() and ff.popen() take an argv list, with no shell and no executable=")


def as_text(kw, stream):
    """Whether subprocess gives stream back as text to the caller."""
    return (kw.get("capture_output") or kw.get(stream) == subprocess.PIPE) and \
        any(kw.get(k) for k in ("text", "universal_newlines", "encoding", "errors"))


def lines(use, text):
    """text, ffmpeg's stderr at level info, with only the lines the use may read: the lines of a named filter with its
    named time fields and its other fields, the lines of the input dump that DUMP names when the use names "dump",
    and every line below info. A line with no level goes."""
    ok, out = names(use), []
    for line in text.splitlines(True):
        m = LINE.match(line)
        if not m:
            continue
        ctx, lv = (re.findall(r"\[([^\]\n]*?)(?: @ 0x[0-9a-f]+)?\] ", m.group(1)) or [None])[-1], m.group(2)   # the last context logs
        body = line[m.end():]
        if lv in ("panic", "fatal", "error", "warning"):
            out.append(line)
        elif lv != "info":
            continue
        elif ctx is None:
            if "dump" in ok and (DUMP.match(body) or DUMP_KEYS.match(body)):
                out.append(line)
        elif (ch := re.sub(r"^Parsed_|_\d+$", "", ctx)) in LOG and ch in ok and (kept := fields(use, ch, body)):
            out.append(line[:m.end()] + kept)
    return "".join(out)


def fields(use, ch, body):
    """The body of one log line of channel ch with only the fields use may read, or "" when none is left. A line
    that starts with no field goes."""
    ok, (times, other) = names(use), LOG[ch]
    found = list(FIELD.finditer(body))
    if not found or found[0].start():
        return ""
    for m in reversed(found):
        if not (re.fullmatch(other, m.group(1)) or m.group(1) in times and f"{ch}.{m.group(1)}" in ok):
            body = body[:m.start()] + body[m.end():]
    return body if body.strip(" |\t\n") else ""


def progress(use, text):
    """ffmpeg's -progress output with only the keys use may read."""
    ok, times, other = names(use), *PROGRESS
    keep = lambda k: re.fullmatch(other, k) or k in times and f"progress.{k}" in ok
    return "".join(line for line in text.splitlines(True) if keep(line.partition("=")[0]))


def dropped(use, mux):
    """The time columns of a framemd5, framecrc or framehash output that the use does not name, counted from 0."""
    ok = names(use)
    return [k + 1 for k, f in enumerate(FRAMEHASH) if f"{mux}.{f}" not in ok]


def columns(use, mux, text):
    """The lines of a framemd5, framecrc or framehash output with each time column the use does not name left empty."""
    if not (drop := dropped(use, mux)):
        return text
    out = []
    for line in text.splitlines(True):
        if line[:1].isdigit():
            p = line.split(",")
            for k in drop:
                if k < len(p):
                    p[k] = ""
            line = ",".join(p)
        out.append(line)
    return "".join(out)


def allowed(sec, ok):
    """The keys of an ffprobe section that a use with the names ok may read."""
    return KEYS.get(sec, frozenset()) | {n.partition(".")[2] for n in ok if n.startswith(f"{sec}.")}


def strip(use, d):
    """d, ffprobe JSON as Python objects, as a Probe for use, with each section a Probe. A section ff.py does not know
    goes, unless the use names a field of it. packets_and_frames keeps each item by its type."""
    ok = names(use)
    if not isinstance(d, dict):
        return d
    top = {}
    for key, v in d.items():
        if key == "packets_and_frames" and isinstance(v, list):
            top[key] = [Probe(x, x.get("type"), use, ok) for x in v if isinstance(x, dict)]
        elif (sec := PLURAL.get(key)) and allowed(sec, ok):
            top[key] = Probe(v, sec, use, ok) if isinstance(v, dict) else [Probe(x, sec, use, ok) for x in v if isinstance(x, dict)] \
                if isinstance(v, list) else v
    return Probe(top, None, use, ok, {k for k, s in PLURAL.items() if allowed(s, ok)} | {"packets_and_frames"})


class Probe(dict):
    """One section of an ffprobe read with only the keys its use may read. Asking for any other key raises."""
    __slots__ = ("allow", "where")

    def __init__(self, d, sec, use, ok, allow=None):
        allow = allowed(sec, ok) if allow is None else frozenset(allow)
        kept = {}
        for k, v in d.items():
            if k not in allow:
                continue
            if (sec, k) in NESTED and isinstance(v, dict):
                kept[k] = Probe(v, NESTED[sec, k], use, ok)
            elif sec is None or not isinstance(v, (dict, list)):   # the top holds the sections strip() made
                kept[k] = v
        super().__init__(kept)
        self.allow, self.where = allow, f"{use} ({sec or 'ffprobe'})"

    def __reduce__(self):   # a plain dict in a pickle or a copy, as it holds only the keys its use may read
        return dict, (dict(self),)

    def ask(self, k):
        if k not in self.allow:
            refuse(f"{self.where} reads {k}, which ff.READS does not name and ff.py does not know")

    def __getitem__(self, k):
        self.ask(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.ask(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.ask(k)
        return super().__contains__(k)

    def pop(self, k, *default):
        self.ask(k)
        return super().pop(k, *default)

    def setdefault(self, k, default=None):
        self.ask(k)
        return super().setdefault(k, default)


def tidy(use, o, r, cwd=None):
    """The output of a finished run of use with only what it may read. r is a CompletedProcess. cwd is the folder of
    the run, where ffmpeg writes an output with a relative path."""
    if o is None or not isinstance(r, subprocess.CompletedProcess):   # another tool, or a test's fake
        return
    n = need(use, o)
    if "stderr" in n and isinstance(r.stderr, str):
        r.stderr = lines(use, r.stderr)
    if "stdout" in n and isinstance(r.stdout, str):
        if o["tool"] == "ffprobe":
            try:
                r.stdout = json.dumps(strip(use, json.loads(r.stdout))) if r.stdout.strip() else r.stdout
            except ValueError:   # no JSON: the caller's parse raises as before, and no text of it passes
                r.stdout = "{"
        elif "progress" in o["got"]:
            r.stdout = progress(use, r.stdout)
        else:
            r.stdout = next(columns(use, m, r.stdout) for m, _, t in o["outs"] if t == "stdout" and m in FRAMEHASH_MUXERS)
    for mux, path, t in o["outs"]:
        path = os.path.join(os.fsdecode(cwd), path) if cwd else path
        if mux in FRAMEHASH_MUXERS and t == "file" and dropped(use, mux) and os.path.isfile(path):
            with open(path) as f:
                kept = columns(use, mux, f.read())
            with open(path, "w") as f:
                f.write(kept)


# --- the stop ---------------------------------------------------------------------------------------------------------

STOP = {"held": False, "asked": False, "term": None, "skipped": False, "cut": 0, "kept": None}
# held: in a job process, a write has started, and in a backfill, cli.stops() runs. asked: on_stop() took a stop while
# held. No write and no further step start after it, see runner.stop_asked(). skipped: a stop refused or cut a write
# or left a step out, so a finding the stop caused posts nothing, see logs.alert_findings(), and a job process puts its
# job back, see runner.job_process(). term: the SIGTERM or SIGINT of a stop that cuts the step in flight, see
# check_stop(). cut: the count of remux.Swap blocks of the main thread. kept: the stop of a layout repair in a process
# with no handler of its own, which only its next write raises, see runner.raise_term(). runner.STOP is this dict.


def on_stop(signum, _):
    """The SIGTERM and SIGINT handler of a job process, a remux.Swap and a backfill. It only records the stop and kills
    the children of the process, so it never leaves a lock held. CPython runs a handler between any two bytecodes of
    the main thread, also inside a with block of the standard library, and a raise there skips its release. While held
    and outside a Swap the stop only asks, and the step in flight ends. Else it records term, kills each child with
    SIGKILL, and the next check_stop() raises."""
    if STOP["held"] and not STOP["cut"]:
        STOP["asked"] = True
        return
    STOP["term"] = signum
    kill_children()


def check_stop():
    """Raise the stop that on_stop() recorded in STOP["term"], at a point where the job holds no lock. A SIGINT raises
    KeyboardInterrupt, as Ctrl+C does, and a SIGTERM SystemExit(143). The record stays, so each later check raises
    too."""
    if STOP["term"] == signal.SIGINT:
        raise KeyboardInterrupt
    if STOP["term"]:
        raise SystemExit(128 + STOP["term"])


@contextlib.contextmanager
def held_off():
    """Hold a recorded stop off while a cleanup that must run after the stop runs, so its checks let it go on. The stop
    comes back after the block, also when a new stop lands inside it, and the next check raises it."""
    term, STOP["term"] = STOP["term"], None
    try:
        yield
    finally:
        STOP["term"] = STOP["term"] or term


def kill(pid):
    """SIGKILL the child pid, and its whole process group when it leads one, as a child of its own session does."""
    for f in (os.killpg, os.kill):
        try:
            return f(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def kill_children():
    """SIGKILL every child of this process, of each of its threads. It reads them from /proc with os calls only, so a
    signal handler may call it."""
    for t in os.listdir("/proc/self/task"):
        try:
            fd = os.open(f"/proc/self/task/{t}/children", os.O_RDONLY)
        except OSError:   # a thread that ended meanwhile
            continue
        try:
            pids = os.read(fd, 1 << 16).split()
        finally:
            os.close(fd)
        for pid in pids:
            kill(int(pid))


def run(use, argv, **kw):
    """subprocess.run(argv, **kw) for use, see check(). A stream that ff.py must filter comes back as text, else the
    call raises. The output then keeps only what the use may read, and the result holds ff_use for loads(). A stop
    that cut the step raises once the child is reaped, see check_stop()."""
    door(argv, kw)
    o = check(use, argv, kw.get("env"))
    for s in sorted(need(use, o) if o else ()):
        if not as_text(kw, s):
            refuse(f"{use} gets its {s} back as bytes, in a file or in a pipe ff.py does not read, so ff.py cannot filter it")
    r = subprocess.run(argv, **kw)
    check_stop()
    if use is not None:   # another tool, as mkvmerge through checks.run_bounded()
        tidy(use, o, r, kw.get("cwd"))
        try:
            r.ff_use = use
        except AttributeError:   # a test's fake
            pass
    return r


class Log:
    """A temporary file in folder for the stderr of a popen() of use. ff.py owns it, and read() gives its text through
    lines()."""

    def __init__(self, use, folder):
        names(use)
        self.use, self.f = use, tempfile.TemporaryFile("w+", errors="replace", dir=folder)

    def fileno(self):
        return self.f.fileno()

    def read(self):
        self.f.seek(0)
        return lines(self.use, self.f.read())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


def popen(use, argv, **kw):
    """subprocess.Popen(argv, **kw) for use, see check(). A stream that ff.py must filter goes to DEVNULL, or stderr to
    an ff.Log of the use, else the call raises. The caller calls check_stop() once it reaped the child, because a stop
    that cuts the step kills the child, and its output then counts for nothing."""
    door(argv, kw)
    o = check(use, argv, kw.get("env"))
    for s in sorted(need(use, o) if o else ()):
        t = kw.get(s)
        if not (t == subprocess.DEVNULL or s == "stderr" and isinstance(t, Log) and t.use == use):
            refuse(f"{use} needs ff.run() or an ff.Log for its {s}, which ff.py filters")
    if o and any(m in FRAMEHASH_MUXERS and t == "file" and dropped(use, m) for m, _, t in o["outs"]):
        refuse(f"{use} needs ff.run(), which filters its framemd5 file when the call ends")
    p = subprocess.Popen(argv, **kw)
    try:
        p.ff_use = use
    except AttributeError:
        pass
    return p


def loads(r, default=None):
    """json.loads() of the stdout of r, a run() result, as a Probe for its use. default stands in for an empty stdout.
    A result with no use (a test's fake) parses as it is."""
    d = json.loads(r.stdout if default is None else (r.stdout or default))
    use = getattr(r, "ff_use", None)
    return strip(use, d) if use else d


# --- the launch guard -------------------------------------------------------------------------------------------------

HERE = {os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.realpath(__file__))}   # the package folder
HOMES = HERE | {os.path.dirname(d) for d in HERE}   # the folders of the scripts a Python child of the package runs: lid.py, the launcher
STDLIB = {os.path.dirname(os.path.abspath(os.__file__)), os.path.dirname(os.path.realpath(os.__file__))}   # the standard library
LAUNCH = frozenset(("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "_posixsubprocess.fork_exec"))
INSIDE, STD = {}, {}   # file name of a code object: whether it is in the package folder, and in the standard library


def inside(filename):
    if filename not in INSIDE:
        INSIDE[filename] = os.path.dirname(os.path.abspath(filename)) in HERE
    return INSIDE[filename]


def caller(f):
    """The nearest frame from f back whose code is outside the standard library, or None. A module of site-packages
    counts as outside."""
    while f:
        name = f.f_code.co_filename
        if name not in STD:
            path = os.path.abspath(name)
            STD[name] = name.startswith("<frozen") or any(path.startswith(d + os.sep) for d in STDLIB) and \
                not re.search(r"[/\\](?:site|dist)-packages[/\\]", path)
        if not STD[name]:
            return f
        f = f.f_back
    return None


def where(f):
    """file:line of frame f for a refusal."""
    return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}" if f else "code of the standard library alone, as a thread or a pool,"


def audit(event, args):
    """sys.addaudithook() calls this for each audit event of the process. Package code may start ffmpeg and ffprobe
    through ff.run() and ff.popen() only, a Python only to run a script of the package, and else only the programs of
    OTHERS. Any other start raises: a launcher such as env, sh or timeout, and executable=. So does a lookup of a C
    function through ctypes, and an import of PyAV by a module of the package other than ff.py. Code outside
    the package, as a test or faster_whisper, passes. A start whose callers are all in the standard library, as a
    thread or a pool that runs subprocess.run(), counts as package code."""
    if event == "import":
        if not (isinstance(args[0], str) and (args[0] == "av" or args[0].startswith("av."))):
            return
        f = sys._getframe(1)
        while f and (f.f_code.co_filename.startswith("<frozen") or os.path.basename(os.path.dirname(f.f_code.co_filename)) == "importlib"):
            f = f.f_back
        if f and inside(f.f_code.co_filename) and os.path.basename(f.f_code.co_filename) != "ff.py":
            refuse(f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno} imports PyAV. PyAV comes through ff.pyav()")
        return
    if event in ("ctypes.dlsym", "ctypes.dlsym/handle"):   # system(), exec*() or fork() of the C library would start a process unseen
        if (f := caller(sys._getframe(1))) and inside(f.f_code.co_filename):
            refuse(f"{where(f)} looks up the C function {args[-1]!r}. The package calls no C function through ctypes")
        return
    if event not in LAUNCH:
        return
    # ponytail: a stop that lands between this check and the fork lets that one child run to its end, and the check
    # after its reap raises. A thread that kills each child a stop finds would close that window.
    check_stop()   # no child starts once a stop cut the step, see on_stop()
    if event == "os.system":
        exe, argv = None, ["sh", "-c", os.fsdecode(args[0])]
    else:
        exe, argv = (args[1], args[2]) if event == "os.spawn" else ((args[0] or [None])[0], args[1]) if event == "_posixsubprocess.fork_exec" else args[:2]
        argv = ["sh", "-c", os.fsdecode(argv)] if isinstance(argv, (str, bytes)) else [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in argv or ()]
    exe = os.fsdecode(exe) if isinstance(exe, (bytes, os.PathLike)) else exe
    name, k = find(argv)
    if exe and os.path.basename(exe) != os.path.basename(argv[0] if argv else ""):   # executable= starts another program than argv[0]
        name, k = os.path.basename(exe), None
    start = sys._getframe(1)
    if name in TOOLS:   # the nearest frame of the package starts it, through any helper
        f = start
        while f and not inside(f.f_code.co_filename):
            f = f.f_back
        if f and os.path.basename(f.f_code.co_filename) == "ff.py" and f.f_code.co_name in ("run", "popen") or not f and caller(start):
            return
        refuse(f"{where(f)} starts {name} outside ff.run() and ff.popen()")
    f = caller(start)   # the code that calls subprocess, so a test's stand-in for serve.spawn() passes
    if f and not inside(f.f_code.co_filename) or name is None or name in OTHERS:
        return
    if PYTHON.fullmatch(name):
        if k is not None and len(argv) > k + 1 and not argv[k + 1].startswith("-") and os.path.isfile(argv[k + 1]) and \
                os.path.dirname(os.path.abspath(argv[k + 1])) in HOMES:
            return
        name = "a Python that runs no script of the package"   # such a child has no guard of its own
    refuse(f"{where(f)} starts {name}, which ff.py does not allow")


if not getattr(sys, "_amg_ff_audit", False):   # one hook a process, though a test loads this module more than once
    sys._amg_ff_audit = True
    sys.addaudithook(audit)


# --- PyAV -------------------------------------------------------------------------------------------------------------

USED = {}   # {use: the names of PyAV it read}, for the test of the PyAV entries
# The attributes of each kind of PyAV object that hold no time. Any other attribute passes only when the use names
# "kind.attribute". So metadata, chapters, samples and every attribute of a kind ff.py does not know raise, as of a
# filter graph, whose add() takes any filter, or a resampler. AMG decodes video frames only. An audio frame has its
# own kind, because to_ndarray() of it gives the count of its samples.
AV = {"container": ("streams", "demux", "decode", "close", "format"), "streams": ("video", "audio", "subtitles"),
      "stream": ("codec_context", "time_base", "disposition", "index_entries", "index", "type"),
      "codec": ("name", "open", "decode", "flush_buffers", "color_trc", "width", "height"), "packet": ("is_keyframe", "stream_index", "size"),
      "frame": ("is_corrupt", "key_frame", "to_ndarray", "reformat", "width", "height"), "audioframe": (), "format": ("name",), "index": (),
      "index_entry": ()}


def pyav(use):
    """The av module of PyAV for use. Each PyAV object it gives allows the attributes AV knows and the times the use
    names, and raises TimeRead on any other attribute."""
    import av
    names(use)
    return Module(av, use, {})


KINDS = {}   # {id of the av module: kinds()}


def kinds():
    """[(PyAV class, kind, whether one proxy serves each object)]. A test's fake av module has none."""
    key = id(sys.modules.get("av"))
    if key in KINDS:
        return KINDS[key]
    out = KINDS[key] = []
    for mod, cls, kind, one in (("av.container", "Container", "container", True), ("av.stream", "Stream", "stream", True),
                                ("av.codec.context", "CodecContext", "codec", True), ("av.container.streams", "StreamContainer", "streams", True),
                                ("av.packet", "Packet", "packet", False), ("av.audio.frame", "AudioFrame", "audioframe", False),
                                ("av.frame", "Frame", "frame", False),
                                ("av.index", "IndexEntries", "index", False), ("av.index", "IndexEntry", "index_entry", False),
                                ("av.format", "ContainerFormat", "format", False)):
        try:
            __import__(mod)
            out.append((getattr(sys.modules[mod], cls), kind, one))
        except (ImportError, AttributeError):   # a PyAV without it, or a test's fake
            pass
    return out


PLAIN = (bool, int, float, complex, str, bytes, fractions.Fraction, enum.Enum)


def wrap(v, use, seen):
    """v as the use sees it: an object of PyAV as an Av, a list or tuple of them as one of Avs, an iterator as one that
    gives Avs, a function or method as one that unwraps its arguments. seen keeps one Av for each container, stream
    and codec, so "is" holds."""
    if v is None or isinstance(v, (Av, Iter, Cls, Module, *PLAIN)):
        return v
    if type(v).__module__.split(".")[0] == "av":
        for cls, kind, one in kinds():
            if isinstance(v, cls):
                if not one:
                    return Av(v, kind, use, seen)
                if id(v) not in seen:
                    seen[id(v)] = (v, Av(v, kind, use, seen))
                return seen[id(v)][1]
        return Av(v, type(v).__name__.lower(), use, seen)
    if isinstance(v, (list, tuple)):
        return type(v)(wrap(x, use, seen) for x in v)
    if hasattr(v, "__next__"):   # a generator of demux() or decode()
        return Iter(v, use, seen)
    if callable(v) and not isinstance(v, type) and type(v).__name__ in ("builtin_function_or_method", "method", "cython_function_or_method", "function"):
        return lambda *a, **kw: wrap(v(*map(_unwrap, a), **{k: _unwrap(x) for k, x in kw.items()}), use, seen)
    return v


def _unwrap(v):
    """The PyAV object behind an Av or a Cls."""
    return object.__getattribute__(v, "_o") if isinstance(v, (Av, Cls)) else v


class Module:
    """A module of av for one use. A submodule comes as a Module, a class of PyAV as a Cls, an exception or enum class
    as it is, and every other name through wrap()."""

    def __init__(self, m, use, seen):
        self._m, self._use, self._seen = m, use, seen

    def __getattr__(self, k):
        v = getattr(self._m, k)
        if isinstance(v, types.ModuleType):
            return Module(v, self._use, self._seen)
        if isinstance(v, type):
            return v if issubclass(v, (BaseException, enum.Enum)) else Cls(v, self._use, self._seen)
        return wrap(v, self._use, self._seen)

    def __repr__(self):
        return f"<module {self._m.__name__} for {self._use}>"


class Cls:
    """A class of PyAV for one use: an object it makes, or a class method gives, comes through wrap(). isinstance()
    takes it as the class."""
    __slots__ = ("_o", "_use", "_seen")

    def __init__(self, o, use, seen):
        for k, v in (("_o", o), ("_use", use), ("_seen", seen)):
            object.__setattr__(self, k, v)

    def __call__(self, *a, **kw):
        o = object.__getattribute__(self, "_o")
        return wrap(o(*map(_unwrap, a), **{k: _unwrap(x) for k, x in kw.items()}), object.__getattribute__(self, "_use"),
                    object.__getattribute__(self, "_seen"))

    def __getattr__(self, k):
        return wrap(getattr(object.__getattribute__(self, "_o"), k), object.__getattribute__(self, "_use"), object.__getattribute__(self, "_seen"))

    def __instancecheck__(self, x):
        return isinstance(_unwrap(x), object.__getattribute__(self, "_o"))

    def __repr__(self):
        return f"<class {object.__getattribute__(self, '_o').__name__} for {object.__getattribute__(self, '_use')}>"


class Iter:
    """An iterator of PyAV that gives Avs."""

    def __init__(self, it, use, seen):
        self._it, self._use, self._seen = it, use, seen

    def __iter__(self):
        return self

    def __next__(self):
        return wrap(next(self._it), self._use, self._seen)


class Av:
    """A PyAV object for one use. An attribute that AV does not know for its kind, and the use does not name, raises
    TimeRead. repr() and str() give its class and use, no time."""
    __slots__ = ("_o", "_kind", "_use", "_seen")

    def __init__(self, o, kind, use, seen):
        for k, v in (("_o", o), ("_kind", kind), ("_use", use), ("_seen", seen)):
            object.__setattr__(self, k, v)

    @property
    def __class__(self):   # isinstance() sees the PyAV class
        return type(object.__getattribute__(self, "_o"))

    def __getattr__(self, k):
        o, kind, use = object.__getattribute__(self, "_o"), object.__getattribute__(self, "_kind"), object.__getattribute__(self, "_use")
        if k.startswith("__"):
            raise AttributeError(k)
        if k not in AV.get(kind, ()):
            if f"{kind}.{k}" not in names(use):
                refuse(f"{use} reads {kind}.{k} of PyAV, which ff.READS does not name and ff.py does not know")
            USED.setdefault(use, set()).add(f"{kind}.{k}")
        return wrap(getattr(o, k), use, object.__getattribute__(self, "_seen"))

    def __setattr__(self, k, v):
        setattr(object.__getattribute__(self, "_o"), k, _unwrap(v))

    def __enter__(self):
        object.__getattribute__(self, "_o").__enter__()
        return self

    def __exit__(self, *a):
        return object.__getattribute__(self, "_o").__exit__(*a)

    def __iter__(self):
        return Iter(iter(object.__getattribute__(self, "_o")), object.__getattribute__(self, "_use"), object.__getattribute__(self, "_seen"))

    def __len__(self):
        return len(object.__getattribute__(self, "_o"))

    def __bool__(self):
        return bool(object.__getattribute__(self, "_o"))

    def __getitem__(self, k):
        return wrap(object.__getattribute__(self, "_o")[k], object.__getattribute__(self, "_use"), object.__getattribute__(self, "_seen"))

    def __repr__(self):
        return f"<{type(object.__getattribute__(self, '_o')).__name__} for {object.__getattribute__(self, '_use')}>"
