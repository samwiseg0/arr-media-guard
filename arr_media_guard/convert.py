# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The conversion of a file that is not Matroska into .mkv, and its import into the app."""
import collections, contextlib, datetime, fcntl, os, re, select, shutil, subprocess, threading, time, urllib.parse, uuid

from . import apps, checks, config, content, decide, logs, proof, regrab, remux, runner, store, subtitles, vault


# The conversion of a file that is not Matroska into <base>.mkv (docs/design.md, "Conversion"). Every stream is proven at packet
# level before the original goes, the app takes the new file before the original is deleted, and nothing is kept.
HELD = b".convert-held"   # the end of the hidden name the original holds while the app takes the new file, see convert()
COMMAND_WAIT = 600        # seconds convert() waits for the app's ManualImport of the new file
RELINK_WAIT = 60          # seconds relink() reads the app's record again after the import, until it names the file


def convert_captions(path, streams, folder, size):
    """({ffprobe stream index: {path, name, charset, cues}} of the SubRip file each CEA-608 caption stream becomes, why
    none can, or None). ffmpeg reads the c608 track and writes SubRip, one read of the file and no video decode. The
    text keeps its line breaks and loses the markup SubRip players show as text: ASS override tags such as {\\an7}, the
    hard spaces \\h and the Monospace font. A stream that gives no cue, or a read that fails, keeps the original."""
    out = {}
    for s in streams:
        f = os.path.join(folder, f"cc{s['index']}.srt")
        r = checks.run_bounded(["ionice", "-c3", "nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{s['index']}",
                         "-c:s", "srt", "-f", "srt", f], max(600, size / proof.PROOF_RATE), capture_output=True, text=True, errors="replace")
        raw, clean = "", lambda line: " ".join(re.sub(r"\{\\[^}]*\}|</?font[^>]*>", "", line).replace("\\h", " ").split())
        if r.returncode == 0:
            with contextlib.suppress(OSError), open(f, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        text = "\n".join(clean(line) for line in raw.split("\n") if not line.strip() or clean(line))   # a markup-only line never splits a cue
        cues = proof.srt_cues(text)
        if not cues:
            return None, (f"the CEA-608 captions of stream {s['index']} give no text" +
                          (f", ffmpeg exited {r.returncode}: {config.mask(r.stderr.strip())[-150:]}" if r.returncode else ""))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
        out[s["index"]] = {"path": f, "name": proof.CC_NAME, "charset": "UTF-8", "cues": len(cues)}
    return out, None


def convert_cmd(src, tmp, j, subs, streams=None, captions=(), drop=()):
    """The remux of src, its captions and its sidecars subs into tmp. mkvmerge keeps the track order and every track it
    reads but the subtitle track ids in drop, then adds each caption SubRip of convert_captions() as "English (CC)",
    then the sidecars. A sidecar with new times goes in from its "mux" file. A container
    mkvmerge cannot read (ASF) goes through ffmpeg -c copy -copyinkf instead, with streams, its ffprobe streams, and never
    a new encode. -copyinkf keeps the frames before the first keyframe, which the proof reads. The ffmpeg path takes no sidecar and no caption."""
    nice = ["ionice", "-c3", "nice", "-n", "19"]
    if streams is not None:   # no sidecar: they keep the base name, so the player still finds them beside the new file
        return nice + ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src] + [a for s in proof.media_streams(streams) for a in ("-map", f"0:{s['index']}")] \
            + ["-c", "copy", "-copyinkf", "-f", "matroska", tmp]   # the frames before the first keyframe too
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("id") not in drop]
    extra = len(captions) + len(subs)
    order = ["--track-order", ",".join([f"0:{i}" for i in ids] + [f"{n}:0" for n in range(1, extra + 1)])] if ids and None not in ids else []
    argv = nice + ["mkvmerge", "-q", "--disable-lacing", "-o", tmp, *order, *(["-s", "!" + ",".join(map(str, drop))] if drop else []),
                   src]   # each frame its own block, see times_fault()
    for c in captions:
        argv += ["--language", "0:en", "--track-name", f"0:{proof.CC_NAME}", "--sub-charset", "0:UTF-8", "--default-track-flag", "0:0",
                 "--forced-display-flag", "0:0", c["path"]]
    for s in subs:
        argv += ["--language", f"0:{s['lang']}", "--sub-charset", f"0:{s['charset']}", "--default-track-flag", "0:0"]
        argv += [a for f in s["flags"] for a in (f, "0:1")] + [s.get("mux") or s["path"]]
    return argv


