#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""One video window of checks.window() through PyAV, for a file whose film is not ffmpeg's first video stream.

ffmpeg's -ss seeks by the first video stream that is no attached picture. A still-image track before the film then
moves the seek, and the film decodes from a frame that is no keyframe. PyAV seeks by the film's own stream. The venv
of lid.py holds PyAV, and the hook runs this module by its path in that venv, as it runs lid.py:

    /opt/arr-media-guard-lid/venv/bin/python avwindow.py PATH INDEX START SECS

It decodes the ffprobe stream INDEX from START for SECS seconds, as the ffmpeg command of checks.window() does: the
seek to the keyframe at or before START, 2 decoder threads, no loop filter, and SECS seconds of frames from the first
frame at or after START. The demuxer discards every other stream, as ffmpeg's -map does, so damage in another track
is no part of the window. An EAGAIN of the demuxer, as after an MPEG-TS resync, reads on, as ffmpeg does.
It writes its stderr in the shape of that command's: libav's log lines as "[name @ 0x0] [level] message", a line
for each packet that fails to decode or demux, "corrupt decoded frame" for each corrupt frame, and a showinfo line
for each frame with its time from START. So decide.parse_window() reads both, with the same rules. Exit 1 means the
file did not open, has no decoder for the stream, or the decoder did not open. Each libav line goes out as libav logs
it, from any thread, so a window that checks.watch() kills keeps the lines up to the kill, as ffmpeg's does. The lines leave out ffmpeg's stream list, so an encv FourCC never
shows. checks.video_stages() then reads the bytes for encryption, see checks.encrypted_video().
"""
import logging
import sys

LEVELS = {50: "fatal", 40: "error", 30: "warning", 20: "info"}   # PyAV's Python levels of libav's lines, as ffmpeg's -loglevel level+ names them


def say(line):   # one write per line, so the lines of libav's decoder threads never mix with it
    sys.stderr.write(line + "\n")


class Libav(logging.Handler):
    """Each libav line, when libav logs it, in the shape of ffmpeg's."""

    def emit(self, r):
        say(f"[{r.name.removeprefix('libav.')} @ 0x0] [{LEVELS.get(r.levelno, 'info')}] {r.getMessage().rstrip()}")


def main(path, index, start, secs):
    """Decode the window and write each line to stderr. Returns the exit code."""
    import av
    import av.logging
    av.logging.set_level(av.logging.INFO)
    logging.getLogger("libav").addHandler(Libav())
    with av.open(path) as c:   # a file that does not open raises, and exits 1
        s = c.streams[index]
        for o in c.streams:   # as ffmpeg's -map, so a damaged audio track never shows in the window
            if o is not s:
                o.discard = av.stream.Discard.all
        cc = s.codec_context
        if cc is None:   # PyAV has no codec context for a codec with no decoder
            say(f"[vist#0:{index} @ 0x0] [fatal] Decoding requested, but no decoder found")
            return 1
        cc.thread_type, cc.thread_count, cc.options = "AUTO", 2, {"skip_loop_filter": "all"}
        try:   # PyAV would open the decoder in the first decode, and fail each packet
            cc.open()
        except av.error.FFmpegError as e:
            say(f"[vist#0:{index}/{cc.name} @ 0x0] [fatal] Error while opening decoder for input stream #0:{index} : {e}")
            return 1
        t0 = start + (c.start_time or 0) / 1e6   # ffmpeg's -ss counts from the file's start time
        try:
            c.seek(int(t0 / s.time_base), stream=s, backward=True, any_frame=False)
        except av.error.FFmpegError:
            say(f"[in#0/{c.format.name} @ 0x0] [warning] could not seek to position {t0:.3f}")   # ffmpeg reads on from the start
        n, first = 0, None   # ffmpeg's trim counts SECS from the first frame at or after START, where an MPEG-TS seek may land late

        def frames(fs):
            nonlocal n, first
            for f in fs:
                if f.is_corrupt:
                    say(f"[vist#0:{index}/{cc.name} @ 0x0] [warning] corrupt decoded frame")
                t = None if f.pts is None else float(f.pts * s.time_base)   # a flushed frame has no time base, so no f.time
                if t is not None and t >= t0 and (first is None or t < first + secs):
                    first = t if first is None else first
                    say(f"[Parsed_showinfo_0 @ 0x0] [info] n:{n:4d} pts:{f.pts} pts_time:{t - t0:.6g}")
                    n += 1

        def decode(p):
            try:
                frames(cc.decode(p))
            except av.error.EOFError:   # the flush packet at the end of the file drained the decoder already
                pass
            except av.error.FFmpegError as e:
                say(f"[vist#0:{index}/{cc.name} @ 0x0] [error] Decoding error: {e}")
        packets = c.demux(s)
        while True:
            try:
                p = next(packets)
            except StopIteration:
                break
            except av.error.BlockingIOError:   # ffmpeg reads on
                packets = c.demux(s)
                continue
            except av.error.FFmpegError as e:   # ffmpeg ends the input at a read error
                say(f"[in#0/{c.format.name} @ 0x0] [error] Error during demuxing: {e}")
                break
            if first is not None and p.dts is not None and p.dts * s.time_base >= first + secs:
                break
            decode(p)
        decode(None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])))
