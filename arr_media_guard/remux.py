# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The remuxes that replace a Matroska file, the header repair and the subtitle remux, and the swap they share."""
import contextlib, fractions, itertools, os, re, shutil, signal, subprocess, threading, time

from . import align, checks, config, decide, logs, proof, runner, subsync, vault


# The track properties a remux must keep, with the Matroska default of each. uid too, except on a track a trim replaced.
KEEP_PROPS = (("language", "und"), ("track_name", None), ("default_track", True), ("forced_track", False), ("enabled_track", True),
              ("flag_original", False), ("flag_commentary", False), ("flag_hearing_impaired", False), ("flag_visual_impaired", False),
              ("flag_text_descriptions", False), ("codec_private_data", None))
# mkvmerge's option for each flag in KEEP_PROPS, for the SubRip file that replaces a trimmed track
FLAG_OPTS = (("--default-track-flag", "default_track", True), ("--forced-display-flag", "forced_track", False),
             ("--track-enabled-flag", "enabled_track", True), ("--original-flag", "flag_original", False),
             ("--commentary-flag", "flag_commentary", False), ("--hearing-impaired-flag", "flag_hearing_impaired", False),
             ("--visual-impaired-flag", "flag_visual_impaired", False), ("--text-descriptions-flag", "flag_text_descriptions", False))


def header_fault(j, tmp, new, old_size, hp, info):
    """Why the remuxed file tmp may not replace a Matroska original whose header has an issue, or None. j and new are
    their mkvmerge probes, hp is the header stage of check_video() for the original. The checks compare against the
    stream ends hp read from the last clusters, never the old header. info gets the new duration, the new Cues, the
    frame count and the windows.

    Every track keeps its type, codec, language, name, flags, codec private data and UID, a trimmed one all but its UID.
    The new video track must hold as many frames as its end over its default duration, less FRAME_SLACK. mkvmerge drops
    a damaged cluster with no warning. A track with no
    default duration passes only when every window of the check decoded clean. A trimmed track must hold its events
    less the ones the trim dropped."""
    kind = (new.get("container") or {}).get("type")
    if kind != "Matroska": return f"the new file reads as {kind}"
    trim, remove = set(hp.get("trim") or ()), set(hp.get("remove") or ())
    kept = {"tracks": [t for t in j.get("tracks") or [] if t.get("id") not in remove]}   # the planned removal, and nothing else
    if checks.track_list(kept) != checks.track_list(new): return f"the tracks changed from {checks.track_list(kept)} to {checks.track_list(new)}"
    gone = {(t.get("properties") or {}).get("uid") for t in j.get("tracks") or [] if t.get("id") in remove}
    if gone & {(t.get("properties") or {}).get("uid") for t in new.get("tracks") or []}: return "a removed subtitle track is still in the file"
    for a, b in zip(kept["tracks"], new["tracks"]):
        pa, pb = a.get("properties") or {}, b.get("properties") or {}
        diff = [k for k, d in KEEP_PROPS + ((("uid", None),) if a.get("id") not in trim else ()) if pa.get(k, d) != pb.get(k, d)]
        if diff: return f"track {a.get('id')} changed its {', '.join(diff)}"
    info["new_duration"] = d1 = round(decide.duration(new), 3)
    if abs(d1 - hp["expect"]) > decide.REPAIR_END: return f"the new header says {d1} seconds, but the streams end at {hp['expect']}"
    now = checks.header_probe(tmp, new)
    info["new_cues"] = (now or {}).get("cues")
    if not now or now["cues"] is not True or now["issue"]:
        return "the new file still has a header issue: " + "; ".join((now or {}).get("issue") or [f"no usable Cues, {info['new_cues']}"])
    for i in trim:   # one block per event
        n, t = (new["tracks"][[x.get("id") for x in kept["tracks"]].index(i)].get("properties") or {}).get("tag_number_of_frames"), info["trimmed"][i]
        if str(n) != str(t["events"] - t["dropped"]): return f"the trimmed track {i} holds {n} events, not {t['events'] - t['dropped']}"
    p = next(((x.get("properties") or {}) for x in new["tracks"] if x.get("type") == "video"), None)
    if p is not None:
        info["frames"] = n = int(p.get("tag_number_of_frames") or 0)
        if p.get("default_duration"):
            want = round(hp["video"] * 1e9 / p["default_duration"])
            if n < want - decide.FRAME_SLACK: return f"the new file holds {n} video frames, but the video end at {hp['video']} s needs {want:.0f}"
        elif not hp.get("windows_clean"):
            return "the video track has no default duration, so lost frames cannot be counted, and not every window of the check decoded clean"
    size, old_size = os.path.getsize(tmp), old_size - (hp.get("tail") or 0)   # the bytes past the Segment end go
    if abs(size - old_size) > config.REPACK_SIZE * old_size: return f"the size changed from {old_size} to {size} bytes"
    video = hp["video"] or 0
    info["windows"] = ws = [checks.window(tmp, video * s, min(decide.VIDEO_SECS, video * (1 - s))) for s in decide.VIDEO_AT] if video >= 60 else []
    certain, doubts = decide.video_verdict([], ws)
    doubts += [decide.stopped_doubt(w) for w in ws if decide.read_capped(w)]   # the new file has Cues, so a read-cap stop is no clean window
    if certain or doubts: return "a video window of the new file is not clean: " + (certain or "; ".join(doubts))
    return None


def trim_inputs(path, j, hp, folder):
    """mkvmerge arguments that put back each SubRip track in hp["trim"], extracted with mkvextract into folder and cut
    at hp["streams"] by decide.trim_srt(), with its language, name and flags. The tracks in hp["remove"] are
    extracted and counted too, and never put back. Returns (the arguments, {trimmed track id: the counts of
    trim_srt()}, {removed track id: its language, name, codec, lines and late lines}). The rules:
    every line that runs past the real end ends there, and a track with REMOVE_SHARE of its lines starting after the end
    is timed for another cut and goes. trim_srt() counts again, and a track that falls on the other side of the rule
    than the plan stops the remux."""
    trim, remove, tracks = hp.get("trim") or [], hp.get("remove") or [], {t.get("id"): t for t in j.get("tracks") or []}
    out = {i: os.path.join(folder, f"{i}.srt") for i in trim + remove}
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvextract", path, "tracks", *[f"{i}:{f}" for i, f in out.items()]],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:   # 1 is a warning, and a trim never builds on one
        raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    args, counts, removed = [], {}, {}
    for i, f in out.items():
        with open(f, encoding="utf-8") as fh:
            text, n = decide.trim_srt(fh.read(), hp["streams"])
        far = hp.get("listed") is None or hp["subtitles"][i] > decide.REMOVE_END * hp["listed"] * 60   # see subtitle_plan()
        if (decide.remove_track(n["events"], n["dropped"]) and far) != (i in remove):
            raise RuntimeError(f"subtitle track {i} has {n['dropped']} of {n['events']} lines starting after the end, so the plan to "
                               f"{'remove' if i in remove else 'trim'} it no longer holds")
        p = tracks[i].get("properties") or {}
        if i in remove:
            removed[i] = {"language": p.get("language"), "name": p.get("track_name"), "codec": tracks[i].get("codec"), "lines": n["events"],
                          "late": n["dropped"]}
            continue
        counts[i] = n
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
        args += ["--sub-charset", "0:UTF-8", "--language", f"0:{p.get('language_ietf') or p.get('language') or 'und'}"]
        args += ["--track-name", f"0:{p['track_name']}"] if p.get("track_name") else []
        for opt, key, default in FLAG_OPTS:
            args += [opt, f"0:{int(bool(p.get(key, default)))}"]
        args.append(f)
    return args, counts, removed


