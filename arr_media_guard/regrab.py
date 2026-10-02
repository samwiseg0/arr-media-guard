# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The re-grab of a broken or wrong download, and the restore of the old file of an upgrade."""
import contextlib, json, os, re, tempfile, time, urllib.parse, uuid

from . import apps, checks, config, content, decide, logs, plex, process, report, runner, store, vault


def video_probe(path, f, fresh=False):
    """probe() for regrab(): the video check from scratch under its own time limit. Returns (the fault class of a
    certain verdict or None, log fields), so the second check must find the same kind of damage. fresh is the second
    check, which samples other parts of the file, see check_video()."""
    config.DEADLINE.start(config.BUDGET)
    certain, doubts, fields = checks.video_check(path, again=fresh)
    return fields["fault"], {"video": checks.video_summary(certain, doubts, fields)}


def planned(path, j, original, kids, release, heard=None, spoken=None):
    """The flag edits the rules plan for a file. Empty for a non-mkv file, which is never edited."""
    return decide.decide(j, original, kids, release, heard, spoken)["edits"] if path.lower().endswith(".mkv") else []


def audio_probe(original, kids, release, runtime, heard=None, spoken=None):
    """probe() for regrab(): probe and sample a file from scratch. Returns (certain audio fault or None, log fields).
    f is None for the job's own file, else its unit_files() entry. Each probe gets its own time limit. Every probe
    samples the audio again, so fresh changes nothing here."""
    def probe(path, f, fresh=False):
        config.DEADLINE.start(config.BUDGET)
        j = checks.mkvmerge(path)
        certain, doubts, samples = checks.check_audio(path, j, planned(path, j, original, kids, release, *((heard, spoken) if f is None else ())),
                                               f["runtime"] if f else runtime)
        return certain, {"audio": logs.audio_summary(certain, doubts, samples)}
    return probe


def without_language(ev, why):
    """The wrong-content evidence with its language points taken out, and why in their reason."""
    sig = [dict(s, points=0, why=f'{s["why"]}, {why}') if s["kind"] in ("language", "release_language") else s for s in ev["signals"]]
    points = sum(s["points"] for s in sig)
    return dict(ev, signals=sig, points=points, regrab=points >= content.REGRAB_POINTS, why="; ".join(s["why"] for s in sig if s["points"]))


def content_probe(app, owner, original, kids, release, ctx, heard=None):
    """probe() for regrab(): the metadata checks on a new probe. Returns ("wrong content" or None, log fields).

    Only files of the job's own series count, owner is its series id. Radarr imports one film per file, so another
    file of the download is another film and is never judged. A file it does not judge returns (None, {"skipped"}).
    regrab() calls it for the job's own file (f None) only as the second check. That check hears the audio again
    past the cache, and asks TMDB past its cache. When it cannot hear what the first check heard, the language
    points do not count. fresh asks TMDB past its cache for another file of the unit. A subtitle position in heard got
    its language from its text, and no hearing repeats that."""
    heard = {p: x for p, x in (heard or {}).items() if p.startswith("a")}
    def probe(path, f, fresh=False):
        config.DEADLINE.start(config.BUDGET)
        if f is not None and (other := apps.ARR[app].other(f, owner)):
            return None, {"skipped": other}
        c = ctx if f is None else dict(ctx, listed=[e.get("runtime") or 0 for e in f["eps"]])
        j = checks.mkvmerge(path)
        mkv = path.lower().endswith(".mkv")
        d = decide.decide(j, original, kids, release) if mkv else {"tracks": decide.classify(j), "reasons": []}
        again = {}
        if f is None and mkv and (heard or config.LID_WHEN & set(d["reasons"])) and decide.duration(j) >= config.LID_MIN_SECONDS:
            again = checks.hear(path, j, d, original, fresh=True)[0]
            if again:
                d = decide.decide(j, original, kids, release, again)
        m = process.metadata(app, path, j, os.path.getsize(path), d, original, release, c, fresh=fresh or f is None)
        ev = m["evidence"]
        if f is None and any(pos not in again for pos in heard):
            ev = without_language(ev, "not heard again on the second check")
        return ("wrong content" if ev["regrab"] else None), {"evidence": ev, "trusted": m["trusted"], "heard_again": again}
    return probe


def save_unit(app, download_id, unit):
    """Store the unit of download_id of the instance app, see runner.download_unit(). A unit older than 7 days goes."""
    with store.tx():
        store.drop("unit", older=time.time() - 7 * 86400)
        store.put("unit", f"{app}|{download_id}", unit, unit["time"])


