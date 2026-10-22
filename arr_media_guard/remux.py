# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The remuxes that replace a Matroska file, the header repair and the subtitle remux, and the swap they share."""
import contextlib, filecmp, fractions, hashlib, itertools, math, mmap, os, re, shutil, signal, subprocess, threading, time

from . import align, checks, config, decide, logs, matroska, proof, runner, subsync, vault


# The track properties a remux must keep, with the Matroska default of each. uid too, except on a track a trim replaced.
KEEP_PROPS = (("language", "und"), ("track_name", None), ("default_track", True), ("forced_track", False), ("enabled_track", True),
              ("flag_original", False), ("flag_commentary", False), ("flag_hearing_impaired", False), ("flag_visual_impaired", False),
              ("flag_text_descriptions", False), ("codec_private_data", None))
# Each flag in KEEP_PROPS: its mkvpropedit property, as an edit names it (decide.prop()), mkvmerge's option for it, its
# mkvmerge -J key and its Matroska default. track_args() sets every flag of a track that a remux writes again.
FLAG_OPTS = (("flag-default", "--default-track-flag", "default_track", True), ("flag-forced", "--forced-display-flag", "forced_track", False),
             ("flag-enabled", "--track-enabled-flag", "enabled_track", True), ("flag-original", "--original-flag", "flag_original", False),
             ("flag-commentary", "--commentary-flag", "flag_commentary", False),
             ("flag-hearing-impaired", "--hearing-impaired-flag", "flag_hearing_impaired", False),
             ("flag-visual-impaired", "--visual-impaired-flag", "flag_visual_impaired", False),
             ("flag-text-descriptions", "--text-descriptions-flag", "flag_text_descriptions", False))
# The option, mkvmerge -J key and default of each property that a remux may set for an edit, see planned_state(). These
# are the five that decide edits, and the ones decide.unapplied() can read back. --language sets the legacy tag and the
# BCP 47 tag, so a language edit and its LANG_IETF edit share one option.
EDIT_OPTS = {p: (opt, key, d) for p, opt, key, d in FLAG_OPTS if p in ("flag-default", decide.FORCED_FLAG, decide.ORIGINAL_FLAG)} | {
    decide.LANG_EDIT: ("--language", "language_ietf", "und"), decide.LANG_IETF: ("--language", "language_ietf", "und")}
# The lines mkvmerge 102 prints for each input of a file when its read meets the zero run that mkvpropedit encloses in
# a Segment, see hp["enclosed"]: where the run starts, the last timestamp it read, and the resync that found nothing.
RESYNC = (re.compile(r"Warning: .*: Error in the Matroska file structure at position (\d+)\. Resyncing to the next level 1 element\."),
          re.compile(r"Warning: The last timestamp processed before the error was encountered was (\d+):(\d+):(\d+(?:\.\d+)?)\."),
          re.compile(r"Warning: Resync failed: no valid Matroska level 1 element found\."))
# The spread in seconds, (low, high), of an audio track's stored block times around an even frame clock that marks
# ffmpeg's estimates of laced frame times, see lace_guess(). Matroska stores whole milliseconds, so true times spread
# under 1 ms: 0.98 ms at most in the real files measured on 2026-10-18. ffmpeg's copies of laced audio spread
# 1.67 ms (DTS) to 2.4 ms (AAC) in their first 500 packets, and its estimates of DTS with no default duration run up to
# 5 ms early. Mixed frame lengths or a gap spread far more (Vorbis 286 ms, a 1 s gap 997 ms) and mark nothing.
LACE_GUESS = (0.0015, 0.010)
# The share of an audio track's steps from block to block, at least, that falls outside floor..ceil of the frame
# length times the frames between the blocks, see lace_guess(). ffmpeg's estimates catch up once per lace: 4.2 to 10
# percent of the steps. True times with one jump of a few milliseconds, or a slow wander of about 1 ms, have 0 to 0.2
# percent (measured 2026-10-18).
LACE_STEPS = 0.03
REFERENCE_BLOCK = 0xFB   # the child of a BlockGroup that names the frame it refers to, so no keyframe (RFC 9559)
KEY_BLOCKS = 300   # the video blocks lace_guess() reads for the first keyframe


# The mkvmerge hacks of resub() and ended_track() (mkvmerge --engage list). By default mkvmerge trims the spaces and
# tabs at the ends of each line of a text subtitle packet, and puts the codec header units in front of each HEVC
# keyframe. Both change packets that resub() copies or ended_track() writes again, and the proof refuses them.
KEEP_PACKETS = ("--engage", "keep_whitespaces_in_text_subtitles", "--engage", "dont_normalize_parameter_sets")
MUX_TICK = 1000000        # ns of mkvmerge's TimestampScale for a file with video, its coarsest. A file of audio alone gets a finer one, see mux_tick().
REFRAMED = ("A_PCM/",)    # audio codecs whose frames mkvmerge cuts again, so a list of frame times cannot time them
RETIMED = ("A_VORBIS",)   # audio codecs whose frame times mkvmerge 102 changes on its own: Vorbis from ffmpeg by up to 2 ms
LACE_RETIMED = ("A_MPEG/L",)   # codecs whose lace frames mkvmerge 102 times wrong with no DefaultDuration, MP2 and MP3 by up to 144 ms.
# It keeps the lace times of AC-3, E-AC-3, DTS and Opus (measured 2026-10-21).


def prop_changed(pa, pb, k, d):
    """Whether a remux changed the property k, Matroska default d, of a track whose mkvmerge -J properties are pa before
    it and pb after it. mkvmerge writes a legacy tag for a track that has only a BCP 47 tag, so eng/it, as such a
    track reads, becomes ita/it. A new legacy tag beside a BCP 47 tag passes when it is the track's language, see
    decide.track_tag()."""
    if k == "language" and pa.get("language_ietf") and decide.track_tag(pb) == (pb.get("language") or "und").lower():
        return False
    return pa.get(k, d) != pb.get(k, d)


def tracks_fault(j, new, edits=(), drop=(), trim=(), ended=()):
    """Why the tracks of the remuxed probe new differ from the planned state, or None. The planned state is the tracks of
    j less the track ids in drop, with edits applied, see planned_state(). Each track keeps in order its type, codec and
    language (see checks.track_list()), its KEEP_PROPS, its UID and its BCP 47 tag. A track in trim, which a trim
    wrote again, may take a new UID. The codec private data of a track must pass header_kept(), and ended holds the
    track ids that ended_track() wrote again. mkvmerge writes a BCP 47 tag for a track that has none, the one
    checks.langs() maps its legacy language to, and that passes. A new legacy tag passes as prop_changed() says. A track
    that a language edit names takes its language from decide.unapplied(), which must show every edit in new. No track
    in drop may keep its UID in new."""
    want, opts = planned_state(j, edits)
    kept, uid = [t for t in want["tracks"] if t.get("id") not in drop], lambda t: (t.get("properties") or {}).get("uid")
    langs, ietf = {k for k, t in enumerate(kept) if "--language" in opts.get(t.get("id"), {})}, checks.langs()[1]
    shown = lambda ts: [(a, b, None if k in langs else c) for k, (a, b, c) in enumerate(checks.track_list({"tracks": ts}))]
    if shown(kept) != shown(new.get("tracks") or []):
        return f"the tracks changed from {checks.track_list({'tracks': kept})} to {checks.track_list(new)}"
    if {uid(t) for t in j.get("tracks") or [] if t.get("id") in drop} & {uid(t) for t in new.get("tracks") or []} - {None}:
        return "a removed track is still in the file"
    for k, (a, b) in enumerate(zip(kept, new["tracks"])):
        pa, pb = a.get("properties") or {}, b.get("properties") or {}
        diff = [x for x, d in KEEP_PROPS + ((("uid", None),) if a.get("id") not in trim else ()) + (("language_ietf", None),)
                if not (k in langs and x in ("language", "language_ietf")) and prop_changed(pa, pb, x, d)
                and not (x == "codec_private_data" and header_kept(pa.get(x), pb.get(x), pa.get("codec_id"), a.get("id") in ended))
                and not (x == "language_ietf" and pa.get(x) is None and pb.get(x) == ietf.get(pa.get("language") or "und"))]
        if diff: return f"track {a.get('id')} changed its {', '.join(diff)}"
    if not edits:
        return None
    ids = track_ids(j)
    sel = {i: s for s, i in track_ids(dict(new, tracks=[dict(b, id=a.get("id")) for a, b in zip(kept, new["tracks"])])).items()}
    missed = decide.unapplied(new, [[sel.get(ids[e[0]]), *e[1:]] for e in edits])   # each edit by its track's selector in new
    return f"the new file does not show the edits {missed}" if missed else None


def header_fault(j, tmp, new, old_size, hp, info, folder, edits=(), src=None):
    """Why the remuxed file tmp may not replace a Matroska original whose header has an issue, or None. j and new are
    their mkvmerge probes, hp is the header stage of check_video() for the original, folder the work folder of the
    remux. The tracks must hold the planned state of j less the tracks in hp["remove"], with edits applied, see
    tracks_fault(). A trimmed track keeps all but its UID, and must hold its events less the ones the trim dropped.
    The rest are the header checks, see header_checks(). src is the original's path."""
    kind = (new.get("container") or {}).get("type")
    if kind != "Matroska": return f"the new file reads as {kind}"
    trim, remove = set(hp.get("trim") or ()), set(hp.get("remove") or ())
    if fault := tracks_fault(j, new, edits, remove, trim): return fault
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("id") not in remove]
    for i in trim:   # one block per event
        n, t = (new["tracks"][ids.index(i)].get("properties") or {}).get("tag_number_of_frames"), info["trimmed"][i]
        if str(n) != str(t["events"] - t["dropped"]): return f"the trimmed track {i} holds {n} events, not {t['events'] - t['dropped']}"
    return header_checks(tmp, new, old_size, hp, info, folder, src)


def header_checks(tmp, new, old_size, hp, info, folder, src=None):
    """Why the header of the remuxed file tmp, whose mkvmerge probe is new, fails the header repair of its original, or
    None. hp is the header stage of check_video() for the original, folder the work folder of the remux. The checks
    compare against the stream ends hp read from the last clusters, never the old header. info gets the new duration,
    the new Cues, the frame count, the windows and the worst lag of each stream. With a still image before the film and
    no PyAV, no window runs, and info["windows_skipped"] says why.

    The streams of the new file must end where the original's end, hp["expect"], within REPAIR_END. Both ends come
    from decide.stream_ends() of the last bytes of each file, see checks.tail_ends(). mkvmerge writes the Segment
    duration from the first block, so the header can differ from the stream end by that time.

    mkvmerge drops a damaged cluster with no warning. So the new video track must hold every video frame the original
    src stores, each at its time, see original_frames() and frames_fault(). Up to FRAME_SLACK frames may go at the end.
    The block walk reads every Cluster of src by its bytes, so a cluster that a demuxer skips still counts, and a frame
    that an encoder left out is no loss. When the walk of src stops before its end, the count of the original is not
    known, and the repair refuses with the walk's reason. info["frame_rate"] gets the share of the steps between the
    new file's frames that are not its usual frame length, see decide.off_rate(), and whether that makes the rate
    variable. The decision line names it, and no check takes it. The times of the new file come
    from proof.stored_places(). No packet may trail the packets before it in the new file by more than LAG_SLACK, see proof.lags().
    The bound holds whatever the original's lag, so a repair must put late packets in place. The size may differ by
    REPACK_SIZE from old_size less the bytes past the Segment end (hp["tail"], hp["edit_tail"]) and less the zero run
    that mkvpropedit enclosed (hp["enclosed_size"]). mkvmerge reads the elements after that run through the SeekHead
    and keeps them, see resynced(). An RFC 9559 fault of the new file that the original holds too fails no repair,
    because mkvmerge wrote it again. info["rfc_left"] then names it, see checks.rfc_again()."""
    info["new_duration"] = round(decide.duration(new), 3)
    scale = (((new.get("container") or {}).get("properties") or {}).get("timestamp_scale") or 1000000)
    entries = {(t.get("properties") or {}).get("number"): t for t in new.get("tracks") or []}
    with open(tmp, "rb") as f:   # the new file holds no bytes past its Segment
        ends = checks.tail_ends(f, os.path.getsize(tmp), {n: ((t.get("properties") or {}).get("default_duration") or 0) / scale for n, t in entries.items()},
                                {n: t.get("type") for n, t in entries.items()})[0]
    info["new_end"] = end = round(max(ends.values()) * scale / 1e9, 3) if ends else None
    if end is None or abs(end - hp["expect"]) > decide.REPAIR_END:
        return f"the streams of the new file end at {end} seconds, but the streams of the original end at {hp['expect']}"
    now = checks.header_probe(tmp, new)
    info["new_cues"] = (now or {}).get("cues")
    if again := checks.rfc_again(now, hp):
        info["rfc_left"] = again
    issue = [x for x in (now or {}).get("issue") or [] if not (again and x.startswith(checks.RFC_ISSUE))]
    if not now or now["cues"] is not True or issue:
        return "the new file still has a header issue: " + "; ".join(issue or [f"no usable Cues, {info['new_cues']}"])
    size = os.path.getsize(tmp)
    try:
        places, v = proof.stored_places(tmp, proof.ff_streams(tmp)[1]), checks.main_stream(tmp)   # each frame's time and place
    except RuntimeError as ex:
        return str(ex)
    times = [None if t != t else t for t in places[v["index"]][0]] if v and v["index"] in places else []   # the film's frames
    p = decide.main_video(new["tracks"])   # the film, never a cover picture
    p = None if p is None else p.get("properties") or {}
    if p is not None:
        info["frames"] = int(p.get("tag_number_of_frames") or 0)
        if p.get("default_duration"):   # the decision line names it, and no check takes it
            off = decide.off_rate(decide.frame_steps(times), scale / 1e9)
            info["frame_rate"] = {"off": None if off is None else round(off, 4), "variable": off is not None and off >= decide.VFR_SHARE}
        old = original_frames(src) if src else "no original to count them in"
        if isinstance(old, str):
            return f"the video frames of the original are not all known, so lost frames cannot be counted: {old}"
        info["original_frames"] = len(old[0])
        if fault := frames_fault(old, times, scale, v.get("codec_name") if v else None):
            return fault
    # The bytes past the Segment end go, once when both counts hold them, and so does the enclosed zero run
    old_size = old_size - max(hp.get("tail") or 0, hp.get("edit_tail") or 0) - (hp.get("enclosed_size") or 0)
    if abs(size - old_size) > config.REPACK_SIZE * old_size: return f"the size changed from {old_size} to {size} bytes"
    video = hp["video"] or 0
    by = {"stream": v["index"]} if video >= 60 and v and v["behind"] else {}   # a still image first: PyAV seeks by the film, see checks.window_stream()
    if by and not checks.lid_ready():   # no PyAV, so no window, as in checks.video_stages()
        info["windows_skipped"], video = checks.STILL_FIRST, 0
    info["windows"] = ws = [checks.window(tmp, video * s, min(decide.VIDEO_SECS, video * (1 - s)), **by) for s in decide.VIDEO_AT] if video >= 60 else []
    certain, doubts = decide.video_verdict([], ws)
    doubts += [decide.stopped_doubt(w) for w in ws if decide.read_capped(w)]   # the new file has Cues, so a read-cap stop is no clean window
    if certain or doubts: return "a video window of the new file is not clean: " + (certain or "; ".join(doubts))
    info["lag"] = lag = {i: round(x, 3) for i, x in proof.lags(places).items()}
    late = max(lag, key=lag.get, default=None)
    if late is not None and lag[late] > proof.LAG_SLACK: return f"stream {late} sits up to {lag[late]:.1f} s behind the other streams in the new file"
    return None


