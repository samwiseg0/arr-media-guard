# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The guard of the time reads (docs/design.md, "Time reads"). A check or a write takes each time from the file as it
stores it, or as its spec defines it: matroska.frames(), mp4.samples() or ts.frames(). ffmpeg and ffprobe estimate the
time of a frame their parser splits off, and their muxers move a time that goes back. So a function that reads a time
from ffmpeg or ffprobe output must be in READS, with the reason that read may stay. A new read then fails here and
needs its reason in review."""
import ast, pathlib

import pytest

# WORDS name a time in ffmpeg or ffprobe output. They cover the packet, frame, format and stream fields, the framemd5,
# framecrc and framehash muxers, the packet and frame dumps, showinfo, -progress and -debug_ts. ATTRS name the times
# PyAV gives a packet, a frame, a stream or a container. time.time() is the wall clock, so it never counts.
WORDS = ("pts_time", "dts_time", "duration_time", "best_effort_timestamp", "framemd5", "framecrc", "-show_packets", "-show_frames",
         "packet=", "frame=", "=duration", "start_time", "framehash", "out_time", "showinfo", "-debug_ts")
ATTRS = ("pts", "dts", "best_effort_timestamp", "time", "duration", "start_time")
GATE = "the container duration gates a check by its length or places its windows, and judges no frame time (plan A3)"

# module.function: why its read of an ffmpeg or ffprobe time may stay
READS = {
    "proof.packet_hashes": "the packet data and counts of every proof. Its times count only for a source with no reader (AVI, ASF), see prove()",
    "proof.packet_places": "ffprobe's byte of each MP4 packet pairs it with its sample. Its times count only for a source with no reader",
    "remux.cover_images": "the md5 of the first packet of a cover picture, no time",
    "remux.resub": "the md5s of the packets of a garbled track and a cover picture, no time",
    "convert.avi_sync": "an AVI times each frame by its index, so ffmpeg's first time is the container's own definition",
    "checks.packet_read": "ffprobe's format start_time, the origin of the -ss of each sample and of the verdict times. The frame times come from the readers (plan A1)",
    "checks.ffprobe_packets": "the stops-early, hole and held checks of an AVI, which times each frame by its index, and of a format with no reader",
    "content.last_packet": "the stored time of the film's last packet, the runtime check in minutes",
    "proof.bad_lines": "the name of the framemd5 muxer in an error line, no time",
    "decide.<module>": "FRAME, the decoded frames of a picture window for decide.parse_window(), which judge the decode, never the sync",
    "avwindow.main": "the start and the end of a picture window's read, as ffmpeg's -ss and -t place it, see avwindow.main.frames (plan A5)",
    "avwindow.main.frames": "the decoded frames of a picture window through PyAV, as decide.parse_window() reads them",
    "burnin.Sampler.keyframe": "the time of a keyframe that a burn-in sample shows, which places the sample and judges no time",
    "checks.full_decode": "the decoded length of a whole decode, which counts the samples the decoder gives (plan A2)",
    "checks.ffprobe_duration": "the format duration, which places a sample or a window, or gates a doubt (plan A3)",
    "content.trusted_duration": "the runtime check in minutes, which needs two sources that agree (plan A4)",
    "checks.window": "showinfo's decoded frames of a picture window, which judge the decode, never the sync",
    "burnin.Sampler.__init__": "the container's start time, from which a burn-in sample counts, as ffmpeg's -ss does",
    "burnin.Sampler.frame_at": "the decoded frame that a burn-in sample shows. Its time places the sample and judges no sync",
    "burnin.main": "the file's duration from the command line, which places the windows",
    "lid.main": "the file's duration from the command line, which places the windows",
    "lid.start_delay": "the start of the audio stream against the file's, as the container stores them, puts the decoded speech on the file's clock",
    "proof.hevc_packets": "the RPU check holds the pts, the dts and the keyframe flag of each HEVC packet equal in two reads, and measures no time",
    "proof.source_read": "the duration bounds where a skip of a damage warning counts as damage (plan P12)",
    "checks.header_probe": "the Segment Duration that the header stores, compared with where the streams end",
    "checks.lid_cached": "the container duration keys the language-ID cache",
    "process.refresh": "the container duration in the decision record, as information",
    "remux.header_checks": "the container duration of the new file in the facts of the header repair, as information",
    **dict.fromkeys(("checks.audio_span", "checks.lid_cli", "convert.convert", "convert.convert_subs", "process.burn_probe", "process.flag_hearing",
                     "process.languages", "process.subtitle_checks", "regrab.content_probe.probe", "subtitles.sub_jobs", "subtitles.sub_layout",
                     "subtitles.sub_match", "subtitles.sub_reference", "subtitles.sub_verdicts", "subtitles.whole_heard"), GATE),
}


def reads(path):
    """{qualified function name: [line]} of each function of the module at path whose code holds a WORDS string or
    reads an ATTRS attribute. Code outside every function counts as "<module>". A docstring and a comment are no code."""
    out = {}

    def code(body):   # the statements of a body without its docstring
        doc = body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
        return body[1:] if doc else body

    def walk(node, name):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{name}.{node.name}" if name else node.name
            for x in (*node.decorator_list, *([node.args] if hasattr(node, "args") else []), *code(node.body)):
                walk(x, name)
            return
        wall = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) == ("time", "time")
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and any(w in node.value for w in WORDS)) or \
                (isinstance(node, ast.Attribute) and node.attr in ATTRS and not wall):
            out.setdefault(name or "<module>", []).append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, name)

    for node in code(ast.parse(path.read_text()).body):
        walk(node, "")
    return out


def test_every_time_read_of_ffmpeg_is_listed_with_its_reason():
    root = pathlib.Path(__file__).resolve().parent.parent / "arr_media_guard"
    found = {f"{p.stem}.{name}": lines for p in sorted(root.glob("*.py")) for name, lines in reads(p).items()}
    new = {k: v for k, v in found.items() if k not in READS}
    assert not new, f"these functions read a time from ffmpeg or ffprobe, see READS: {new}"
    gone = sorted(set(READS) - set(found))
    assert not gone, f"these entries of READS read no time any more, take them out: {gone}"


def test_the_guard_finds_a_read_in_code_and_never_in_a_docstring(tmp_path):
    path = tmp_path / "m.py"
    path.write_text('def a():\n    """It reads pts_time."""\n    return time.time()\n\n\ndef b(p):\n    x = ["ffprobe", "-show_entries", "packet=pts_time"]\n'
                    '    return p.pts\n\n\nclass C:\n    def d(self):\n        return "framemd5"\n')
    assert reads(path) == {"b": [7, 8], "C.d": [13]}


# The shapes of a time read that the guard of 3703782 missed. Each one is a hit now.
SHAPES = {
    "packet ticks": 'ffprobe = ["ffprobe", "-show_entries", "packet=pts,dts,duration", p]',
    "frame fields": 'ffprobe = ["ffprobe", "-show_entries", "frame=pts,pkt_dts", p]',
    "format duration": 'ffprobe = ["ffprobe", "-show_entries", "format=duration", p]',
    "stream start": 'ffprobe = ["ffprobe", "-show_entries", "stream=start_time", p]',
    "framehash": 'ffmpeg = ["ffmpeg", "-i", p, "-f", "framehash", "-"]',
    "showinfo read by pts": 'ffmpeg = ["ffmpeg", "-i", p, "-vf", "showinfo", "-f", "null", "-"]\n    return run(ffmpeg).split("pts:")',
    "progress": 'return run(["ffmpeg", "-progress", "pipe:1", "-i", p, "-f", "null", "-"]).split("out_time_us=")',
    "debug_ts": 'ffmpeg = ["ffmpeg", "-debug_ts", "-i", p, "-f", "null", "-"]',
    "PyAV frame": "return [f.time for f in p.decode(video=0)]",
    "PyAV packet": "return [x.duration for x in p.demux()]",
    "PyAV stream": "return p.streams.video[0].start_time",
    "PyAV container": "return p.duration",
}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_guard_finds_each_shape_of_a_time_read(tmp_path, shape):
    path = tmp_path / "m.py"
    path.write_text(f"def f(p):\n    {SHAPES[shape]}\n")
    assert "f" in reads(path), shape