def repack_block(path, st, keep=True):
    """(code, text) of why a repack of the file st describes cannot run, or (None, None). The codes, in the order of the
    checks, are "hardlinked", "cap" for the size cap, "space" for low space and "keep" for no place to keep the original.
    report.block() words each code for a dry run. keep=False leaves out the "keep" check, see convert_skip()."""
    fs = os.statvfs(os.path.dirname(path))
    if vault.links(st) > 1:   # the rename would split the link from the download client's copy
        return "hardlinked", "hardlinked"
    if st.st_size > config.CFG.repack_max:
        return "cap", f"over the {config.CFG.repack_max / 1e9:.0f} GB repack cap"
    if fs.f_bavail * fs.f_frsize < 2 * st.st_size:
        return "space", f"low space: {fs.f_bavail * fs.f_frsize / 1e9:.1f} GB free for {st.st_size / 1e9:.1f} GB"
    why = keep and config.CFG.keep_days and vault.keepable(path, st)
    return ("keep", f"the original cannot be kept: {why}") if why else (None, None)


def repack_skip(path, st):
    """Why a repack of the file st describes cannot run, or None, see repack_block()."""
    return repack_block(path, st)[1]


class SourceChanged(Exception):
    """The app replaced or renamed the original while mkvmerge ran."""


def stopped(signum, _):
    """SIGTERM during a repack, as a stop of the app's unit sends it, or Ctrl+C. It raises, so repack() removes its temp
    file. It records the signal first, because CPython drops the raise when it lands in Popen.__del__ after a remux.
    Swap.sync() and Swap.check() raise it again, see runner.raise_term()."""
    runner.STOP["term"] = signum
    runner.raise_term()


def repack_tmp(path):
    """The temp file of a repack, in hide_dir beside the file. The apps' disk scan and Plex skip a hidden folder. A hidden
    file beside the video is not enough: the scan takes it as an extra of the item, and a conversion then hides it with
    the extras. The name is cut to the share's 255-byte limit."""
    name = os.fsencode(os.path.basename(path))[:255 - len(b"." + config.REPACK_TMP)]
    return os.path.join(os.path.dirname(path), config.CFG.hide_dir, os.fsdecode(b"." + name + config.REPACK_TMP))


def new_tmp(tmp, excl=False):
    """Create the empty temp file tmp in its hidden folder, before the remux writes it. Another conversion in the same
    folder removes that folder once it is empty, so the file holds it, and a create that finds no folder tries again.
    With excl a tmp that is there raises FileExistsError, else the create empties it."""
    for n in range(3):
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            return os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if excl else os.O_TRUNC), 0o600))
        except FileNotFoundError:
            if n == 2:
                raise


def drop_tmp(tmp):
    """Remove the temp file tmp, and its hidden folder when that is empty."""
    with contextlib.suppress(OSError):
        os.remove(tmp)
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(tmp))