def original_frames(src):
    """(the stored times in seconds of the main video frames of the Matroska file src, in file order, its
    TimestampScale in ns), or why they are not all known, see proof.stored_places(). The walk must read every Cluster
    whole. A Segment size past the end of the file, or a level-1 element after the last Cluster whose size does not
    read, still gives them all: a failed edit leaves such a fault, and the header repair exists to fix it."""
    try:
        streams = proof.ff_streams(src)[1]
        v = checks.main_stream(src)
        got = proof.stored_places(src, streams, whole_ok=True).get(v["index"]) if v else None
    except RuntimeError as ex:
        return str(ex)
    return ([None if t != t else t for t in got[0]], got[3]["scale"]) if got else "the walk finds no video track"


def frames_fault(old, times, scale, codec):
    """Why the video frame times of a new file, times, are not the stored ones of its original, old of
    original_frames(), or None. scale is the TimestampScale of the new file in ns, codec the video's. The new file
    holds every frame of the original in file order, up to FRAME_SLACK frames at the end, and each one at its own
    time, as mkvmerge keeps the times of a Matroska file, see proof.times_fault(). So a frame lost at the start
    refuses too. When the new file holds fewer frames, the reason names a loss only when each frame from the first one
    that differs is a frame of the original some places on, and it names the first lost frame. Else it names the move."""
    if not len(old[0]) - decide.FRAME_SLACK <= len(times) <= len(old[0]):
        return f"the new file holds {len(times)} video frames, and the original stores {len(old[0])}"
    xt, slack = old[0][:len(times)], proof.us(proof.mkv_slack(old[1], scale, "video"))
    why = proof.times_fault("video", codec, xt, times, slack=slack / 1e6, zero=(0, 0), paired=True)[0]
    same = lambda a, b: all(None in (p, q) or abs(proof.us(p) - proof.us(q)) <= slack for p, q in zip(a, b))
    if why and len(times) < len(old[0]):   # the first frame that differs, then how many places on the rest sits
        k = next(i for i, (p, q) in enumerate(zip(xt, times)) if not same([p], [q]))
        n = next((m for m in range(1, len(old[0]) - len(times) + 1) if same(old[0][k + m:], times[k:])), None)
        if n:
            return f"the new file lacks {n} video frame{'s' if n > 1 else ''} the original stores, the first at {old[0][k]:.3f} s"
    return why


def track_args(p):
    """mkvmerge options that give track 0 of the next file the language, BCP 47 tag, name and flags of the mkvmerge
    -J properties p."""
    args = ["--language", f"0:{p.get('language_ietf') or p.get('language') or 'und'}"]
    args += ["--track-name", f"0:{p['track_name']}"] if p.get("track_name") else []
    return args + [a for _, opt, key, default in FLAG_OPTS for a in (opt, f"0:{int(bool(p.get(key, default)))}")]


def track_ids(j):
    """{the mkvpropedit selector that decide.classify() gives each audio and subtitle track of j: its track id}."""
    return dict(zip((t["sel"] for t in decide.classify(j)),
                    (t.get("id") for t in j.get("tracks") or [] if t.get("properties") is not None and t.get("type") in ("audio", "subtitles"))))


def planned_state(j, edits):
    """(j with edits applied to the mkvmerge -J properties of its tracks, {track id: {mkvmerge option: value}} of the
    edited properties). edits is the edit list of a decide plan, [selector, new, old] with a property as a fourth item,
    see decide.prop(). The first is the planned state of a remux that carries edits, see tracks_fault(). A language
    edit sets language_ietf, which track_args() writes. A new value of None takes the Matroska default. Raises
    RuntimeError when an edit names no track of j, or a property that EDIT_OPTS leaves out."""
    ids = track_ids(j) if edits else {}
    props, opts = {t.get("id"): dict(t.get("properties") or {}) for t in j.get("tracks") or []}, {}
    for e in edits:
        p = decide.prop(e)
        if e[0] not in ids or p not in EDIT_OPTS:
            raise RuntimeError(f"a remux cannot set {p} of {e[0]}")
        i, (opt, key, default) = ids[e[0]], EDIT_OPTS[p]
        v = props[i][key] = default if e[1] is None else bool(e[1]) if p.startswith("flag-") else e[1]
        opts.setdefault(i, {})[opt] = int(v) if p.startswith("flag-") else v or ""
    return dict(j, tracks=[dict(t, properties=props[t.get("id")]) for t in j.get("tracks") or []]), opts


def compression_args(tracks, tid=None):
    """mkvmerge options that keep each of tracks, mkvmerge -J tracks, uncompressed when its source holds it so.
    mkvmerge compresses PGS and VobSub tracks with zlib by default, and some hardware players read none. tid gives a
    track's id in its input, its own id by default. A track the source compresses keeps mkvmerge's default."""
    return [a for t in tracks if not (t.get("properties") or {}).get("content_encoding_algorithms")
            for a in ("--compression", f"{t.get('id') if tid is None else tid}:none")]


def trim_inputs(path, j, hp, folder):
    """The SubRip files in folder that put back each track in hp["trim"], extracted with mkvextract and cut at
    hp["streams"] by decide.trim_srt(). merge_argv() gives each its language, name and flags. The tracks in hp["remove"]
    are extracted and counted too, and never put back. Returns ({trimmed track id: its file}, {trimmed track id: the
    counts of trim_srt()}, {removed track id: its language, name, codec, lines and late lines}). The rules:
    every line that runs past the real end ends there, and a track with REMOVE_SHARE of its lines starting after the end
    is timed for another cut and goes. trim_srt() counts again, and a track that falls on the other side of the rule
    than the plan stops the remux."""
    trim, remove, tracks = hp.get("trim") or [], hp.get("remove") or [], {t.get("id"): t for t in j.get("tracks") or []}
    out = {i: os.path.join(folder, f"{i}.srt") for i in trim + remove}
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvextract", path, "tracks", *[f"{i}:{f}" for i, f in out.items()]],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:   # 1 is a warning, and a trim never builds on one
        raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    files, counts, removed = {}, {}, {}
    for i, f in out.items():
        with open(f, encoding="utf-8") as fh:
            text, n = decide.trim_srt(fh.read(), hp["streams"])
        far = hp.get("listed") is None or hp["subtitles"][i] > decide.REMOVE_END * hp["listed"] * 60   # see subtitle_plan()
        if (decide.remove_track(n["events"], n["dropped"]) and far) != (i in remove):
            raise RuntimeError(f"subtitle track {i} has {n['dropped']} of {n['events']} lines starting after the end, so the plan to "
                               f"{'remove' if i in remove else 'trim'} it no longer holds")
        p = tracks[i].get("properties") or {}
        if i in remove:
            removed[i] = {"language": decide.track_tag(p), "name": p.get("track_name"), "codec": tracks[i].get("codec"), "lines": n["events"],
                          "late": n["dropped"]}
            continue
        counts[i], files[i] = n, f
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
    return files, counts, removed


def merge_argv(path, j, tmp, ids, copied=(), own=None, edits=(), lace=False, head=()):
    """The mkvmerge argv of a remux of path, which j probes, into tmp, see repack() and resub(). ids are the track ids
    of j that the new file keeps, in their order, and --track-order keeps it. Input 0 is path, with the chapters, the
    tags and the attachments. Input 1 is path again with only the subtitle tracks in copied, so mkvmerge reads them on
    their own and writes them by time. own {track id: file} gives each track that comes from its own file, track 0 of
    it, with the language, name and flags of its planned properties, see track_args(). A SubRip file is read as UTF-8.
    edits, the edit list of a decide plan, become track options of the tracks of input 0 and 1, see planned_state(). A
    track of own gets no such option. An edit of a track that ids leave out raises RuntimeError.
    Each track that the source holds uncompressed stays so, see compression_args(). KEEP_PACKETS keeps the packets of
    text subtitles and HEVC byte for byte. Without lace, each audio frame goes in its own block, as convert_cmd() does.
    The Segment UID stays, as an ordered chapter of another file may point at it. head goes before the inputs, so a
    track option there, as the --timestamps of planned_args(), applies to input 0.
    mkvmerge prints its messages in English in any locale, so resynced() reads them. --ui-language en_US needs no
    locale file, and it leaves the charset of the paths to the locale."""
    own, tracks = own or {}, {t.get("id"): t for t in j.get("tracks") or []}
    want, opts = planned_state(j, edits)
    if gone := sorted(i for i in opts if i not in ids):
        raise RuntimeError(f"an edit names track {', '.join(map(str, gone))}, which the remux removes")
    props = {t.get("id"): t.get("properties") or {} for t in want["tracks"]}
    sets = lambda i: [a for opt, v in opts.get(i, {}).items() for a in (opt, f"{i}:{v}")]
    zero = [i for i in ids if i not in copied and i not in own]   # the tracks of input 0
    leave = []   # the tracks input 0 leaves out, by type
    for kind, some, every in (("video", "-d", "-D"), ("audio", "-a", "-A"), ("subtitles", "-s", "-S")):
        out = [i for i, t in tracks.items() if t.get("type") == kind and i not in zero]
        leave += [] if not out else [every] if len(out) == sum(t.get("type") == kind for t in tracks.values()) else [some, "!" + ",".join(map(str, out))]
    uid = ((j.get("container") or {}).get("properties") or {}).get("segment_uid")
    first = 1 + bool(copied)   # the input of the first file of one track
    argv = ["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", "--ui-language", "en_US", *([] if lace else ["--disable-lacing"]), *KEEP_PACKETS,
            "-o", tmp,
            *(["--segment-uid", uid] if uid else []), *head,
            *(["--track-order", ",".join(f"{list(own).index(i) + first}:0" if i in own else f"{int(i in copied)}:{i}" for i in ids)] if ids else []),
            *compression_args(tracks[i] for i in zero), *[a for i in zero for a in sets(i)], *leave, path]
    if copied:
        argv += ["-D", "-A", "-B", "-M", "--no-chapters", "--no-global-tags", "-s", ",".join(map(str, copied)),
                 *compression_args(tracks[i] for i in copied), *[a for i in copied for a in sets(i)], path]
    for i, f in own.items():   # the track alone, never the chapters or tags of its file
        argv += [*(["--sub-charset", "0:UTF-8"] if f.endswith(".srt") else []), *track_args(props[i]),
                 *compression_args([{"properties": props[i]}], 0), "--no-chapters", "--no-global-tags", f]
    return argv