def regrab_times(app):
    """The re-grab times of app in the last 24 hours. Every kind of re-grab shares the one count."""
    return [t for t in store.get("regrabs", app, []) if time.time() - t < 86400]


def count_regrab(app):
    """Count one re-grab of app. The read, the cap check and the write run in one store transaction, so parallel job
    processes never lose a count or pass REGRAB_CAP together. False when the cap is reached."""
    with store.tx():
        recent = regrab_times(app)
        if len(recent) >= config.CFG.regrab_cap:
            return False
        store.put("regrabs", app, recent + [time.time()])
    return True


def job_episodes(job):
    """The episode ids of a Sonarr job, each once and in order. A list with an id twice makes Sonarr answer 500."""
    return sorted({int(i) for i in str(job.get("episode_ids") or "").split(",") if i.strip()})


def unit_files(app, records, job):
    """The files still in the library that this download imported: {file id: {"path", "items", "eps", "runtime", "owner"}}.

    items are the episode ids (Sonarr) or the movie id (Radarr) whose current file is that file, and owner is the
    movie or the series. The job's own file is always in it, even when its history record is not written yet. A manual
    import can map a file of the download to another series. Such a file is left out and logged, and its own job checks it.
    """
    imported = [h for h in records if h.get("eventType") == "downloadFolderImported" and (h.get("data") or {}).get("fileId")]
    out = apps.ARR[app].unit_files(imported, {int(h["data"]["fileId"]) for h in imported} | {int(job["file_id"])}, job)
    for f in out.values():
        f.setdefault("runtime", apps.episode_runtime(f["eps"]))
        f.setdefault("owner", int(job["owner"]))
    return out


# The outcome codes of the two results of report.FAULTS. A clean file of a damage unit has no code of its own.
RESULT_CODES = {"audio": ("broken_audio", "audio_checked"), "content": ("wrong_content", "content_checked"),
                "video": ("corrupt_video", "video_checked"), "damage": ("damaged_source", "other")}


def regrab(app, job, fault, probe, kind="audio"):
    """A download is one unit. Check every file it imported, delete each faulty one, re-monitor their items, then
    mark the grab failed once. That order matters: the app searches at once, and a faulty file still on disk would
    make it reject the replacement as not an upgrade. Clean files stay. Returns the action, {"code", **facts}, which
    report.ACTIONS words. code is regrabbed, would_regrab, unconfirmed, capped, no_grab or failed. The facts of a
    re-grab that put old files back come from restore_facts().

    fault is the certain fault found in the job's file. probe(path, f, fresh) checks a file from scratch and returns
    (its certain fault or None, log fields), see audio_probe(), content_probe(), video_probe() and damage_probe(). Every
    second check passes fresh=True. kind is audio, content, video or damage. Every kind counts against the one
    REGRAB_CAP of the app. A kind REGRAB does not list stops after the second check of the job's file and deletes
    nothing.
    """
    name, dry = apps.ARR[app].name, kind not in config.CFG.regrab
    records = []
    if job.get("download_id"):
        records = apps.arr(app, "history?" + urllib.parse.urlencode({"downloadId": job["download_id"], "pageSize": 1000})).get("records", [])
    grabs = [h for h in records if h.get("eventType") == "grabbed"]
    if not grabs and config.CFG.restore and job.get("file_id") and job.get("deleted") and not dry:   # a manual import that replaced a file
        return restore_manual(app, job, fault, probe)
    if not grabs or not job.get("file_id"):
        return {"code": "no_grab", "name": name}
    unit = store.get("unit", f"{app}|{job['download_id']}")
    capped = {"code": "capped", "cap": config.CFG.regrab_cap}
    if not unit and len(regrab_times(app)) >= config.CFG.regrab_cap:   # a first look. count_regrab() counts in its transaction.
        return capped
    try:
        # A NAS error that clears, or a header that probes differently, must never cost a file: check again first.
        if probe(job["path"], None, fresh=True)[0] != fault:
            return {"code": "unconfirmed"}
        if dry:
            return {"code": "would_regrab", "kind": kind}
        files = unit_files(app, records, job)
        broken = {int(job["file_id"])}
        unit = unit or {"time": time.time(), "failed": False, "deleted": [], "clean": [], "kind": kind}
        for fid, f in files.items():
            if fid in broken or fid in unit["clean"] + unit["deleted"] or not os.path.exists(f["path"]):
                continue
            started = time.time()
            other, fields = probe(f["path"], f)
            if "skipped" in fields:   # never judged, so it gets no decision line and stays out of clean
                logs.log(dict(app=app, source="hook", path=f["path"], download_id=job["download_id"], result="warning",
                         note=f'not judged for {report.FAULTS[kind][0]}: {fields["skipped"]}'))
                continue
            if other and probe(f["path"], f, fresh=True)[0] == other:   # the second check of every file, from scratch
                broken.add(fid)
            else:
                unit["clean"].append(fid)
            logs.decision(dict(id=uuid.uuid4().hex[:12], app=app, source="hook", apply=True, ids={"file_id": fid, "items": f["items"]},
                          label=os.path.basename(f["path"]), path=f["path"], download_id=job["download_id"],
                          result=f"{report.FAULTS[kind][0]}: {other}" if fid in broken else report.FAULTS[kind][1],
                          outcome=RESULT_CODES[kind][0] if fid in broken else RESULT_CODES[kind][1], **fields), started)
        plans = restore_plans(app, job, files, broken)   # read-only: which old files of an upgrade may come back
    finally:
        config.DEADLINE.stop()
    if not unit["failed"] and not unit["deleted"] and not count_regrab(app):   # a new unit counts once, and only now that it acts
        return capped
    step, done = "the delete", {"code": "regrabbed", "name": name, "kind": kind, "n": len(broken)}
    with runner.no_stop():   # the changes finish before a SIGTERM lands, so a stop never leaves a deleted file unmarked
        try:
            restoring(app, job, plans, broken)
            for fid in sorted(broken):
                apps.arr_write(app, f"{apps.ARR[app].file_kind}/{fid}", "DELETE")
                unit["deleted"].append(fid)
            step = "the restore"
            back = restore(job, plans)   # after the delete: a delete after a same-name restore would take the old file
            step = "the re-monitor"
            apps.ARR[app].remonitor(sorted({i for fid in broken for i in files[fid]["items"]}))
            rescan_back(app, back)
            read_back(app, back, files)
            if unit["failed"]:
                return dict(done, failed_before=True, **restore_facts(app, job, back))
            step = "marking the grab failed"
            apps.arr_write(app, f'history/failed/{grabs[0]["id"]}', "POST")
            unit["failed"] = True
        except Exception as ex:
            return {"code": "failed", "step": step, "error": config.mask(f"{type(ex).__name__}: {ex}")[:300]}
        finally:
            save_unit(app, job["download_id"], unit)   # a unit is written only under the exclusive file lock
    return dict(done, **restore_facts(app, job, back))