class Swap:
    """The steps repack(), resub() and convert() share to put tmp, a remux of path in hide_dir, in place of path, the
    file st describes. Each caller runs them in its own order. sync() comes before its checks read tmp, own() and check()
    after them, then the rename. unkeep() runs on a failure and close() at the end. Only the main thread sets the
    SIGTERM and SIGINT handlers. A conversion worker of a backfill runs in another thread and sets none. A process that
    ignores SIGINT keeps it ignored, as a backfill started with & from a script does."""

    def __init__(self, path, st, tmp, info):
        """End the time limit, so no step of the remux, its proof or the swap stops at it. SIGTERM and Ctrl+C raise
        through stopped(), so the caller removes the temp file. process() starts a new time limit on info["warnings"]."""
        self.path, self.st, self.tmp, self.info = path, st, tmp, info
        main = threading.current_thread() is threading.main_thread()
        config.DEADLINE.stop()
        info["warnings"] = None
        self.term = {s: signal.signal(s, stopped) for s in (signal.SIGTERM, signal.SIGINT)
                     if s == signal.SIGTERM or signal.getsignal(s) != signal.SIG_IGN} if main else {}

    def sync(self):
        """fsync tmp. A write error the NAS still holds shows here, before the checks read the file. A SIGTERM whose
        raise a finalizer dropped during the remux stops it here. The inode, size and mtime of tmp then go into
        proven, see check()."""
        runner.raise_term()
        fd = os.open(self.tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
            self.proven = runner.file_key(os.fstat(fd))
        finally:
            os.close(fd)

    def own(self):
        """Give tmp the owner and mode of the original and return its size. A NAS share may refuse chown, and
        info["owner"] then says so."""
        st = self.st
        try:
            os.chown(self.tmp, st.st_uid, st.st_gid)
        except PermissionError as ex:
            new = os.stat(self.tmp)
            self.info["owner"] = {"from": f"{st.st_uid}:{st.st_gid}", "to": f"{new.st_uid}:{new.st_gid}", "why": f"chown refused: {ex}"}
        os.chmod(self.tmp, st.st_mode & 0o7777)
        return os.path.getsize(self.tmp)

    def check(self, text, keep):
        """Raise SourceChanged(text) when path is gone or holds another file than st describes. The app replaced or
        renamed the original during the remux, and the rename would overwrite the app's file. Raise RuntimeError when
        tmp is not the file that sync() saw before the checks read it. A run under the exclusive lock can write over
        the temp file of a conversion that waits for its swap, see convert.busy_tmp(). Then, when keep is true, keep
        the original, see keep_original(). A failed keep stops the swap, and the original stays. A SIGTERM whose raise
        a finalizer dropped during the proof stops it here, before the rename."""
        runner.raise_term()
        now = None
        with contextlib.suppress(FileNotFoundError):
            now = os.stat(self.path)
        if not now or runner.file_key(now) != runner.file_key(self.st):
            raise SourceChanged(text)
        if runner.file_key(os.stat(self.tmp)) != self.proven:
            raise RuntimeError("the temp file changed after its checks, so another remux of the file wrote it")
        if keep:
            try:
                self.info["kept"] = vault.keep_original(self.path, self.info)
            except OSError as ex:
                raise RuntimeError(f"the original could not be kept, so it stays: {ex}") from None

    def unkeep(self, *paths):
        """Remove the kept link after a failure when one of paths still holds the original. The rename then never ran."""
        with contextlib.suppress(OSError):
            if self.info.get("kept") and self.st.st_ino in {os.stat(p).st_ino for p in paths if os.path.lexists(p)}:
                os.remove(self.info["kept"])
                del self.info["kept"]

    def release(self):
        """Put the SIGTERM and SIGINT handlers of the caller back."""
        for s, h in self.term.items():
            signal.signal(s, h)

    def close(self, folder):
        """Put the signal handlers back, then remove the work folder and the temp file. After a rename only the hidden
        folder goes, when it is empty."""
        self.release()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        drop_tmp(self.tmp)


def sweep_repack_tmp(folder, source):
    """Remove the temp files of repacks that a kill left behind in folder, when older than a day. Logs each one. A
    conversion worker of a backfill runs beside the hook and other workers, and a proof of a file at the size cap
    may take hours. The folder itself is swept too."""
    for d in (folder, os.path.join(folder, config.CFG.hide_dir)):
        try:
            names = [n for n in os.listdir(d) if n.startswith(".") and n.endswith(os.fsdecode(config.REPACK_TMP))]
        except OSError:
            continue
        for n in names:
            f = os.path.join(d, n)
            try:
                if time.time() - os.path.getmtime(f) > 86400:
                    os.remove(f)
                    logs.log(dict(source=source, result="warning", path=f, note="removed a repack temp file older than a day"))
            except OSError:
                pass


def repack(path, j, st, apply, hp):
    """Remux a Matroska file in place for a header repair (docs/design.md, "Header repair"), under the caller's file lock. hp is
    the header stage of check_video(). The remux writes the Segment duration, the Cues and the Segment size from the
    real streams. When hp lists SubRip tracks to trim or remove, trim_inputs() cuts their late lines first. Returns
    (code, result, info). result is "header repaired", "subtitles trimmed", "subtitles removed" or "tail removed", "would
    repair header: ...", "header repair failed: ..." or "header repair skipped, ...", and code its reason code, as
    header_repaired or header_repair_skipped. info holds the container, the old and new size, the old and new track
    lists, mkvmerge's warnings, the kept original, the old and new duration, the stream end, the new Cues, the frame
    count, the trim counts and the windows of its check. "tail removed" is a header repair of a file with bytes past the
    Segment end, see header_probe(). header_fault() then compares with the Segment size. A file that is not Matroska
    goes to convert().

    mkvmerge writes repack_tmp() in hide_dir beside the original at nice 19 and idle I/O, with no time limit, so a slow
    NAS never stops a remux halfway. --track-order keeps the tracks in their order. By default mkvmerge puts video
    first, and a file may hold subtitles, audio, video in that order. The track UIDs and the Segment UID stay. Any mkvmerge warning is a
    fault: it resyncs past a damaged cluster and drops it. The temp file is synced, and header_fault() compares it to
    the original. The original must still be the file st describes, else the app replaced it meanwhile
    (repack_source_changed). On any fault the temp file goes and the original stays. Otherwise the temp file takes the
    original's owner and mode. The original is hard-linked into originals_root() for keep_days, then the temp file is
    renamed over it. Any exception removes the temp file: Ctrl+C, the time limit, and SIGTERM through stopped()."""
    info = {"container": (j.get("container") or {}).get("type"), "old_size": st.st_size, "tracks": checks.track_list(j)}
    trim, remove = list(hp.get("trim") or ()), list(hp.get("remove") or ())
    tracks = {t.get("id"): t for t in j.get("tracks") or []}
    lines = hp.get("sublines") or {}
    info["removed"] = {i: {"language": (tracks[i].get("properties") or {}).get("language"), "name": (tracks[i].get("properties") or {}).get("track_name"),
                           "codec": tracks[i].get("codec"), "lines": (lines.get(i) or [None])[0], "late": (lines.get(i) or [None, None])[1]}
                       for i in remove}   # the plan. The remux counts again, see trim_inputs().
    gone = ", ".join(f'{i} ({r["language"]}, {r["late"]} of {r["lines"]} lines start after the end)' for i, r in info["removed"].items())
    done = ("subtitle_removed", "subtitles removed") if remove else ("subtitle_trimmed", "subtitles trimmed") if trim \
        else ("tail_removed", "tail removed") if hp.get("tail") else ("header_repaired", "header repaired")
    would, failed, skip = "would repair header", "header repair failed", "header repair skipped"
    what = "; ".join(hp["issue"]) + (f", trim subtitle track {trim}" if trim else "") + (f", remove subtitle track {gone}" if remove else "")
    info.update(old_duration=hp["duration"], end=hp["expect"], cues=hp["cues"])
    why = repack_skip(path, st)
    if why:   # a hard link, the cap, low space or no place for the original: nothing is written
        return "header_repair_skipped", f"{skip}, {why}: {what}", info
    if not apply:
        return "would_repair_header", f"{would}: {what}", info
    tmp, folder = repack_tmp(path), runner.work_dir("trim") if trim or remove else None
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("id") not in remove]   # a removed track leaves the file
    order = ["--track-order", ",".join(f"{trim.index(i) + 1}:0" if i in trim else f"0:{i}" for i in ids)] if ids and None not in ids else []
    uid = ((j.get("container") or {}).get("properties") or {}).get("segment_uid")
    keep = ["--segment-uid", uid] if uid else []   # an ordered chapter of another file may point at it
    sw = Swap(path, st, tmp, info)
    try:
        new_tmp(tmp)
        extra = []
        if trim or remove:
            extra, info["trimmed"], info["removed"] = trim_inputs(path, j, hp, folder)
        r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", "-o", tmp, *keep, *order,
                            *(["-s", "!" + ",".join(map(str, trim + remove))] if trim or remove else []), path, *extra],
                           capture_output=True, text=True, errors="replace")
        info["warnings"] = config.mask((r.stdout + r.stderr).strip())[-500:] or None
        if r.returncode or info["warnings"]:
            raise RuntimeError(f"mkvmerge exited {r.returncode}: {info['warnings']}")
        sw.sync()
        new = checks.mkvmerge(tmp)
        fault = header_fault(j, tmp, new, st.st_size, hp, info)
        if fault: raise RuntimeError(fault)
        size = sw.own()
        sw.check("the app replaced or renamed the original during the header repair", config.CFG.keep_days)
        os.replace(tmp, path)
    except BaseException as ex:
        sw.unkeep(path)
        if not isinstance(ex, Exception):
            raise
        return "header_repair_failed", config.mask(f"{failed}{', the original changed' if isinstance(ex, SourceChanged) else ''}: {ex}")[:500], info
    finally:
        sw.close(folder)
    if info.get("kept"):
        vault.prune_originals(vault.originals_root(path))
    info.update(new_size=size, new_tracks=checks.track_list(new))
    return *done, info


# The properties a retime puts back with mkvpropedit after ffmpeg's remux, as (mkvmerge -J name, mkvpropedit name).
# ffmpeg writes new UIDs and no BCP 47 tag, and the plan's flag edits name the tracks by UID.
PROP_EDITS = (("uid", "track-uid"), ("language_ietf", "language-ietf"), ("track_name", "name"), ("default_track", "flag-default"),
              ("forced_track", "flag-forced"), ("enabled_track", "flag-enabled"), ("flag_original", "flag-original"),
              ("flag_commentary", "flag-commentary"), ("flag_hearing_impaired", "flag-hearing-impaired"),
              ("flag_visual_impaired", "flag-visual-impaired"), ("flag_text_descriptions", "flag-text-descriptions"))


# The Format line of the [Events] section that mkvextract writes for an ASS or SSA header without one, by codec. mkvmerge
# reads it back into the header, see header_kept().
EVENTS_FORMAT = {"S_TEXT/ASS": "Layer, Start, End, Style, Actor, MarginL, MarginR, MarginV, Effect, Text",
                 "S_TEXT/SSA": "Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"}


# The Matroska codec of HEVC video, whose codec private data is an hvcC record, see hvcc_cleared()
HEVC = "V_MPEGH/ISO/HEVC"