def planned_times(t, scale):
    """The time in ns of each frame of the audio track t of matroska.frames(), in file order, as a remux writes it, or
    None (docs/design.md, "Planned audio times"). scale is the TimestampScale of t's file in ns.

    Each frame keeps its stored, default or bitstream time. Where a block starts before the block before it ends
    (t["lapped"]), the frames from that block on play on. Each one then starts one frame (t["frame"]) after the frame
    before it. The run goes on while that moves a frame later by more than the rounding of the two files. The move is
    proof.CLOSE_MOVE less half a MUX_TICK at most, so the new file's rounding of a time off its grid keeps the move
    within CLOSE_MOVE. A frame whose own time comes later ends the run, so a gap of the original stays. The rounding is
    mkv_slack() plus half a MUX_TICK. The proof takes a smaller move for no move, and a frame after it would then follow
    no overlap. A frame that shares its time with the frame before it goes on with the run as any frame does. The walk
    finds its block in t["lapped"], so it starts a run too. A frame past the cap keeps its time. So the plan steps back
    in time only where it keeps an overlap of the original that a move of CLOSE_MOVE does not close. None when a time
    lies before 0, as matroska.NONE marks a time that is not known, when a frame holds no data, or when the codec is in
    REFRAMED."""
    if t["codec"].startswith(REFRAMED) or 0 in t["full"]:
        return None
    least, most, step = proof.mkv_slack(scale, MUX_TICK, "audio") * 1e9 + MUX_TICK / 2, proof.CLOSE_MOVE * 1e9 - MUX_TICK / 2, t["frame"]
    lapped, out, run = set(t["lapped"]), [], False
    for x in t["time"]:
        if out and step and (run or x in lapped):   # the proof finds an overlapping block by its time too
            run = least < out[-1] + step - x <= most
        out.append(out[-1] + step if run else x)
    out = [round(x) for x in out]
    return out if min(out, default=0) >= 0 else None


def mux_tick(j, ids):
    """The TimestampScale in ns of a new file that mkvmerge 102 writes from the tracks ids of j, the mkvmerge -J probe of
    its input, or None where a video track stays. mkvmerge then takes MUX_TICK. For a file with audio and no video,
    mkvmerge takes one sample of the highest audio rate, less 1 ns, cut to whole ns: 20832 ns at 48 kHz. It reads the
    rate of some codecs, such as AAC and FLAC, from the bitstream, and j shows the rate that the file stores. So
    planned_args() gives mkvmerge this tick as --timestamp-scale. A file with no audio rate in j gets MUX_TICK.
    mkvmerge takes a TimestampScale from 1000 to 10000000 ns."""
    out = [t for t in j.get("tracks") or [] if t.get("id") in ids]
    if any(t.get("type") == "video" for t in out):
        return None
    rate = max(((t.get("properties") or {}).get("audio_sampling_frequency") or 0 for t in out if t.get("type") == "audio"), default=0)
    return min(max(int(1e9 / rate - 1), 1000), 10000000) if rate else MUX_TICK


def planned_args(path, j, ids, folder, lag=None):
    """(the mkvmerge options of the input path that give its audio tracks their planned_times(), {the place of each
    such track in the new file, from 1: (its DefaultDuration in ns or None, its planned times)}). j is the mkvmerge -J
    probe of path, and ids holds the track ids of j in their order in the new file. mkvmerge 102 times an audio frame
    on its own where its time is not later than the time before it, as at an overlap of the blocks, and for a codec in
    RETIMED. It writes wrong times for the frames of a lace with no DefaultDuration of a codec in LACE_RETIMED. So a
    track gets a plan when the walk finds an overlapping block, a bitstream time of such a codec or a codec in RETIMED.
    mkvmerge keeps the times of every other track, an Opus lace too, and the remux then needs no mkvpropedit.
    The walk finds overlaps and bitstream times on audio tracks only. Each plan goes to a v4 timestamps file in folder, one line per frame in file order, which mkvmerge keeps in
    that order. A walk of path that fails gives no plan.

    proof.times_fault() checks each plan against the walk, as prove() pairs the new file with the original. The check
    rounds each planned time half up to the tick of the new file, as mkvmerge rounds it. That tick is MUX_TICK where a
    video track stays. Otherwise it is mux_tick(), which the options give mkvmerge. A track whose plan the proof would
    refuse keeps the time of each frame instead. So no writer writes a plan that its own proof refuses, and the header
    repair, which runs no proof, writes none either. lag is a time in seconds or None. Where a plan keeps an overlap of
    the original's blocks, a frame may drop at most lag below the latest planned time before it. The track would sit
    that far behind the other streams in the new file, and header_checks() refuses a lag over LAG_SLACK. A larger drop
    raises RuntimeError, which names the original's overlap."""
    try:
        r = matroska.frames(path)
    except (OSError, ValueError):
        return [], {}
    tracks = {t["number"]: t for t in r["tracks"]} if not r["error"] else {}
    args, plans, tick = [], {}, mux_tick(j, ids)
    step = tick or MUX_TICK
    for x in j.get("tracks") or []:
        t = tracks.get((x.get("properties") or {}).get("number"))
        if x.get("id") not in ids or not t or not (t["lapped"] or t["codec"].startswith(LACE_RETIMED) and matroska.BITSTREAM in t["tag"]
                                                    or t["codec"] in RETIMED):   # audio only
            continue
        if (plan := planned_times(t, r["scale"])) is None:
            continue
        if plan != (kept := list(t["time"])) and proof.times_fault(
                "audio", t["codec"], [v / 1e9 for v in kept], [(v + step // 2) // step * step / 1e9 for v in plan], (0.0, 0.0),
                proof.mkv_slack(r["scale"], step, "audio"), paired=True, length=t["frame"] and t["frame"] / 1e9, lapped=[v / 1e9 for v in t["lapped"]])[0]:
            plan = kept
        n, worst, first = t["overlaps"]
        if lag is not None and n and (drop := max(top - v for top, v in zip(itertools.accumulate(plan, max), plan))) > lag * 1e9:
            raise RuntimeError(f"audio track {x['id']} of the original stores {n} block{'s' if n > 1 else ''} that start{'' if n > 1 else 's'} before "
                               f"the block before {'them' if n > 1 else 'it'} ends, up to {worst / 1e6:.0f} ms, the first at {first / 1e9:.3f} s. "
                               f"The new file would keep that overlap, and the audio would sit up to {drop / 1e9:.1f} s behind the other streams")
        f = os.path.join(folder, f"times-{x['id']}.txt")
        with open(f, "w") as fh:
            fh.write("# timestamp format v4\n" + "".join(f"{v // 1000000}.{v % 1000000:06d}\n" for v in plan))   # ms, exact to the ns
        args += ["--timestamps", f"{x['id']}:{f}"]
        plans[ids.index(x["id"]) + 1] = (t["dd"], plan)
    return [*(["--timestamp-scale", str(tick)] if args and tick else []), *args], plans


def plan_fault(tmp, plans):
    """Why the remux tmp does not hold each audio track of plans, see planned_args(), at its planned times and with the
    original's DefaultDuration, or None. mkvmerge sets the DefaultDuration of a track from the steps of its timestamps
    file, 43 ms for frames of 42.667 ms whose blocks store whole ms. So mkvpropedit first gives each track whose
    DefaultDuration changed the original's again, or removes it where the original holds none. That edit changes a few
    bytes of Tracks in place, and the layout check of the writer reads tmp after it. The block walk of tmp then reads
    each planned time back, within half a tick of tmp's TimestampScale, as mkvmerge rounds it, and 1 ns for the read of
    the file's lines. A walk that stops early holds fewer frames, and a walk that raises fails the remux."""
    if not plans:
        return None
    have = dict(enumerate(((t.get("properties") or {}).get("default_duration") for t in checks.mkvmerge(tmp).get("tracks") or []), 1))
    edits = [a for n, (dd, _) in plans.items() if have.get(n) != dd
             for a in ("--edit", f"track:{n}", *(("--set", f"default-duration={dd}") if dd else ("--delete", "default-duration")))]
    e = subprocess.run(["mkvpropedit", tmp, *edits], capture_output=True, text=True, errors="replace") if edits else None
    if e and e.returncode not in (0, 1):   # a signal gives a negative code
        return f"mkvpropedit exited {e.returncode}: {config.mask((e.stdout + e.stderr).strip())[-300:]}"
    r = matroska.frames(tmp)
    for n, (dd, plan) in plans.items():
        t = r["tracks"][n - 1]   # mkvpropedit found it
        if t["type"] != 2 or t["dd"] != dd:
            return f"track {n} of the new file is not the planned audio track, or its DefaultDuration is not the original's"
        got, half = list(t["time"]), r["scale"] // 2 + 1
        if len(got) != len(plan) or any(abs(a - b) > half for a, b in zip(plan, got)):
            k = next((k for k, (a, b) in enumerate(zip(plan, got)) if abs(a - b) > half), min(len(plan), len(got)))
            at = f", at {plan[k] / 1e9:.3f} s," if k < len(plan) else ""
            return f"audio track {n} of the new file holds {len(got)} frames, the plan {len(plan)}, and frame {k + 1}{at} is the first at another time"
    return None


def resynced(text, hp):
    """Whether text, all that mkvmerge printed in a remux of a file, is only its resync at the zero run that mkvpropedit
    encloses in a Segment (docs/design.md, "Header repair"). That needs hp["enclosed"], where hp found the run, and
    the three RESYNC lines for each input of the file. The first names a position at or after the run. The last
    timestamp read must lie within REPAIR_END of hp["streams"], so the read stopped at the end of the streams."""
    lines, at, end = text.splitlines(), hp.get("enclosed"), hp.get("streams")
    if at is None or end is None or not lines or len(lines) % len(RESYNC):
        return False
    for k in range(0, len(lines), len(RESYNC)):
        m = [rx.fullmatch(x.strip()) for rx, x in zip(RESYNC, lines[k:k + len(RESYNC)])]
        if not all(m) or int(m[0][1]) < at or abs(int(m[1][1]) * 3600 + int(m[1][2]) * 60 + float(m[1][3]) - end) > decide.REPAIR_END:
            return False
    return True


def lace_guess(path, j, n=500):
    """(the mkvmerge ids of the audio tracks of path whose block times hold ffmpeg's estimates of laced frame times,
    why mkvmerge must not lace them again or None). ffmpeg copies a lace frame by frame, each frame in its own block at
    the time it estimated from floored durations. The walk of the first Clusters gives the stored time of each block
    among the first n frames of each audio track, see matroska.head() and lace_read(). The whole walk serves when the
    file has no SeekHead entry for a Tracks after the first Cluster. The frames of one block share its byte, so each
    block counts once, at the number of frames before it. For AAC, AC-3, E-AC-3, DTS and MPEG audio, the frame clock is
    the length the codec defines. mkvmerge 102 writes that length in the DefaultDuration of a relace. Another codec
    takes the track's DefaultDuration, else its codec length. Two signs together mark the estimates. The block times
    spread around the clock within LACE_GUESS. At least LACE_STEPS of the steps from block to block fall outside
    floor..ceil of the frame length times the frames between the blocks. The bound counts in ticks of the
    TimestampScale. ffmpeg's estimates catch up once per lace, so they make such steps. A track with no defined frame length marks nothing. When any audio track starts before the first
    video keyframe, a lace there would cost audio at a seek to 0, so the estimates stay. The keyframe flag is the one
    each video block stores. A track plays each block at its time less the track's CodecDelay (RFC 9559), so the
    start of each track counts that way. A walk that fails finds none, and a video keyframe that does not read keeps
    the estimates. The k-th audio or video TrackEntry is mkvmerge's k-th track of that type."""
    kinds = {2: "audio", 1: "video"}
    own = {k: [t for t in j.get("tracks") or [] if t.get("type") == kind] for k, kind in kinds.items()}
    delay = lambda t: (t.get("properties") or {}).get("codec_delay") or 0   # ns
    ids = [t.get("id") for t in own[2]]
    try:
        r = matroska.head(path, lambda b, entries, blocks: lace_read(b, entries, blocks, n)) or matroska.frames(path)
    except (OSError, ValueError):
        return [], None
    audio, scale, out = [t for t in r["tracks"] if t["type"] == 2], r["scale"], []
    for i, t in zip(ids, audio):
        size = t["codec_frame"] or t["frame"]
        b = [(k, t["time"][k]) for k in range(min(n, len(t["time"]))) if (not k or t["place"][k] != t["place"][k - 1]) and t["time"][k] != matroska.NONE]
        if not size or len(b) < 20:
            continue
        res = [x - k * size for k, x in b]
        odd = sum(not math.floor(size * (k2 - k1) / scale) <= (t2 - t1) // scale <= math.ceil(size * (k2 - k1) / scale) for (k1, t1), (k2, t2) in zip(b, b[1:]))
        if LACE_GUESS[0] * 1e9 < max(res) - min(res) <= LACE_GUESS[1] * 1e9 and odd >= LACE_STEPS * (len(b) - 1):
            out.append(i)
    video = next((t for t in r["tracks"] if t["type"] == 1), None)
    if not out or not video:
        return out, None
    held = f"audio stream {', '.join(map(str, out))} keeps ffmpeg's estimated times"
    key = keyframe(path, video, KEY_BLOCKS)
    if key is None:
        return out, f"{held}, as the first video keyframe did not read"
    starts = [t["time"][0] - delay(x) for x, t in zip(own[2], audio) if len(t["time"]) and t["time"][0] != matroska.NONE]
    lead = round((key - delay((own[1] or [{}])[0]) - min(starts)) / 1e9, 3)
    return out, f"{held}, as audio starts {lead * 1000:.0f} ms before the first video keyframe" if lead > 0 else None


def lace_read(b, entries, blocks, n):
    """Whether blocks, the blocks of a walk so far in the bytes b of a file, see matroska.head(), hold all that
    lace_guess() reads: n frames of each audio track of entries, and of the first video track a keyframe or KEY_BLOCKS
    blocks, see keyblock()."""
    video = next((t["number"] for t in entries if t["type"] == 1), None)
    got = blocks.get(video, ())
    return all(sum(x[1] for x in blocks.get(t["number"], ())) >= n for t in entries if t["type"] == 2) and (
        video is None or len(got) >= KEY_BLOCKS or any(keyblock(b, x[2]) for x in got))


def keyframe(path, t, n):
    """The stored time in ns of the first keyframe among the first n blocks of the track t of matroska.frames(), or
    None, see keyblock()."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as b:
            return next((t["time"][k] for k in [k for k in range(len(t["place"])) if not k or t["place"][k] != t["place"][k - 1]][:n]
                         if keyblock(b, t["place"][k])), None)
    except (OSError, ValueError):
        return None


def keyblock(b, q):
    """Whether the block at byte q of b holds a keyframe. A SimpleBlock stores the keyframe flag (0x80) in its flags,
    and a BlockGroup is a keyframe when it holds no ReferenceBlock (RFC 9559 10.2)."""
    e = decide.element(b, q)
    if not e or e[2] is None:
        return False
    tn = 9 - b[e[1]].bit_length() if e[1] < len(b) else 9   # the bytes of the track number, then 2 of time and the flags
    return bool(e[0] == decide.SIMPLEBLOCK and tn <= 8 and e[2] > tn + 2 and b[e[1] + tn + 2] & 0x80 or
                e[0] == decide.BLOCKGROUP and all(i != REFERENCE_BLOCK for i, _, _ in decide.children(b, e[1], e[1] + e[2])))


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
    file and hands the stop on, see Swap.stop(). It records the signal first, because CPython drops the raise when it
    lands in Popen.__del__ after a remux. Swap.sync() and Swap.check() raise it again, see runner.raise_term()."""
    runner.STOP["term"] = signum
    runner.raise_term()


def repack_tmp(path):
    """The temp file of a repack, in hide_dir beside the file. The apps' disk scan and Plex skip a hidden folder. A hidden
    file beside the video is not enough: the scan takes it as an extra of the item, and a conversion then hides it with
    the extras. The name is cut to the share's 255-byte limit."""
    name = os.fsencode(os.path.basename(path))[:255 - len(b"." + config.REPACK_TMP)]
    return os.path.join(os.path.dirname(path), config.CFG.hide_dir, os.fsdecode(b"." + name + config.REPACK_TMP))


def new_tmp(tmp, st, excl=False):
    """Create the empty temp file tmp in its hidden folder, before the remux writes it. Another conversion in the same
    folder removes that folder once it is empty, so the file holds it, and a create that finds no folder tries again.
    With excl a tmp that is there raises FileExistsError, else the create empties it. tmp gets the mode of st, the
    file it replaces, less the umask, so a share that refuses chmod still gets that mode, see own_like(). The owner
    may always write it, so a read-only original still remuxes."""
    for n in range(3):
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            return os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if excl else os.O_TRUNC), st.st_mode & 0o666 | 0o600))
        except FileNotFoundError:
            if n == 2:
                raise