KEEP_MARGIN = 3600   # seconds before keep_days ends that a kept copy stays out of a restore, so the prune never takes it then
RESTORE_WAIT = 120   # seconds a restore waits for the app's rescan before it reads the file record back
EXTRA_SLACK = 300    # seconds between the recycle of an old video file and of its extras. The app moves them in one upgrade.
EXTRA_WAIT = 30      # seconds a restore waits for Radarr to move the broken file's own extras off a same-name path
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm", ".mov", ".flv", ".vob", ".iso")


def old_files(job):
    """([(old path, recycle bin path)], why none may come back) for the files an upgrade job replaced. The app passes
    <app>_deletedpaths and <app>_deletedrecyclebinpaths, pipe-separated in the same order, and hook() keeps them in the
    job. A recycle bin path is empty when the app has no recycle bin or the old file was not on disk. ([], None) for an
    import that replaced nothing."""
    if not job.get("deleted"):
        return [], None
    old, rb = job["deleted"].split("|"), (job.get("recycled") or "").split("|")
    return (list(zip(old, rb)), None) if len(old) == len(rb) else ([], "the app's lists of old paths and recycle bin paths do not pair up")


def upgrade_jobs(app, download_id):
    """{file id: job} of the queued and claimed jobs of one download of the instance app. Each job carries the old files
    of its upgrade. A job of another instance never counts, because its file ids are that instance's."""
    # ponytail: a job leaves the store when it ends, so the old file of a sibling whose job ended first stays in the bin.
    # Keep the old files in the unit if that ever costs a restore.
    out = {}
    for raw, in store.db().execute("SELECT job FROM jobs WHERE name NOT LIKE 'deep-analysis-%' AND due <= ? ORDER BY claimed, name",
                                   (time.time_ns(),)):
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            j = json.loads(raw)
            if download_id and j.get("download_id") == download_id and j.get("app") == app and j.get("file_id"):
                out[int(j["file_id"])] = j
    return out