def convert_subs(path, j, streams, subs, dur, apply, folder, info, known=()):
    """The subtitle match check of a conversion (docs/design.md, "Subtitle match"). It checks each sidecar in subs and
    each built-in text track, in a SUB_ROLES role and in the language of a main audio track, against the audio. A
    forced sidecar is never checked. Returns (the sidecars to mux, [(mkvmerge track id, ffprobe stream index)] of the
    tracks to leave out). info gets the results in subcheck and what changed.

    A sidecar that does not match is not muxed. On apply it moves into originals_root() as a kept original. With
    KEEP_ORIGINALS_DAYS 0, or no place to keep it, it stays beside the file. A built-in track that does not match is
    left out of the remux, and convert() keeps the original. With no place to keep it the track stays in. A sidecar whose times need a fix is kept the same way, and its text with new times goes in
    the remux. A built-in track whose times need a fix gets them after the conversion, from the check of the new
    Matroska file, which reads the same windows and the words carried over, see process(). known holds the item's
    original languages for sub_hold()."""
    if dur < config.SUB_MIN_SECONDS or not checks.lid_ready():
        return subs, []
    ts = decide.classify(j)
    audio = subtitles.sub_audio(ts)
    items, text = {}, [x for x in proof.media_streams(streams) if x.get("codec_type") == "subtitle"]
    ids = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    for s in subs:
        lang = subtitles.side_code(s)
        k = decide.lang_key(lang)
        if k in audio and proof.SIDECAR_FLAGS["forced"] not in s["flags"] and subtitles.spaced(lang):
            items[s["name"]] = (lang, audio[k], [(a / 1000, b / 1000, t) for a, b, t in s["cues"]])
    for n, t in enumerate(t for t in ts if t["kind"] == "s"):
        x = text[n] if len(text) == len(ids) else None   # the k-th ffprobe subtitle is mkvmerge's k-th
        k = decide.lang_key(t["lang"])
        if x and x.get("codec_name") in ("subrip", "ass", "ssa", "webvtt", "mov_text") and t["role"] in config.SUB_ROLES and k in audio and subtitles.spaced(t["lang"]):
            r = checks.run_bounded(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{x['index']}", "-c:s", "srt", "-f", "srt", "-"],
                            600, capture_output=True, text=True, errors="replace")
            items[t["pos"]] = (t["lang"], audio[k], [(a / 1000, b / 1000, c) for a, b, c in (proof.srt_cues(r.stdout) if r.returncode == 0 else [])])
    if not items:
        return subs, []
    c = j.get("container") or {}   # mkvmerge gives no duration for an MP4, and the hearing needs one, so ffprobe's goes in
    timed = j if decide.duration(j) else dict(j, container=dict(c, properties=dict(c.get("properties") or {}, duration=round(dur * 1e9))))
    got = info["subcheck"] = subtitles.sub_held(subtitles.sub_verdicts(path, timed, items), items, ts, known)
    keep = []
    for s in subs:
        r = got.get(s["name"]) or {}
        fix = (r.get("timing") or {}).get("fix")
        if r.get("verdict") == "mismatch" or (fix and apply):
            why = (vault.keepable(s["path"], os.stat(s["path"])) or None) if config.CFG.keep_days else "KEEP_ORIGINALS_DAYS is 0"
        if r.get("verdict") == "mismatch":   # rather no subtitle than a wrong one
            entry = {"name": s["name"], "why": r["why"]}
            info.setdefault("sidecars_unmatched", []).append(entry)
            try:
                if apply and why: raise OSError(why)
                if apply:
                    entry["moved"] = vault.keep_original(s["path"], entry)
                    os.remove(s["path"])
            except OSError as ex:
                entry["left"] = config.mask(str(ex))[:200]
            continue
        if fix and apply:
            try:
                if why: raise OSError(why)
                with open(s["path"], "rb") as f:
                    text_now = f.read().decode(proof.PY_CHARSET[s["charset"]], errors="replace")
                mux = os.path.join(folder, f"retimed-{len(keep)}.srt")
                with open(mux, "w", encoding="utf-8") as f:
                    f.write(remux.srt_moved(text_now, fix))
                info.setdefault("sidecars_kept", []).append(vault.keep_original(s["path"]))
                s = dict(s, mux=mux, charset="UTF-8", retimed=fix)
                info.setdefault("sidecars_retimed", []).append(s["name"])
            except OSError as ex:
                info.setdefault("sidecars_not_retimed", []).append(config.mask(f"{s['name']}: {ex}")[:200])
        elif fix:
            info.setdefault("sidecars_would_retime", []).append(s["name"])
        keep.append(s)
    wrong = [(n, t["pos"]) for n, t in enumerate(t for t in ts if t["kind"] == "s") if (got.get(t["pos"]) or {}).get("verdict") == "mismatch"]
    # A track that leaves the file comes back only from the kept original, see convert(). Without one it stays, and
    # the check of the new Matroska file turns its flags off.
    why = wrong and ((vault.keepable(path, os.stat(path)) or None) if config.CFG.keep_days else "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept")
    if wrong:
        info["tracks_unmatched"] = [p for _, p in wrong]
        if why:
            info["tracks_kept_back"] = why
    return keep, [] if why else [(ids[n], text[n]["index"]) for n, _ in wrong]


def convert_skip(app, path, new, st, ids):
    """(code, why) a conversion of the file st describes cannot run, or None: a hard link, the size cap or low space as
    for a repack, see repack_block(), no item id to import the new name with, or the .mkv name taken by another file.
    Most conversions keep no original, so the keep check of a repack is left out. It writes nothing."""
    code, why = remux.repack_block(path, st, keep=False)
    if why:
        return {"hardlinked": "repack_hardlinked", "cap": "repack_too_big", "space": "repack_low_space"}[code], why
    if new != path and not ((ids or {}).get("app_id") and (apps.ARR[app].film or (ids or {}).get("file_id"))):
        return "not_matroska", "no item id, so the app cannot take the new name"
    if new != path and os.path.lexists(new):
        return "repack_name_taken", f"the name {os.path.basename(new)} is taken"
    return None


def app_refuses(path, old, items, home):
    """Why the app's record does not allow a manual import of the new name, or None. The record must name this file,
    and the file must sit in the item's folder. A file outside it would be an import from a download, which the app
    moves and renames (ImportApprovedMovie with newDownload true)."""
    if not old.get("id") or not items:
        return "the app lists no file for the item"
    if old.get("path") != path:
        return f"the app lists {old.get('path')}, not this file"
    if not home or not path.startswith(home.rstrip("/") + "/"):
        return f"the file sits outside the item's folder {home}, so a manual import would move it"
    return None


def app_lists(app, owner, items):
    """({file id: path} the app lists for the items now, {item id: monitored})."""
    paths, of = apps.ARR[app].files(owner, items)
    return {f: p for f, p in paths.items() if f}, {i: m for i, (m, _) in of.items()}