def own_like(path, st):
    """Give path the owner and mode of st, the file it replaces, and return what the share refused. A NAS share may
    refuse chown with EPERM, or with EINVAL when the owner has no id in the process's user namespace or the NFSv4 id
    map. A share that gives each new file its own owner refuses chmod with EPERM too. path then keeps its create
    mode, and a mode that loses a read bit of st raises PermissionError, so the swap stops and the original stays."""
    why = []
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except OSError as ex:
        why.append(f"chown refused: {ex}")
    try:
        os.chmod(path, st.st_mode & 0o7777)
    except PermissionError as ex:
        mode = os.stat(path).st_mode & 0o7777
        # ponytail: compares mode bits only. An ACL share decides access by its ACL, which the bits may not show.
        if st.st_mode & 0o444 & ~mode:
            raise PermissionError(f"chmod refused, and the new file's mode {mode:o} cannot be read where the "
                                  f"original's mode {st.st_mode & 0o7777:o} can: {ex}") from None
        why.append(f"chmod refused, mode {mode:o}: {ex}")
    return why


def drop_tmp(tmp):
    """Remove the temp file tmp, and its hidden folder when that is empty."""
    with contextlib.suppress(OSError):
        os.remove(tmp)
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(tmp))


class Swap:
    """The steps repack(), resub() and convert() share to put tmp, a remux of path in hide_dir, in place of path, the
    file st describes. Each caller runs them in its own order. start() comes first, sync() before its checks read tmp,
    own() and check() after them, then the rename. unkeep() and stop() run on a failure and close() at the end. Only the
    main thread sets the SIGTERM and SIGINT handlers. A conversion worker of a backfill runs in another thread and sets
    none. A process that ignores SIGINT keeps it ignored, as a backfill started with & from a script does."""

    def __init__(self, path, st, tmp, info):
        """End the time limit, so no step of the remux, its proof or the swap stops at it. SIGTERM and Ctrl+C raise
        through stopped(), so the caller removes the temp file and calls stop(). process() starts a new time limit on
        info["warnings"]."""
        self.path, self.st, self.tmp, self.info = path, st, tmp, info
        main = threading.current_thread() is threading.main_thread()
        config.DEADLINE.stop()
        info["warnings"] = None
        self.term = {s: signal.signal(s, stopped) for s in (signal.SIGTERM, signal.SIGINT)
                     if s == signal.SIGTERM or signal.getsignal(s) != signal.SIG_IGN} if main else {}

    def start(self):
        """Refuse the remux once a stop was asked, see runner.refuse(). The caller calls it first in its try, so stop()
        gives its result for "the run was stopped", and nothing is written."""
        runner.refuse()

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
        """Give tmp the owner and mode of the original and return its size. When a NAS share refuses either,
        info["owner"] says so, see own_like()."""
        st = self.st
        if why := own_like(self.tmp, st):
            new = os.stat(self.tmp)
            self.info["owner"] = {"from": f"{st.st_uid}:{st.st_gid}", "to": f"{new.st_uid}:{new.st_gid}", "why": ", ".join(why)}
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
        """Drop info["kept"] after a failure when one of paths still holds the original. The rename then never ran. The
        keep goes only when this write made it. A keep that an earlier write made stays, see vault.keep_original()."""
        with contextlib.suppress(OSError):
            if self.info.get("kept") and self.st.st_ino in {os.stat(p).st_ino for p in paths if os.path.lexists(p)}:
                if self.info.get("kept_how") != vault.KEPT_BEFORE:
                    os.remove(self.info["kept"])
                del self.info["kept"]

    def release(self):
        """Put the SIGTERM and SIGINT handlers of the caller back."""
        for s, h in self.term.items():
            signal.signal(s, h)

    def stop(self, ex):
        """Whether ex, the exception that ended the remux, is a stop that the caller's own handler took. An Exception
        is a failure of the remux, and gives False, unless stopped() recorded a stop. mkvmerge then died of the same
        Ctrl+C, and CPython dropped the raise. For a stop, put the caller's handlers back and hand the stop to them, see
        runner.pass_stop(). The caller then returns its result for "the run was stopped", and info["stopped"] and
        runner.STOP["skipped"] are True. A backfill ends after the file, and a job process that has written ends its run
        with its decision line. A job process that has not written raises in job_term(), and its job goes back to the
        queue. A stop that start() refused counts as taken. A process with no handler of its own, such as
        --check, gets the stop raised again."""
        if isinstance(ex, Exception) and not runner.STOP["term"]:
            return False
        self.release()
        if not (runner.pass_stop() or runner.STOP["asked"]):
            runner.raise_term()
            raise ex
        self.info["stopped"] = runner.STOP["skipped"] = True
        return True

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


def repack(path, j, st, apply, hp, edits=()):
    """Remux a Matroska file in place for a header repair (docs/design.md, "Header repair"), under the caller's file lock. hp is
    the header stage of check_video(). The remux writes the Segment duration, the Cues and the Segment size from the
    real streams. When hp lists SubRip tracks to trim or remove, trim_inputs() cuts their late lines first. edits, the
    edit list of a decide plan, go into the same remux as mkvmerge track options, see merge_argv(). Returns
    (code, result, info). result is "header repaired", "subtitles trimmed", "subtitles removed" or "tail removed", "would
    repair header: ...", "header repair failed: ..." or "header repair skipped, ...", and code its reason code, as
    header_repaired or header_repair_skipped. info holds the container, the old and new size, the old and new track
    lists, mkvmerge's warnings, the kept original, the old and new duration, the stream end, the new Cues, the frame
    count, the trim counts and the windows of its check. "tail removed" is a header repair of a file with bytes past the
    Segment end, hp["tail"] or hp["edit_tail"], see header_probe(). header_fault() then compares with the Segment size.
    A file that is not Matroska goes to convert().

    mkvmerge writes repack_tmp() in hide_dir beside the original at nice 19 and idle I/O, with no time limit, so a slow
    NAS never stops a remux halfway. --track-order keeps the tracks in their order. By default mkvmerge puts video
    first, and a file may hold subtitles, audio, video in that order. The track UIDs and the Segment UID stay. mkvmerge
    reads one input in file order, so a subtitle packet that sits minutes late in the file stays late, and a player
    never shows it. The ffmpeg subtitle remux of 1.4.0 to 2.17.0 left such files. So the subtitle tracks come from a
    second input of the same file, which mkvmerge reads on its own and writes by time, see merge_argv().
    --disable-lacing writes each audio frame in its own block, as
    convert_cmd() does. A player that seeks to 0 skips each block before the first video keyframe, and a lace there
    holds up to 170 ms of audio. A file whose audio holds ffmpeg's estimates of laced frame times, see lace_guess(),
    is laced again, so each lace start takes the true time of its first frame, as before ffmpeg's copy. info["relaced"]
    lists those streams, and the dry run's result names them. When an audio track starts before the first video
    keyframe, the estimates stay, and info["relace_skipped"] says why. Any mkvmerge warning is a fault: it resyncs past
    a damaged cluster and drops it. The one exception is the resync at the zero run that mkvpropedit encloses in a
    Segment, hp["enclosed"], see resynced(). info["warnings"] keeps what mkvmerge printed. The
    temp file is synced, and header_fault() compares it to the original and to the planned state of edits. The original must still be the file st
    describes, else the app replaced it meanwhile (repack_source_changed). On any fault the temp file goes and the
    original stays. Otherwise the temp file takes the original's owner and mode. The original is hard-linked into
    originals_root() for keep_days, then the temp file is renamed over it. Any exception removes the temp file: Ctrl+C, the time limit, and SIGTERM through stopped().
    A stop that the caller's own handler takes gives header_not_repaired, see Swap.stop()."""
    info = {"container": (j.get("container") or {}).get("type"), "old_size": st.st_size, "tracks": checks.track_list(j)}
    trim, remove = list(hp.get("trim") or ()), list(hp.get("remove") or ())
    tracks = {t.get("id"): t for t in j.get("tracks") or []}
    lines = hp.get("sublines") or {}
    info["removed"] = {i: {"language": decide.track_tag(tracks[i].get("properties") or {}), "name": (tracks[i].get("properties") or {}).get("track_name"),
                           "codec": tracks[i].get("codec"), "lines": (lines.get(i) or [None])[0], "late": (lines.get(i) or [None, None])[1]}
                       for i in remove}   # the plan. The remux counts again, see trim_inputs().
    gone = ", ".join(f'{i} ({r["language"]}, {r["late"]} of {r["lines"]} lines start after the end)' for i, r in info["removed"].items())
    done = ("subtitle_removed", "subtitles removed") if remove else ("subtitle_trimmed", "subtitles trimmed") if trim \
        else ("tail_removed", "tail removed") if hp.get("tail") or hp.get("edit_tail") else ("header_repaired", "header repaired")
    would, failed, skip = "would repair header", "header repair failed", "header repair skipped"
    what = "; ".join(hp["issue"]) + (f", trim subtitle track {trim}" if trim else "") + (f", remove subtitle track {gone}" if remove else "")
    info.update(old_duration=hp["duration"], end=hp["expect"], cues=hp["cues"])
    why = repack_skip(path, st)
    if why:   # a hard link, the cap, low space or no place for the original: nothing is written
        return "header_repair_skipped", f"{skip}, {why}: {what}", info
    relace, held = lace_guess(path, j)   # before the dry run returns, so its result names them
    if held:   # mkvmerge 102 laces all tracks or none
        relace, info["relace_skipped"] = [], held
    elif relace:
        info["relaced"] = relace
    what += f", lace audio stream {', '.join(map(str, relace))} again, its times hold ffmpeg's estimates" if relace else f", {held}" if held else ""
    if not apply:
        return "would_repair_header", f"{would}: {what}", info
    tmp, folder = repack_tmp(path), runner.work_dir("trim")   # trim_inputs() and the packet read of header_fault() write there
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("id") not in remove]   # a removed track leaves the file
    sw = Swap(path, st, tmp, info)
    try:
        sw.start()
        new_tmp(tmp, st)
        own = {}
        if trim or remove:
            own, info["trimmed"], info["removed"] = trim_inputs(path, j, hp, folder)
        copied = [i for i in ids if i not in own and tracks[i].get("type") == "subtitles"]   # input 1, see above
        times, plans = ([], {}) if relace else planned_args(path, j, ids, folder, proof.LAG_SLACK)   # a lace keeps the time of its first frame only
        r = subprocess.run(merge_argv(path, j, tmp, ids, copied, own, edits, lace=bool(relace), head=times), capture_output=True, text=True, errors="replace")
        text = (r.stdout + r.stderr).strip()
        info["warnings"] = config.mask(text)[-500:] or None
        if (r.returncode or text) and not (r.returncode == 1 and resynced(text, hp)):
            raise RuntimeError(f"mkvmerge exited {r.returncode}: {info['warnings']}")
        if fault := plan_fault(tmp, plans): raise RuntimeError(fault)
        sw.sync()
        new = checks.mkvmerge(tmp)
        fault = header_fault(j, tmp, new, st.st_size, hp, info, folder, edits, path)
        if fault: raise RuntimeError(fault)
        size = sw.own()
        sw.check("the app replaced or renamed the original during the header repair", config.CFG.keep_days)
        os.replace(tmp, path)
    except BaseException as ex:
        sw.unkeep(path)
        if sw.stop(ex):   # the issue stays, as before the remux
            return "header_not_repaired", "not repaired: the run was stopped. The issue: " + "; ".join(hp["issue"]), info
        return "header_repair_failed", config.mask(f"{failed}{', the original changed' if isinstance(ex, SourceChanged) else ''}: {ex}")[:500], info
    finally:
        sw.close(folder)
    if info.get("kept"):
        vault.kept_write(path, info["kept"])
        vault.prune_originals(vault.originals_root(path))
    info.update(new_size=size, new_tracks=checks.track_list(new))
    return *done, info