def volume(path):
    """(st_dev, mount_top()) of path, or of its nearest folder that exists. The app may remove an empty folder after a
    delete. Two bind mounts of one file system share st_dev, but a rename between them fails with EXDEV, so the mount
    top counts too. The mount top comes from the real path, so a root folder named through a symlink finds the mount
    that holds it."""
    while path not in ("", "/") and not os.path.exists(path):
        path = os.path.dirname(path)
    return os.stat(path).st_dev, vault.mount_top(os.path.realpath(path))


def bin_warnings(app):
    """Why the restore after a bad upgrade cannot work for app, as warnings: the app has no recycle bin, its bin does not
    exist where the hook runs, or it sits on another file system than a root folder, and a restore only renames. With
    KEEP_REPLACED on, keep_warnings() come first. When they find nothing, a bin warning says that the hook's own copies
    stand in. [] when all is well or the app does not answer. --selftest and the Test event print them, and neither
    fails on them."""
    name = apps.ARR[app].name
    try:
        rbin = (apps.arr(app, "config/mediamanagement") or {}).get("recycleBin") or ""
        roots = [r["path"] for r in apps.arr(app, "rootfolder")]
        out = keep_warnings(app, roots) if config.CFG.keep_replaced else []
    except Exception:
        return []
    own = " The hook keeps its own copy of each file an upgrade replaces, so the restore after a bad upgrade still works." \
        if config.CFG.keep_replaced and not out else ""
    if not rbin:
        return out + [f"{name} has no recycle bin." + own if own else f"{name} has no recycle bin, so the restore after a bad upgrade "
                      "cannot work. Set one in Media Management, on the media's file system."]
    if not os.path.isdir(rbin):   # a Docker mount or a path map that leaves it out
        return out + [f"{name}'s recycle bin {rbin} does not exist where the hook runs." + own if own else f"{name}'s recycle bin {rbin} "
                      "does not exist where the hook runs, so the restore after a bad upgrade cannot use it. Mount it at that path, or "
                      f"{apps.map_fix(app)}."]
    with contextlib.suppress(OSError):
        other = [r for r in roots if volume(r) != volume(rbin)]
        if other:
            return out + [f"{name}'s recycle bin {rbin} is on another file system than {', '.join(other)}." + own if own else
                          f"{name}'s recycle bin {rbin} is on another file system than {', '.join(other)}, and the restore after a "
                          "bad upgrade needs a rename. Move the bin onto the media's file system."]
    return out


def keep_warnings(app, roots):
    """Why KEEP_REPLACED keeps nothing for app, as warnings: KEEP_ORIGINALS_DAYS is 0, the app's connection to this hook
    does not send Grab, or the hook cannot hard-link a file on the mount of a root folder in roots. The connection to
    this hook is a Custom Script with this script's path, or a Webhook whose URL path is /radarr or /sonarr. The API
    lists the saved connections, so a Test before a Save sees the old triggers. When keep_root() takes a folder below
    the mount top, a warning says where the grab links go."""
    name, out = apps.ARR[app].name, []
    if not config.CFG.keep_days:
        out.append("KEEP_REPLACED is on, but KEEP_ORIGINALS_DAYS is 0, so the hook keeps nothing at a grab. Set KEEP_ORIGINALS_DAYS above 0.")
    ours = []
    for n in apps.arr(app, "notification") or []:
        f = {x.get("name"): x.get("value") for x in n.get("fields") or []}
        if (n.get("implementation") == "CustomScript" and os.path.realpath(f.get("path") or "/") == config.SCRIPT) or \
                (n.get("implementation") == "Webhook" and urllib.parse.urlparse(f.get("url") or "").path.rstrip("/") == f"/{app}"):
            ours.append(n)
    if ours and not any(n.get("onGrab") for n in ours):
        out.append(f"KEEP_REPLACED is on, but {name}'s connection {', '.join(str(n.get('name')) for n in ours)} does not send Grab, so the "
                   "hook keeps nothing. Turn on On Grab in that connection.")
    seen = set()
    for r in sorted(roots):
        root = vault.replaced_root(os.path.join(r, "x"))
        if root in seen:
            continue
        seen.add(root)
        why, top = link_probe(root), vault.mount_top(r)
        if why:
            out.append(f"KEEP_REPLACED is on, but {why}")
        elif os.path.dirname(root) != top:   # keep_root() took a folder below the mount top
            blocked = os.path.join(top, config.CFG.recycle_dir) if os.path.lexists(os.path.join(top, config.CFG.recycle_dir)) else top
            out.append(f"uid {os.getuid()} and gid {os.getgid()} cannot write in {blocked}, so the hook keeps the grab links of {r} in {root}."
                       + (f" {name}'s Library Import lists that folder as unmapped. Do not import it." if os.path.dirname(root) == r.rstrip("/") else ""))
    return out