def hvcc_cleared(b):
    """The hvcC record b with the array_completeness bit of each NAL array cleared, or None when b does not parse.
    ffmpeg 7.1's Matroska muxer writes the hvcC again with that bit 0 and keeps every other byte. A 0 only says the
    stream may hold more NAL units of the array's type, so the decoder reads the same video. The record holds 22 bytes
    of fields, then a byte with the count of arrays. Each array has a byte with the bit and the NAL type, a 2-byte NAL
    count, and each NAL after a 2-byte length."""
    b, at = bytearray(b), 23
    if len(b) < at or b[0] != 1:
        return None
    for _ in range(b[22]):
        if at + 3 > len(b):
            return None
        b[at] &= 0x7f
        count, at = int.from_bytes(b[at + 1:at + 3], "big"), at + 3
        for _ in range(count):
            if at + 2 > len(b):
                return None
            at += 2 + int.from_bytes(b[at:at + 2], "big")
    return bytes(b) if at <= len(b) else None


def header_kept(old, new, codec):
    """Whether the codec private data new, in hex, keeps old for a track of codec. It must be the same. An HEVC
    header may also be old with the array_completeness bits cleared, see hvcc_cleared(). After ended_track(), a
    HandBrake ASS header ends in a NUL byte and has no [Events] section. mkvmerge drops the NUL and adds the
    section, so such a header may also be old less that byte, then a blank line, "[Events]" and the Format line of
    EVENTS_FORMAT. A section name matches in any case, as libass reads it, so an old header with "[events]" fails too.
    Any other change fails."""
    if new == old:
        return True
    try:
        a, b = bytes.fromhex(old or ""), bytes.fromhex(new or "")
    except ValueError:
        return False
    if codec == HEVC:
        return b == hvcc_cleared(a)
    events = f"\r\n[Events]\r\nFormat: {EVENTS_FORMAT.get(codec)}\r\n\r\n".encode()
    return codec in EVENTS_FORMAT and a.endswith(b"\0") and b"[events]" not in a.lower() and b == a[:-1] + events


def grid_plan(plan, fix, codec):
    """A time_plan() of the flash_plan() plan of a track of codec, with its fix, or None. Only an ASS or SSA track whose
    starts fall between centiseconds gets one. ASS times are centiseconds, and the text round trip of ended_track()
    puts each start on that grid. HandBrake writes starts in milliseconds, so each one moves up to 5 ms, and up to
    about 9 ms against the stream's first packet. That is past the proof's TIME_SLACK for a track whose starts stay.
    The time plan puts each start on the nearest centisecond, and the proof holds each start and end to the plan."""
    if codec not in EVENTS_FORMAT or not any(round(s * 1000) % 10 for s, *_ in plan):
        return None
    return time_plan([(s, o, t) for s, t, o, _ in plan], fix, (), [n for *_, n in plan], ass=True)


def grid_moved(plan, fix):
    """The starts of the time_plan() plan of grid_plan() that the grid moved: its new start is more than half a ms off
    the start the fix gives, at 0 or later, as time_plan() keeps it. A start the fix alone moved does not count."""
    at = lambda s: max(0.0, subsync.moved(s * 1000, fix) / 1000 if fix else s)
    return sum(abs(a - at(s)) > 0.0005 for s, _, _, a, _ in plan)


def props_fault(j, new, drop=(), ended=()):
    """Why the tracks of the remuxed probe new differ from those of j less the track ids in drop, or None: the type,
    codec and language of each track in order, its KEEP_PROPS, its UID and BCP 47 tag, and the count of attachments
    and chapters. The codec private data of an HEVC track, and of a track in ended, must pass header_kept(). ended holds
    the track ids of j that ended_track() wrote again."""
    j = dict(j, tracks=[t for t in j.get("tracks") or [] if t.get("id") not in drop])
    if checks.track_list(j) != checks.track_list(new): return f"the tracks changed from {checks.track_list(j)} to {checks.track_list(new)}"
    for a, b in zip(j.get("tracks") or [], new.get("tracks") or []):
        pa, pb = a.get("properties") or {}, b.get("properties") or {}
        own = a.get("id") in ended or pa.get("codec_id") == HEVC   # a header that header_kept() may pass
        diff = [k for k, d in KEEP_PROPS + (("uid", None), ("language_ietf", None)) if pa.get(k, d) != pb.get(k, d)
                and not (k == "codec_private_data" and own and header_kept(pa.get(k), pb.get(k), pa.get("codec_id")))]
        if diff: return f"track {a.get('id')} changed its {', '.join(diff)}"
    for k in ("attachments", "chapters"):
        if len(j.get(k) or []) != len(new.get(k) or []): return f"the {k} changed from {len(j.get(k) or [])} to {len(new.get(k) or [])}"
    return None


def srt_moved(text, fix):
    """SubRip text with each time line moved by fix, see subsync.moved(). A start before 0 becomes 0. An end at or
    before 0 becomes 1 ms, so the cue never shows. mkvmerge drops a cue of length 0, and the proof of a conversion
    then refuses the new file. The rest of the text stays as it is, its line ends too."""
    ms = lambda h, m, sec, f: int(h) * 3600000 + int(m) * 60000 + int(sec) * 1000 + int(f.ljust(3, "0")[:3])
    at = lambda x, lo=0: (lambda v: f"{v // 3600000:02d}:{v // 60000 % 60:02d}:{v // 1000 % 60:02d},{v % 1000:03d}")(max(lo, subsync.moved(x, fix)))
    def line(m):
        g = m.groups()
        return f"{at(ms(*g[:4]))} --> {at(ms(*g[4:8]), 1)}{g[8]}"
    return "".join(x if k % 2 else decide.SRT_TIME.sub(line, x) for k, x in enumerate(re.split(r"(\r\n|\r|\n)", text)))


def flash_plan(cues, ass=False):
    """[(start, text, old end, new end)] of subsync.flash() for cues [(start, end, text)], or None when the cues do
    not flash. ASS keeps centiseconds, so an ASS end rounds to them."""
    ends = subsync.flash(cues)
    return [(s, t, e, round(n, 2) if ass else n) for (s, e, t), n in zip(cues, ends)] if ends else None


class Plan(list):
    """The rows of a time_plan(), with the kept blocks it follows in blocks. So blocks_moved() counts the cues they
    keep."""
    blocks = ()


def time_plan(cues, fix, blocks, ends=None, ass=False, starts=None):
    """A Plan [(start, text, old end, new start, new end)] of cues [(start, end, text)] in their order, or None when no
    time moves. fix is a fix of subsync.timing(), or None, and a cue starts at moved(start, fix). ends are the new ends
    of flash_plan() in the same order, or None. A cue's end takes its new end first, then moves as its start does.

    blocks are the "kept" blocks of subsync.keep_blocks(): a cue whose start lies in one keeps its start and end, or its
    flash end, whatever the fix. starts {place of a cue in start order: its new start} are the moves of the whole-file
    timing, see subtitles.sub_whole(). Cues that start together keep their order in cues. Such a cue takes that start,
    and align.ends() gives the new ends of the cues around the moves from their ends, the flash ends when there are
    any.

    A start stays at 0 or later. An end stays 1 ms after its start, as in srt_moved(). ASS keeps centiseconds, so an
    ASS time rounds to them, and an ASS end stays 1 cs after its start. With subsync.INVARIANTS, subsync.ordered()
    checks the order of the new starts."""
    step = 10 if ass else 1   # ms
    at = lambda t: round((subsync.moved(t * 1000, fix) if fix else t * 1000) / step) * step
    new_starts, new_ends = {}, {}
    if starts:   # the ends rule is align.ends(), on the lines in start order
        order = sorted(range(len(cues)), key=lambda k: cues[k][0])
        new_starts = {order[n]: x for n, x in starts.items()}
        new_ends = {order[n]: x for n, x in align.ends([(cues[k][0], ends[k] if ends else cues[k][1], "") for k in order], starts).items()}
    out = []
    for k, (s, e, text) in enumerate(cues):
        end = ends[k] if ends else e
        if k in new_starts or k in new_ends:
            a = max(0, round(new_starts.get(k, s) * 1000 / step) * step)
            out.append((s, text, e, a / 1000, max(round(new_ends.get(k, end) * 1000 / step) * step, a + step) / 1000))
        elif subsync.mover(blocks, s) is not None:   # a run a partial shift keeps, see subsync.keep_blocks(): its own times
            out.append((s, text, e, s, end))
        else:
            a = max(0, at(s))
            out.append((s, text, e, a / 1000, max(at(end), a + step) / 1000))
    if subsync.INVARIANTS:   # the order rule
        subsync.ordered([(s, max(0, at(s)) / 1000, a) for s, _, _, a, _ in out], {"cues": cues, "fix": fix, "blocks": blocks, "ends": ends, "ass": ass, "plan": out})
    plan = Plan(out)
    plan.blocks = blocks
    return plan if any(abs(a - s) + abs(b - e) > 0.0005 for s, _, e, a, b in out) else None