# The Format line of the [Events] section that mkvextract writes for an ASS or SSA header without one, by codec. mkvmerge
# reads it back into the header, see header_kept().
EVENTS_FORMAT = {"S_TEXT/ASS": "Layer, Start, End, Style, Actor, MarginL, MarginR, MarginV, Effect, Text",
                 "S_TEXT/SSA": "Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"}


# The Matroska codecs whose header mkvmerge 102 writes again in a remux, see header_kept()
AVC, VC1 = "V_MPEG4/ISO/AVC", "V_MS/VFW/FOURCC"


def header_kept(old, new, codec, ended=False):
    """Whether the codec private data new, in hex, keeps old for a track of codec. It must be the same, but for the
    bytes mkvmerge writes again, in place.
    - An AVC header (avcC): bytes 1 to 3, the profile, the compatibility flags and the level, may become those of its
      first SPS. mkvmerge 4.9.1 wrote them as 0, and mkvmerge 102 writes them from the SPS.
    - A VC-1 header (a BITMAPINFOHEADER of FourCC WVC1): biSize may become the header's length, and biXPelsPerMeter
      and biYPelsPerMeter 1 where they are 0. ffmpeg writes a byte of padding past biSize and both as 0.
    - ended is true for a track that ended_track() wrote again. A HandBrake ASS header ends in a NUL byte and has no
      [Events] section. mkvmerge drops the NUL and adds the section, so such a header may also be old less that byte,
      then a blank line, "[Events]" and the Format line of EVENTS_FORMAT. A section name matches in any case, as libass
      reads it, so an old header with "[events]" fails too.
    Any other change fails."""
    if new == old:
        return True
    try:
        a, b = bytes.fromhex(old or ""), bytes.fromhex(new or "")
    except ValueError:
        return False
    if codec == AVC:
        sps = a[8:8 + int.from_bytes(a[6:8], "big")] if len(a) > 8 and a[5] & 0x1f else b""
        return len(sps) >= 4 and len(a) == len(b) and b[1:4] == sps[1:4] and a[:1] + a[4:] == b[:1] + b[4:]
    if codec == VC1:
        if len(a) != len(b) or len(a) < 40 or a[16:20] != b"WVC1":
            return False
        want = bytearray(a)
        want[0:4] = len(a).to_bytes(4, "little")
        for at in (24, 28):
            want[at:at + 4] = b"\1\0\0\0" if a[at:at + 4] == b"\0\0\0\0" else a[at:at + 4]
        return b == bytes(want)
    events = f"\r\n[Events]\r\nFormat: {EVENTS_FORMAT.get(codec)}\r\n\r\n".encode()
    return ended and codec in EVENTS_FORMAT and a.endswith(b"\0") and b"[events]" not in a.lower() and b == a[:-1] + events


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


def props_fault(j, new, drop=(), ended=(), edits=()):
    """Why the remuxed probe new differs from j less the track ids in drop, or None: the tracks against the planned state
    of edits, see tracks_fault(), the count of attachments, and the count of chapters in each edition. ended holds the
    track ids of j that ended_track() wrote again."""
    entries = lambda x: [c.get("num_entries") for c in x.get("chapters") or []]   # per edition
    if fault := tracks_fault(j, new, edits, drop, (), ended): return fault
    if len(j.get("attachments") or []) != len(new.get("attachments") or []):
        return f"the attachments changed from {len(j.get('attachments') or [])} to {len(new.get('attachments') or [])}"
    if entries(j) != entries(new): return f"the chapters changed from {entries(j)} to {entries(new)} entries"
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


class Unended(list):
    """The cues [(start, end, text)] of a track whose last block has no BlockDuration and no block after it, so its end
    is not known (RFC 9559, BlockDuration). That cue ends at its start here, see subtitles.timed(). A plan of new ends
    or new times would make one up, so flash_plan() and time_plan() give none for such a track."""


UNENDED = "its last cue has no duration and no block after it, so its end is not known, and no plan changes its times"


def flash_plan(cues, ass=False):
    """[(start, text, old end, new end)] of subsync.flash() for cues [(start, end, text)], or None when the cues do
    not flash or are Unended. ASS keeps centiseconds, so an ASS end rounds to them."""
    if isinstance(cues, Unended):
        return None
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
    checks the order of the new starts. Unended cues get no plan."""
    if isinstance(cues, Unended):
        return None
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

    A block with no BlockDuration ends at the next block's start, see subtitles.timed(), and mkvextract writes a SubRip
    cue so. It writes an ASS event that ends at its start, and it leaves out such a SubRip cue when it is the last
    one. Those cues find no pair, so no other end goes into the file. A track whose last cue has no end gets no plan,
    see Unended, and prove() refuses it too, see its timed and ended checks."""
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
            if k >= len(plan) or abs(proof.us(start) - proof.us(plan[k][0])) > 1500:   # whole us: exactly 2 ms is no float edge
                raise RuntimeError(f"cue {k + 1} of the extracted text starts at {start:.3f} s, and the plan's at {plan[k][0] if k < len(plan) else None}")
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")[:3]) / 1000
            if abs(proof.us(end) - proof.us(plan[k][2])) > 2000:
                raise RuntimeError(f"cue {k + 1} of the extracted text ends at {end:.3f} s, and the plan's at {plan[k][2]:.3f} s, so its end is not known")
            head = line[:m.start(5)].rstrip().removesuffix("-->").rstrip() if abs(plan[k][3] - start) < 0.0005 else fmt(plan[k][3])
            lines[i] = f"{head} --> {fmt(plan[k][4])}{g[8]}"
            k += 1
    if k != len(plan):
        raise RuntimeError(f"the extracted text holds {k} cues, and the plan {len(plan)}")
    return "\n".join(lines)


def srt_packets(path, j, tid):
    """[(start, end in seconds, the bytes)] of each packet of SubRip track tid of the Matroska file path, in file order.
    The walk of the blocks gives the time and the duration of each packet, see matroska.frames(), and a read at its
    byte its bytes. An empty frame is no packet, as in subtitles.timed(). A packet with no duration ends at the next
    block, an empty frame too, see matroska.rfc_cues(), and the last such packet at its time. Raises RuntimeError when
    the walk fails or a packet does not read."""
    p = next((t.get("properties") or {} for t in j.get("tracks") or [] if t.get("id") == tid), {})
    if p.get("content_encoding_algorithms") not in (None, "0"):
        raise RuntimeError(f"its content encoding {p.get('content_encoding_algorithms')} is not zlib")
    try:
        walked = matroska.frames(path)
        if walked["error"]:
            raise ValueError(walked["error"])
    except ValueError as ex:
        raise RuntimeError(f"the block walk failed: {ex}") from ex
    t = next((t for t in walked["tracks"] if t["number"] == p.get("number")), None) or {"time": ()}
    with open(path, "rb", buffering=0) as f:
        data = [matroska.block_data(f, t["place"][k], p["number"], packed=p.get("content_encoding_algorithms") == "0")[0] for k in range(len(t["time"]))]
    if None in data:
        raise RuntimeError(f"packet {data.index(None) + 1} does not read")
    blocks = [(t["time"][k], None if t["dur"][k] == matroska.NONE else t["dur"][k], data[k] or None) for k in range(len(t["time"]))]   # in ns
    return [(s / 1e9, (s if e is None else e) / 1e9, b) for s, e, b in matroska.rfc_cues(blocks)]