def relink(app, owner, old, items, target, imports=None):
    """Make the app list the file at target for items, by a ManualImport with explicit ids, and read its record back.

    old is the app's record of the original. The import carries its quality, languages, release group and indexer flags,
    and on Sonarr its release type. For a file in the item's folder the app neither moves nor renames it (ManualImport
    with newDownload false), and it links the items to the new record. The record of the original then has no item: the
    item's next rescan or the app's daily housekeeping deletes it (CleanupOrphanedMovieFiles, CleanupOrphanedEpisodeFiles),
    and neither unmonitors anything, because no item points at it. Only an import from a download sets the scene name,
    so it and Radarr's edition go back with PUT moviefile/bulk or episodefile/bulk. The custom formats that the release
    name gave then stay. The record is judged once it names the new file, read again every 2 s for RELINK_WAIT seconds
    at most. A record with no scene name takes the name of its original download path instead: the app scores
    custom formats on the scene name, else that path's name, else the file name (CustomFormatCalculationService). The
    app must list target and only target for every item, and the custom format score must not drop, else ok is false
    and info names it in refused. An item that lost its
    monitored flag gets it back. imports, a backfill's Imports(), sends the import with the other files of the item
    that wait. Returns (ok, info)."""
    f = {"path": target, "quality": old.get("quality"), "languages": old.get("languages") or [], "releaseGroup": old.get("releaseGroup") or "",
         "indexerFlags": old.get("indexerFlags") or 0}
    f.update(apps.ARR[app].import_item(owner, items, old))
    status = imports.run(app, owner, f) if imports else apps.ARR[app].command({"name": "ManualImport", "importMode": "auto", "files": [f]})
    end = time.monotonic() + RELINK_WAIT   # the app links the file inside the command, but a read is judged only once it shows
    while True:
        files, now = app_lists(app, owner, items)
        if status != "completed" or list(files.values()) == [target] or time.monotonic() >= end:
            break
        time.sleep(2)
    ok = status == "completed" and list(files.values()) == [target] and len(now) == len(items)
    info = {"import": status, "file_id": next(iter(files)) if ok else None, "listed": sorted(set(files.values()))}
    if ok:
        kind, keep = apps.ARR[app].file_kind, {k: old[k] for k in ("sceneName", "edition") if old.get(k)}
        if not keep.get("sceneName") and old.get("originalFilePath"):   # the app keeps it only when it reads as a release name
            keep["sceneName"] = os.path.splitext(os.path.basename(old["originalFilePath"]))[0]
        try:
            got = (apps.arr_write(app, f"{kind}/bulk", "PUT", [dict(keep, id=info["file_id"])]) or [{}])[0] if keep else apps.arr(app, f"{kind}/{info['file_id']}")
            info.update(scene_name=got.get("sceneName"), score=[old.get("customFormatScore"), got.get("customFormatScore")])
        except Exception as ex:   # the file is linked, and the score below decides
            info["scene_name_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:200]
            with contextlib.suppress(Exception):
                info["score"] = [old.get("customFormatScore"), apps.arr(app, f"{kind}/{info['file_id']}").get("customFormatScore")]
        was, now_score = (info.get("score") or [None, None])
        if isinstance(was, int) and (not isinstance(now_score, int) or now_score < was):
            ok, info["refused"] = False, f"the custom format score dropped from {was} to {now_score}"
    info["remonitored"] = back = sorted(i for i, m in items.items() if m and not now.get(i))
    if back:
        apps.ARR[app].remonitor(back)
    return ok, info


class Imports:
    """The ManualImports of a backfill's conversion workers. A worker whose item has no import running sends its file at
    once. The files of the item that reach the import meanwhile wait, and the first of them sends them all in one
    command. Sonarr runs one disk command at a time, so the files of a series cost one wait, not one each."""

    def __init__(self):
        self.mu, self.waiting = threading.Lock(), {}   # (app, owner) -> the files that wait, the one being sent first

    def run(self, app, owner, f):
        """Import the file f of item owner, alone or with others of the item. Returns the command's status."""
        me, key = {"file": f, "go": threading.Event()}, (app, owner)
        with self.mu:
            q = self.waiting.setdefault(key, [])
            q.append(me)
            if len(q) == 1:
                me["go"].set()
        me["go"].wait()
        if "status" in me:   # another worker sent it
            return me["status"]
        with self.mu:
            batch = q[:]
        try:
            status = apps.ARR[app].command({"name": "ManualImport", "importMode": "auto", "files": [b["file"] for b in batch]})
        except Exception as ex:   # every waiter gets an answer, so none waits forever
            status = config.mask(f"error: {type(ex).__name__}: {ex}")[:200]
        with self.mu:
            del q[:len(batch)]
            for b in batch[1:]:
                b["status"] = status
                b["go"].set()
            if q:
                q[0]["go"].set()   # it sends the files that came meanwhile
            else:
                del self.waiting[key]
        return status


class Refused(RuntimeError):
    """The app took the new file, but the hook refuses it: its custom format score dropped. convert_undo() puts the
    original back into the app first."""


SWAP_POLL = 600                 # seconds a swap tries for the exclusive lock without the gate, see swap_lock()
EXTRA_ROWS_WAIT = 120           # seconds settle_extras() waits for the app to delete the old record's extra rows


def pending_edit(key=None, entry=None):
    """Set or drop (entry None) one conversion in the store, and return all entries. An entry lives from before the
    swap until the app links the extras to the new file, so a kill leaves a record for pending_recover(). With no key
    it only reads."""
    if key is not None and entry is not None:
        store.put("convert", key, entry)
    elif key is not None:
        store.drop("convert", key)
    return store.items("convert")


def parse_refuses(app, items, names):
    """Why Sonarr would link one of names, the new file's name or an extra, to other episodes, or None.
    The second rescan of settle_extras() links each extra to the episodes its name parses as, and a later rescan reads
    the video's name the same way. So the new name and every extra that goes back must parse as the file's own
    episodes, or some of them, see other_episodes(). A name that does not parse, or parses as other episodes, refuses
    the conversion before anything is written, as scene or absolute numbering can. Radarr
    keeps one movie a folder, so it needs no parse."""
    for p in names:
        why = regrab.other_episodes(app, p, items, os.path.basename(p))
        if why:
            return config.mask(f"Sonarr reads a name as other episodes: {why}")[:300]
    return None


def score_refuses(app, owner, old, new):
    """Why the new file would lose a custom format that scores, or None. The app scores a file on
    its scene name, else its original download path's name, else its file name (CustomFormatCalculationService). A
    ManualImport keeps no download path, and the bulk PUT keeps a scene name only when it reads as a release with a
    group (SceneChecker.IsSceneTitle), the old scene name too. Many scene names do not. So the title may change to
    the new file name. The app's parse API gives the custom formats of both titles, and the item's quality profile
    their scores. A lost format that scores above 0, or a new one below 0, refuses the conversion before the remux. A
    lost format that scores 0 does not."""
    orig = apps.ARR[app].original(owner, old) if old.get("id") else None
    before = old.get("sceneName") or os.path.basename(orig or "") or os.path.basename(old.get("path") or "")
    parse = lambda t: apps.arr(app, "parse?" + urllib.parse.urlencode({"title": t})) or {}
    def kept(t):   # the bulk PUT keeps a scene name only when it reads as a release with a group (IsSceneTitle)
        info = (parse(t).get(apps.ARR[app].parsed) or {}) if t else {}
        return bool(t and "." in t and " " not in t and info.get("releaseGroup")
                    and ((info.get("quality") or {}).get("quality") or {}).get("name") not in (None, "Unknown"))
    cand = os.path.splitext(os.path.basename(orig or ""))[0] if apps.ARR[app].film else ""   # relink() sends it on Radarr only
    after = next((t for t in (old.get("sceneName"), cand) if kept(t)), None) or os.path.basename(new)
    if after == before:
        return None
    item = apps.arr(app, f"{apps.ARR[app].kind}/{owner}") or {}
    score = {f.get("format"): f.get("score") or 0 for f in (apps.arr(app, f"qualityprofile/{item.get('qualityProfileId')}") or {}).get("formatItems") or []}
    a, b = ({c["id"]: c["name"] for c in parse(t).get("customFormats") or []} for t in (before, after))
    lost = sorted(f"{a[i]} ({score.get(i, 0):+d})" for i in a.keys() - b.keys() if score.get(i, 0) > 0)
    gained = sorted(f"{b[i]} ({score.get(i, 0):+d})" for i in b.keys() - a.keys() if score.get(i, 0) < 0)
    if lost or gained:
        return config.mask(f"the new name loses custom formats: {before!r} gives {sorted(a.values())}, {after!r} gives {sorted(b.values())}"
                    + (f", lost {', '.join(lost)}" if lost else "") + (f", gained {', '.join(gained)}" if gained else ""))[:300]
    return None


def hide_extras(paths):
    """Move each extra into hide_dir beside it. The apps skip a hidden folder in a scan (DiskScanService,
    ExcludedSubFoldersRegex), and so does Plex. Returns [[hidden path, path], ...]. A name taken there raises before
    anything moves, and a failed move puts the moved ones back."""
    pairs = [[os.path.join(os.path.dirname(p), config.CFG.hide_dir, os.path.basename(p)), p] for p in paths if os.path.lexists(p)]
    taken = [h for h, _ in pairs if os.path.lexists(h)]
    if taken:
        raise RuntimeError(f"the hidden name {taken[0]} is taken")
    done = []
    try:
        for h, p in pairs:
            os.makedirs(os.path.dirname(h), exist_ok=True)
            os.rename(p, h)
            done.append([h, p])
    except BaseException:
        show_extras(done)
        raise
    return pairs


def show_extras(pairs):
    """Move hidden extras back to their names and remove an empty hide_dir. Returns why an extra did not go back: its
    name was taken meanwhile, and the hidden copy stays."""
    left = []
    for h, p in pairs:
        taken = f"{p} is taken, the extra stays at {h}"
        try:
            if os.path.lexists(p):
                left.append(taken)
            elif os.path.lexists(h):
                try:   # a link fails on a name taken since the check. A rename would replace that file.
                    os.link(h, p, follow_symlinks=False)
                except OSError as ex:
                    if ex.errno not in vault.LINK_REFUSED:
                        raise
                    os.rename(h, p)   # a file system with no hard links
                else:
                    os.remove(h)
        except FileExistsError:
            left.append(taken)
        except OSError as ex:
            left.append(config.mask(f"{p}: {ex}")[:200])
    for d in {os.path.dirname(h) for h, _ in pairs}:
        with contextlib.suppress(OSError):
            os.rmdir(d)
    return left


def app_now(app, owner, items, tries=3):
    """The path the app lists for each item of items, read up to tries times 5 seconds apart, or None when the app does
    not answer. A rollback rests on it, so it never deletes a file the app lists."""
    for k in range(tries):
        try:
            paths, of = apps.ARR[app].files(owner, items)
            return [paths.get(of.get(i, (None, None))[1]) for i in sorted(items)]
        except Exception:
            if k + 1 < tries:
                time.sleep(5)
    return None


def listed_id(app, owner, items):
    """The file id the app lists for the first of items now, or None when the app does not answer. It reads no file
    record, see files()."""
    with contextlib.suppress(Exception):
        return next((f for _, f in apps.ARR[app].files(owner, items, read=False)[1].values()), None)


def convert_undo(app, owner, old, items, path, new, held, hidden, lock, refused):
    """Undo a conversion that stopped after the new file was in place, by what the app lists now.
    Returns (result, info). "completed": the app lists the new file for every item and nothing refused it, so the
    conversion ends instead and the original goes. "restored": the app lists the original again, after a second
    ManualImport when needed, and the new file and the hidden extras went back. "stranded": the app did not answer or
    lists something else. Both files then stay under their names and the extras stay hidden, for a person. The
    original goes back to its name under the exclusive lock, and no app call holds the lock."""
    listed = app_now(app, owner, items)
    out = {"listed": listed}
    if listed and set(listed) == {new} and not refused:
        with contextlib.suppress(FileNotFoundError):
            os.remove(held)
        out["file_id"] = listed_id(app, owner, items)   # the new file's record, which the failed relink() did not read
        return "completed", out
    if lock is not None:
        swap_lock(lock)
    try:
        if os.path.lexists(held) and not os.path.lexists(path):
            os.rename(held, path)
    finally:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
    if listed is not None and set(listed) != {path} and os.path.exists(path):   # the original goes back into the app first
        _, out["import"] = relink(app, owner, old, items, path)
        listed = out["listed"] = app_now(app, owner, items)
    if listed and set(listed) == {path}:
        if "import" in out and not out["import"]["file_id"]:   # relink() gives no id when its command failed or the list did not settle
            out["import"]["file_id"] = listed_id(app, owner, items)
        if new != path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(new)
        if "import" in out and hidden:   # the import dropped the old record, and Radarr recycles its extras later
            out["rows_left"] = old_rows_left(app, owner, {old.get("id")})
            if out["rows_left"]:
                return "stranded", out   # the extras stay hidden, and the pending entry keeps them for a person
        out["left"] = show_extras(hidden)
        if "import" in out and hidden:   # a rescan links them to the original's new record
            out["rescan"] = apps.ARR[app].rescan(owner)
        return "restored", out
    return "stranded", out


def busy_tmp(lock, tmp):
    """Raise Replan for a conversion under the shared lock that found its temp file there. Another job of the file
    writes it, or a killed job left it. The file lock goes, and the job waits until tmp is gone, SWAP_POLL seconds at
    most. The other job's swap removes it, so the job runs again under the exclusive lock after that swap. A temp file
    that a killed job left stays, and the run under the exclusive lock writes over it."""
    fcntl.flock(lock, fcntl.LOCK_UN)
    end = time.monotonic() + SWAP_POLL
    while os.path.lexists(tmp) and time.monotonic() < end:
        select.select([], [], [], 0.5)   # a real wait. The tests fake time.sleep.
    raise runner.Replan("another conversion of the file wrote its temp file")


def swap_lock(lock):
    """Trade the file lock for the exclusive one before a conversion's swap or its undo. For up to SWAP_POLL seconds
    it tries without the gate, so a hook job that waits at the gate never queues behind this swap while it waits for
    another worker's remux. The slots keep that bounded: a worker that waits here starts no
    remux. Then it waits at the gate, as an edit does."""
    fcntl.flock(lock, fcntl.LOCK_UN)
    end = time.monotonic() + SWAP_POLL
    while time.monotonic() < end:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            select.select([], [], [], 0.5)
    runner.gated(lock, fcntl.LOCK_EX)


def old_rows_left(app, owner, old_ids):
    """The count of extra rows the app still holds for the file records old_ids, after waiting up to EXTRA_ROWS_WAIT
    seconds for it to reach 0. Radarr recycles the extras of a deleted record in a background task that can run after
    the command ended (IHandleAsync), and deletes their rows last. So no hidden extra may come back while a row is
    left: the task would move it to the bin. Sonarr gives 0, because its handler ends inside the command, see
    Sonarr.extra_rows()."""
    end = time.monotonic() + EXTRA_ROWS_WAIT
    while True:
        left = sum(f in old_ids for _, f, _ in apps.ARR[app].extra_rows(owner) or ())
        if not left or time.monotonic() >= end:
            return left
        time.sleep(2)


def settle_extras(app, owner, keys):
    """Drop the old records of the conversions keys of one item without their extras, then link the extras to the new
    files. keys name pending entries, see pending_edit(). A rescan with the extras hidden drops each old record as missing
    from disk, and no extra goes to the recycle bin. The extras then go back, and a second rescan links them by their
    names (ExistingExtraFileService): Radarr to the movie's file, Sonarr by the episode each name parses as. Radarr then
    reads each extra back. Sonarr 4 has no API for that, see Sonarr.extra_rows(). An entry whose old record is still
    there keeps its extras hidden for the next run. Returns what happened, for the decision log."""
    entries = {k: e for k, e in pending_edit().items() if k in keys}
    a = apps.ARR[app]
    out = {"rescan": a.command(a.rescan_body(owner))}
    try:
        ids = {f.get("id") for f in apps.arr(app, f"{a.file_kind}?{a.owner_key}={int(owner)}") or []}
    except Exception as ex:
        return dict(out, error=config.mask(f"{type(ex).__name__}: {ex}")[:200], waiting=sorted(entries))
    stay = sorted(k for k, e in entries.items() if e.get("old_id") in ids)
    if out["rescan"] != "completed" or stay:
        return dict(out, waiting=stay or sorted(entries))
    try:
        rows_left = old_rows_left(app, owner, {e.get("old_id") for e in entries.values()})
    except Exception as ex:
        return dict(out, error=config.mask(f"{type(ex).__name__}: {ex}")[:200], waiting=sorted(entries))
    if rows_left:
        why = f"extras still hidden: the app kept {rows_left} extra rows of the old record {EXTRA_ROWS_WAIT} s after the rescan"
        for e in entries.values():
            convert_list(app, {"outcome": "extras_hidden", "result": why, "label": os.path.basename(e["path"]), "path": e["path"]})
        return dict(out, waiting=sorted(entries), rows_left=rows_left)
    out["left"] = [x for e in entries.values() for x in show_extras(e["extras"])]
    out["rescan2"] = a.command(a.rescan_body(owner))
    try:
        rows = a.extra_rows(owner)
    except Exception as ex:
        rows, out["read_error"] = None, config.mask(f"{type(ex).__name__}: {ex}")[:200]
    out["extras"] = sum(len(e["extras"]) for e in entries.values())
    if rows is not None or "read_error" in out:   # Sonarr gives None and no read-back
        rows, linked, other = None if rows is None else {r: f for r, f, _ in rows}, 0, []
        for e in entries.values():
            for _, p in e["extras"]:
                if rows is None:
                    break
                got = next((fid for rel, fid in rows.items() if rel and p.endswith("/" + rel)), None)
                if got == e.get("new_id"):
                    linked += 1
                else:
                    other.append(f"{p}: {'not linked' if got is None else f'linked to file {got}'}")
        out.update(linked=linked, not_linked=other)
    for k in entries:
        pending_edit(k)
    return out


# Where a conversion stopped, per state of its pending entry, see convert()
STOPPED = {"swapping": ", before it hid the original", "held": ", after it hid the original", "stranded": ", and putting the original back failed"}


def pending_recover(app, settle_them):
    """The pending conversions of app that no running process owns, see pending_edit(). A converted one gets the
    settle() a kill cut short, when settle_them. Any other stopped between the swap and the import: its original may
    still hide under held_name(), and its extras in hide_dir. Those are printed and logged for a person, posted once per
    pending entry, and nothing moves. Returns the entries it reported. Only a --convert run settles, so the post names
    it as the check, and the worker's names the worker's start, see report.CHECKS.

    G5: the pid of an entry of another pid namespace, as of a container beside the host, means nothing here. So such
    an entry counts as owned while it is younger than JOB_MAX_AGE, and as stopped after that. A restart of a container
    or an LXC gives it a new namespace, so an older entry would otherwise stay hidden for ever."""
    here = pid_ns()

    def alive(e):
        if e.get("pid") == os.getpid():
            return True
        if e.get("pid_ns", here) == here:
            return job_alive_pid(e.get("pid"), e.get("start"))
        try:
            return time.time() - datetime.datetime.fromisoformat(e["time"]).timestamp() < config.JOB_MAX_AGE
        except (KeyError, TypeError, ValueError):
            return False
    rest = {k: e for k, e in pending_edit().items() if e.get("app") == app and not alive(e)}
    done = collections.defaultdict(list)
    for k, e in rest.items():
        if e.get("state") == "converted" and settle_them:
            done[e["owner"]].append(k)
    for owner, keys in sorted(done.items()):
        out = settle_extras(app, owner, keys)
        logs.log(dict(app=app, source="backfill", ids={"app_id": owner}, result="settle", settle=out, note="left by a stopped run"))
    stranded = {k: e for k, e in rest.items() if k not in {x for v in done.values() for x in v}}
    for k, e in stranded.items():
        note = (f"a conversion to MKV finished, and its {len(e.get('extras') or [])} extras wait in {config.CFG.hide_dir} for the next "
                "--convert run" if e.get("state") == "converted" else
                f"a conversion to MKV stopped partway{STOPPED.get(e.get('state'), '')}. The original may be hidden as {e.get('held')}, the "
                f"new file is {e.get('new')}, and {len(e.get('extras') or [])} extras may be hidden in {config.CFG.hide_dir}")
        print(f"STRANDED {e.get('path')}: {note}", flush=True)
        logs.log(dict(app=app, source="backfill", result="warning", path=e.get("path"), note=note))
        with contextlib.suppress(Exception):
            logs.alert_findings(dict(app=app, source="backfill" if settle_them else "worker", label=os.path.basename(e.get("path") or k),
                                     path=e.get("path") or k, findings=[{"kind": "repack", "state": e.get("state"), "note": note}]), k)   # one post per pending entry
    return stranded


def pid_ns():
    """The pid namespace of this process, as /proc/self/ns/pid names it, or None where it does not read."""
    with contextlib.suppress(OSError):
        return os.readlink("/proc/self/ns/pid")
    return None


def job_alive_pid(pid, start=None):
    """Whether a process with pid runs, and with start the same one: its start time in /proc, so a reused pid does not
    count."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        pass
    except (OSError, ValueError, TypeError):
        return False
    return start is None or proc_start(pid) == start


def proc_start(pid):
    """The start time of process pid in clock ticks since boot (/proc/<pid>/stat field 22), or None."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        return int(open(f"/proc/{int(pid)}/stat").read().rsplit(")", 1)[1].split()[19])
    return None