def blocks_moved(plan, fix=None):
    """How the time_plan() plan moves its cues past fix, the whole-track fix it holds, as "82 lines moved -3.00 s", or
    "300 lines moved -0.12 s to -2.98 s" when they move by different times, or None when it moves none. A move under
    0.05 s is the rounding of ASS times. The runs of a partial shift that keep their times against the fix give "12
    cues kept their times", see subsync.keep_blocks()."""
    kept, moves = 0, []
    for s, _, _, a, _ in plan:
        d = a - max(0, subsync.moved(s * 1000, fix) / 1000 if fix else s)
        if abs(d) > 0.05:
            if subsync.mover(plan.blocks, s) is not None:
                kept += 1
            else:
                moves.append(d)
    if not moves:
        return f"{kept} cues kept their times" if kept else None
    lo, hi = min(moves), max(moves)
    return f"{len(moves)} lines moved {lo:+.2f} s" + (f" to {hi:+.2f} s" if hi - lo >= 0.05 else "")


def set_ends(text, ass, plan):
    """The text of a SubRip, ASS or SSA file with the new times of plan in their order. plan is a time_plan(), or a
    flash_plan(), whose starts stay. A SubRip cue pairs with the plan by its place and must have the plan's old start.
    An ASS event pairs by its start, cut to centiseconds as mkvextract writes it, and its text, because mkvextract may
    write the events in another order. A cue must also have the plan's old end, within 2 ms. mkvextract cuts an ASS time to centiseconds, so an ASS end must be the
    old end cut the same way. A start that does not move keeps its text. Raises when a cue finds no pair.

    A block with no BlockDuration has no known end, and timed() makes one up for the plan. mkvextract then writes an
    ASS event that ends at its start, and a SubRip cue that ends at the next cue's start. It leaves out such a SubRip
    cue when it is the last one. Those cues find no pair, so no made-up end goes into the file. prove() refuses
    the rest, see its timed and ended checks."""
    plan = [p if len(p) == 5 else (*p[:3], p[0], p[3]) for p in plan]   # a flash_plan() keeps each start
    lines, fmt = text.split("\n"), lambda x: (lambda v: f"{v // 3600000:02d}:{v // 60000 % 60:02d}:{v // 1000 % 60:02d},{v % 1000:03d}")(round(x * 1000))
    if ass:
        todo, clock = {}, lambda t: sum(float(x) * m for x, m in zip(t.split(":"), (3600, 60, 1)))
        cs = lambda x: (lambda v: f"{v // 360000}:{v // 6000 % 60:02d}:{v // 100 % 60:02d}.{v % 100:02d}")(round(x * 100))
        for s, t, o, a, e in plan:
            todo.setdefault((round(s * 1000) // 10, t), []).append((o, a, e))   # mkvextract cuts a start to centiseconds too
        for k, line in enumerate(lines):
            m = re.match(r"(Dialogue:\s*[^,]*,)([^,]*),([^,]*)((?:,[^,]*){6},(.*))$", line.rstrip("\r"))
            if m:
                twins, end = todo.get((round(clock(m[2]) * 100), m[5])) or [], round(clock(m[3]) * 100)
                n = next((n for n, (o, _, _) in enumerate(twins) if round(o * 1000) // 10 == end), None)
                if n is None:
                    raise RuntimeError(f"the ASS event at {clock(m[2]):.3f} s has no duration in the file, so its end is not known"
                                       if m[2] == m[3] else f"the ASS event at {clock(m[2]):.3f} s ends at {end / 100:.2f} s in the extracted "
                                       f"text, and the plan's at {twins[0][0]:.3f} s" if twins else "an ASS event of the extracted text is not in the plan")
                _, a, e = twins.pop(n)
                start = m[2] if round(a * 100) == round(clock(m[2]) * 100) else cs(a)
                lines[k] = f"{m[1]}{start},{cs(e)}{m[4]}" + ("\r" if line.endswith("\r") else "")
        if any(todo.values()):
            raise RuntimeError("an ASS event of the plan is not in the extracted text")
        return "\n".join(lines)
    k = 0
    for i, line in enumerate(lines):
        m = decide.SRT_TIME.match(line)
        if m:
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")[:3]) / 1000
            if k >= len(plan) or abs(start - plan[k][0]) > 0.0015:
                raise RuntimeError(f"cue {k + 1} of the extracted text starts at {start:.3f} s, and the plan's at {plan[k][0] if k < len(plan) else None}")
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")[:3]) / 1000
            if abs(end - plan[k][2]) > 0.002:
                raise RuntimeError(f"cue {k + 1} of the extracted text ends at {end:.3f} s, and the plan's at {plan[k][2]:.3f} s, so its end is not known")
            head = line[:m.start(5)].rstrip().removesuffix("-->").rstrip() if abs(plan[k][3] - start) < 0.0005 else fmt(plan[k][3])
            lines[i] = f"{head} --> {fmt(plan[k][4])}{g[8]}"
            k += 1
    if k != len(plan):
        raise RuntimeError(f"the extracted text holds {k} cues, and the plan {len(plan)}")
    return "\n".join(lines)


def ended_track(path, j, tid, plan, folder, text=None):
    """The path of a Matroska file in folder that holds only subtitle track tid of path, with the new times of plan, a
    time_plan() or a flash_plan(). mkvextract writes the track's text, set_ends() puts the times in, and mkvmerge reads
    it back. That round trip keeps the packets and the ASS header byte for byte, with two exceptions. HandBrake ends each
    ASS event and its header with a NUL byte and writes no [Events] section, and the round trip drops each NUL and
    adds the section. mkvextract writes each line break inside a SubRip cue as LF, and mkvmerge may store it as CRLF.
    mkvmerge 92 does, and the mkvmerge of a source may store LF. prove() and header_kept() pass only those changes.
    The language goes in here, as ffmpeg copies it. The flags and names come back later with mkvpropedit, see resub().
    text takes the place of plan for a repair, see resub(): the new SubRip text, which needs no mkvextract, or a
    function of the extracted text."""
    p = next(t.get("properties") or {} for t in j.get("tracks") or [] if t.get("id") == tid)
    codec = p.get("codec_id")
    ext = {"S_TEXT/UTF8": "srt", "S_TEXT/ASS": "ass", "S_TEXT/SSA": "ssa"}[codec]
    raw, out = os.path.join(folder, f"{tid}.{ext}"), os.path.join(folder, f"{tid}.mkv")
    if not isinstance(text, str):
        r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvextract", path, "tracks", f"{tid}:{raw}"], capture_output=True, text=True,
                           errors="replace")
        if r.returncode:   # 1 is a warning, and a fix never builds on one
            raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
        with open(raw, encoding="utf-8", newline="") as f:
            text = text(f.read()) if text else set_ends(f.read(), ext != "srt", plan)
    with open(raw, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", "-o", out, "--sub-charset", "0:UTF-8", "--language",
                        f"0:{p.get('language_ietf') or p.get('language') or 'und'}", raw], capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"mkvmerge exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    return out


def repair_name(r):
    """What the repair r of a garbled track writes, see repair_text(): "its text read back as cp950", and "with 1 cue
    from Movie.3.srt" when a sidecar fills cut cues."""
    side = f' with {r["filled"]} cue{"s" if r["filled"] > 1 else ""} from {r["sidecar"]["name"]}' if r.get("sidecar") else ""
    return f'its text read back as {r["codepage"]}{side}'


def repaired(texts, cp, tag, side=None):
    """(the repaired cue texts, the places of the cues that side fills, the count of cues left cut) of the cue texts of a
    garbled SubRip track tagged tag, read back as the codepage cp (docs/design.md, "Garbled subtitle repair"). side,
    the cue texts of a sidecar in the same order, may fill the cues the old muxer cut: there the read-back is the start
    of the sidecar's text, and the next character holds a byte cp1252 leaves undefined. Every other cue of side must
    equal the read-back, as srt_cues() compares text. Else the result is None, because the sidecar is not the source of
    the track: a garbled copy, the track extracted again or an SDH version."""
    if side is not None and len(side) != len(texts):
        return None
    out, filled, left = [], [], 0
    for k, t in enumerate(texts):
        back, cut = decide.read_back(t, cp, tag)
        if side is not None:
            mine, theirs = proof.clean_text(back.removesuffix("\N{REPLACEMENT CHARACTER}")), proof.clean_text(side[k])
            if theirs != mine or cut:
                rest = theirs[len(mine):].lstrip() if theirs.startswith(mine) else ""
                if not (rest and set(rest[0].encode(cp, errors="ignore")) & set(decide.CP1252_HOLES)):
                    return None
                out.append(side[k])
                filled.append(k)
                continue
        left += cut
        out.append(back)
    return out, filled, left


def srt_rewrite(text, fix):
    """text, a SubRip text, with the text of its cues replaced by fix() of their texts, the lines of each joined by a
    line break. The numbers, the times and the lines between the cues stay as they are."""
    parts = re.split(r"(\n[ \t]*\n)", text.replace("\r\n", "\n"))   # the blocks and the blank lines between them
    cues = []
    for i in range(0, len(parts), 2):
        lines = parts[i].split("\n")
        k = next((n for n, line in enumerate(lines[:2]) if decide.SRT_TIME.match(line)), None)
        if k is not None:
            cues.append((i, k))
    for (i, k), t in zip(cues, fix(["\n".join(parts[i].split("\n")[k + 1:]) for i, k in cues])):
        parts[i] = "\n".join(parts[i].split("\n")[:k + 1] + ([t] if t else []))
    return "".join(parts)


def side_blocks(side):
    """srt_blocks() of the sidecar_subs() entry side as it is on disk now, its lines joined by a line break."""
    with open(side["path"], "rb") as f:
        return proof.srt_blocks(proof.sidecar_text(f.read(), side.get("named"))[1], "\n")


def repair_text(r):
    """(the new text for ended_track(), a function of srt_blocks() of the old text that gives the new cues for prove())
    of the repair r of a garbled SubRip track (docs/design.md, "Garbled subtitle repair"). r holds "codepage", which
    reads the track back, see repaired(), and "lang", its tag. With "sidecar", the sidecar_subs() entry, the sidecar
    fills the cut cues. It must still hold "side", the cues the check read, else the remux stops: a sidecar written
    again since then is read again, and never with the charset the check gave it. The new text must pass
    decide.repair_fault(), and at most decide.REPAIR_CUT of all its cues may stay cut. A cue that does not read back
    stops the remux too."""
    side = None
    if r.get("sidecar"):
        now = [list(b) for b in side_blocks(r["sidecar"])]
        if now != [list(b) for b in r["side"]]:
            raise RuntimeError(f'the sidecar {r["sidecar"]["name"]} changed since the check')
        side = [t for _, _, t in now]

    def fix(texts):
        try:
            got = repaired(texts, r["codepage"], r["lang"], side)
        except UnicodeError:
            raise RuntimeError(f'some of its cues do not read back as {r["codepage"]}') from None
        if got is None:
            raise RuntimeError(f'the sidecar {r["sidecar"]["name"]} no longer fits the track')
        why = (f"{got[2]} of {len(texts)} cues were cut short" if got[2] > decide.REPAIR_CUT * len(texts) else None) \
            or decide.repair_fault(got[0], r["lang"])
        if why:
            raise RuntimeError(why)
        return got[0]
    return (lambda text: srt_rewrite(text, fix)), (lambda blocks: [(a, b, t) for (a, b, _), t in zip(blocks, fix([t for _, _, t in blocks]))])


def text_name(path, lang, taken=()):
    """The first free name beside the video path for the bytes of a garbled track tagged lang, see resub():
    <base>.<lang>.garbled.txt, then <base>.<lang>.garbled.1.txt and on. Players skip a .txt file. A name in taken is
    used too."""
    base = os.path.splitext(path)[0]
    for n in itertools.count():
        name = f"{base}.{lang}.garbled{f'.{n}' if n else ''}.txt"
        if name not in taken and not os.path.lexists(name):
            return name


def keep_text(src, path, lang, st):
    """Copy the file src beside the video path under text_name(), with the video's owner and mode less any execute
    bits, and return the new path. It never writes over a file. It reads the copy back and raises when its bytes are
    not those of src."""
    with open(src, "rb") as f:
        data = f.read()
    while True:
        name = text_name(path, lang)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, st.st_mode & 0o666)
            break
        except FileExistsError:   # a file came since text_name() looked
            continue
    with os.fdopen(fd, "wb") as f:
        os.fchmod(fd, st.st_mode & 0o666)   # os.open() applies the umask
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    with contextlib.suppress(PermissionError):   # a NAS share may refuse chown. The mode stays.
        os.chown(name, st.st_uid, st.st_gid)
    with open(name, "rb") as f:
        if f.read() != data:
            raise RuntimeError(f"{os.path.basename(name)} does not hold the bytes of its track")
    return name


def cover_edits(path, j, pics, folder):
    """mkvpropedit arguments that add back the image attachments of path that ffmpeg read as the cover picture streams
    pics, with their name, MIME type, description and UID. mkvextract writes each one into folder first."""
    names = {(x.get("tags") or {}).get("filename") for x in pics}
    want = [a for a in j.get("attachments") or [] if a.get("file_name") in names]
    if not want:
        return []
    files = {a["id"]: os.path.join(folder, f"cover-{a['id']}") for a in want}
    r = subprocess.run(["mkvextract", path, "attachments", *[f"{i}:{f}" for i, f in files.items()]], capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    out = []
    for a in want:
        out += ["--attachment-name", a["file_name"], "--attachment-mime-type", a.get("content_type") or "application/octet-stream"]
        out += ["--attachment-description", a["description"]] if a.get("description") else []
        out += ["--attachment-uid", str(a["properties"]["uid"])] if (a.get("properties") or {}).get("uid") else []
        out += ["--add-attachment", files[a["id"]]]
    return out


# The name of ffmpeg's default decoder of a codec, where it is not the codec's name, see copy_fault(). ffmpeg 7.1 shows
# each in its lines. A copy of a damaged MS-MPEG4 v3 frame in Matroska printed nothing, but a decode prints msmpeg4.
DECODER = {"mp3": "mp3float", "dts": "dca", "av1": "libdav1d", "msmpeg4v3": "msmpeg4"}


def copy_fault(text, streams):
    """The lines of text, the output of an ffmpeg stream copy, that fail it. streams are the ffprobe streams of its
    source. ffmpeg decodes a few frames of each stream while it opens a file, and the decoder of a damaged frame
    prints there, as in "[eac3 @ 0x...] error decoding the audio block". A copy decodes nothing. So a line of the
    decoder of an audio or video stream of streams passes, and a "Last message repeated" line after it too. The
    decoder's name is the codec's name, or its name in DECODER. Every other line fails: a muxer line such as
    "[matroska @ 0x...]", a filter line and a plain error."""
    names = {n for s in streams if s.get("codec_type") in ("audio", "video")
             for n in (s.get("codec_name"), DECODER.get(s.get("codec_name")))}
    bad, passed = [], False
    for line in text.splitlines():
        if line.strip():
            m = re.match(r"\[(\w+) @ 0x[0-9a-f]+\] ", line)
            passed = bool(m and m[1] in names or passed and re.match(r"\s+Last message repeated", line))
            bad += [] if passed else [line]
    return bad


def resub(path, j, st, apply, fixes, drop=(), ends=None, timed=None, recode=None, strip=None):
    """Remux a Matroska file in place with new times for the subtitle tracks in fixes, {mkvmerge track id: fix of
    subsync.timing()}, new cue ends for the text tracks in ends, {mkvmerge track id: the plan of flash_plan()}, new
    times for the text tracks in timed, {mkvmerge track id: time_plan()}, new text for the garbled SubRip tracks in
    recode, {mkvmerge track id: its repair, see repair_text()}, and without the subtitle track ids in drop and in
    strip, in one remux under the caller's exclusive file lock (docs/design.md, "Subtitle match"). Returns (code, result,
    info). result is "subtitles remuxed", "would remux subtitles: ...", "subtitle remux failed: ..." or "subtitle remux
    skipped, ...", and code subtitles_remuxed, would_remux_subtitles, subtitle_remux_failed or subtitle_remux_skipped.

    A track in recode comes from its own input as a track in ends does, with the text of its repair, see repair_text().
    Its times stay. A repair with default_off turns its default flag off in the same remux. The proof holds each cue to
    the repair's text and to the original's times, see prove().

    A track with new ends comes from its own input: mkvextract writes its text, set_ends() puts each new end in, and
    mkvmerge makes a file of that one track. The text, the starts and the ASS header stay byte for byte, but for the
    NUL bytes of a HandBrake ASS track and the line breaks of a SubRip cue, see ended_track(). The proof holds each end
    to the plan. An ASS track whose starts fall between centiseconds goes in as a track in timed, with each start on the
    nearest centisecond, see grid_plan(). Its log and what text stay those of new ends. info["starts_rounded"] counts
    the starts that the grid moved off the time its fix gives, see grid_moved().

    A track in timed comes from its own input the same way, with every start and end of its plan. Its plan holds its
    fix and its flash ends, so it gets no -itsoffset or -itsscale. Its entries in fixes and ends only go to the log and
    the what text, and the what text names the cues its blocks move past that fix, see blocks_moved(). The proof holds
    each start and end to the plan. ended_track() takes SubRip, ASS and SSA only.

    ffmpeg copies every other stream with -copyinkf, and reads each retimed track from a second input of the same file
    with -itsoffset and -itsscale, so its cues move to (time - offset) / rate. A line that ffmpeg prints fails the
    remux, but for the line of a decoder that reads a damaged frame while ffmpeg opens the file, see copy_fault().
    info["warnings"] keeps every line. mkvmerge moves the times of laced AAC
    frames by up to 2 ms in a Matroska to Matroska remux, and the proof refuses that. ffmpeg keeps the times it reads,
    and the proof passes. mkvpropedit then puts back each kept track's UID, BCP 47 tag, name and flags, and the Segment
    UID, which ffmpeg does not keep. The new file must keep every other track and its properties (props_fault()), and
    prove() must show every packet of every kept stream the same, the retimed tracks' times as the fix moves them.

    A garbled SubRip track in strip, {mkvmerge track id: its language tag}, leaves the file, and its bytes go beside the
    video, see keep_text(). The remux's ffmpeg also writes its packets as they are into a SubRip file, and
    proof.packet_text() shows each cue's bytes equal to its packet's. After the proof and the kept original, keep_text()
    copies that file beside the video. A failure after that removes the copy. info["garbled_text"] names each copy.

    It runs like repack(): the temp file in hide_dir, no time limit during the remux, and SIGTERM removes the temp file.
    The original must still be the file st describes. The temp file takes its owner and mode, the original is kept in
    originals_root() for keep_days, and the temp file is renamed over it. A removal needs that kept original, so with
    KEEP_ORIGINALS_DAYS 0 the caller never asks for one. The extracted text, the proof's reads and the cover
    attachments go into a folder under STATE_DIR, never the system temp dir, which is often a small tmpfs."""
    ends, timed, recode, strip = ends or {}, timed or {}, recode or {}, strip or {}
    drop = [*drop, *strip]
    names, written = {}, []
    for i, lang in strip.items():   # the plan's names. keep_text() takes the first free name when it writes.
        names[i] = text_name(path, lang, names.values())
    info = {"old_size": st.st_size, "fixes": {str(i): f for i, f in fixes.items()}, "drop": list(drop), "ends": {str(i): len(e) for i, e in ends.items()},
            **({"timed": {str(i): len(plan) for i, plan in timed.items()}} if timed else {}),
            **({"recode": {str(i): repair_name(r) for i, r in recode.items()}} if recode else {}),
            **({"garbled_text": {str(i): os.path.basename(n) for i, n in names.items()}} if strip else {})}
    what = "; ".join([f'track {i}: {f["offset"]:+.3f} s' + ("" if f["rate"] == "1/1" else f', ratio {f["rate"]}') for i, f in fixes.items()]
                     + [f"track {i}: new ends for {sum(e != o for _, _, o, e in plan)} of {len(plan)} cues" for i, plan in ends.items()]
                     + [f"track {i}: {blocks_moved(plan, fixes.get(i)) or f'new times for {len(plan)} cues'}" for i, plan in timed.items()]
                     + [f"track {i}: {repair_name(r)}" + (", default flag off" if r.get("default_off") else "") for i, r in recode.items()]
                     + [f"remove track {i}" for i in drop if i not in strip]
                     + [f"take out track {i}, its bytes to {os.path.basename(n)}" for i, n in names.items()])
    codec = {t.get("id"): (t.get("properties") or {}).get("codec_id") for t in j.get("tracks") or []}
    grid = {i: x for i, plan in ends.items() if i not in timed and (x := grid_plan(plan, fixes.get(i), codec.get(i)))}
    if grid:   # the starts on the ASS grid, see grid_plan()
        timed = {**timed, **grid}
        info["starts_rounded"] = {str(i): grid_moved(x, fixes.get(i)) for i, x in grid.items()}
    why = repack_skip(path, st)
    if why:
        return "subtitle_remux_skipped", f"subtitle remux skipped, {why}: {what}", info
    if not apply:
        return "would_remux_subtitles", f"would remux subtitles: {what}", info
    tmp, folder = repack_tmp(path), runner.work_dir("resub")
    subs = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]   # the k-th of them is ffprobe's k-th
    sw = Swap(path, st, tmp, info)
    try:
        streams = proof.ff_streams(path)[1]
        text = [x["index"] for x in streams if x.get("codec_type") == "subtitle"]
        if len(text) != len(subs):
            raise RuntimeError(f"ffprobe reads {len(text)} subtitle streams and mkvmerge {len(subs)}")
        moved, gone = {text[subs.index(i)]: f for i, f in fixes.items() if i not in timed}, {text[subs.index(i)] for i in drop}
        ended = {text[subs.index(i)]: ended_track(path, j, i, plan, folder) for i, plan in {**ends, **timed}.items()}
        repairs = {i: repair_text(r) for i, r in recode.items()}   # (the new text for ended_track(), the cues for the proof)
        ended.update({text[subs.index(i)]: ended_track(path, j, i, None, folder, new) for i, (new, _) in repairs.items()})
        j = dict(j, tracks=[dict(t, properties=dict(t.get("properties") or {}, default_track=False)) if (recode.get(t.get("id")) or {}).get("default_off")
                            else t for t in j.get("tracks") or []])   # the new flags, for mkvpropedit and props_fault()
        argv = ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path]
        own = sorted(set(moved) | set(ended))
        for x in own:   # one input per track: -itsoffset holds for a whole input
            f, at = moved.get(x), 0 if x in ended else x   # the stream of this track in its input
            argv += ["-itsoffset", f"{-f['offset']:.3f}", f"-itsscale:{at}", repr(1 / float(fractions.Fraction(f["rate"])))] if f else []
            argv += ["-i", ended.get(x, path)]
        # ffmpeg reads an image attachment as a cover picture stream and would write it back as a video track, so it
        # stays out of the map, and mkvpropedit adds it back below.
        pics = [x for x in streams if (x.get("disposition") or {}).get("attached_pic")]
        source, kept = {x: n for n, x in enumerate(own, 1)}, [x["index"] for x in streams if x["index"] not in gone and x not in pics]
        argv += [a for x in kept for a in ("-map", f"{source.get(x, 0)}:{0 if x in ended else x}")]
        # A cue the fix moves before 0 starts at 0, as srt_moved() does. A negative time would make ffmpeg move every stream.
        # A cue that ends before 0 keeps 1 ms, as in srt_moved(). With a length of 0 the proof refused the remux.
        # -itsscale scales the times and leaves each duration, so the filter scales it too, and a cue ends where the fix moves its end.
        clamp = lambda k: (f"setts=pts=max(PTS\\,0):dts=max(DTS\\,0):duration=if(lt(PTS\\,0)\\,max(1\\,PTS+DURATION*{k})\\,DURATION*{k})")
        argv += [a for n, x in enumerate(kept) if x in moved for a in (f"-bsf:{n}", clamp(repr(1 / float(fractions.Fraction(moved[x]["rate"])))))]
        argv += ["-c", "copy", "-copyinkf", "-default_mode", "passthrough", "-f", "matroska", tmp]
        texts = {i: (os.path.join(folder, f"{i}.garbled.srt"), os.path.join(folder, f"{i}.md5")) for i in strip}   # the same read writes them
        for i, (srt, md5) in texts.items():
            x = text[subs.index(i)]
            argv += ["-map", f"0:{x}", "-c", "copy", "-f", "framemd5", md5, "-map", f"0:{x}", "-c", "copy", "-f", "srt", srt]
        new_tmp(tmp)
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace")
        info["warnings"] = config.mask((r.stdout + r.stderr).strip())[-500:] or None   # the lines copy_fault() passes too
        bad = copy_fault(r.stdout + r.stderr, streams)
        if r.returncode or bad:
            raise RuntimeError(f"ffmpeg exited {r.returncode}: {config.mask(chr(10).join(bad))[-500:] or info['warnings']}")
        for i, (srt, md5) in texts.items():
            if why := proof.packet_text(srt, md5):
                raise RuntimeError(f"the bytes of track {i} were not written whole: {why}")
        uid = ((j.get("container") or {}).get("properties") or {}).get("segment_uid")
        edits = ["--edit", "info", "--set", f"segment-uid=0x{uid}"] if uid else []
        for n, t in enumerate((t for t in j.get("tracks") or [] if t.get("id") not in drop), 1):   # the n-th track of the new file
            p = t.get("properties") or {}
            sets = [f"{name}={int(v) if isinstance(v, bool) else v}" for k, name in PROP_EDITS if (v := p.get(k)) is not None]
            edits += ["--edit", f"track:{n}"] + [a for x in sets for a in ("--set", x)] if sets else []
        edits += cover_edits(path, j, pics, folder)
        e = subprocess.run(["mkvpropedit", tmp, *edits], capture_output=True, text=True, errors="replace")
        if e.returncode > 1:
            raise RuntimeError(f"mkvpropedit exited {e.returncode}: {config.mask((e.stdout + e.stderr).strip())[-300:]}")
        sw.sync()
        new = checks.mkvmerge(tmp)
        fault = props_fault(j, new, drop, {*ends, *timed, *recode})   # the tracks of ended_track()
        if not fault:
            kept = [i for i in subs if i not in drop]
            refused, info["proof"] = proof.prove(path, tmp, [], folder, dropped=gone, absolute=True,
                                                 retimed={kept.index(i): f for i, f in fixes.items() if i not in timed},
                                                 ended={kept.index(i): [e for _, _, _, e in plan] for i, plan in ends.items() if i not in timed},
                                                 timed={kept.index(i): [(a, e) for _, _, _, a, e in plan] for i, plan in timed.items()},
                                                 recoded={kept.index(i): cues for i, (_, cues) in repairs.items()})
            fault = refused and refused[1]
        if fault: raise RuntimeError(fault)
        size = sw.own()
        sw.check("the app replaced or renamed the original during the subtitle remux", config.CFG.keep_days)
        for i, (srt, _) in texts.items():
            written.append(keep_text(srt, path, strip[i], st))
            info["garbled_text"][str(i)] = os.path.basename(written[-1])
        os.replace(tmp, path)
    except BaseException as ex:
        sw.unkeep(path)
        for n in written:
            with contextlib.suppress(OSError):
                os.remove(n)
        if not isinstance(ex, Exception):
            raise
        return "subtitle_remux_failed", config.mask(f"subtitle remux failed{', the original changed' if isinstance(ex, SourceChanged) else ''}: {ex}")[:500], info
    finally:
        sw.close(folder)
    if info.get("kept"):
        vault.prune_originals(vault.originals_root(path))
    info["new_size"] = size
    return "subtitles_remuxed", "subtitles remuxed", info