def srt_track(path, j, tid, plan, raw):
    """Write raw, a Matroska file of SubRip track tid of path alone, each packet as it is at the new times of plan, a
    time_plan() or a flash_plan(). srt_packets() gives the time, the end and the bytes of each packet. A packet pairs
    with the plan by its place, and it must have the plan's old start and end, as in set_ends(). Raises when a packet
    finds no pair, or when a new start lies before the one of the packet before it. mkvmerge's SubRip reader drops the
    blank lines at the start of a cue, and mkvmerge 8 stored them as line breaks, so such a packet cannot go back
    through a SubRip text."""
    packets = srt_packets(path, j, tid)
    plan = [p if len(p) == 5 else (*p[:3], p[0], p[3]) for p in plan]   # a flash_plan() keeps each start
    if len(packets) != len(plan):
        raise RuntimeError(f"the track holds {len(packets)} cues, and the plan {len(plan)}")
    for n, ((start, end, _), (s, _, e, a, _)) in enumerate(zip(packets, plan), 1):
        if abs(proof.us(start) - proof.us(s)) > 1500 or abs(proof.us(end) - proof.us(e)) > 2000:
            raise RuntimeError(f"cue {n} runs {start:.3f} to {end:.3f} s, and the plan's {s:.3f} to {e:.3f} s, so its end is not known"
                               if abs(proof.us(start) - proof.us(s)) <= 1500 else f"cue {n} starts at {start:.3f} s, and the plan's at {s:.3f} s")
        if n > 1 and a < plan[n - 2][3]:
            raise RuntimeError(f"cue {n} would start at {a:.3f} s, before the cue before it")
    el = lambda i, b: i.to_bytes((i.bit_length() + 7) // 8, "big") + (1 << 56 | len(b)).to_bytes(8, "big") + b   # an EBML element, its size in 8 bytes
    u = lambda i, v: el(i, v.to_bytes(8, "big"))
    ms = lambda t: round(t * 1000)
    head = el(0x1A45DFA3, u(0x4286, 1) + u(0x42F7, 1) + u(0x42F2, 4) + u(0x42F3, 8) + el(0x4282, b"matroska") + u(0x4287, 4) + u(0x4285, 2))
    app = f"arr-media-guard {config.VERSION}".encode()   # MuxingApp and WritingApp, which RFC 9559 asks for
    info = el(0x1549A966, u(0x2AD7B1, 1000000) + el(0x4D80, app) + el(0x5741, app))
    info += el(0x1654AE6B, el(0xAE, u(0xD7, 1) + u(0x73C5, 1) + u(0x83, 0x11) + el(0x86, b"S_TEXT/UTF8")))
    # One Cluster for each cue, with its Timestamp and a BlockGroup. The BlockGroup holds a Block of track 1 at 0 ms and its BlockDuration.
    clusters = b"".join(el(0x1F43B675, u(0xE7, ms(a)) + el(0xA0, el(0xA1, b"\x81\0\0\0" + b) + u(0x9B, ms(e) - ms(a))))
                        for (_, _, b), (_, _, _, a, e) in zip(packets, plan))
    with open(raw, "wb") as f:
        f.write(head + el(0x18538067, info + clusters))


def ended_track(path, j, tid, plan, folder, text=None):
    """The path of a Matroska file in folder that holds only subtitle track tid of path, with the new times of plan, a
    time_plan() or a flash_plan(). A SubRip track comes from srt_track(), which keeps each packet as it is, and
    mkvmerge reads that file. For an ASS or SSA track, mkvextract writes the track's text, set_ends() puts the times
    in, and mkvmerge reads it back. That round trip keeps the packets and the ASS header byte for byte, with two
    exceptions. HandBrake ends each ASS event and its header with a NUL byte and writes no [Events] section, and the
    round trip drops each NUL and adds the section. mkvmerge numbers the ASS events from 0 in ReadOrder order, so the
    events of a track whose ReadOrders skip a number, as HandBrake's may, take new ones. KEEP_PACKETS keeps the spaces
    and tabs at the ends of each line. mkvmerge may store each line break inside a SubRip cue as CRLF. mkvmerge 92
    and 102 do, and a source's mkvmerge may store LF. prove() and header_kept() pass only those changes.
    resub() gives the track its UID, language, name and flags, see track_args().
    text takes the place of plan for a repair, see resub(): the new SubRip text, which needs no mkvextract, or a
    function of the extracted text. Each RuntimeError it raises starts with "track <tid>:", so the record names the track."""
    p = next(t.get("properties") or {} for t in j.get("tracks") or [] if t.get("id") == tid)
    codec = p.get("codec_id")
    ext = {"S_TEXT/UTF8": "srt", "S_TEXT/ASS": "ass", "S_TEXT/SSA": "ssa"}[codec]
    raw, out = os.path.join(folder, f"{tid}.{ext}"), os.path.join(folder, f"{tid}.mkv")
    try:   # the reason names the track, as the temp file names only its own
        if ext == "srt" and text is None:
            raw = os.path.join(folder, f"{tid}.packets.mkv")
            srt_track(path, j, tid, plan, raw)
        elif not isinstance(text, str):
            r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvextract", path, "tracks", f"{tid}:{raw}"], capture_output=True, text=True,
                               errors="replace")
            if r.returncode:   # 1 is a warning, and a fix never builds on one
                raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
            with open(raw, encoding="utf-8", newline="") as f:
                text = text(f.read()) if text else set_ends(f.read(), ext != "srt", plan)
        if isinstance(text, str):
            with open(raw, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", *KEEP_PACKETS, "-o", out, "--sub-charset", "0:UTF-8", "--language",
                            f"0:{p.get('language_ietf') or p.get('language') or 'und'}", raw], capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise RuntimeError(f"mkvmerge exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    except RuntimeError as ex:
        raise RuntimeError(f"track {tid}: {ex}") from None
    return out


def repair_name(r):
    """What the repair r of a garbled track writes, see repair_text(): "its text read back as cp950", and "with 1 cue
    from Movie.3.srt" when a sidecar fills cut cues."""
    side = f' with {r["filled"]} cue{"s" if r["filled"] > 1 else ""} from {r["sidecar"]["name"]}' if r.get("sidecar") else ""
    return f'its text read back as {r["codepage"]}{side}'


def repaired(texts, cp, side=None):
    """(the repaired cue texts, the places of the cues that side fills, the count of cues left cut) of the cue texts of a
    garbled SubRip track, read back as the codepage cp (docs/design.md, "Garbled subtitle repair"). side,
    the cue texts of a sidecar in the same order, may fill the cues the old muxer cut: there the read-back is the start
    of the sidecar's text, and the next character holds a byte cp1252 leaves undefined. Every other cue of side must
    equal the read-back, as srt_cues() compares text. Else the result is None, because the sidecar is not the source of
    the track: a garbled copy, the track extracted again or an SDH version. A right cue stays as it is, see
    decide.right_cue()."""
    if side is not None and len(side) != len(texts):
        return None
    out, filled, left = [], [], 0
    for k, t in enumerate(texts):
        back, cut = (t, False) if decide.right_cue(t) else decide.read_back(t, cp)
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


def repair_text(r, cut=decide.REPAIR_CUT):
    """(the new text for ended_track(), a function of srt_blocks() of the old text that gives the new cues for prove())
    of the repair r of a garbled SubRip track (docs/design.md, "Garbled subtitle repair"). r holds "codepage", which
    reads the track back, see repaired(), and "lang", its tag. With "sidecar", the sidecar_subs() entry, the sidecar
    fills the cut cues. It must still hold "side", the cues the check read, else the remux stops: a sidecar written
    again since then is read again, and never with the charset the check gave it. The new text must pass
    decide.repair_fault(), and at most the share cut of all its cues may stay cut. A sidecar gives 0: no muxer cut its
    cues, and a cut writes U+FFFD over a right letter. A cue that does not read back stops the remux too."""
    side = None
    if r.get("sidecar"):
        now = [list(b) for b in side_blocks(r["sidecar"])]
        if now != [list(b) for b in r["side"]]:
            raise RuntimeError(f'the sidecar {r["sidecar"]["name"]} changed since the check')
        side = [t for _, _, t in now]

    def fix(texts):
        try:
            got = repaired(texts, r["codepage"], side)
        except UnicodeError:
            raise RuntimeError(f'some of its cues do not read back as {r["codepage"]}') from None
        if got is None:
            raise RuntimeError(f'the sidecar {r["sidecar"]["name"]} no longer fits the track')
        why = (f"{got[2]} of {len(texts)} cues were cut short" if got[2] > cut * len(texts) else None) \
            or decide.repair_fault(got[0], r["lang"])
        if why:
            raise RuntimeError(why)
        return got[0]
    return (lambda text: srt_rewrite(text, fix)), (lambda blocks: [(a, b, t) for (a, b, _), t in zip(blocks, fix([t for _, _, t in blocks]))])


TEXT_EXT = {"S_TEXT/UTF8": "srt", "S_TEXT/ASS": "ass", "S_TEXT/SSA": "ssa", "S_TEXT/WEBVTT": "vtt"}   # the extension of each text format
BROKEN_END = ".broken-encoding.{}.txt"   # the end of the name of the file that keeps removed subtitle bytes, with its format's extension
BROKEN_SIDECAR = BROKEN_END.format("srt")


def holds(name, data):
    """Whether the file name holds the bytes data."""
    with contextlib.suppress(OSError):
        if os.path.isfile(name) and os.path.getsize(name) == len(data):
            with open(name, "rb") as f:
                return f.read() == data
    return False


def broken_plan(path, broken):
    """Give each entry of broken, {key: {"lang", "data", "ext", "numbers"}}, its sidecar in "sidecar", and return why no
    name is free, or None (docs/design.md, "Unreadable subtitles"). It writes nothing. This is the one place that names
    the file of removed subtitle bytes: of a broken MP4 track, see convert.broken_tracks(), of a garbled track, see
    resub(), and of a garbled sidecar, see subtitles.broken_side(). An entry's key is a track's place, as s3, or a
    sidecar's name. lang is its language tag or the language part of its name. data is the bytes of its file, or None
    for a plan made before they are read. ext is the extension of its format, srt by default. numbers are the numbers
    its name may take in a clash, the track's own by default.

    The name is <base>.<lang>.broken-encoding.<ext>.txt beside the video at path, as Film.per.broken-encoding.srt.txt.
    An untagged entry has no language part. Players skip a .txt file. Bazarr skips this name, because it does not
    match the video's. Sonarr and Radarr count .txt as a subtitle extension, so a rescan records the file, and Radarr
    may rename it when it renames the movie. Two entries with one
    language part and one format, and a name that a file of other bytes holds, put a number in front, as
    Film.per.3.broken-encoding.srt.txt. A file with the same bytes stays, and "reused" says so. No two entries take one
    name, and no file is written over. With data None a name a file holds counts as taken, and with none free the last
    name stands, because the write plans again with the bytes."""
    base, taken = os.path.splitext(path)[0], set()
    part = {k: ("" if e["lang"] in decide.UNTAGGED else f".{e['lang']}", BROKEN_END.format(e.get("ext", "srt"))) for k, e in broken.items()}
    for k, e in broken.items():
        (lang, end), alone = part[k], list(part.values()).count(part[k]) == 1
        names = ([f"{base}{lang}{end}"] if alone else []) + [f"{base}{lang}.{n}{end}" for n in e.get("numbers") or [k[1:]]]
        free = next((n for n in names if n not in taken and (not os.path.lexists(n) or e["data"] is not None and holds(n, e["data"]))),
                    None if e["data"] is not None else names[-1])
        if not free:
            return f"the name {os.path.basename(names[-1])} is taken"
        taken.add(free)
        e.update(sidecar=free, reused=os.path.lexists(free))
    return None


def keep_broken(e, st):
    """Write the bytes of the entry e of broken_plan() to its sidecar, owned like st, see own_like(), and read them back.
    Returns whether it wrote the file. A file with the same bytes stays as it is, and e["reused"] says so. e["written"]
    says whether it wrote, for drop_broken(). It never writes over a file, so a name taken since the plan raises
    FileExistsError. The file gets the mode of st at its create, as in new_tmp(), so a share that refuses chmod keeps it."""
    e.update(reused=holds(e["sidecar"], e["data"]), written=False)
    if e["reused"]:
        return False
    fd = os.open(e["sidecar"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, st.st_mode & 0o666 | 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(e["data"])
            f.flush()
            os.fsync(f.fileno())
        own_like(e["sidecar"], st)
        if not holds(e["sidecar"], e["data"]):
            raise RuntimeError(f"{os.path.basename(e['sidecar'])} does not hold the bytes of its track")
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(e["sidecar"])
        raise
    e["written"] = True
    return True


def drop_broken(entries):
    """The undo of keep_broken(): remove the file it wrote for each of the entries, when the change that took their bytes
    out does not stand. A file it reused stays. e["removed"] says it went."""
    for e in entries:
        if e.get("written"):
            with contextlib.suppress(OSError):
                os.remove(e["sidecar"])
            e["removed"] = True


def events_file(path, tid, codec, folder):
    """The path of the file mkvextract writes in folder of the ASS, SSA or WebVTT track tid of path, with its header
    from the track's codec private data. A garbled track's bytes go beside the video in it, see resub()."""
    out = os.path.join(folder, f"{tid}.{TEXT_EXT[codec]}")
    r = subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvextract", path, "tracks", f"{tid}:{out}"], capture_output=True, text=True, errors="replace")
    if r.returncode:   # 1 is a warning, and the bytes of a removed track never build on one
        raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-300:]}")
    return out


# The attachment of a cover picture, by ffmpeg's codec name of its track: its file name and MIME type, see cover_tracks()
COVER_FILES = {"mjpeg": ("cover.jpg", "image/jpeg"), "png": ("cover.png", "image/png"), "bmp": ("cover.bmp", "image/bmp"), "gif": ("cover.gif", "image/gif")}
COVER_READS = {}   # (path, size, mtime_ns, what, ffprobe indexes) -> what cover_tracks() read, so a pass that Replan runs again reads none twice
COVER_LOCK = threading.Lock()   # the workers of a backfill share COVER_READS


def cover_read(key, read):
    """COVER_READS[key], from read(folder) the first time, with a work folder that goes after it. A read that raises
    stores nothing. COVER_LOCK guards each change of COVER_READS, never the read."""
    if (value := COVER_READS.get(key)) is not None:
        return value
    folder = runner.work_dir("resub")
    try:
        value = read(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    with COVER_LOCK:
        while len(COVER_READS) >= 8:   # the files a backfill's workers read now, never the whole library
            COVER_READS.pop(next(iter(COVER_READS)))
        COVER_READS[key] = value
    return value


def cover_images(path, indexes, images, folder, timeout):
    """({ffprobe index: the md5 of its first packet}, {attachment id: the md5 of its bytes}) of path, for the
    streams in indexes and the attachments in images, as mkvmerge -J lists them. ffmpeg stops after the first packet
    of each stream, which a cover picture has near the start of the file."""
    sums, names = {}, {}
    if indexes:
        out = os.path.join(folder, "first.md5")
        r = checks.run_bounded(["ffmpeg", "-nostdin", "-v", "error", "-i", path, *[a for i in indexes for a in ("-map", f"0:{i}")], "-c", "copy",
                                "-copyinkf", "-frames:v", "1", "-f", "framemd5", out], timeout, capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise RuntimeError(f"ffmpeg exited {r.returncode}: {config.mask(r.stderr.strip())[-150:]}")
        with open(out) as f:
            for line in f:
                if line[:1].isdigit():
                    sums.setdefault(indexes[int(line.split(",")[0])], line.split(",")[5].strip())
    files = cover_files(path, images, folder, "att", timeout)
    for a in images:
        with open(files[a["id"]], "rb") as f:
            names[a["id"]] = hashlib.md5(f.read()).hexdigest()
    return sums, names


def cover_files(path, atts, folder, tag, timeout=None):
    """{attachment id: the file in folder that one mkvextract call writes it to} for the attachments atts of path, as
    mkvmerge -J lists them."""
    files = {a["id"]: os.path.join(folder, f"{tag}-{a['id']}") for a in atts}
    if files:
        r = checks.run_bounded(["mkvextract", path, "attachments", *[f"{i}:{f}" for i, f in files.items()]], timeout, capture_output=True, text=True,
                               errors="replace")
        if r.returncode:
            raise RuntimeError(f"mkvextract exited {r.returncode}: {config.mask((r.stdout + r.stderr).strip())[-150:]}")
    return files


def cover_tracks(path, j):
    """{mkvmerge track id: entry} of each video track of path that is a still image and not the film, see
    decide.still() and decide.main_video(). An entry holds the track's place among the video tracks (v2), its ffprobe
    index, its codec, its count of frames, its track name as title, and name, the attachment that holds its image after
    resub(), with its MIME type, see COVER_FILES. md5 is the md5 of its image, and hashed says every packet hashes to
    it. copy says the track goes and adds nothing, because the file holds its
    image as an attachment already (there), or an earlier track of this remux adds it. att is the id of the
    attachment that holds the image of a copy that is there. why says why a track stays.

    A track counts when every packet holds the same image. mkvmerge's frame count of 1, for this file, needs no read.
    The others are read once with -c copy, see proof.packet_hashes(), within the job's time left less SUB_RESERVE.
    The file keeps one copy of each image. A track whose image an image attachment holds byte for byte goes, and the
    attachment stays as it is. Tracks of one image that no attachment holds go into one new attachment, cover.jpg, or
    cover2.jpg, cover3.jpg when an attachment has that file name, or an image attachment that stem, such as cover.png.
    A font named cover.ttf takes no name. A track whose image no other track or attachment holds
    becomes cover.jpg, unless the file holds an image attachment named cover, or this remux adds one first. Then it
    stays. The md5s of the images decide, see cover_images(), and resub() proves them byte for byte. A 1-frame track
    is read only when there is something to compare it with. A file with one video track, or whose film is a still image too, has none."""
    vids = [t for t in j.get("tracks") or [] if t.get("type") == "video"]
    if len(vids) < 2:
        return {}
    try:
        streams = proof.ff_streams(path)[1]
    except RuntimeError:   # ffprobe read no stream: no cover move
        return {}
    vs = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    if len(vs) != len(vids) or not (film := decide.main_video(vs)) or decide.still(film):
        return {}
    app = ((j.get("container") or {}).get("properties") or {}).get("writing_application") or ""
    fresh = lambda p: app.startswith("mkvmerge") and p.get("tag__statistics_writing_app", app) == app   # ffmpeg copies stale tags
    out = {t["id"]: {"place": f"v{k}", "index": x["index"], "codec": x["codec_name"], "name": COVER_FILES[x["codec_name"]][0],
                     "mime": COVER_FILES[x["codec_name"]][1], **({"title": p["track_name"]} if (p := t.get("properties") or {}).get("track_name") else {}),
                     "frames": 1 if fresh(p) and str(p.get("tag_number_of_frames")) == "1" else None}
           for k, (t, x) in enumerate(zip(vids, vs), 1) if decide.still(x)}
    pics = {(s.get("tags") or {}).get("filename") for s in streams if (s.get("disposition") or {}).get("attached_pic")}
    images = [a for a in j.get("attachments") or [] if a.get("file_name") in pics]   # the attachments ffmpeg reads as pictures
    st = os.stat(path)
    left = config.DEADLINE.left()
    timeout = min(max(600, st.st_size / proof.PROOF_RATE), left - config.SUB_RESERVE if left else float("inf"))
    key = lambda what, xs: (path, st.st_size, st.st_mtime_ns, what, tuple(xs))
    read = [e["index"] for e in out.values() if e["frames"] is None]
    one_md5 = lambda m: "" if not m else bytes(m[:16]).hex() if bytes(m) == bytes(m[:16]) * (len(m) // 16) else None
    packets = lambda folder: {i: (h["count"], one_md5(h["md5s"]))   # {index: (packets, the md5 of each, "" for none, None when they differ)}
                              for i, h in proof.packet_hashes(path, read, {}, [], folder, timeout)[0].items()}
    got, failed = {}, None
    if read and (key("packets", read) in COVER_READS or timeout > 0):
        try:
            got = cover_read(key("packets", read), packets)
        except (RuntimeError, subprocess.TimeoutExpired) as ex:
            failed = f"its frames were not all read: {config.mask(str(ex))[:150]}"
    for e in out.values():
        if e["frames"] is None:
            e["frames"], md5 = got.get(e["index"], (0, ""))
            if why := failed or (None if got else "its frames were not read in the time left") or (md5 is None and "its frames differ") \
                    or (md5 == "" and "it holds no frame"):
                e["why"] = why
            else:
                e.update(md5=md5, hashed=True)   # every packet hashes to md5, so the remux reads them no second time
    keep, have = [e for e in out.values() if "why" not in e], {}
    if keep and (images or len(keep) > 1):   # something to compare
        one = [e["index"] for e in keep if "md5" not in e]   # a 1-frame track whose image was not read
        k = key("images", [*one, *(a["id"] for a in images)])
        try:
            if k not in COVER_READS and timeout <= 0:
                raise RuntimeError("no time left")
            sums, names = cover_read(k, lambda folder: cover_images(path, one, images, folder, timeout))
        except (RuntimeError, subprocess.TimeoutExpired) as ex:
            sums, names = {}, {}
            for e in keep:
                e["why"] = f"its picture was not compared: {config.mask(str(ex))[:150]}"
        for e in keep:
            if not e.setdefault("md5", sums.get(e["index"])):
                e.setdefault("why", "its picture was not read")
        keep, have = [e for e in keep if "why" not in e], {m: i for i, m in reversed(names.items())}   # the first attachment of each image
    many = {m for m in (e.get("md5") for e in keep) if m and sum(x.get("md5") == m for x in keep) > 1}
    stem_of, added = lambda a: os.path.splitext(a.get("file_name") or "")[0].lower(), {}
    taken = {(a.get("file_name") or "").lower() for a in j.get("attachments") or []}
    stems = {stem_of(a) for a in images}   # a player shows an image named cover
    shown = "cover" in stems
    name_of = {a["id"]: a.get("file_name") for a in images}
    for e in keep:
        m = e.get("md5")
        if m in have:
            e.update(name=name_of[have[m]], att=have[m], copy=True, there=True)
        elif m in added:
            e.update(name=added[m], copy=True)
        elif m in many or not shown:
            ext = os.path.splitext(e["name"])[1]
            stem = next(s for s in ("cover", *(f"cover{k}" for k in range(2, len(stems) + len(taken) + 3))) if s not in stems and s + ext not in taken)
            stems.add(stem)
            shown = shown or stem == "cover"
            e["name"] = stem + os.path.splitext(e["name"])[1]
            added.update({m: e["name"]} if m else {})   # a lone track had nothing to compare, so it has no md5
        else:
            e["why"] = "the file has a cover picture already"
    return out


def moved_tracks(moves):
    """Write each move of moves, [(src, stream index, fix of subsync.timing(), out, track)], alone into the Matroska
    file out, each time moved by the fix to (time - offset) / rate and each duration scaled by 1 / rate. One ffmpeg call reads
    each src once. resub() takes it for every track a fix moves. mkvmerge reorders the packets of one input only
    within about 20 MB, so a track that --sync moved earlier than that in a file of many tracks stayed late. A file of
    one stream holds its packets in time order. A cue the fix moves before 0 starts at 0, as srt_moved() does, and a
    cue that ends before 0 keeps 1 ms. With a length of 0 the proof refused the remux. -itsoffset moves every stream of
    an input, so the setts filter makes the whole move, in the ms that Matroska stores. -copyts keeps the times of src,
    which ffmpeg would move by the file's start time. Each file keeps the track's tags. The chapters and global tags
    ffmpeg copies there stay out of the merge, see resub(). track is the track id a refusal names.

    ffmpeg's muxer changes a decode time that is lower than the one before it to that one, and logs "Non-monotonic
    DTS". A source that stores a track out of time order would then get its cues seconds late. So the call reads the
    warnings, see levels(), and such a warning refuses the move before mkvmerge writes a merge. ffmpeg 7.1 logs a step
    back of 2 ms or less at debug level only, and prove() refuses that move, see proof.mkv_slack(). Every other
    warning is left out, and the rest goes to proof.bad_lines(), as a read with -v error gives it."""
    srcs = list(dict.fromkeys(src for src, *_ in moves))
    argv = ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "level+warning", "-y", "-copyts", *[a for src in srcs for a in ("-i", src)]]
    for src, at, fix, out, _ in moves:
        n, k = srcs.index(src), repr(1 / float(fractions.Fraction(fix["rate"])))
        # The moved time in ms, so it rounds once. In a finer source time base, setts rounded to its tick, and the
        # muxer rounded that to ms again. A cue then sat up to 0.5 ms plus half a tick off the fix.
        t = lambda ts: f"({ts}-({float(fix['offset'])!r})/TB)*{k}*TB*1000"
        clamp = (f"setts=time_base=1/1000:pts=max({t('PTS')}\\,0):dts=max({t('DTS')}\\,0)"
                 f":duration=if(lt({t('PTS')}\\,0)\\,max(1\\,{t('PTS')}+DURATION*{k}*TB*1000)\\,DURATION*{k}*TB*1000)")
        argv += ["-map", f"{n}:{at}", "-c", "copy", "-bsf:0", clamp, "-f", "matroska", out]
    r = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    lines = levels(r.stderr)
    for level, line in lines:   # "[sost#2:0/copy @ 0x..] Non-monotonic DTS; ...": output file 2 is the third move
        if level == "warning" and (m := re.match(r"\[\w+#(\d+):\d+/\w+ @ 0x[0-9a-f]+\] Non-monotonic DTS", line)):
            raise RuntimeError(f"subtitle track {moves[int(m[1])][4]} stores its packets out of time order, so ffmpeg cannot move it")
    err = "\n".join(line for level, line in lines if level != "warning")
    if r.returncode or proof.bad_lines(err):
        raise RuntimeError(f"ffmpeg exited {r.returncode}: {config.mask(err.strip())[-300:]}")
    return [out for _, _, _, out, _ in moves]


def levels(stderr):
    """[(level, line)] of the stderr of an ffmpeg call with -v level+warning, each line without its level tag:
    "[matroska @ 0x..] [error] text" gives ("error", "[matroska @ 0x..] text"). A line with no tag goes on with the
    message before it and takes its level, as "Consider increasing the value for the 'analyzeduration'" does after
    a warning. A first line with no tag counts as an error."""
    out, level = [], "error"
    for line in stderr.splitlines():
        if m := re.match(r"(\[[^\]]+ @ 0x[0-9a-f]+\] )?\[(warning|error|fatal|panic)\] (.*)", line):
            level, line = m[2], (m[1] or "") + m[3]
        out.append((level, line))
    return out


def resub(path, j, st, apply, fixes, drop=(), ends=None, timed=None, recode=None, strip=None, covers=None, edits=(), hp=None):
    """Remux a Matroska file in place with new times for the subtitle tracks in fixes, {mkvmerge track id: fix of
    subsync.timing()}, new cue ends for the text tracks in ends, {mkvmerge track id: the plan of flash_plan()}, new
    times for the text tracks in timed, {mkvmerge track id: time_plan()}, new text for the garbled SubRip tracks in
    recode, {mkvmerge track id: its repair, see repair_text()}, and without the subtitle track ids in drop and in
    strip, in one remux under the caller's exclusive file lock (docs/design.md, "Subtitle match"). Returns (code, result,
    info). result is "subtitles remuxed", "would remux subtitles: ...", "subtitle remux failed: ..." or "subtitle remux
    skipped, ...", and code subtitles_remuxed, would_remux_subtitles, subtitle_remux_failed or subtitle_remux_skipped.

    A track in recode comes from its own file as a track in ends does, with the text of its repair, see repair_text().
    Its times stay. A repair with default_off turns its default flag off in the same remux. The proof holds each cue to
    the repair's text and to the original's times, see prove().

    A track with new ends comes from a file of that one track, see ended_track(). A SubRip track's packets go into it as
    they are, see srt_track(). For an ASS or SSA track, mkvextract writes its text, set_ends() puts each new end in, and
    mkvmerge reads it back. The text, the starts and the ASS header stay byte for byte, but for the NUL bytes of a
    HandBrake ASS track, the ReadOrders of an ASS track, which become their ranks, and the line breaks of a SubRip cue.
    The proof holds each end to the plan. An ASS track whose starts fall between centiseconds goes in as a track in
    timed, with each start on the nearest centisecond, see grid_plan(). Its log and what text stay those of new ends.
    info["starts_rounded"] counts the starts that the grid moved off the time its fix gives, see grid_moved().

    A track in timed comes from its own file the same way, with every start and end of its plan. Its plan holds its
    fix and its flash ends, so moved_tracks() never moves it. Its entries in fixes and ends only go to the log and
    the what text, and the what text names the cues its blocks move past that fix, see blocks_moved(). The proof holds
    each start and end to the plan. ended_track() takes SubRip, ASS and SSA only. info["timed_cues"] counts the cues of
    each plan by track id. The caller's record uses "timed" for the places of the moved tracks.

    One mkvmerge call writes the new file (docs/design.md, "Subtitle match"). It copies every other track with its
    packets, see KEEP_PACKETS, its UID, language, BCP 47 tag, name and flags, and the file's attachments, chapters,
    tags and Segment UID. --track-order keeps the order of the tracks. A retimed track comes from its own file of
    moved_tracks(), its cues moved to (time - offset) / rate, so its packets are in time order. mkvmerge reads each
    input in file order and holds back only about 20 MB of it. A track that --sync moved earlier than 20 MB of the
    other tracks stayed late in the new file, where no player shows it, and mkvmerge drops a cue that --sync moves
    before 0. A track from its own file gets the language, name and flags of its planned properties with track_args(),
    and mkvpropedit gives that file the original's UID first. mkvpropedit edits the new file only to give a planned
    audio track its DefaultDuration back, see plan_fault(). A copied subtitle track
    that the source stores late would stay late too. So the subtitle tracks it copies come from a second input of the
    same file, which mkvmerge reads on its own and writes by time. The chapters, tags and attachments come from the first input only. Each track that the
    source holds uncompressed stays so, see compression_args(). Any line mkvmerge prints fails the remux, and
    info["warnings"] keeps it. The new file must keep every other track and its properties (props_fault()), and prove() must show every
    packet of every kept stream the same, the retimed tracks' times as the fix moves them.

    A garbled text track in strip, {mkvmerge track id: its language tag}, leaves the file, and its bytes go beside the
    video under the name of broken_plan(), in the track's own format. Before the remux, one ffmpeg read of the
    original writes its packets and their md5s: a SubRip track's as SubRip, and proof.packet_text() shows each cue's bytes equal to its packet's. An ASS, SSA or
    WebVTT track's go as they are, and mkvextract writes the file with its header, see events_file(). proof.event_text()
    shows the text of each packet in it. After the proof and the kept original, keep_broken() writes the bytes beside
    the video. A failure after that removes each new file, see drop_broken(). info["garbled_text"] names each file,
    and info["garbled_text_removed"] each one the failure removed again.

    A cover picture track in covers, {mkvmerge track id: its entry of cover_tracks()}, leaves the file, and its image
    becomes an attachment with the entry's name and MIME type, and its track name as the description. The ffmpeg read
    before the remux also writes its first packet, and the md5 of each of its packets, unless cover_tracks() hashed
    them already (hashed). Every md5 must be the same, and the first packet must hash to it. The same mkvmerge call adds that packet as the attachment. A copy adds nothing.
    mkvextract reads the attachment of each cover track back from the new file, and its bytes must be the track's
    packet: the one this remux adds, or for a copy the one the file keeps or an earlier track adds. prove() leaves the
    track out, as it does a removed subtitle. The first input drops it (-d), and it gets no --compression option.
    info["cover_names"] names each attachment. A remux that moves only covers says so in its result.

    edits, the edit list of a decide plan, go into the same remux as mkvmerge track options, see merge_argv(). A track
    from its own file takes its edited flags and language from track_args(). The proof holds every track to the
    planned state, see tracks_fault().

    hp, the header stage of check_video(), may hold a layout or tail issue of the header in hp["issue"], never a trim or
    a removal. The remux then repairs it too, and its new file must pass header_checks() as a header repair's does.
    It keeps --disable-lacing, so audio that lace_guess() would lace again keeps ffmpeg's estimates, and
    info["relace_skipped"] says why.

    It runs like repack(): the temp file in hide_dir, no time limit during the remux, and SIGTERM removes the temp file.
    A stop that the caller's own handler takes gives "subtitle remux failed: the run was stopped", see Swap.stop().
    The original must still be the file st describes. The temp file takes its owner and mode, the original is kept in
    originals_root() for keep_days, and the temp file is renamed over it. A removal needs that kept original, so with
    KEEP_ORIGINALS_DAYS 0 the caller never asks for one. The extracted text, the files of single tracks, the cover
    pictures and the proof's reads go into a folder under STATE_DIR, never the system temp dir, which is often a small
    tmpfs."""
    ends, timed, recode, strip, covers = ends or {}, timed or {}, recode or {}, strip or {}, covers or {}
    drop = [*drop, *strip]
    place = {i: f"s{n}" for n, i in enumerate((t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"), 1)}
    codec = {t.get("id"): (t.get("properties") or {}).get("codec_id") for t in j.get("tracks") or []}
    broken = {i: {"lang": lang, "data": None, "ext": TEXT_EXT[codec[i]]} for i, lang in strip.items()}
    broken_plan(path, {place[i]: e for i, e in broken.items()})   # the plan's names. The write names them again with the bytes.
    info = {"old_size": st.st_size, "fixes": {str(i): f for i, f in fixes.items()}, "drop": list(drop), "ends": {str(i): len(e) for i, e in ends.items()},
            **({"timed_cues": {str(i): len(plan) for i, plan in timed.items()}} if timed else {}),
            **({"recode": {str(i): repair_name(r) for i, r in recode.items()}} if recode else {}),
            **({"garbled_text": {str(i): os.path.basename(e["sidecar"]) for i, e in broken.items()}} if strip else {}),
            **({"cover_names": {str(i): e["name"] for i, e in covers.items()}} if covers else {})}
    what = "; ".join([f'track {i}: {f["offset"]:+.3f} s' + ("" if f["rate"] == "1/1" else f', ratio {f["rate"]}') for i, f in fixes.items()]
                     + [f"track {i}: new ends for {sum(e != o for _, _, o, e in plan)} of {len(plan)} cues" for i, plan in ends.items()]
                     + [f"track {i}: {blocks_moved(plan, fixes.get(i)) or f'new times for {len(plan)} cues'}" for i, plan in timed.items()]
                     + [f"track {i}: {repair_name(r)}" + (", default flag off" if r.get("default_off") else "") for i, r in recode.items()]
                     + [f"remove track {i}" for i in drop if i not in strip]
                     + [f"take out track {i}, its bytes to {os.path.basename(e['sidecar'])}" for i, e in broken.items()]
                     + [f"track {i}: " + (f"removed, the attachment {e['name']} holds its picture" if e.get("copy") else
                                          f"cover picture to the attachment {e['name']}") for i, e in covers.items()])
    if head := bool(hp and hp.get("issue")):
        what += f"; repair the header: {'; '.join(hp['issue'])}"
        relace, held = lace_guess(path, j)
        if relace:   # resub() writes each audio frame in its own block
            info["relace_skipped"] = held or f"audio stream {', '.join(map(str, relace))} keeps ffmpeg's estimated times, as a subtitle remux never laces"
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
        sw.start()
        text = [x["index"] for x in proof.ff_streams(path)[1] if x.get("codec_type") == "subtitle"]
        if len(text) != len(subs):
            raise RuntimeError(f"ffprobe reads {len(text)} subtitle streams and mkvmerge {len(subs)}")
        index = dict(zip(subs, text))
        gone = {index[i] for i in drop} | {e["index"] for e in covers.values()}   # a cover picture comes back as an attachment
        j = dict(j, tracks=[dict(t, properties=dict(t.get("properties") or {}, default_track=False)) if (recode.get(t.get("id")) or {}).get("default_off")
                            else t for t in j.get("tracks") or []])   # the new flags, for track_args() and props_fault()
        props = {t.get("id"): t.get("properties") or {} for t in j.get("tracks") or []}
        own = {i: ended_track(path, j, i, plan, folder) for i, plan in {**ends, **timed}.items()}   # {track id: the file it comes from}
        repairs = {i: repair_text(r) for i, r in recode.items()}   # (the new text for ended_track(), the cues for the proof)
        own.update({i: ended_track(path, j, i, None, folder, new) for i, (new, _) in repairs.items()})
        moved = [i for i in fixes if i not in timed]   # a time plan holds its fix
        if moved:
            own.update(zip(moved, moved_tracks([(own.get(i, path), 0 if i in own else index[i], fixes[i], os.path.join(folder, f"{i}.moved.mkv"), i)
                                                for i in moved])))
        # The packets of a track in strip, from a read of their own: their md5s, and their bytes as they are for
        # mkvextract's file, see below. A SubRip track's file is written from the walk, with the times the file stores.
        texts = {i: (os.path.join(folder, f"{i}.garbled.{'srt' if codec[i] == 'S_TEXT/UTF8' else 'bin'}"), os.path.join(folder, f"{i}.md5")) for i in strip}
        pics = {i: (os.path.join(folder, f"cover-track-{i}"), os.path.join(folder, f"cover-track-{i}.md5")) for i in covers}   # its first packet, its md5s
        if texts or pics:
            argv = ["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path]
            for i, (srt, md5) in texts.items():
                argv += ["-map", f"0:{index[i]}", "-c", "copy", "-f", "framemd5", md5,
                         *([] if srt.endswith(".srt") else ["-map", f"0:{index[i]}", "-c", "copy", "-f", "data", srt])]
            for i, (img, md5) in pics.items():   # a GIF frame is no keyframe, and a copy drops it without -copyinkf
                x = covers[i]["index"]
                argv += [*([] if covers[i].get("hashed") else ["-map", f"0:{x}", "-c", "copy", "-copyinkf", "-f", "framemd5", md5]),
                         "-map", f"0:{x}", "-c", "copy", "-copyinkf", "-frames:v", "1", "-f", "data", img]
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace")
            if r.returncode or proof.bad_lines(r.stderr):
                raise RuntimeError(f"ffmpeg exited {r.returncode}: {config.mask(r.stderr.strip())[-300:]}")
        for i, (srt, md5) in texts.items():   # an ASS, SSA or WebVTT file holds its header too, which mkvextract writes
            if srt.endswith(".srt"):
                with open(srt, "wb") as f:
                    f.write(proof.timed_srt(srt_packets(path, j, i)))
            out = srt if srt.endswith(".srt") else events_file(path, i, codec[i], folder)
            if why := (proof.packet_text(srt, md5) if out == srt else proof.event_text(out, md5, srt, codec[i])):
                raise RuntimeError(f"the bytes of track {i} were not written whole: {why}")
            texts[i] = (out, md5)
        for i, (img, md5) in pics.items():
            with open(img, "rb") as g:
                pic = hashlib.md5(g.read()).hexdigest()
            if covers[i].get("hashed"):
                sums = {covers[i]["md5"]}
            else:
                with open(md5) as f:
                    sums = {line.split(",")[5].strip() for line in f if line[:1].isdigit()}
            if sums != {pic}:
                raise RuntimeError(f"the packets of cover track {i} are not all its image: {len(sums)} different" if sums else
                                   f"ffmpeg wrote no packet of cover track {i}")
        added = {}   # each attachment this remux adds, by name: (the track, its image). A copy adds nothing.
        for i, (img, _) in pics.items():
            if not covers[i].get("there"):
                added.setdefault(covers[i]["name"], (i, img))
        for i, f in own.items():   # mkvmerge keeps the UID a file gives its track
            e = subprocess.run(["mkvpropedit", f, "--edit", "track:1", "--set", f"track-uid={props[i]['uid']}"], capture_output=True, text=True,
                               errors="replace") if props[i].get("uid") else None
            if e and e.returncode not in (0, 1):   # a signal gives a negative code
                raise RuntimeError(f"mkvpropedit exited {e.returncode}: {config.mask((e.stdout + e.stderr).strip())[-300:]}")
        ids = [t.get("id") for t in j.get("tracks") or [] if t.get("id") not in drop and t.get("id") not in covers]
        copied = [i for i in ids if i in index and i not in own]   # the subtitle tracks of input 1, see above
        # Each frame its own block, as in convert_cmd(). A player that seeks to 0 skips a whole lace that starts before the
        # first video keyframe, up to 170 ms of AAC audio (docs/design.md, "Subtitle match").
        times, plans = planned_args(path, j, ids, folder)
        argv = merge_argv(path, j, tmp, ids, copied, own, edits, head=[*times, *(
            a for name, (i, img) in added.items() for a in ("--attachment-name", name, "--attachment-mime-type", covers[i]["mime"],
                                                             *(["--attachment-description", covers[i]["title"]] if covers[i].get("title") else []),
                                                             "--attach-file", img))])
        new_tmp(tmp, st)
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace")
        info["warnings"] = config.mask((r.stdout + r.stderr).strip())[-500:] or None
        if r.returncode or info["warnings"]:   # a warning may say mkvmerge left data out, as in repack()
            raise RuntimeError(f"mkvmerge exited {r.returncode}: {info['warnings']}")
        if fault := plan_fault(tmp, plans): raise RuntimeError(fault)
        sw.sync()
        new = checks.mkvmerge(tmp)
        fault = props_fault(dict(j, attachments=[*(j.get("attachments") or []), *({"file_name": n} for n in added)]), new, [*drop, *covers],
                            {*ends, *timed, *recode}, edits)   # the tracks of ended_track()
        uid = lambda a: (a.get("properties") or {}).get("uid")   # each cover track's image must be its attachment's, byte for byte
        uids = {a.get("id"): uid(a) for a in j.get("attachments") or []}   # mkvmerge keeps the UID of each attachment it copies
        # The attachment of each cover in the new file: one that stays by its UID, one this remux adds by its name, which
        # no attachment of the original has, see cover_tracks()
        at = {i: next((a for a in new.get("attachments") or [] if (uid(a) == uids.get(c.get("att")) if c.get("there") else
                                                                 a.get("file_name") == c["name"])), None) for i, c in covers.items()}
        try:   # read back byte for byte
            back = cover_files(tmp, {a["id"]: a for a in at.values() if a}.values(), folder, "back") if not fault else {}
        except RuntimeError as ex:
            fault = str(ex)
        for i, (img, _) in pics.items():
            if not fault and not (at[i] and filecmp.cmp(img, back[at[i]["id"]], shallow=False)):
                fault = f"the attachment {covers[i]['name']} does not hold the image of track {i}"
        if not fault:
            kept = [i for i in subs if i not in drop]
            refused, info["proof"] = proof.prove(path, tmp, [], folder, dropped=gone, absolute=True,
                                                 retimed={kept.index(i): f for i, f in fixes.items() if i not in timed},
                                                 ended={kept.index(i): [e for _, _, _, e in plan] for i, plan in ends.items() if i not in timed},
                                                 timed={kept.index(i): [(a, e) for _, _, _, a, e in plan] for i, plan in timed.items()},
                                                 recoded={kept.index(i): cues for i, (_, cues) in repairs.items()})
            fault = refused and refused[1]
        if not fault and head:
            fault = header_checks(tmp, new, st.st_size, hp, info, folder, path)
        if fault: raise RuntimeError(fault)
        size = sw.own()
        sw.check("the app replaced or renamed the original during the subtitle remux", config.CFG.keep_days)
        for i, (srt, _) in texts.items():
            with open(srt, "rb") as f:
                broken[i]["data"] = f.read()
        if taken := broken_plan(path, {place[i]: e for i, e in broken.items()}):
            raise RuntimeError(taken)
        for i, e in broken.items():
            keep_broken(e, st)
            info["garbled_text"][str(i)] = os.path.basename(e["sidecar"])
        os.replace(tmp, path)
    except BaseException as ex:
        sw.unkeep(path)
        drop_broken(broken.values())
        if undone := sorted(os.path.basename(e["sidecar"]) for e in broken.values() if e.get("removed")):
            info["garbled_text_removed"] = undone
        if sw.stop(ex):
            return "subtitle_remux_failed", "subtitle remux failed: the run was stopped", info
        return "subtitle_remux_failed", config.mask(f"subtitle remux failed{', the original changed' if isinstance(ex, SourceChanged) else ''}: {ex}")[:500], info
    finally:
        sw.close(folder)
    if info.get("kept"):
        vault.kept_write(path, info["kept"])
        vault.prune_originals(vault.originals_root(path))
    info["new_size"] = size
    return "subtitles_remuxed", "subtitles remuxed" if fixes or drop or ends or timed or recode or not covers else \
        "cover picture moved to an attachment" if not all(e.get("copy") for e in covers.values()) else "cover picture track removed as a copy", info