def held_name(path):
    """The name the original holds while the app takes the new file, in hide_dir beside it, as repack_tmp() explains."""
    name = os.fsencode(os.path.basename(path))[:255 - len(b"." + HELD)]
    return os.path.join(os.path.dirname(path), config.CFG.hide_dir, os.fsdecode(b"." + name + HELD))


def convert(app, path, j, st, apply, ids=None, lock=None, pool=None, force=None, sub=False, known=(), shared=False, source="backfill"):
    """Remux a file that is not Matroska, and its sidecar subtitles, into <base>.mkv, under the caller's file lock
    (docs/design.md, "Conversion"). j is its mkvmerge -J probe, st its stat, ids the app's ids of the item. Returns (code,
    result, info, the file's path now). result is "repacked", "would repack: ...", "repack failed: ..." or "skipped, not
    matroska, ...", and code its outcome code, as repacked, would_repack, repack_failed or repack_hardlinked.

    A CEA-608 caption track, which mkvmerge drops, becomes a SubRip track first, see convert_captions(). The remux goes
    to repack_tmp() with no time limit and no new encode. An error fails it, and an mkvmerge warning alone does not.
    prove() then checks every stream at packet level, and the captions and the sidecars by their text. On a name change
    the new file goes in place, and the original holds held_name() while relink() makes the app list the new file. Only
    then the original and the sidecars go. The extras the app tracks for the original's record wait in hide_dir from the
    swap until settle() runs, see Radarr.extras(). A pending entry holds the swap from its start, see pending_edit(). When anything fails
    after the new file is in place, convert_undo() decides by what the app lists: a file the app lists is never
    deleted. Nothing is kept once the new file is in place. SIGTERM waits for the swap to end.

    lock is the held file lock: shared in a backfill worker and in a job process of the hook, so several files convert
    at once, exclusive in the one-file worker and in a re-run. shared says the lock is held shared. Then a temp file
    that is there already raises Replan, see busy_tmp(), so a second conversion of one file never writes the first
    one's temp file. The app's record is read again before the swap. The swap then takes the lock exclusive, see
    swap_lock(), checks the original's inode, size and mtime once more, checks that the temp file is the one the proof
    read, see Swap.check(), and renames. The app's import holds no lock, and an undo takes it exclusive again. The lock
    comes back free once the remux started. pool is the worker's pool, see backfill_files(): the import holds no worker
    slot, and pool.imports sends it with the other files of the item that wait. Only the main thread sets a signal
    handler.

    force is None for a file no person listed with --force-convert. For a listed file it is the last proof refusal of
    the file in the decision log, as its result text, or "" when the log holds none, see logged_refusals(). A person
    checked it and accepts it. The file then converts when prove() refuses it for that same reason. info names the
    refusal in forced, and the original is kept in originals_root() for keep_days, as a header repair keeps it. Another
    refusal is not forced, and info names the logged one in not_forced. A listed Sonarr file also skips
    parse_refuses() for its own new name, because the ManualImport names its episodes by id. info names the parse result
    in forced_name, and the original is kept too. Its extras still go back by a rescan, which links them by their parsed
    names, so each extra must still parse as the file's episodes. Every other check still runs.

    source is the source of the decision log, hook, deep_analysis or backfill. The converting line carries it.

    info names a sign that the original is damaged in damage, see DAMAGE. process() then re-grabs an import.

    sub runs the subtitle match check first, see convert_subs(): a sidecar that does not match the audio is moved aside,
    a built-in text track that does not match is left out of the remux, and a sidecar whose times need a fix is muxed
    with new times. A conversion that leaves a track out keeps the original, as a forced one does. known holds the item's original languages, see sub_hold()."""
    c = j.get("container") or {}
    unreadable = c.get("recognized") is False or c.get("supported") is False   # mkvmerge cannot read ASF or WMV
    container = c.get("type") or ("a container mkvmerge cannot read" if unreadable else "unknown")
    new, subs = os.path.splitext(path)[0] + ".mkv", [] if unreadable else proof.sidecar_subs(path)
    dur = decide.duration(j) or checks.ffprobe_duration(path) or 0
    late = [s for s in subs if s["end"] > dur + max(decide.HEADER_OFF, decide.HEADER_SHARE * dur) or not s["ordered"]]
    subs = [s for s in subs if s not in late]
    audio = {t["lang"] for t in decide.classify(j) if t["kind"] == "a" and t["role"] == "main"}
    for s in subs:   # a sidecar whose text reads as another language than its name is muxed with the text's language
        base = s["lang"].split("-")[0]
        got = decide.sidecar_language(checks.langs()[0].get(base) or (base if len(base) == 3 else None), s["read"], audio)
        if got:
            s.update(lang=got[0], mismatch=got[2], flags=s["flags"] if got[1] else [f for f in s["flags"] if f != proof.SIDECAR_FLAGS["forced"]])
    info = {"container": container, "old_size": st.st_size, "tracks": checks.track_list(j),
            "sidecars": [{**{k: s[k] for k in ("name", "lang", "flags")}, "read": s["read"][0], **({"mismatch": s["mismatch"]} if "mismatch" in s else {})}
                         for s in subs], **({"old_path": path, "new_path": new} if new != path else {})}
    if late:   # it stays beside the file, where it keeps the base name the player looks for
        info["sidecars_left"] = [f"{s['name']}: its cues are out of order" if not s["ordered"] else
                                 f"{s['name']}: its last cue ends at {content.hms(s['end'])}, the file at {content.hms(dur)}" for s in late]
    try:
        streams = proof.ff_streams(path)[1]
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as ex:   # why: the (code, text) of a skip
        streams, why = [], ("repack_unreadable", config.mask(f"ffprobe cannot read it: {ex}")[:200])
        if damage := proof.damage_of("ffprobe", str(ex)):
            info["damage"] = damage
    else:
        why = None
    caps = [] if unreadable else [s for s in proof.media_streams(streams) if s.get("codec_name") in proof.CAPTIONS]
    what = f"the container is {container}" + (", renamed to .mkv" if new != path else "") + (f", {len(subs)} sidecar subtitles" if subs else "") \
        + (f", {len(caps)} CEA-608 caption tracks as SubRip" if caps else "")
    why, old, items, cc, drop = why or convert_skip(app, path, new, st, ids), {}, {}, {}, []
    said = lambda code, text: (code, text) if text else None
    if not why and new != path:   # read-only: the app's record of the original, which the ManualImport copies
        old, items, home = apps.ARR[app].record(ids)
        why = said("repack_app_refused", app_refuses(path, old, items, home))
        if not why and not apps.ARR[app].film:   # the new name and the extras that go back must parse as this file's episodes
            muxed = {s["path"] for s in subs}
            extras = [x for x in apps.ARR[app].extras(ids["app_id"], old.get("id"), path, home, items) if x not in muxed]
            why = said("repack_parse_refused", parse_refuses(app, items, extras if force is not None else [new, *extras]))
            if not why and force is not None and (name := parse_refuses(app, items, [new])):   # the relink names the episodes by id
                info["forced_name"] = name
        why = why or said("repack_score_refused", score_refuses(app, ids["app_id"], old, new))
    folder = runner.work_dir("convert")
    if not why and sub and not unreadable:
        try:
            subs, drop = convert_subs(path, j, streams, subs, dur, apply, folder, info, known)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
    if not why and caps:   # a read of the whole file, in a dry run too, so the dry run lists a caption track with no text
        try:
            cc, why = convert_captions(path, caps, folder, st.st_size)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        why = said("repack_no_captions", why)
        info["captions"] = [{"stream": i, "cues": c["cues"]} for i, c in (cc or {}).items()]
    if why or not apply:
        shutil.rmtree(folder, ignore_errors=True)
        if why:
            return why[0], f"skipped, not matroska, {why[1]}: {what}", info, path
        return "would_repack", f"would repack: {what}", info, path
    tmp, held, state = remux.repack_tmp(path), held_name(path), "remux"
    key, entry, hidden = f"{app}:{os.getpid()}:{uuid.uuid4().hex[:8]}", None, []   # the pending entry, and the hidden extras
    if shared:
        try:
            remux.new_tmp(tmp, excl=True)
        except FileExistsError:   # before the Swap, so this job never removes the other job's temp file
            shutil.rmtree(folder, ignore_errors=True)
            busy_tmp(lock, tmp)
        except OSError:
            pass   # the create below fails the same way, and the conversion fails with it
    sw = remux.Swap(path, st, tmp, info)
    try:
        remux.new_tmp(tmp)
        r = subprocess.run(convert_cmd(path, tmp, j, subs, streams if unreadable else None, list(cc.values()), [t for t, _ in drop]),
                           capture_output=True, text=True, errors="replace")
        info["warnings"] = config.mask((r.stdout + r.stderr).strip())[-500:] or None
        # mkvmerge exits 1 on warnings alone. A warning can be harmless, such as zero bytes it skips at an audio end, so
        # the proof decides. An error of mkvmerge, and any message of ffmpeg, keep the original.
        if r.returncode not in (0, 1) or (unreadable and (r.returncode or info["warnings"])):
            raise RuntimeError(f"{'ffmpeg' if unreadable else 'mkvmerge'} exited {r.returncode}: {info['warnings']}")
        out = "" if unreadable else r.stdout + r.stderr   # a warning of invalid audio data, see audio_damage()
        sw.sync()
        new_j = checks.mkvmerge(tmp)
        if (new_j.get("container") or {}).get("type") != "Matroska":
            raise RuntimeError(f"the new file reads as {(new_j.get('container') or {}).get('type')}")
        refused, info["proof"] = proof.prove(path, tmp, subs, folder, cc, **({"dropped": {i for _, i in drop}} if drop else {}))
        fault = refused and refused[1]
        seen = fault and config.mask(f"repack failed: {fault}")[:500]   # the refusal as the decision log holds it
        if fault and force and seen != force:
            info["not_forced"] = f"the last refusal in the decision log differs: {force.partition(': ')[2][:200]}"
        if fault and seen != force:
            if damage := proof.audio_damage(out, j, streams, dur, refused):   # invalid data, and the same audio stream refused
                info["damage"] = damage
            raise RuntimeError(fault)
        if fault:
            info["forced"] = fault
        size = sw.own()
        extras = []
        if new != path:   # again, right before the swap and outside the exclusive lock: the app may have changed the file meanwhile
            old, items, home = apps.ARR[app].record(ids)
            why = app_refuses(path, old, items, home)
            if why:
                raise remux.SourceChanged(f"the app changed the file during the repack: {why}")
            muxed = {s["path"] for s in subs}   # they go with the original once the new file holds them
            extras = [x for x in apps.ARR[app].extras(ids["app_id"], old.get("id"), path, home, items) if x not in muxed]
            if os.path.lexists(held):
                raise RuntimeError(f"the hidden name {held} is taken")
        if lock is not None:   # the swap needs the lock exclusive. flock cannot upgrade in one step, see exclusive().
            swap_lock(lock)
            state = "locked"
        # A forced conversion, or one that leaves a track out, keeps the original as a hard link. One move puts it back.
        sw.check("the app replaced or renamed the original during the repack", info.get("forced") or info.get("forced_name") or drop)
        sw.release()   # from here a stop waits for the swap and the job's end, as for mkvpropedit
        with runner.no_stop():
            if new == path:
                vault.kept_replaced(path)   # no keep_original() before this rename, unless forced
                os.replace(tmp, path)
                if lock is not None:   # process() takes it again at the gate. Kept, it would deadlock a worker waiting there.
                    fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                entry = dict(app=app, owner=ids["app_id"], items=sorted(items), path=path, new=new, held=held, old_id=old.get("id"), pid=os.getpid(),
                             start=proc_start(os.getpid()), pid_ns=pid_ns(),
                             time=datetime.datetime.now().astimezone().isoformat(timespec="seconds"), state="swapping",
                             extras=[[os.path.join(os.path.dirname(x), config.CFG.hide_dir, os.path.basename(x)), x] for x in extras])
                pending_edit(key, entry)   # before the first move, so a kill from here leaves a record, see pending_recover()
                logs.log(dict(app=app, source=source, outcome="converting", result="converting", path=path, new_path=new, held=held, extras=len(extras)))
                hidden = hide_extras(extras)
                info["extras_hidden"] = len(hidden)
                os.link(tmp, new)   # fails when the name is taken meanwhile
                state = "placed"
                os.rename(path, held)   # while the temp file still holds the hidden folder, see new_tmp()
                state = "held"
                pending_edit(key, dict(entry, state="held"))
                os.remove(tmp)
                if lock is not None:   # the app's import holds no lock
                    fcntl.flock(lock, fcntl.LOCK_UN)
                if pool:   # and no worker slot, so another file's remux runs meanwhile
                    pool.slots.release()
                try:
                    ok, info["relink"] = relink(app, ids["app_id"], old, items, new, pool and pool.imports)
                finally:
                    if pool:
                        pool.slots.acquire()
                if info["relink"].get("refused"):
                    raise Refused(info["relink"]["refused"])
                if not ok:
                    raise RuntimeError(f"the app did not take the new file: import {info['relink']['import']}, it lists {info['relink']['listed']}")
                if hidden:   # settle() moves them back once the app dropped the old record
                    pending_edit(key, dict(entry, state="converted", new_id=info["relink"]["file_id"]))
                    info["pending"] = key
                else:
                    pending_edit(key)
            state = "done"
            for f in ([] if new == path else [held]) + [s["path"] for s in subs]:   # the sidecars are proven in the new file by their text
                try:
                    os.remove(f)
                except OSError as ex:
                    info.setdefault("left", []).append(config.mask(f"{f}: {ex}")[:200])
    except BaseException as ex:
        if isinstance(ex, proof.ReadFailed) and ex.path == path and (damage := proof.damage_of("ffmpeg", str(ex))):
            info["damage"] = damage   # the proof's read of the original, see packet_hashes(). The temp file has another name.
        if lock is not None:   # no app call holds the lock. convert_undo() takes it again for the rename.
            fcntl.flock(lock, fcntl.LOCK_UN)
        undo = None
        if state in ("placed", "held"):
            try:
                undo, info["restored"] = convert_undo(app, ids["app_id"], old, items, path, new, held, hidden, lock, isinstance(ex, Refused))
            except Exception as again:
                undo, info["restored"] = "stranded", {"error": config.mask(f"{type(again).__name__}: {again}")[:200]}
            info["restored"]["result"] = undo
        elif hidden:   # nothing is in place: the old record still holds them
            info["extras_left"] = show_extras(hidden)
        if undo == "completed":   # the app took the new file after all, so the conversion ends
            info["relink"] = dict(info.get("relink") or {}, file_id=info["restored"].get("file_id"), listed=info["restored"]["listed"])
            if hidden:
                pending_edit(key, dict(entry, state="converted", new_id=info["restored"].get("file_id")))
                info["pending"] = key
            else:
                pending_edit(key)
            for f in [s["path"] for s in subs]:
                with contextlib.suppress(OSError):
                    os.remove(f)
        elif undo == "stranded":
            pending_edit(key, dict(entry, state="stranded", error=config.mask(str(ex))[:200]))
        elif entry is not None:
            pending_edit(key)
        if undo != "completed":   # the original stays, at its name or the held name, so its kept link goes
            sw.unkeep(path, held)
        if not isinstance(ex, Exception):
            raise
        if undo == "completed":
            info.update(new_size=size, new_tracks=checks.track_list(new_j))
            return "repacked", "repacked", info, new
        stranded = ", the original and the new file both stay for a person" if undo == "stranded" else ""
        return ("repack_source_changed" if isinstance(ex, remux.SourceChanged) else "repack_failed",
                config.mask(f"repack failed{', the original changed' if isinstance(ex, remux.SourceChanged) else ''}{stranded}: {ex}")[:500], info, path)
    finally:
        sw.close(folder)   # the temp file last, after the extras are back. A temp file hidden with them came back too.
    if info.get("kept"):
        vault.prune_originals(vault.originals_root(path))
    info.update(new_size=size, new_tracks=checks.track_list(new_j))
    return "repacked", "repacked", info, new


def convert_list(app, rec):
    """Add a conversion that was refused or failed to <STATE_DIR>/convert-<app>.txt, the list to review by hand."""
    with contextlib.suppress(OSError), open(os.path.join(config.CFG.state_dir, f"convert-{app}.txt"), "a") as f:
        f.write(f'{rec.get("time") or datetime.datetime.now().astimezone().isoformat(timespec="seconds")}\t{rec["outcome"]}\t'
                f'{rec.get("label")}\t{rec["result"][:300]}\t{rec["path"]}\n')