def link_probe(root):
    """Why the hook cannot keep a file in root, as the end of a warning, or None. It links a small temp file in root,
    or in the folder above it while root does not exist, and removes both. A folder it cannot write names the uid and
    gid of this run, which PUID and PGID set in Docker."""
    where = root if os.path.isdir(root) else os.path.dirname(root)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".link-probe-", dir=where)
        os.close(fd)
    except OSError as ex:
        give = f"Give {root} to them." if where == root else f"Create {root} and give it to them."
        return (f"uid {os.getuid()} and gid {os.getgid()} cannot write in {where} ({ex.strerror or ex}), so the hook keeps nothing on that mount. "
                f"{give} In Docker, PUID and PGID set them.")
    try:
        os.link(tmp, tmp + ".link")
    except OSError as ex:
        return (f"{where} takes no hard link ({ex.strerror or ex}), so the hook keeps nothing on that mount. Keep the media on a file system "
                "that takes hard links.")
    finally:   # also after a time limit between the link and its removal
        for f in (tmp + ".link", tmp):
            with contextlib.suppress(OSError):
                os.remove(f)
    return None


def old_file_check(path, original, kids, runtime):
    """The audio and video checks of an old file in the recycle bin, the same an import gets, under a fresh time limit.
    Returns (why it may not go back or None, log fields). A certain fault keeps it out, and so does a video check that
    did not run to its end. A doubt does not, because the file played in the library before the upgrade."""
    config.DEADLINE.start(config.BUDGET)
    j = checks.mkvmerge(path)
    certain, doubts, samples = checks.check_audio(path, j, planned(path, j, original, kids, ""), runtime)
    out = {"audio": logs.audio_summary(certain, doubts, samples)}
    if certain:
        return f"its audio is broken too: {certain}", out
    failed = [x for x in samples if not x.get("ran")]
    if failed or (runtime and not samples):   # a decode failure or a timeout is never clean
        return f"{len(failed)} of {len(samples)} of its audio samples did not run" if failed else "its audio could not be sampled", out
    vcertain, vdoubts, fields = checks.video_check(path, j=j)
    out["video"] = checks.video_summary(vcertain, vdoubts, fields)
    if vcertain:
        return f"its video is corrupt too: {vcertain}", out
    windows = (fields.get("windows") or {}).get("list") or []
    if "error" in fields or any("time limit" in str((fields.get(k) or {}).get("skipped", "")) for k in ("zeros", "windows")) \
            or any(not x.get("ran") or x.get("stopped") for x in windows):
        return "its video check did not run to its end", out
    return None, out


def bin_sig(path):
    """(inode, size, mtime in ns) of a file in the recycle bin, or None when it is gone. The app gives a freed bin name
    to the next file it recycles, so restore() compares this with the plan's right before each rename."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def other_episodes(app, old, items, what="the old file"):
    """Why a Sonarr old file may not come back for the episodes items, or None. The parse API of the Sonarr instance app
    names the episodes of the old path. Each must be among items, the broken file's episodes. Sonarr checks every
    episode a file maps to, so an old two-episode file never comes back for one of them. The parse needs a title, and
    the path decides."""
    r = apps.arr(app, "parse?" + urllib.parse.urlencode({"title": os.path.basename(old), "path": old})) or {}
    eps = {e["id"] for e in r.get("episodes") or [] if e.get("id")}
    if not eps:
        return f"Sonarr cannot tell which episodes {what} holds"
    extra = sorted(eps - {int(i) for i in items})
    if extra and len(extra) == len(eps):   # scene or absolute numbering
        return f"Sonarr's parse maps {what} to other episodes {extra}, as scene or absolute numbering does"
    return f"{what} also holds episodes {extra}, which the broken file does not" if extra else None


def restore_plans(app, job, files, broken):
    """The old files of the broken files of a re-grab unit that may come back, read-only, before regrab() changes
    anything. The job's own old files come from its job, the others from the queued and claimed jobs of the download.
    An old file may come back when the recycle bin still holds it on the volume of its old path, no other file took
    that path, and old_file_check() finds no certain fault. With KEEP_REPLACED on, the hook's own copy from the grab
    stands in for a bin copy it cannot use, see kept_copy(). A usable bin copy wins. Returns {file id: {"back": [(old
    path, recycle bin path)], "not": [(old path, why)], "owner", "label", "want", "checks", "kept": {old path: its
    kept record}}} for each broken file an upgrade put in the library."""
    if not config.CFG.restore:
        return {}
    jobs = upgrade_jobs(app, job.get("download_id"))
    jobs[int(job["file_id"])] = job
    plans = {}
    for fid in sorted(broken):
        pairs, why = old_files(jobs.get(fid) or {})
        if not pairs and not why:
            continue
        f = files[fid]
        p = plans[fid] = {"back": [], "not": [], "owner": f["owner"], "label": os.path.basename(f["path"]), "want": None, "checks": {},
                          "extras": {}, "new": f["path"], "kept": {}}
        if why:
            p["not"] = [(jobs[fid]["deleted"], why)]
            continue
        try:
            p["label"], original, _, p["want"], kids, _ = apps.ARR[app].item(f["owner"], fid)
        except Exception as ex:
            p["not"] = [(old, config.mask(f"the item did not read: {type(ex).__name__}: {ex}")[:200]) for old, _ in pairs]
            continue
        for old, rb in pairs:
            try:
                why = "the app kept no copy in its recycle bin" if not rb else "the recycle bin no longer holds it" if not os.path.isfile(rb) \
                    else "the recycle bin is on another volume, and a restore never copies" if volume(rb) != volume(old) else None
                if why and config.CFG.keep_replaced:
                    rec, stale = vault.kept_copy(app, old, jobs[fid].get("download_id"))
                    if rec:
                        rb, why, p["kept"][old] = rec["kept"], None, rec
                    elif stale:
                        why += f". {stale}"
                why = why or ("another file holds its path now" if os.path.lexists(old) and old != f["path"] else None)
                if why is None and not apps.ARR[app].film:
                    why = other_episodes(app, old, f["items"])
                sig = bin_sig(rb)   # before the check, so a change during the check shows too
                if why is None:
                    why, p["checks"][old] = old_file_check(rb, original, kids, f.get("runtime") or 0)
                if why is None:
                    p["extras"][old] = vault.kept_extras(p["kept"][old]) if old in p["kept"] else old_extras(old, rb)
            except Exception as ex:   # the time limit too: a check that did not end keeps the old file in the bin
                why = config.mask(f"its check failed: {type(ex).__name__}: {ex}")[:200]
            if why:
                p["not"].append((old, why))
            else:
                p["back"].append((old, rb, sig))
    return plans


def old_extras(old, rb):
    """[(extra in the recycle bin, its path beside old)]: the subtitles, .nfo and images the upgrade recycled with the old
    video file rb. The app names each like the video, <old name>.en.srt or <old name>-thumb.jpg. A name the bin holds
    already gets _2, _3 before the extension, the video's own rule. The app sets each file's mtime when it recycles it,
    so the copy of each name nearest the video's mtime, within EXTRA_SLACK seconds, is this upgrade's."""
    stem, folder, t0, best = os.path.splitext(os.path.basename(old))[0], os.path.dirname(rb), os.path.getmtime(rb), {}
    for n in os.listdir(folder):
        x, (base, ext) = os.path.join(folder, n), os.path.splitext(n)
        plain = re.sub(r"_\d+$", "", base) + ext
        name = plain if vault.extra_of(stem, plain) else n
        if x == rb or not vault.extra_of(stem, name) or not os.path.isfile(x):
            continue
        gap = abs(os.path.getmtime(x) - t0)
        if gap <= EXTRA_SLACK and gap < best.get(name, (None, gap + 1))[1]:
            best[name] = (x, gap)
    return sorted((x, os.path.join(os.path.dirname(old), name), bin_sig(x)) for name, (x, _) in best.items())


def restore_extras(extras):
    """Move each extra of a restored video file back beside it, only after the video. Radarr moves the broken file's
    own extras to its bin a moment after its delete returns (IHandleAsync), so a taken path gets up to EXTRA_WAIT seconds
    in all. An extra whose path stays taken stays in the bin, and so does one that changed in the bin since the plan.
    Returns one {"file", "recycle", "result"} per extra."""
    out, end = [], time.monotonic() + EXTRA_WAIT
    for x, target, sig in extras:
        while os.path.lexists(target) and time.monotonic() < end:
            time.sleep(1)
        try:
            if bin_sig(x) != sig:
                result = "stays in the bin: it changed there since the plan"
            elif os.path.lexists(target):
                result = "stays in the bin: another file holds its path"
            else:
                os.rename(x, target)
                result = "restored"
        except OSError as ex:
            result = config.mask(f"stays in the bin: {type(ex).__name__}: {ex}")[:300]
        out.append({"file": target, "recycle": x, "result": result})
    return out


def restore_manual(app, job, fault, probe):
    """regrab() for a manual import that replaced a file. It has no grab record, so nothing is marked failed and the app
    does not search. The second check from scratch must find the fault again. When the old file may come back, the app
    deletes the broken import into its recycle bin, the old file goes back, and the app rescans. The items it monitored
    before are monitored again. It counts against the cap. Otherwise the file stays, as before. The plan is
    checked again right before the delete. When no old file came back after the delete, the app gets a search for the
    items that were monitored before the delete, since no grab can be marked failed. An API search would
    grab an unmonitored item too. Returns the action as regrab() does. Its code is restored, searched, deleted, no_grab,
    unconfirmed, capped or failed."""
    a, fid = apps.ARR[app], int(job["file_id"])
    said = lambda code, back: dict(restore_facts(app, job, back), code=code)
    try:
        if probe(job["path"], None, fresh=True)[0] != fault:
            return {"code": "unconfirmed"}
        files = unit_files(app, [], job)
        plans = restore_plans(app, job, files, {fid})
    finally:
        config.DEADLINE.stop()
    if not any(p["back"] for p in plans.values()):
        return said("no_grab", restore(job, plans))
    if not count_regrab(app):
        return {"code": "capped", "cap": config.CFG.regrab_cap}
    step = "reading which items are monitored"
    with runner.no_stop():   # the changes finish before a SIGTERM lands
        try:
            for p in plans.values():   # the plan again, right before the delete
                keep = [b for b in p["back"] if bin_sig(b[1]) == b[2] and (not os.path.lexists(b[0]) or b[0] == p["new"])]
                p["not"] += [(b[0], "its recycle bin copy or its path changed before the delete") for b in p["back"] if b not in keep]
                p["back"] = keep
            if not any(p["back"] for p in plans.values()):
                return said("no_grab", restore(job, plans))
            watched = sorted(i for i, (m, _) in a.files(files[fid]["owner"], files[fid]["items"], read=False)[1].items() if m)
            restoring(app, job, plans, [fid])
            step = "the delete"
            apps.arr_write(app, f"{a.file_kind}/{fid}", "DELETE")
            step = "the restore"
            back = restore(job, plans)
            step = "the re-monitor"
            if watched:
                a.remonitor(watched)
            if not any(r["result"] == "restored" for r in back) and not watched:   # an API search would grab an unmonitored item too
                return said("deleted", back)
            if not any(r["result"] == "restored" for r in back):   # the item has no file now, and no grab to fail
                step = "the search"
                apps.arr_write(app, "command", "POST", {"name": a.search, a.ids_key: watched})
                return said("searched", back)
            step = "the rescan"
            rescan_back(app, back)
            read_back(app, back, files, watched)
        except Exception as ex:
            return {"code": "failed", "manual": True, "step": step, "error": config.mask(f"{type(ex).__name__}: {ex}")[:300]}
    return said("restored", back)


def restore(job, plans):
    """Move the old files of plans back from the recycle bin, or from the hook's own copies, after regrab() had the app
    delete the broken files. The app puts a deleted file in its recycle bin and drops its record, so the path is free
    even when the old file had the same name. One rename on the same volume per file, so nothing is copied. The path is
    checked right before, so it never overwrites a file. The bin file must still be the one the plan checked: the app
    gives a freed bin name to the next file it recycles, the broken file included. A grab after the import may have
    linked the broken file, so each rename first makes the kept copies of its path stale, see kept_replaced(). Returns
    one entry per old file, {"file_id", "label", "old", "recycle", "result", "extras"}, and keeps them in job["restore"]
    for the decision line and the Plex analyze, see restore_log(). The caller re-monitors, then rescan_back() has the
    app link the files."""
    out = []
    for fid, p in plans.items():
        for old, rb, sig in p["back"]:
            try:
                if bin_sig(rb) != sig:
                    result = "not restored: the recycle bin copy changed since its check"
                elif os.path.lexists(old):   # the app's delete freed a same-name path, so something new took it
                    result = "not restored: another file holds its path now"
                else:
                    os.makedirs(os.path.dirname(old), exist_ok=True)   # the app may remove an empty folder after the delete
                    vault.kept_replaced(old)
                    os.rename(rb, old)
                    result = "restored"
            except OSError as ex:
                result = config.mask(f"not restored: {type(ex).__name__}: {ex}")[:300]
            out.append(dict(file_id=fid, owner=p["owner"], label=p["label"], want=p["want"], old=old, new=p["new"], recycle=rb, result=result,
                            check=p["checks"].get(old), extras=restore_extras(p["extras"].get(old) or []) if result == "restored" else [],
                            **({"kept": True} if old in p["kept"] else {})))
        out += [dict(file_id=fid, owner=p["owner"], label=p["label"], old=old, result=f"not restored: {why}") for old, why in p["not"]]
    job["restore"] = out
    return out


def rescan_back(app, back):
    """Have the app rescan each item that got an old file back, once per item, and wait RESTORE_WAIT seconds at most for each."""
    scans = {}
    for r in back:
        if r["result"] == "restored":
            r["rescan"] = scans[r["owner"]] = scans.get(r["owner"]) or apps.ARR[app].rescan(r["owner"], RESTORE_WAIT)


def restoring(app, job, plans, delete):
    """The restoring line in the decision log, before the first delete: the file ids the app deletes, and each old file
    and extra with its bin path. A job killed after the delete then leaves the record of where the old files wait, and
    the bin keeps them for its cleanup days."""
    moves = [{"file_id": fid, "old": old, "recycle": rb, "extras": [[x, t] for x, t, _ in p["extras"].get(old) or []]}
             for fid, p in plans.items() for old, rb, _ in p["back"]]
    if moves:
        logs.log(dict(source="hook", app=app, path=job.get("path"), download_id=job.get("download_id"), result="restoring",
                 delete=sorted(delete), moves=moves))


def read_back(app, back, files, need=None):
    """Read each restored file's record back from the app: the old path must be the file of its movie or episodes, and
    they must be monitored. need lists the items that must be monitored, all of them by default. Sets "linked" on each
    restored entry of back. A failed read leaves linked False and says why."""
    for r in back:
        if r["result"] != "restored":
            continue
        try:
            items = files[r["file_id"]]["items"]
            paths, of = apps.ARR[app].files(r["owner"], items)
            watched = [of.get(i, (None,))[0] for i in items if need is None or i in need]
            r["linked"] = r["old"] in paths.values() and all(watched)
            if not r["linked"]:
                r["read_back"] = f'{apps.ARR[app].name} lists {sorted(p or "no file" for p in set(paths.values()))}, monitored {watched}'[:300]
        except Exception as ex:
            r["linked"], r["read_back"] = False, config.mask(f"the read-back failed: {type(ex).__name__}: {ex}")[:200]


def restore_facts(app, job, back):
    """The facts of a re-grab that restored old files, for report.restored(): the names of the job's own old files that
    came back, whether the app links them again and whether they came from the hook's own copies, the number of other
    files of the download whose old file came back, and why the job's own old file stayed out."""
    fid = int(job["file_id"])
    mine = [r for r in back if r["file_id"] == fid]
    came = [r for r in mine if r["result"] == "restored"]
    return {"name": apps.ARR[app].name, "came": [os.path.basename(r["old"]) for r in came], "linked": all(r.get("linked") for r in came),
            "own_copy": all(r.get("kept") for r in came), "others": len({r["file_id"] for r in back if r["file_id"] != fid and r["result"] == "restored"}),
            "stayed": next((r["result"].removeprefix("not restored: ") for r in mine if r["result"] != "restored"), None)}


def restore_log(rec, job, pending, app):
    """Add the restore of run_job()'s job to its decision line, and queue a Plex analyze of each old file that came back,
    because the item's file changed. The analyze waits for an idle section like any other, see plex_step()."""
    back = job.get("restore") or []
    if not back:
        return rec
    mine = [r for r in back if r["file_id"] == int(job["file_id"])]
    code = "old_file_restored" if any(r["result"] == "restored" for r in mine) else "old_file_not_restored" if mine else None
    scans = set()   # a restored file under another name is not in Plex until Plex scans its folder, once per folder
    for r in back:
        if r["result"] == "restored" and r["old"] == r["new"] and r.get("want"):
            pending.append(plex.plex_job(app, "hook", r["label"], r["old"], r["want"], rec.get("id")))
        elif r["result"] == "restored" and os.path.dirname(r["old"]) not in scans:
            scans.add(os.path.dirname(r["old"]))
            pending.append(plex.plex_folder_job(app, "hook", r["label"], os.path.dirname(r["old"]), rec.get("id")))
    return dict(rec, restore=[{k: v for k, v in r.items() if k != "want"} for r in back],
                reasons=([code] if code else []) + (rec.get("reasons") or []))
