# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The queue, the worker, the job processes and the file lock. hook() is the Custom Script entry."""
import collections, contextlib, dataclasses, fcntl, hashlib, http.client, json, os, re, select, shutil, signal, sqlite3, sys, tempfile, time, traceback, types, urllib.error, urllib.parse, urllib.request

from . import apps, checks, cli, config, content, convert, decide, logs, plex, process, regrab, report, store, subsync, subtitles, vault


def gated(f, op, wait=config.DEADLINE):
    """flock f with op, after the gate (lock.gate). A taker holds the gate only while it waits for f. So an edit that
    waits holds the gate, no new scan file starts, and the edit waits only for the files in flight. Linux flock gives
    a waiting LOCK_EX no preference, so without the gate a stream of shared scan locks could keep an edit out. wait is
    the content.Deadline of both waits, the job's by default."""
    with open(os.path.join(config.CFG.state_dir, "lock.gate"), "w") as gate:
        wait.lock(gate, fcntl.LOCK_EX)
        wait.lock(f, op)
    return f


def locked(shared=False, wait=config.DEADLINE):
    """The one lock every hook run and every backfill file takes, so two edits never touch a file at once. A scan takes
    it shared, so its workers read side by side, and an edit waits for them. wait is as in gated()."""
    # ponytail: one global lock, per-path locks if imports queue behind it
    return gated(open(os.path.join(config.CFG.state_dir, "lock"), "w"), fcntl.LOCK_SH if shared else fcntl.LOCK_EX, wait)


def file_key(st):
    """The inode, size and mtime of the stat st. Two stats with the same file_key describe one file with no change between them."""
    return st.st_ino, st.st_size, st.st_mtime_ns


class Replan(Exception):
    """A job process found its file or its download changed after its checks. run_job() runs the job again with the
    file lock exclusive from the start, as HOOK_WORKERS 1 does."""


def same_file(path, st, step="the checks", during="since"):
    """Raise Replan when the file at path is gone since step, or is not the file the stat st describes any more."""
    try:
        now = os.stat(path)
    except FileNotFoundError:
        raise Replan(f"the file is gone since {step}") from None
    if file_key(now) != file_key(st):
        raise Replan(f"the file changed {during} {step}")


def exclusive(lock, path, st, name, job, unit):
    """Trade the shared file lock of a job process for the exclusive one, before an edit or a re-grab. flock cannot
    upgrade a lock in one step, so the shared lock goes first. A process that waited at the gate while it held the
    lock shared would deadlock with the edit that holds the gate. wait_turn() first lets the older jobs of the same
    download finish. Then the file must still be the one st describes, the stat the checks started from, and the
    download's re-grab unit must still be unit, the one the job read before its checks. Raises Replan when
    either changed, so no edit or re-grab rests on a stale read. The wait has LOCK_WAIT of its own, and the job keeps
    the time it had left."""
    fcntl.flock(lock, fcntl.LOCK_UN)
    with config.DEADLINE.paused() as left:
        wait = content.Deadline(config.LOCK_WAIT, f"gave up after waiting {config.LOCK_WAIT} seconds for the exclusive lock")
        wait_turn(name, job)
        gated(lock, fcntl.LOCK_EX, wait)
    if left is None:   # a job has a limit after the wait, even when an OutOfTime ended its own and a handler took it
        config.DEADLINE.start(config.BUDGET)
    same_file(path, st)
    if download_unit(job) != unit:
        raise Replan("a re-grab of its download ran since the checks")


def reshared(lock, path, st):
    """Take the file lock shared again after a step that ran without it, the hearing in process(). A running time limit
    stops while it waits, as in exclusive(). Raises Replan when the file is not the one st describes any more, so the
    checks after the hearing never read a file an edit changed meanwhile."""
    with config.DEADLINE.paused() as left:   # a backfill has no limit, so its wait has none
        why = f"gave up after waiting {config.LOCK_WAIT} seconds for the lock after the hearing"
        gated(lock, fcntl.LOCK_SH, content.Deadline(config.LOCK_WAIT, why) if left else config.DEADLINE)
    same_file(path, st, "the hearing", "during")


def download_unit(job):
    """The re-grab unit of the job's download in the store, {} when there is none: {"time", "failed", "deleted": [file
    ids], "clean": [file ids], "kind"}. kind is audio, content, video or damage, the fault the unit was checked for. A
    unit from before kinds existed is audio. A unit keys on the instance too, because two instances can grab one torrent
    under one download id."""
    return store.get("unit", f"{job.get('app')}|{job.get('download_id') or ''}", {})


def wait_turn(name, job):
    """Wait while an older job of the same download is claimed, not settled, and its job process runs, see settle(). An
    unsettled job may still re-grab, so a younger job takes its exclusive step only after every re-grab of an older one.
    Its exclusive() then sees the unit changed and runs again, the way one worker would run it after the older job.
    Only older jobs are waited for, and a job settles before it waits, so no two jobs wait on each other. A job put back
    in the queue after a crash is not waited for. Neither is a claimed job whose process is gone, as when its requeue
    failed or its coordinator died. That one gets a warning line, once. After LOCK_WAIT it goes on."""
    if not job.get("download_id"):
        return
    end, gone = time.monotonic() + config.LOCK_WAIT, set()
    while time.monotonic() < end:
        older = False
        for n, raw in store.read("SELECT name, job FROM jobs WHERE claimed = 1 AND settled = 0 AND name < ? ORDER BY name", name):
            with contextlib.suppress(ValueError, AttributeError):
                if json.loads(raw).get("download_id") != job["download_id"]:
                    continue
                if job_alive(n):
                    older = True
                elif n not in gone:
                    gone.add(n)
                    logs.log(dict(source="hook", job=name, result="warning", note=f"the older job {n} of this download is claimed, but no job "
                             "process runs it, so this job goes on without waiting for it"))
        if not older:
            return
        select.select([], [], [], 0.5)   # a real wait. The tests fake time.sleep.


def job_alive(name):
    """Whether the job process of the claimed job name runs. The coordinator stores its pid right after the fork,
    before it claims the next job, so a younger job of the download always finds it."""
    # ponytail: a pid the kernel reused reads as alive, and the waiter then waits up to LOCK_WAIT as before
    rows = store.read("SELECT pid FROM jobs WHERE name = ? AND claimed = 1", name)
    return bool(rows) and convert.job_alive_pid(rows[0][0])


def settle(name, on=True):
    """Mark a claimed job settled: it re-grabs nothing more, so younger jobs of its download need not wait for it. A
    re-run unsettles it first, because the re-run may re-grab. The mark goes with the job."""
    with contextlib.suppress(sqlite3.Error):
        store.write("UPDATE jobs SET settled = ? WHERE name = ? AND claimed = 1", int(on), name)


STOP = {"held": False, "asked": False, "term": None}   # in a job process: a write has started, and a SIGTERM came since
# term: the SIGTERM or SIGINT that job_term() or stopped() raised on. CPython drops an exception that a handler raises
# while a finalizer such as Popen.__del__ runs, so a later step that must not run past a stop raises it again.


def job_term(signum, _):
    """SIGTERM in a job process. Before any write the job stops at once and goes back to the queue. Once no_stop()
    started an edit or a re-grab, the job runs to its end, so its decision line, its alerts and its Plex analyze are
    written. job_process() then exits."""
    if STOP["held"]:
        STOP["asked"] = True
    else:
        STOP["term"] = signum
        raise SystemExit(128 + signum)


def raise_term():
    """Raise the stop that job_term() or stopped() recorded, see STOP. A SIGINT raises KeyboardInterrupt, as Ctrl+C does."""
    if STOP["term"] == signal.SIGINT:
        raise KeyboardInterrupt
    if STOP["term"]:
        raise SystemExit(128 + STOP["term"])


@contextlib.contextmanager
def no_stop():
    """SIGTERM and SIGINT wait while mkvpropedit runs or a re-grab changes the app. A child inherits the blocked
    signals, so a stop that signals every process never cuts mkvpropedit mid-write. Ctrl+C in a terminal signals the
    whole foreground process group. The one-file worker then stops once the block ends: CPython runs the SIGINT
    handler inside pthread_sigmask(), so a Ctrl+C raises right after the write. A job process finishes its job first
    on SIGTERM, see job_term(). A stop whose raise a finalizer dropped stops it here, before the write."""
    raise_term()
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    STOP["held"] = True   # a job process now runs its job to the end, see job_term()
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def flags(j):
    return [dict({k: t[k] for k in ("pos", "sel", "lang", "title", "default", "role", "original")}, forced=t["flagged"]) for t in decide.classify(j)]


def queue_dir():
    """The folder of the job files that the hook writes when the store does not take a job, see queue_job()."""
    return os.path.join(config.CFG.state_dir, "queue")


def adopt():
    """Move the job files of queue_dir() into the queue of the store. A job that is there stays. A half-written one that
    a killed hook left goes after an hour."""
    with contextlib.suppress(FileNotFoundError):
        for n in sorted(os.listdir(queue_dir())):
            f = os.path.join(queue_dir(), n)
            with contextlib.suppress(FileNotFoundError, sqlite3.Error):   # another worker took it, or the store is busy and the next look takes it
                if not n.startswith("."):
                    with open(f) as fh:
                        store.write("INSERT OR IGNORE INTO jobs (name, at, job) VALUES (?, ?, ?)", n, os.path.getmtime(f), fh.read())
                    os.remove(f)
                elif time.time() - os.path.getmtime(f) > 3600:
                    os.remove(f)


def queued():
    """Import job names, oldest first. The name starts with the enqueue time in nanoseconds. A job put back for a later
    try waits until that time, see webhook_job(), so it never holds up the jobs behind it, and no worker runs for it
    before then. The job files of queue_dir() join the queue first."""
    adopt()
    return [n for n, in store.read("SELECT name FROM jobs WHERE claimed = 0 AND due <= ? AND name NOT LIKE 'deep-analysis-%' ORDER BY name",
                                   time.time_ns())]


def deep_analysis_queued():
    """The names of the deep analysis and recheck jobs, see deep_name(). The deep analyses go first, so a new import's
    waits behind no batch of rechecks. Then each kind runs the one queued longest ago first. json.dumps() writes the
    key of a recheck as "recheck": with a space, and a value that holds the word escapes its quotes, so the test
    matches the key alone, on any SQLite."""
    return [n for n, in store.read("SELECT name FROM jobs WHERE claimed = 0 AND name LIKE 'deep-analysis-%' "
                                   """ORDER BY instr(job, '"recheck": ') > 0, at, name""")]


def job_of(name, claimed=False):
    """The job name of the queue, or of the claimed jobs."""
    row = store.read("SELECT job FROM jobs WHERE name = ? AND claimed = ?", name, int(claimed))
    if not row:
        raise FileNotFoundError(f"the job {name} is gone")
    return json.loads(row[0][0])


def claim(name):
    """Claim the queued job name for a job process, see coordinate(). One change of the store does it, so of two
    workers one alone gets the job. False when it is gone or claimed."""
    return store.write("UPDATE OR IGNORE jobs SET claimed = 1 WHERE name = ? AND claimed = 0", name).rowcount == 1


def put_job(name, job, claimed=False):
    store.write("UPDATE jobs SET job = ? WHERE name = ? AND claimed = ?", json.dumps(job), name, int(claimed))


def drop_job(name, claimed=False):
    store.write("DELETE FROM jobs WHERE name = ? AND claimed = ?", name, int(claimed))


def deep_wanted(rec):
    """Whether the import of the decision record rec queues the deep analysis of its file: SUBTITLES is deep, and the
    Matroska file holds a subtitle track or has an .srt beside it."""
    path = rec.get("path")
    return bool(config.CFG.subtitles == "deep" and path and path.lower().endswith(".mkv") and os.path.exists(path)
                and (any(t["i"].startswith("s") for t in rec.get("tracks") or []) or subtitles.side_stats(path)))


def queue_deep_analysis(job, rec, inputs, held=None):
    """Queue the deep analysis of the file an import job checked (docs/design.md, "Subtitle match"), when
    deep_wanted(rec). The job carries inputs, the decision inputs of the import: its label, original language,
    runtime, Plex lookup, kids flag, metadata context and release name. So the deep analysis asks no app and decides as
    the import did. The job is named by the path, so a newer import of the path replaces a queued one. The job holds
    the file_key() of the file the import left, see deep_replaced(). It names the import's job in "from", and it keeps
    held, the alerts the import held for it, see process.alerts() and held_after(). Returns its name, or None."""
    if not deep_wanted(rec):
        return None
    path, ids, name = rec["path"], rec.get("ids") or {}, deep_name(rec["path"])
    store.write("INSERT OR REPLACE INTO jobs (name, at, job) VALUES (?, ?, ?)", name, time.time(),
                json.dumps(dict(app=job.get("app"), path=path, ids=ids, inputs=inputs, time=time.time(), key=file_key(os.stat(path)),
                                **({"from": rec["job"]} if rec.get("job") else {}), **({"held": held} if held else {}))))
    return name


JUDGED = ("match", "fit", "mismatch")   # the verdicts that judge a subtitle, as subtitles.sub_found() reads them


def judged(rec, keys):
    """Whether the run of rec judged each subtitle of keys, see logs.alert_findings(): its words or where its lines show
    gave one of JUDGED. A failed hearing gives unknown, and a missing language model gives no verdict at all."""
    seen = lambda k: {**(rec.get("subcheck") or {}), **(rec.get("subtime") or {})}.get(k) or {}
    return bool(keys) and all({seen(k).get("verdict"), (seen(k).get("layout") or {}).get("verdict")} & set(JUDGED) for k in keys)


def times_judged(rec, k):
    """Whether the run of rec judged the times of subtitle k again, for a held timing alert, see held_after(). Its
    timing reached a fix, "in time", steps, an offset it left (unfixed) or a fix it did not confirm, or dense hearing
    moved its lines, or its remux retimed it. A subtitle that does not match the audio is judged by that, and its own
    alert says so. A word check that matched but timed too few windows judged no times, so the held alert posts."""
    r = {**(rec.get("subcheck") or {}), **(rec.get("subtime") or {})}.get(k) or {}
    t = r.get("timing") or {}
    return (r.get("verdict") == "mismatch" or bool(t.get("fix")) or t.get("why") == "in time" or any(x in t for x in ("piecewise", "unfixed", "unconfirmed"))
            or bool(((rec.get("blocks") or {}).get(k) or {}).get("blocks")) or k in remuxed(rec))


def remuxed(rec):
    """The subtitles that a finished subtitle remux or a sidecar rewrite of the run of rec retimed, moved to their speech
    or removed, by their place before that remux, see process.subtitle_checks() and process.after_edit(). Longer ends
    and a repaired text fix no timing and no mismatch, so they leave a held alert to post."""
    rm = rec.get("subremux") or {}
    done = [p for k in ("fixed", "timed", "removed") for p in rm.get(k) or []] if rm.get("done") else []
    return set(done) | {e["name"] for e in rec.get("sidecars") or [] if e.get("result") in ("moved", "retimed")}


def app_file(job):
    """Where the app lists the file of the deep analysis job now, asked by its file id, see moved(): (the new path, None,
    False) after a rename or a move, else (None, why, whether the app said it no longer has the file). Raises when the
    app cannot be asked, as for a job with no file id."""
    ids = job.get("ids") or {}
    if not ids.get("file_id"):
        raise LookupError("the job has no file id to ask the app about")
    return moved(job["app"], {"file_id": ids["file_id"], "owner": ids.get("app_id"), "episode_ids": ids.get("episode_ids"), "path": job["path"]})


def gone_for_good(job, rec):
    """Whether the file of the deep analysis job of the record rec is gone for good: the app replaced it at its path, or
    the app says it no longer has it, see moved(). A file the app moved, one it lists where this container does not see
    it, as at the same missing path while a mount is down, and an app that does not answer, are not."""
    if rec.get("outcome") == "file_replaced" or rec.get("app_said") == "gone":
        return True
    try:
        return rec.get("outcome") == "file_gone" and app_file(job)[2]
    except Exception:
        return False


def held_after(job, rec):
    """What became of the alerts the import held for the deep analysis job, see queue_deep_analysis(), as the decision
    record rec of the job ends, one entry each. A job whose file is gone for good drops them, see gone_for_good(). A job
    that judged each subtitle a held alert names posted what it still found in its place, see judged(). A held timing
    alert also needs the times of each subtitle judged again, see times_judged(). Verdicts reach rec only when the run reached
    its alerts, because every step after the subtitle checks runs. A job whose remux changed those subtitles before an
    error counts too, see remuxed(). A dropped alert still posts the changes it keeps, see logs.held_changes(). A flag
    edit that a posted alert of the job says already posts nothing. Every other held alert posts now, because nothing
    judged its subtitles again. That is an error, a failed hearing, a word check that timed nothing, SUBTITLES off, or a
    file the app moved during the run, or one the app lists where this container does not see it."""
    app, out = job["app"], []
    if rec.get("outcome") in ("file_gone", "file_replaced") and gone_for_good(job, rec):
        return logs.held_changes(app, job["held"], "dropped with the file")
    said = set().union(*(report.tells(f, report.track_langs(rec))["flags"] for f in rec.get("findings") or [] if report.posts(f, rec)))
    for h in job["held"]:
        timing = h["kind"] != "subtiming" or all(times_judged(rec, k) for k in h["keys"])   # a timing alert needs the times judged too
        if judged(rec, h.get("keys")) and timing:   # only a run that reached its alerts holds verdicts, see process.STEPS
            out += logs.held_changes(app, [h], "checked again", said)
        elif rec.get("outcome") == "error" and h.get("keys") and set(h["keys"]) <= remuxed(rec):
            out += logs.held_changes(app, [h], "fixed before the error")
        else:
            out += logs.post_held(app, job["path"], [h])
    return out


def deep_name(path):
    """The name of the job of the background queue for path, a deep analysis or a recheck. The worker runs a job of
    this name only while no import waits, one at a time."""
    return f"deep-analysis-{hashlib.sha1(path.encode()).hexdigest()[:16]}.json"


def queue_rechecks():
    """Queue a recheck job for each file whose saved subtitle result a newer check can fix, see subtitles.sub_stale(),
    once per version of the code. RECHECK_ON_UPDATE false or SUBTITLES off queues nothing. A recheck is a job of the
    background queue, as a deep analysis is, so imports and then deep analyses go first, and one runs at a time. The
    job takes the inputs of its item from the app now, as --sub-check does. A job of the path that waits or runs
    already stays. The mark of the version goes in only after every app answered. So after a stop or a failure part
    way, the next start or nightly audit queues the rest. One line on stdout and in the decision log gives the count,
    or the failure. With nothing queued and nothing failed it writes none. It never raises. Returns the count, or None
    when nothing ran."""
    if not config.CFG.recheck_on_update or config.CFG.subtitles == "off":
        return None
    n, failed = 0, []
    try:
        if store.get("mark", "recheck") == config.VERSION:
            return None
        for app, paths in sorted(subtitles.sub_stale().items()):
            try:
                found = apps.ARR[app].library(apps.arr(app, apps.ARR[app].kind), paths=list(paths))[0]
            except Exception:
                failed.append(apps.ARR[app].name)
                continue
            for f, (label, original, runtime, want, kids, item) in found:
                if f.get("path") not in paths:   # a Radarr movie whose movieFile names the path, while movieFileId names another file
                    continue
                with contextlib.suppress(OSError):
                    item = {k: v for k, v in (item or {}).items() if k != "episodes"}   # a series' episodes serve only the title check
                    job = dict(app=app, path=f["path"], ids=cli.file_ids(f, want, item), time=time.time(), key=file_key(os.stat(f["path"])),
                               recheck=paths[f["path"]], inputs=dict(label=label, original=original, runtime=runtime, want=want, kids=kids,
                                                                    ctx=item, release=f.get("sceneName") or ""))
                    name = deep_name(f["path"])   # a job of the path that waits or runs stays, so a second pass queues no double
                    n += store.write("INSERT INTO jobs (name, at, job) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE name = ?)",
                                     name, time.time(), json.dumps(job), name).rowcount
        text = (f"this version can improve the subtitle check of {n} file{'s' if n != 1 else ''} checked before. "
                f"{'They wait' if n != 1 else 'It waits'} in the background queue for a recheck.") if n else ""
        if failed:
            text += f" {report.and_list(failed)} did not answer, so the next start or nightly audit asks again."
        else:   # only once every app answered, so a stop or a failure part way queues the rest next time
            store.put("mark", "recheck", config.VERSION)
    except Exception as ex:
        failed, text = [True], config.mask(f"the recheck after an update did not run, so the next start or nightly audit tries again: "
                                           f"{type(ex).__name__}: {ex}")[:300]
    text = text.strip()
    if not text:   # nothing to recheck, and nothing failed
        return n
    print(f"arr-media-guard: recheck: {text}", flush=True)
    with contextlib.suppress(OSError):
        logs.log(dict(source="recheck", result="warning" if failed else "queued", queued=n, note=text))
    return n


def deep_replaced(job):
    """The outcome and result of a deep analysis job that drops itself, or None. It drops when the file at its path is
    gone, or has another inode than its key names, as after an upgrade, whose import queues a deep analysis of its own.
    An edit in place keeps the inode, and the job goes on, because no new import queues another one. A job queued
    before jobs held a key checks only that the file is there."""
    try:
        now = os.stat(job["path"])
    except FileNotFoundError:
        return dict(outcome="file_gone", result="dropped, the file is gone")
    if job.get("key") and job["key"][0] != now.st_ino:
        return dict(outcome="file_replaced", result="dropped, another file replaced it since its import")
    return None


def deep_waits():
    """Raise Yielded when an import job waits in the queue. The deep analysis asks between two steps, see deep_analysis()."""
    if queued():
        raise subtitles.Yielded("an import waits")
    return False


def deep_analysis(name, pending, claimed=False):
    """One deep analysis job, queued or claimed (docs/design.md, "Subtitle match"): the check of
    --sub-time on the file of an import, with the proof, the kept originals, the subtitle alerts and the Plex analyze of
    an import. It decides with the import's inputs, which the job carries, and it asks the app only where its file is
    when the file is gone from its path, see app_file(). It follows a rename or a move there. It keeps every flag
    the import set, after a remux of its own too, and only a subtitle verdict changes a flag, see process(). It has no
    time limit, and it never drops by age. It drops itself when its file is gone or replaced since its import, see
    deep_replaced(). It checks that at its start, each time it takes the file lock, after its remux, see
    process.deep_drop(), and after an error.

    A recheck job, see queue_rechecks(), runs here too. Its "recheck" names the run of the saved result it repeats,
    import, sub_check, sub_time or deep, and it checks no deeper than that run, see process.Ctx. Its source is recheck.
    Its inputs come from the app at the time it was queued. At SUBTITLES off it drops itself before any read.

    It reads the whole file for the tracks the Cues do not index before it takes the file lock, since a film takes
    minutes. That read is kept by the file's size and mtime, so a file that changed is read again under the lock. It
    takes the lock shared, hears without it, and takes it exclusive before an edit, and it plans again when the file
    changed, as a backfill file does. When an import waits in the queue, it stops between two steps: after the
    whole-file read, between two groups of its sweep, before each read and each fit of a track, and before a remux.
    The job goes back to the deep analysis queue, and its next run finds the words heard so far in the cache, and the
    whole-file read in the store. A track of that read that was cut short is a Cut again, see subtitles.Cut. A read
    kept by a version that did not mark such tracks is read again. A conversion of its own before the stop changed the
    file, so the job then takes the key of the new file."""
    started, rec, want, job, ctx = time.time(), dict(source="deep_analysis", job=name), None, {}, None
    try:
        job = job_of(name, claimed)
        app, path, got = job["app"], job["path"], job.get("inputs") or {}
        rec.update(app=app, path=path, **({"source": "recheck"} if job.get("recheck") else {}))
        if (drop := deep_replaced(job)) and drop["outcome"] == "file_gone":   # a rename or a move of the app queues no new import
            try:
                new, why, gone = app_file(job)
            except Exception as ex:
                new, why, gone = None, config.mask(f"the app was not asked where the file is: {type(ex).__name__}: {ex}")[:300], False
            if new:   # the job goes on at the new path, and a yield keeps it
                logs.log(dict(source=rec["source"], app=app, job=name, result="file_moved", old_path=path, path=new))
                job, path, drop = dict(job, path=new, key=file_key(os.stat(new))), new, None
                put_job(name, job, claimed)
                rec["path"] = path
            else:
                drop.update(note=why, **({"app_said": "gone"} if gone else {}))
        if drop:
            rec.update(drop)
        elif job.get("recheck") and not subtitles.sub_on("recheck"):   # SUBTITLES went off after the job was queued
            rec.update(outcome="subtitles_off", result="dropped, SUBTITLES is off")
        elif not got:
            rec.update(outcome="job_stale", result="dropped, the job holds no decision inputs of its import")
        else:
            st, read = os.stat(path), store.get("deep-read", name) or {}   # the whole-file read of a run that yielded
            if read.get("key") == [st.st_size, st.st_mtime_ns] and "cut" in read:   # JSON keeps no Cut, so "cut" names those tracks
                subtitles.FULL[(path, st.st_size, st.st_mtime_ns)] = dict(read["read"], cues={p: (subtitles.Cut if p in read["cut"] else list)(map(tuple, c))
                                                                                             for p, c in read["read"]["cues"].items()})
            if (path, st.st_size, st.st_mtime_ns) not in subtitles.FULL and job.get("recheck") != "import":   # outside the file lock, and kept for a yield
                whole = subtitles.full_read(path, checks.mkvmerge(path))
                store.put("deep-read", name, {"key": [st.st_size, st.st_mtime_ns], "read": whole,
                                              "cut": [p for p, c in whole["cues"].items() if isinstance(c, subtitles.Cut)]})
            deep_waits()
            want = got.get("want")

            def run(lock, shared):
                nonlocal ctx
                if drop := deep_replaced(job):   # the file went or was replaced while the job read it or waited for the lock
                    return dict(rec, **drop)
                ctx = process.Ctx(app, path, got.get("label") or os.path.basename(path), got.get("original"), got.get("runtime") or 0,
                                  mode={"import": "sub_check"}.get(job.get("recheck"), job.get("recheck") or "deep"), recheck=job.get("recheck"),
                                  kids=bool(got.get("kids")), release=got.get("release") or "", ids=job.get("ids"), item=got.get("ctx"),
                                  lock=lock, shared=shared)
                return process.process(ctx)
            try:
                with locked(shared=True) as lock:
                    rec = run(lock, types.SimpleNamespace(exclusive=lambda st: cli.upgrade(lock, path, st), settle=lambda: None, turn=lambda: None,
                                                          reshare=lambda st: reshared(lock, path, st)))
            except Replan as ex:
                with locked() as lock:
                    rec = subtitles.swept_before(run(lock, False), ex)
            rec["job"] = name
    except subtitles.Yielded as ex:
        logs.log(dict(rec, result="yielded", note=str(ex)))
        if getattr(ctx, "st", None) and list(file_key(ctx.st)) != job.get("key"):   # a step of the run changed the file, see process.refresh()
            put_job(name, dict(job, key=file_key(ctx.st)), claimed)
        if claimed:   # back to the deep analysis queue, unless a newer import queued the path again meanwhile
            requeue(name)
        return
    except Exception as ex:   # record it and go on: a deep analysis job never stops the worker
        if isinstance(ex, subsync.Broken):   # only a run with AMG_INVARIANTS=1 raises one, and a test run must see it
            raise
        held = dict(job, key=file_key(ctx.st)) if getattr(ctx, "st", None) else job   # ctx.st follows the run's own changes
        if job.get("path") and (drop := deep_replaced(held)):   # the app replaced or removed the file during the run
            rec.update(drop, note=config.mask(f"{type(ex).__name__}: {ex}")[:300])
        else:
            rec.update(outcome="error", result=config.mask(f"error: {type(ex).__name__}: {ex}")[:500])
            rec["trace"] = traceback.format_exc(limit=3)[-800:]
            if getattr(ctx, "rec", None):   # what the run changed before the error, see held_after()
                rec.update({k: ctx.rec[k] for k in ("subremux", "sidecars") if k in ctx.rec})
    if job.get("from"):   # the import job that queued it, so a reader can match the two lines
        rec["from"] = job["from"]
    if job.get("held"):
        rec["held_result"] = held_after(job, rec)
    rec = logs.decision(rec, started)
    path = rec.get("path")
    if want and path and os.path.exists(path) and process.changed(rec):
        pending.append(plex.plex_after(rec["app"], rec.get("source", "deep_analysis"), rec, want))
    store.drop("deep-read", name)
    drop_job(name, claimed)


def try_lock(name):
    """A non-blocking flock under STATE_DIR. Returns the open file, or None when another process holds it."""
    f = open(os.path.join(config.CFG.state_dir, name), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def queue_job(job):
    """Put one job into the queue. hook() and the Webhook listener in serve.py both call it, so the worker reads one
    format. Returns the job's name. A store that does not take the job within store.wait gets it as a file in
    queue_dir(), which the worker adopts, see adopt(). So a busy store never fails an import, and the hook waits
    HOOK_WAIT seconds at most."""
    name = f"{time.time_ns()}-{os.getpid()}.json"
    try:
        claim_kept(job)
        store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", name, time.time(), json.dumps(job))
        return name
    except (sqlite3.Error, OSError):
        pass
    os.makedirs(queue_dir(), exist_ok=True)
    with open(os.path.join(queue_dir(), "." + name), "w") as f:
        json.dump(job, f)
    os.replace(os.path.join(queue_dir(), "." + name), os.path.join(queue_dir(), name))   # a worker never sees half a job
    return name


def claim_kept(job):
    """The old files of an upgrade claim the hook's own copies of their grab, see vault.kept_replaced()."""
    for old in (job.get("deleted") or "").split("|") if config.CFG.keep_replaced else ():
        if old:
            vault.kept_replaced(old, job.get("download_id") or "", job.get("app"))


class Refused(Exception):
    """A Webhook request the listener refuses. code is the HTTP status of the answer."""
    def __init__(self, code, why):
        super().__init__(why)
        self.code = code


CONTROL = str.maketrans({c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7f, 0xa0))})   # a control character, escaped for a log
MAX_ID = 200   # characters a download id may hold
ASK_AGAIN = 10   # seconds between two asks of an app that does not answer, see app_check()
TEST_TIMEOUT = 10   # seconds each API call of app_check() waits, so a hung app fails the Test fast


def plain(path):
    """Whether path is a plain absolute path: normalized, and with no control character, NUL included."""
    return isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) == path and path == path.translate(CONTROL)


def inside(path, folder):
    """Whether path is a plain absolute path under folder, with no '|'. The job joins the old files with '|'."""
    return plain(path) and "|" not in path and bool(folder) and path.startswith(folder.rstrip("/") + "/")


def positive(i):
    return type(i) is int and i > 0   # type(), because a JSON true is an int too


@dataclasses.dataclass
class Event:
    """One event of an app. hook() reads it from the Custom Script variables with from_env(), and the Webhook listener
    from the body with from_webhook(), so both give the same Event. event is Download, Grab, Test, or a trigger the
    connection was not meant to send. owner is the movie or series id. eps are the episode ids of a Sonarr grab. The
    other fields make the job file of a Download, see job()."""
    app: str
    event: object
    time: float
    path: str | None = None
    owner: str | None = None
    file_id: str | None = None
    episode_ids: str | None = None
    download_id: str | None = None
    release: str | None = None
    deleted: str | None = None   # the old files of an upgrade and their recycle bin copies, pipe-joined, see regrab.old_files()
    recycled: str | None = None
    nfo_title: str | None = None
    eps: list = dataclasses.field(default_factory=list)

    def job(self):
        return {k: v for k, v in dataclasses.asdict(self).items() if k != "eps"}

    @classmethod
    def from_env(cls, app):
        """The Event of the Custom Script variables of the instance app. Their names start with its program. Sonarr names
        the episodes of a grab by numbers only, so the API gives their ids. A Download reads the NFO title now, because a
        usenet download folder can be gone when the worker runs."""
        p = config.program(app)
        env, a, ev = os.environ.get, apps.ARR[app], cls(app, os.environ[f"{p}_eventtype"], time.time())
        file = f"{p}_{a.file_kind}"   # radarr_moviefile_path, sonarr_episodefile_path and so on
        if ev.event == "Grab":
            owner, eps = int(os.environ[f"{p}_{a.kind}_id"]), []
            if not a.film:
                absolute = env("sonarr_release_absoluteepisodenumbers", "").split(",")
                if env("sonarr_series_type", "").lower() == "anime" and all(n.strip().isdigit() for n in absolute):
                    # An anime batch can cross a season, and the season number names the first episode's only
                    nums = {int(n) for n in absolute}
                    eps = [e["id"] for e in apps.arr(app, f"episode?seriesId={owner}") if e.get("absoluteEpisodeNumber") in nums]
                else:
                    nums = {int(n) for n in env("sonarr_release_episodenumbers", "").split(",") if n.strip()}
                    season = int(os.environ["sonarr_release_seasonnumber"])
                    eps = [e["id"] for e in apps.arr(app, f"episode?seriesId={owner}&seasonNumber={season}") if e.get("episodeNumber") in nums]
            return dataclasses.replace(ev, owner=str(owner), download_id=env(f"{p}_download_id") or "", eps=sorted(set(eps)))
        if ev.event != "Download":
            return ev
        ev = dataclasses.replace(ev, path=env(f"{file}_path"), owner=env(f"{p}_{a.kind}_id"), file_id=env(f"{file}_id"),
                                 episode_ids=env("sonarr_episodefile_episodeids"), download_id=env(f"{p}_download_id"),
                                 release=env(f"{file}_scenename"), deleted=env(f"{p}_deletedpaths"), recycled=env(f"{p}_deletedrecyclebinpaths"))
        if not a.film:
            ev.nfo_title = process.release_nfo_title(env("sonarr_episodefile_sourcepath") or "", ev.release or "")
        return ev

    @classmethod
    def from_webhook(cls, app, body, ask=True):
        """The Event of a Webhook body. The listener is a trust boundary, so each id must be a positive int, the
        downloadId text of at most MAX_ID characters, and deletedFiles a list of files. A Download asks the app's API by
        the file id for the path, the scene name and the episode ids, and never uses the path in the body. The file
        record must belong to the item the body names. Sonarr's episodeFile.sourcePath, mapped like any app path, names
        the folder whose NFO gives the episode title. Raises Refused when a check fails, and the API's error when the API
        fails. ask=False checks the body only, for a job the worker completes, see run_job()."""
        a, ev = apps.ARR[app], cls(app, body.get("eventType"), time.time())
        if ev.event == "Grab":
            item, eps, down = body.get(a.kind), body.get("episodes") or [], body.get("downloadId") or ""
            owner = item.get("id") if isinstance(item, dict) else None
            ids = [] if a.film or not isinstance(eps, list) else [e.get("id") if isinstance(e, dict) else None for e in eps]
            if not (positive(owner) and all(positive(i) for i in ids) and isinstance(down, str) and len(down) <= MAX_ID):
                raise Refused(400, f"the Grab body has no {a.kind} id, episode ids or downloadId that arr-media-guard can read")
            return dataclasses.replace(ev, owner=str(owner), download_id=down, eps=sorted(set(ids)))
        if ev.event != "Download":
            return ev
        name, item, f = a.name, body.get(a.kind), body.get(a.body_file)
        if not a.film and not f and body.get("episodeFiles"):
            raise Refused(400, "an On Import Complete event names no single file. Turn it off, and turn on On File Import and On File Upgrade")
        ids = [(x or {}).get("id") if isinstance(x, dict) else None for x in (item, f)]
        if not all(positive(i) for i in ids):
            raise Refused(400, f"the body names no {a.kind} id and file id")
        (owner, fid), down, files = ids, body.get("downloadId") or "", body.get("deletedFiles")
        if not isinstance(down, str) or len(down) > MAX_ID:
            raise Refused(400, f"the downloadId is no text of at most {MAX_ID} characters")
        if files and not (isinstance(files, list) and all(isinstance(d, dict) for d in files)):
            raise Refused(400, "deletedFiles is no list of files")
        ev = dataclasses.replace(ev, owner=str(owner), file_id=str(fid), download_id=down)
        if not ask:
            return ev
        try:
            rec = apps.arr(app, f"{a.file_kind}/{fid}")
        except urllib.error.HTTPError as ex:
            if ex.code != 404:
                raise
            raise Refused(400, f"{name} has no file {fid}") from None
        if rec.get(a.owner_key) != owner:
            raise Refused(400, f"{name} file {fid} belongs to {a.kind} {rec.get(a.owner_key)}, and the body names {owner}")
        ev.path, ev.release = rec.get("path"), rec.get("sceneName") or ""
        if not plain(ev.path):
            raise Refused(400, f"{name} lists file {fid} at {ev.path!r}, which is no plain absolute path")
        if not a.film:
            ev.episode_ids = ",".join(str(i) for i in sorted(e["id"] for e in apps.arr(app, f"episode?episodeFileId={fid}")))
        ev.deleted, ev.recycled = body_old_files(app, files, owner)
        if not a.film:   # the NFO beside the file Sonarr imported from, read now, see release_nfo_title()
            source, pairs = apps.mapped(f.get("sourcePath"), app), config.CFG.map_of(app)
            mapped = not pairs or any(inside(source, local) or source == local for _, local in pairs)   # with a map, only its folders
            ev.nfo_title = process.release_nfo_title(source, ev.release) if plain(source) and mapped else None
        return ev


def body_old_files(app, files, owner):
    """The values of <app>_deletedpaths and <app>_deletedrecyclebinpaths for the old files of a Webhook upgrade,
    pipe-joined in one order, or (None, None) when the import replaced nothing. Each old path must sit in the item's
    folder, and each recycle bin path in the app's recycle bin, both as the API names them. An empty recycle bin path
    means the app kept no copy."""
    if not files:
        return None, None
    try:
        home = apps.arr(app, f"{apps.ARR[app].kind}/{owner}").get("path") or ""
    except urllib.error.HTTPError as ex:
        if ex.code != 404:
            raise
        raise Refused(400, f"{apps.ARR[app].name} has no {apps.ARR[app].kind} {owner}") from None
    rbin = apps.arr(app, "config/mediamanagement").get("recycleBin") or ""
    old, rb = [], []
    for d in files:
        o, r = apps.mapped(d.get("path"), app), apps.mapped(d.get("recycleBinPath") or "", app)
        if not inside(o, home):
            raise Refused(400, f"the old file {o!r} is not in the folder {home!r} of {apps.ARR[app].name}'s item {owner}")
        if r and not inside(r, rbin):
            raise Refused(400, f"the recycle bin copy {r!r} is not in {apps.ARR[app].name}'s recycle bin {rbin!r}")
        old.append(o)
        rb.append(r)
    return "|".join(old), "|".join(rb)


def app_check(app, until=0, policy=True, routed=False, fix=None):
    """(why the setup cannot work for app or None, the warnings: a name clash and those of regrab.bin_warnings()). It
    checks that the policy loaded, that the app's API answers with the key, that its Instance Name picks app, see
    name_clash(), and that this script sees each root folder. The Test event of the hook and of the listener, --selftest and the listener's start
    check run it. Each API call waits TEST_TIMEOUT seconds. An app that does not answer is asked again every ASK_AGAIN
    seconds until the monotonic time until. policy=False leaves the policy to a caller that checks it once for every
    app. routed is a Custom Script run, the hook's Test, where the Instance Name picks the instance. It checks the name
    of every instance of the program, because a run of a misnamed one reaches the Test of another, and a clash fails.
    --selftest on a host checks the name of app, and a clash only warns. Under the listener and in the image, the URL
    path of the Webhook picks the instance, so it checks no name. fix passes to regrab.keep_warnings(). Only the
    listener's start check sets it. An env file that does not read fails it first, see config.settings(). policy=False
    leaves that to the caller too."""
    if policy and config.CFG.env_error:
        return config.CFG.env_error, []
    if policy and decide.POLICY is None:
        return config.policy_help(), []
    short = apps.ARR_TIMEOUT.set(TEST_TIMEOUT)
    try:
        while True:
            try:
                roots = [r["path"] for r in apps.arr(app, "rootfolder")]
                break
            except Exception as ex:
                if time.monotonic() >= until:
                    return config.mask(f"the {apps.ARR[app].name} API did not answer: {type(ex).__name__}: {ex}")[:300], []
                time.sleep(min(ASK_AGAIN, max(0, until - time.monotonic())))
        peers = [a for a in config.CFG.apps if config.program(a) == config.program(app)] if routed else [] if config.SERVE or config.IMAGE else [app]
        clashes = [c for c in map(name_clash, peers) if c]
        if clashes and routed:
            return " ".join(clashes), []
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing:
            return (f"this {config.here()} does not see the root folders {', '.join(missing)}. Mount the "
                    f"media at the app's paths, or {apps.map_fix(app)}"), clashes
        return None, clashes + regrab.bin_warnings(app, fix)
    finally:
        apps.ARR_TIMEOUT.reset(short)


def service_checks(until=0):
    """[(service, why its check failed or None)] for each service this setup uses besides the apps: plex with
    PLEX_URL, discord with DISCORD_WEBHOOK, tmdb always, and sabnzbd and newznab each with its subtitle hunter key. Each
    check is one GET that changes nothing. None posts to Discord or sends a search. The listener's start check and
    --selftest run them after the apps. A service that does not answer is asked again every ASK_AGAIN seconds until
    the monotonic time until. Two Radarr instances with the same result give one line."""
    c, out = config.CFG, []
    short = apps.ARR_TIMEOUT.set(TEST_TIMEOUT)   # the hunter's addresses come from Radarr
    try:
        if c.plex_url:
            out.append(("plex", plex_check(until)))
        if c.discord_webhook:
            out.append(("discord", discord_check(until)))
        out.append(("tmdb", tmdb_check(until)))
        if c.sabnzbd_api_key or c.newznab_api_key:
            out += hunter_checks(until)
    finally:
        apps.ARR_TIMEOUT.reset(short)
    return [(s, re.sub(r"(apikey=)[^&\s'\"]+", r"\1<key>", config.mask(why))[:300] if why else None) for s, why in dict.fromkeys(out)]


def service_answer(url, until, headers=None):
    """(HTTP status, answer) of one GET of a service check. The answer is JSON when it reads as JSON, else bytes, for an
    error status too. (None, the error) when the service did not answer. (0, the error) when urllib cannot send the
    request: an address with no http or https, one it cannot read, or a character a header cannot hold, as a curly
    apostrophe in a token. That
    error stays, so it is never asked again, and content._get() reads it as a refused key too. Each ask waits
    TEST_TIMEOUT seconds."""
    while True:
        try:
            if urllib.parse.urlsplit(url).scheme not in ("http", "https"):   # urllib's URLError for another scheme stays too
                return 0, "the address starts with no http:// or https://"
            req = urllib.request.Request(url, headers={"User-Agent": "arr-media-guard", **(headers or {})})
            with urllib.request.urlopen(req, timeout=TEST_TIMEOUT) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as ex:
            code, raw = ex.code, b""
            with contextlib.suppress(Exception):
                raw = ex.read()
        except (ValueError, http.client.InvalidURL) as ex:   # UnicodeEncodeError is a ValueError
            return 0, error_text(ex)
        except Exception as ex:
            if time.monotonic() >= until:
                return None, error_text(ex)
            time.sleep(min(ASK_AGAIN, max(0, until - time.monotonic())))
            continue
        with contextlib.suppress(ValueError):
            return code, json.loads(raw)
        return code, raw


def error_text(ex):
    return f"{type(ex).__name__}: {ex}"


def plex_check(until):
    """Plex lists its libraries only with a token it accepts, and answers 401 to one it refuses. A Plex that lets this
    address in without a token accepts any token."""
    url, token = config.CFG.plex_url.rstrip("/"), config.CFG.plex_token
    code, got = service_answer(f"{url}/library/sections", until, {"X-Plex-Token": token, "Accept": "application/json"})
    if code is None:
        return f"Plex at {url} did not answer: {got}"
    if code == 0:   # a header holds Latin-1 only, so another character is the token's
        return f"Plex at {url} refused PLEX_TOKEN: {got}" if any(ord(c) > 255 for c in token) else f"PLEX_URL is no address amg can use: {got}"
    if code == 401:
        return f"Plex at {url} refused PLEX_TOKEN"
    return None if code == 200 and isinstance(got, dict) and "MediaContainer" in got else f"Plex at {url} gave an unexpected answer: HTTP {code}"


def discord_check(until):
    """Discord answers a GET of the webhook URL with the webhook's details, which proves the webhook and its token
    without a post. It answers 404 and code 10015 to a webhook it does not have, and 401 and code 50027 to a wrong
    token. The GET takes no query, so wait or thread_id stays off it. The line never shows the URL."""
    try:
        url = urllib.parse.urlsplit(config.CFG.discord_webhook)._replace(query="", fragment="").geturl()
    except ValueError as ex:   # as a '[' in the host
        return f"DISCORD_WEBHOOK is no address amg can use: {error_text(ex)}"
    code, got = service_answer(url, until)
    err = got.get("code") if isinstance(got, dict) else None
    if code is None:
        why = f"Discord did not answer: {got}"
    elif code == 0:
        why = f"DISCORD_WEBHOOK is no address amg can use: {got}"
    elif code == 200 and isinstance(got, dict) and got.get("id"):
        return None
    elif code == 404 or err == 10015:
        why = "Discord does not know the webhook in DISCORD_WEBHOOK"
    elif code == 401 or err == 50027:
        why = "Discord refused the token in DISCORD_WEBHOOK"
    else:
        why = f"Discord gave an unexpected answer: HTTP {code}"
    return why.replace(url, "<DISCORD_WEBHOOK>") if url else why


def tmdb_check(until):
    """TMDB's GET /authentication says whether the token amg would use is valid: TMDB_TOKEN, else Radarr's key, as for
    content.expected_languages(). TMDB answers 401 to a token it refuses."""
    token = config.CFG.tmdb_token or content.radarr_token() or content.RADARR_TOKEN
    code, got = service_answer(f"{content.TMDB}/authentication", until, {"Authorization": f"Bearer {token}"})
    if code is None:
        return f"TMDB did not answer: {got}"
    if code == 0:   # the address is fixed, so the token holds a character a header cannot hold
        return f"TMDB refused {content.key_name(config.CFG.tmdb_token)}: {got}"
    if code in (401, 403):
        return f"TMDB refused {content.key_name(config.CFG.tmdb_token)}"
    return None if code == 200 and isinstance(got, dict) and got.get("success") else f"TMDB gave an unexpected answer: HTTP {code}"


def hunter_checks(until):
    """[(service, why or None)] of SABnzbd and the Newznab indexer, each when its subtitle hunter key is set, at the
    addresses of apps.hunter_urls() for each Radarr instance whose API key reads. Without one the hunter does not run."""
    c, out = config.CFG, []
    for app in [a for a in c.apps if config.program(a) == "radarr"]:
        try:
            apps.api_key(app)
        except (OSError, AttributeError):   # an app this host does not run, as for the app checks
            continue
        name, wanted = apps.ARR[app].name, [s for s, key in (("sabnzbd", c.sabnzbd_api_key), ("newznab", c.newznab_api_key)) if key]
        try:
            urls = apps.hunter_urls(app)
        except Exception as ex:
            out += [(s, f"{name} did not answer, so the address of {'SABnzbd' if s == 'sabnzbd' else 'the Newznab indexer'} is not known: "
                        f"{error_text(ex)}") for s in wanted]
            continue
        (sab, sab_why), (hydra, hydra_why) = urls["sab"], urls["hydra"]
        if "sabnzbd" in wanted:
            out.append(("sabnzbd", sab_why or sab_check(sab, until)))
        if "newznab" in wanted:
            out.append(("newznab", hydra_why or newznab_check(hydra, until)))
    return out


def sab_check(url, until):
    """SABnzbd answers its queue, here one slot, only with the full API key (mode=queue). It refuses another key with
    403 or with status false, and says why, as "API Key Incorrect", or "Access denied" for an address it does not let
    in. With api_warnings off the body is empty. Only a reason that names the key blames SABNZBD_API_KEY. The key goes
    in the query, quoted, so a request urllib cannot send has an address it cannot read."""
    q = urllib.parse.urlencode({"mode": "queue", "limit": 1, "output": "json", "apikey": config.CFG.sabnzbd_api_key})
    code, got = service_answer(f"{url}?{q}", until)
    if code is None:
        return f"SABnzbd at {url} did not answer: {got}"
    if code == 0:
        return f"SABnzbd at {url} is no address amg can use: {got}"
    if code == 200 and isinstance(got, dict) and "queue" in got:
        return None
    said = str(got.get("error") or "") if isinstance(got, dict) else got.decode(errors="replace").strip()[:100] if isinstance(got, bytes) else ""
    if code in (401, 403) or isinstance(got, dict) and got.get("status") is False:
        return f"SABnzbd at {url} refused SABNZBD_API_KEY" if "key" in said.lower() else f"SABnzbd at {url} refused the request: {said or f'HTTP {code}'}"
    return f"SABnzbd at {url} gave an unexpected answer: HTTP {code}"


def newznab_check(url, until):
    """The indexer answers t=caps, its list of features, and sends no search to the indexers behind it. A search costs
    each indexer behind NZBHydra2 a hit of its daily limit. NZBHydra2 checks the key before caps and answers a wrong
    one with error code 100. The Newznab spec lets an indexer answer caps without a key, so on another indexer the
    check proves the address only. Codes 100 to 102 refuse the key."""
    code, got = service_answer(f"{url}?{urllib.parse.urlencode({'t': 'caps', 'apikey': config.CFG.newznab_api_key})}", until)
    if code is None:
        return f"the Newznab indexer at {url} did not answer: {got}"
    if code == 0:   # the key goes in the query, quoted
        return f"the Newznab indexer at {url} is no address amg can use: {got}"
    text = got if isinstance(got, bytes) else b""
    err = re.search(rb'<error\b[^>]*\bcode="(\d+)"', text)
    if code in (401, 403) or err and err.group(1) in (b"100", b"101", b"102"):
        return f"the Newznab indexer at {url} refused NEWZNAB_API_KEY"
    return None if code == 200 and b"<caps" in text and not err else f"the Newznab indexer at {url} gave an unexpected answer: HTTP {code}"


def waiting():
    """Whether work waits for a worker: a queued import or deep analysis job, a job a dead worker left claimed, the Plex
    analyzes a stopped worker kept, or a job file in queue_dir() that a busy store left there. A worker exits once no
    import waits, so a deep analysis job left by a stopped container waits for this check."""
    files = os.listdir(queue_dir()) if os.path.isdir(queue_dir()) else []   # read only, so the hook and the listener never wait here
    if any(not n.startswith(".") for n in files):
        return True
    try:
        return bool(store.read("SELECT 1 FROM jobs WHERE claimed = 1 OR name LIKE 'deep-analysis-%' OR due <= ? LIMIT 1", time.time_ns())
                    or store.get("plex", "pending"))
    except sqlite3.DatabaseError:   # a worker looks at a store that does not read, see check_store()
        return True


def ensure_worker(spawn=None):
    """Start a worker when work waits and no worker holds worker.lock, as after each job of hook() and the listener. The
    hook forks, and the worker keeps the lock. The listener passes spawn, which starts a new program that takes the lock
    itself, because a fork would copy the listener's threads and any lock one of them held. Returns spawn's Popen, or
    None."""
    if not waiting():
        return None
    lock = try_lock("worker.lock")
    if not lock:
        return None            # the running worker picks the job up
    if spawn:
        lock.close()
        return spawn("worker")
    # ponytail: the worker stays in the app's cgroup, so an app restart kills it. Its jobs stay queued for the next event.
    if store.fork():
        os._exit(0)            # the app gets its answer now, the child keeps the worker lock
    os.setsid()
    store.wait, store.until = store.WAIT, None   # the hook's run waits HOOK_WAIT in all
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2): os.dup2(null, fd)   # close the app's pipes, or it waits for them to reach EOF
    return worker(lock)


def instance_named(program, said):
    """The instance of program that the Instance Name said picks, or None: the one whose name is said in lower case,
    with each run of characters other than letters and digits as one '-'. So "Sonarr 4K" picks sonarr-4k."""
    want = re.sub(r"[^a-z0-9]+", "-", said.lower()).strip("-")
    return next((app for app, a in config.CFG.apps.items() if a.program == program and app.lower() == want), None)


def peers(app):
    """The instances of the program of app, app among them."""
    return [a for a, s in config.CFG.apps.items() if s.program == config.program(app)]


def name_clash(app, said=None):
    """Why a Custom Script run of the instance app would go to another instance, or None. With one instance of its
    program, every run goes to it. With more, hook_app() picks by the Instance Name, which system/status gives as the
    Custom Script does (ConfigFileProvider.InstanceName, SystemController.cs line 63 of Sonarr v4.0.20 and line 62 of
    Radarr v6.4.4). A 4K Sonarr that keeps the name "Sonarr" would run as sonarr, against another API with its own
    ids. said is the name when the caller read it. A status that does not read gives None."""
    if len(peers(app)) < 2:
        return None
    try:
        said = (apps.arr(app, "system/status") or {}).get("instanceName") or "" if said is None else said
    except Exception:
        return None
    other = instance_named(config.program(app), said)
    if other == app:
        return None
    return (f"{apps.ARR[app].name} names its instance {said!r}, so its Custom Script runs go to "
            f"{f'the instance {other}' if other else 'no instance'}. Set its Instance Name in Settings, General to match {app}, "
            f"for example {' '.join(w.capitalize() for w in app.split('-'))!r}.")


NAMES = {}   # instance: its Instance Name from system/status, read once per worker, see job_refused()


def job_refused(job):
    """Why the worker refuses a job of the host hook, or None. With several instances of a program, hook_app() picked
    the job's instance by the Instance Name of the run, which the job keeps as instance_name. A connection saved before
    a rename never runs its Test again, so the worker checks the names here. It reads the Instance Name of each
    instance of the program once, and refuses the job while one of them picks another instance. Then two apps may
    share the run's name, as a 4K Sonarr that kept "Sonarr", and the job's ids may be another instance's. A job of the
    listener has no instance_name, because its URL path picked the instance, and it passes. So does a job of a
    program with one instance. An instance whose status does not read is left out of the check."""
    # ponytail: each job process of HOOK_WORKERS over 1 reads the names once per job, one status call per instance
    if job.get("instance_name") is None or len(peers(job["app"])) < 2:
        return None
    for app in peers(job["app"]):
        if app not in NAMES:
            with contextlib.suppress(Exception):
                NAMES[app] = (apps.arr(app, "system/status") or {}).get("instanceName") or ""
    clashes = [c for app in peers(job["app"]) if app in NAMES and (c := name_clash(app, NAMES[app]))]
    return f"the run named {job['instance_name']!r} may come from another instance, so the job was not checked. {' '.join(clashes)}" \
        if clashes else None


def hook_app(program):
    """(the instance a Custom Script run of program belongs to, None), or (None, why no instance fits). Sonarr and Radarr
    put their Instance Name, from Settings > General, in <program>_instancename at every event. The source is
    src/NzbDrone.Core/Notifications/CustomScript/CustomScript.cs: Sonarr v4.0.20 lines 65, 112 and 446 (Grab, Download,
    Test), Radarr v6.4.4 lines 65, 102 and 351. The app adds each variable to a StringDictionary, which lowers the case
    of its keys. The one instance of a program takes every run, so a host with one of each needs no setting. With more,
    the instance the Instance Name picks takes it, see instance_named(). name_clash() checks the name of each instance."""
    mine = [app for app, a in config.CFG.apps.items() if a.program == program]
    said = os.environ.get(f"{program}_instancename") or ""
    if len(mine) == 1:
        return mine[0], None
    app = instance_named(program, said)
    return app, None if app else (f"{program.capitalize()} names its instance {said!r}, and no {program} instance in APP_INSTANCES has that name. "
                                  f"Set the Instance Name in Settings, General to match one of {', '.join(mine)}, as "
                                  f"{', '.join(repr(apps.ARR[m].name) for m in mine)}. Case does not count, "
                                  "and a space counts as '-'.")


def hook():
    store.wait, store.until = store.HOOK_WAIT, time.perf_counter() + store.HOOK_WAIT   # the app waits for the hook, see queue_job()
    program = "radarr" if "radarr_eventtype" in os.environ else "sonarr" if "sonarr_eventtype" in os.environ else None
    if not program:
        print(cli.HELP); return
    event = os.environ[f"{program}_eventtype"]
    app, why = hook_app(program)
    if not app:   # the run belongs to no instance, so it asks no app and changes nothing
        with contextlib.suppress(OSError):
            logs.log(dict(source="hook", result="error", note=f"the {event} event was not taken: {why}"))
        sys.exit(f"arr-media-guard: {why}")
    if event == "Download":
        job = Event.from_env(app).job()
        if f"{program}_instancename" in os.environ:   # the worker checks it, see job_refused()
            job["instance_name"] = os.environ[f"{program}_instancename"]
        queue_job(job)
        print(f"arr-media-guard: queued {job['path']}", flush=True)
        return ensure_worker()
    if event == "Grab" and config.CFG.keep_replaced and config.CFG.keep_days:   # keep the files the grab may replace. An error logs, and the app gets its answer.
        try:
            ev = Event.from_env(app)
            vault.keep_grab(app, ev.owner, ev.download_id, ev.eps)
        except Exception as ex:
            with contextlib.suppress(OSError):
                logs.log(dict(source="hook", app=app, result="error", note=config.mask(f"the grab kept nothing: {type(ex).__name__}: {ex}")[:300]))
    if event == "Test":   # a failed check fails the app's Test on the exit code, as the listener's 500 does
        why, warnings = app_check(app, routed=True)
        for w in warnings:
            print(f"arr-media-guard: warning: {w}")
        if why:
            sys.exit(f"arr-media-guard: {why}")
    print(f"arr-media-guard: {event} ok")


def backlog(jobs, warned):
    """Log once per worker when the oldest queued job waited over an hour. Returns whether that line was written."""
    if jobs and not warned and time.time() - int(jobs[0].split("-")[0]) / 1e9 > 3600:
        logs.log(dict(source="hook", result="warning", note=f"the oldest queued job waited over an hour, {len(jobs)} jobs queued"))
        return True
    return warned


WORK_KINDS = ("resub", "trim", "damage", "convert")   # the work folders of work_dir()


def work_dir(kind):
    """A new folder under STATE_DIR for the work files of one step of kind WORK_KINDS, such as a remux. Never the system
    temp dir, which is often a small tmpfs. The step removes it. One that a SIGKILL left goes when a worker starts,
    see stale_work_dirs()."""
    return tempfile.mkdtemp(prefix=f".{kind}-", dir=config.CFG.state_dir)


def stale_work_dirs():
    """Remove the work folders of work_dir() that a killed step left: older than a day, so no running step uses one."""
    with contextlib.suppress(OSError):
        for n in os.listdir(config.CFG.state_dir):
            f = os.path.join(config.CFG.state_dir, n)
            with contextlib.suppress(OSError):
                if n.startswith(tuple(f".{k}-" for k in WORK_KINDS)) and os.path.isdir(f) and time.time() - os.path.getmtime(f) > 86400:
                    shutil.rmtree(f)


def prune_kept():
    """Remove the kept files older than keep_days in each folder of prune_roots(), once a day. At KEEP_ORIGINALS_DAYS 0 it
    leaves the kept originals, as the audit does. The worker runs it, off the app's call path, so a host with no
    nightly audit prunes too. The store holds the day of the last run, and an error never stops the worker."""
    today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
    try:
        with store.tx():
            if store.get("mark", "kept-pruned") == today:
                return
            store.put("mark", "kept-pruned", today)
        for root in sorted(vault.prune_roots(config.CFG.keep_days > 0)):
            vault.prune_originals(root)
    except Exception as ex:
        with contextlib.suppress(OSError):
            logs.log(dict(source="hook", result="warning", note=config.mask(f"the kept files were not pruned: {type(ex).__name__}: {ex}")[:300]))


STORE_ALERT = "store-alert"   # the file in STATE_DIR whose mtime is the time of the last "State store moved" embed


def check_store():
    """Move a broken store aside, start a new one, and carry its jobs over, see store.recover(). A worker does it at its
    start, so the job files the hook wrote meanwhile go into the new store, see adopt(). One error decision line goes to
    the log and to syslog. One ops embed goes to Discord, at most one per KEY_ALERT_EVERY. A file keeps the time of the
    last embed, because the new store does not hold it."""
    got = store.recover()
    if not got:
        return
    moved, why, carried, lost, stopped = got
    name = os.path.basename(moved)
    jobs = f"{carried} queued jobs went to the new one, and {lost} did not read" + (". The read of its jobs stopped at a broken page" if stopped else "")
    logs.decision(dict(source="hook", outcome="store_corrupt", result=f"error: the state store {why}, so it moved to {name} and a new one "
                       f"started. {jobs}."), time.time())
    mark = os.path.join(config.CFG.state_dir, STORE_ALERT)
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < content.KEY_ALERT_EVERY:
        return
    open(mark, "w").close()
    app = next(iter(config.CFG.apps))
    logs.post(app, logs.embed(app, "State store moved", f"The state store {why}. It moved to {name}, and a new one started. {jobs}. "
                              "Its other records are not in the new one.", "amber", []))


def worker(lock):
    """Drain the queue, with the pending Plex lookups in between. Exits when both are empty. HOOK_WORKERS 1 runs one
    file job at a time in this process. A larger value hands the jobs to coordinate()."""
    check_store()
    stale_work_dirs()
    prune_kept()
    for n, in store.read("SELECT name FROM jobs WHERE claimed = 1 ORDER BY name"):   # jobs of job processes that died. A live one would still hold worker.lock.
        requeue(n)
    for app in config.CFG.apps:   # a conversion a kill stopped halfway: reported for a person, nothing moves
        with contextlib.suppress(Exception):
            convert.pending_recover(app, False)
    for why in config.CFG.errors:
        logs.log(dict(source="hook", result="warning", note=why[:300]))
    if config.CFG.from_env and not config.SERVE:   # a host hook reads the app's environment, where a stray key such as LOG wins
        logs.log(dict(source="hook", result="warning", note=f"keys from the environment win over the env file: {', '.join(config.CFG.from_env)}"))
    pending = plex.load_plex()   # plex_job() dicts, the analyze of each edited file, with those a stopped worker kept
    if config.CFG.hook_workers > 1:
        return coordinate(lock, config.CFG.hook_workers, pending)
    warned = False
    while True:
        jobs = queued()
        warned = backlog(jobs, warned)
        deep = deep_analysis_queued()
        if jobs:
            run_job(jobs[0], pending)
        elif deep:   # a deep analysis job only when no import waits
            deep_analysis(deep[0], pending)
        plex.plex_pass(pending)
        if jobs or deep:
            continue
        if pending:
            time.sleep(min(config.POLL, max(0, min(p["due"] for p in pending) - time.monotonic())))
            continue
        lock.close()           # release first, then look once more: a job queued meanwhile found the lock held
        if not queued():   # only a job of this worker queues a deep analysis job
            return
        lock = try_lock("worker.lock")
        if not lock:
            return             # a new event started its own worker


def coordinate(lock, n, pending):
    """The worker when HOOK_WORKERS is over 1. It claims queued jobs oldest first, each by one change in the store, so
    no two processes ever run one job. It forks a job process per job, at most n at a time. A job process runs
    run_job() with the file lock shared for its checks, see process(), and sends its Plex analyze back through a pipe.
    This process alone sends analyze requests, one at a time, so the section gate and the fresh read before each PUT
    hold as with one worker. The job processes inherit worker.lock, so no second worker starts while one of them runs.

    Jobs of one download run side by side. A re-grab and an edit take the file lock exclusive, so a re-grab waits for
    every job in flight, its siblings included, and no job reads or edits while it deletes. A sibling that read its
    file before the re-grab finds it changed or deleted at its exclusive step and runs again, see exclusive().

    Jobs of one download take their exclusive steps in arrival order, see wait_turn(). So a download ends as one
    worker leaves it: the same files deleted, the same files edited. A conversion's swap does not wait, see
    docs/design.md, "One download".

    A job process that died leaves its job claimed, and requeue() puts it back. A job whose processes crash CRASH_TRIES
    times in one run is not claimed again in that run, also when the store lost the count of requeue(). SIGTERM starts no new job and passes
    SIGTERM on. A job process that has not written stops at once and its job goes back to the queue. One that has
    edited or re-grabbed finishes its job first, see job_term(). This process then keeps the pending analyze requests
    for the next worker, see save_plex(), and exits."""
    stop, crashes = [], collections.Counter()   # crashes: job name -> the crashes of its job processes in this run
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    running, warned, told = {}, False, False   # running: the read end of a job's pipe -> [pid, job name, bytes read]
    live = lambda names: [x for x in names if crashes[x] < config.CRASH_TRIES]   # a store that lost the crash count never loops
    while True:
        jobs = live(queued())
        warned = backlog(jobs, warned)
        if not jobs and not any(x[1].startswith("deep-analysis-") for x in running.values()):   # one deep analysis job at a time, when no import waits
            jobs = live(deep_analysis_queued())[:1]
        while jobs and len(running) < n and not stop:
            name = jobs.pop(0)
            if not claim(name):   # gone since the listing
                continue
            old, pipe = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM}), ()   # blocked until the child has its own handler
            try:
                pipe = r, w = os.pipe()
                pid = store.fork()
            except OSError as ex:   # out of processes, memory or file descriptors. The job goes back, and the next pass tries again.
                signal.pthread_sigmask(signal.SIG_SETMASK, old)
                logs.log(dict(source="hook", job=name, result="warning", note=f"no job process: {type(ex).__name__}: {ex}"[:200]))
                for fd in pipe:
                    os.close(fd)
                requeue(name)
                time.sleep(config.POLL)
                break
            if not pid:
                for fd in [r, *running]:
                    os.close(fd)
                job_process(name, w, old)
            signal.pthread_sigmask(signal.SIG_SETMASK, old)
            os.close(w)   # so the pipe reads EOF once the child exits
            with contextlib.suppress(sqlite3.Error, OSError):   # before the next claim, see job_alive(). A job with no pid is not waited for.
                store.write("UPDATE jobs SET pid = ? WHERE name = ? AND claimed = 1", pid, name)
            running[r] = [pid, name, b""]
        plex.plex_pass(pending)
        if stop:
            if not told:
                told = True
                for pid, _, _ in running.values():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGTERM)
            if not running:
                kept = plex.save_plex(pending)
                logs.log(dict(source="hook", result="warning", note=f"stopped by SIGTERM, {len(kept)} Plex analyze requests kept for the next worker",
                         kept=[p["path"] for p in kept]))
                return
        elif not running and not pending:
            lock.close()           # release first, then look once more: a job queued meanwhile found the lock held
            if not live(queued()):   # a deep analysis job queued here was claimed at the top of this pass
                return
            lock = try_lock("worker.lock")
            if not lock:
                return             # a new event started its own worker
            continue
        wait = min([config.POLL] + [max(0, p["due"] - time.monotonic()) for p in pending])
        if not running:
            time.sleep(wait)
            continue
        for r in select.select(list(running), [], [], wait)[0]:
            data = os.read(r, 65536)
            if data:
                running[r][2] += data
                continue
            os.close(r)
            pid, name, out = running.pop(r)
            status = os.waitpid(pid, 0)[1]
            finished(name, status, out, pending)
            if os.waitstatus_to_exitcode(status) not in (0, 128 + signal.SIGTERM):   # a crash, see finished(). A yield exits 0.
                crashes[name] += 1
                if crashes[name] == config.CRASH_TRIES and still_queued(name):   # requeue() drops it, unless the store lost the count
                    logs.log(dict(source="hook", job=name, result="error", note=f"the job process crashed {config.CRASH_TRIES} times in this "
                                  "worker run, and the store still holds the job, so this worker does not run it again"))


def still_queued(name):
    """Whether the store holds the job name. A store that does not read counts as holding it."""
    try:
        return bool(store.read("SELECT 1 FROM jobs WHERE name = ?", name))
    except sqlite3.Error:
        return True


def job_process(name, w, sigmask):
    """A job process of coordinate(): run_job() on one claimed job with its own time limit, then its Plex analyze and
    the TMDB pause as JSON to the pipe w. It never returns. SIGTERM before a write raises SystemExit, see job_term().
    subprocess.run() and the other callers kill their child on the way out, so no ffmpeg or mkvmerge outlives the job.
    The stopped job stays claimed, and coordinate() puts it back in the queue. After a write the job runs to its end."""
    code, started = 1, time.time()
    try:
        STOP.update(held=False, asked=False, term=None)   # a fresh job, whatever the parent process had done
        signal.signal(signal.SIGTERM, job_term)
        signal.pthread_sigmask(signal.SIG_SETMASK, sigmask)
        pending = []
        run_job(name, pending, shared=True, claimed=True)
        os.write(w, json.dumps({"plex": pending, "down": content.DOWN}).encode())
        code = 0
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else 1
    except BaseException as ex:   # its stderr may be /dev/null, so the line is its only trace. requeue() counts the crash.
        with contextlib.suppress(Exception):
            logs.decision(dict(source="hook", job=name, outcome="error", result=config.mask(f"error: {type(ex).__name__}: {ex}")[:300],
                               trace=traceback.format_exc(limit=3)[-800:]), started)
    finally:
        os._exit(code)


def finished(name, status, out, pending):
    """Take the Plex analyze of a job process that exited, and TMDB's pause after a failure, so the next job processes
    skip TMDB the way one process would. A job still claimed goes back to the queue. An exit by SIGTERM is a stop, and
    anything else is a crash."""
    code = os.waitstatus_to_exitcode(status)
    try:
        res = json.loads(out) if out else {}
    except ValueError:
        res = {}
    pending.extend(res.get("plex") or [])
    down = res.get("down") or {}
    if down.get("until", 0) > content.DOWN["until"]:
        content.DOWN.update(down)
    if store.read("SELECT 1 FROM jobs WHERE name = ? AND claimed = 1", name):
        requeue(name, None if code == 128 + signal.SIGTERM else f"died by signal {-code}" if code < 0 else f"exited {code}")


def requeue(name, crash=None):
    """Put a claimed job back in the queue. crash says how its process ended, and a job stopped by SIGTERM or left by a
    dead worker has none. A crash counts in the job, and the CRASH_TRIES-th drops the job with an error line, so a file
    that kills its process never loops."""
    try:
        if crash:
            job = job_of(name, True)
            job["crashes"] = job.get("crashes", 0) + 1
            if job["crashes"] >= config.CRASH_TRIES:
                logs.decision(dict(source="hook", job=name, app=job.get("app"), path=job.get("path"), outcome="error",
                              result=f"error: the job process {crash}, {config.CRASH_TRIES} times, so the job is dropped",
                              **({"from": job["from"]} if job.get("from") else {}),
                              **({"held_result": logs.post_held(job["app"], job["path"], job["held"])} if job.get("held") else {})), time.time())
                drop_job(name, True)
                return
            put_job(name, job, True)
            logs.log(dict(source="hook", job=name, app=job.get("app"), path=job.get("path"), result="warning",
                     note=f"the job process {crash}, so the job goes back to the queue, try {job['crashes'] + 1} of {config.CRASH_TRIES}"))
        with store.tx():   # a newer deep analysis job of the path stays, and this one goes
            store.write("DELETE FROM jobs WHERE name = ? AND claimed = 1 AND EXISTS (SELECT 1 FROM jobs WHERE name = ? AND claimed = 0)", name, name)
            store.write("UPDATE jobs SET claimed = 0, pid = NULL, settled = 0 WHERE name = ? AND claimed = 1", name)
    except (sqlite3.Error, OSError, ValueError) as ex:
        logs.log(dict(source="hook", job=name, result="warning", note=config.mask(f"the job did not go back to the queue: {type(ex).__name__}: {ex}")[:200]))


def moved(app, job):
    """(the file's current path, None, False) when the app moved or renamed the job's file since the import, else (None,
    why the job is dropped, whether the app said it no longer has the file). It says so with a 404, another movie or
    series, or other episodes. A path the app lists that this container does not see says nothing of that. The app is
    asked by the job's file id. Its answer must name the job's movie, or the job's series
    and the same episodes, and its path must sit under one of the app's root folders. A 404 means the app no longer has the file. That is an upgrade or a delete, and a new import
    has its own job. A job marked unseen whose file the app still lists raises Unseen. Any other error raises, and the
    job's line says error."""
    a, name = apps.ARR[app], apps.ARR[app].name
    if not job.get("file_id"):
        return None, "the job has no file id to ask the app about", False
    fid = int(job["file_id"])
    try:
        f = apps.arr(app, f"{a.file_kind}/{fid}")
    except urllib.error.HTTPError as ex:
        if ex.code != 404: raise
        return None, f"{name} no longer has file {fid}", True
    owner = f.get(a.owner_key)
    if str(owner) != str(job.get("owner")):
        return None, f"{name} file {fid} belongs to {a.kind} {owner} now", True
    if not a.film and job.get("episode_ids"):
        eps = sorted(e["id"] for e in apps.arr(app, f"episode?episodeFileId={fid}"))
        if eps != regrab.job_episodes(job):
            return None, f"{name} file {fid} holds episodes {eps} now", True
    if not f.get("path") or f["path"] == job.get("path") or not os.path.exists(f["path"]):
        if job.get("unseen"):   # the listener's container never saw the file, see serve.download()
            raise Unseen(f"{name} lists file {fid} at {f.get('path')}, and this container does not see it there")
        return None, f"{name} lists file {fid} at {f.get('path')}, which is missing too", False
    if not any(f["path"].startswith(r["path"].rstrip("/") + "/") for r in apps.arr(app, "rootfolder")):
        return None, f"{name} lists file {fid} at {f['path']}, outside its root folders", False
    return f["path"], None, False


RETRY_WAIT = 60   # seconds before the next try of a Webhook job whose API did not answer. It doubles each try, up to an hour.


class Unchecked(Exception):
    """A Webhook job whose app's API did not answer before the job reached JOB_MAX_AGE, see webhook_job(), or whose file
    this container did not see by then, see run_job(). Also a job of the hook whose instance is in doubt, see
    job_refused()."""


class Unseen(Exception):
    """The app lists the file of a job marked unseen at a path this container does not see, see moved()."""


def retry(name, job, claimed, why):
    """Put the job name, queued or claimed, back in the queue under a new name, with why as its error, for a later try,
    see queued(). The first wait is RETRY_WAIT seconds, and each next one doubles, up to an hour. One change moves the
    row, so no stop leaves the job twice. Returns the wait in seconds."""
    tries = job.get("tries", 0) + 1
    wait = min(RETRY_WAIT << (tries - 1), 3600)
    due = time.time_ns() + wait * 10**9
    store.write("UPDATE jobs SET name = ?, due = ?, claimed = 0, pid = NULL, settled = 0, job = ? WHERE name = ? AND claimed = ?",
                f"{due}-{os.getpid()}.json", due, json.dumps(dict(job, tries=tries, error=why)), name, int(claimed))
    return wait


def webhook_job(name, job, claimed=False):
    """The job name, queued or claimed, that the listener queued while the app's API failed, completed by the
    listener's checks and lookups, see Event.from_webhook(), and written back to its row. When the API still fails, the
    job goes back to the queue for a later try, see retry(), and the answer is None. A job older than JOB_MAX_AGE
    raises Unchecked, so its one decision line says the import was never checked. A file this container does not see
    marks the job unseen, as serve.download() does. Refused passes up, as for an item the app no longer has. One
    row holds the job at every moment, and the row drops the body before the claim, so a stop never runs the job twice
    or claims the kept copies twice."""
    app, name_ = job["app"], apps.ARR[job["app"]].name
    if time.time() - job["time"] > config.JOB_MAX_AGE:
        raise Unchecked(f"the {name_} API did not answer for a day, so the import was never checked. The last try: {job.get('error')}")
    try:
        done = dict(Event.from_webhook(app, job["webhook"]).job(), time=job["time"])
    except Refused:
        raise
    except Exception as ex:
        why = config.mask(f"{type(ex).__name__}: {ex}")[:300]
        wait = retry(name, job, claimed, why)
        logs.log(dict(source="hook", app=app, job=name, result="warning",
                      note=f"the {name_} API did not answer: {why}. The job goes back to the queue, and the next try runs in {wait} seconds"))
        return None
    if not os.path.isfile(done["path"]):
        done["unseen"] = True
    put_job(name, done, claimed)
    claim_kept(done)
    return done


def run_job(name, pending, shared=False, claimed=False):
    """Run one job of the queue, or a claimed one. Lock, process, log, queue a Plex lookup after an edit, then delete
    the job. A file that is gone is asked for by its file id, see moved(). shared is a job process of coordinate(): the
    checks hold the file lock shared, see process(). A job that raises Replan runs again here with the lock exclusive
    from the start, after a log line that says why."""
    if name.startswith("deep-analysis-"):   # a job process of coordinate() runs a deep analysis job too
        return deep_analysis(name, pending, claimed)
    rec, want, started, path, again, run = dict(source="hook", job=name), None, time.time(), None, None, None
    job = {}   # restore_log() reads it after an error too
    try:
        job = job_of(name, claimed)
        app, path = job["app"], job["path"]
        rec.update(app=app, path=path)
        if job.get("webhook"):   # the listener queued it while the app's API failed, see webhook_job()
            job = webhook_job(name, job, claimed)
            if not job:   # back in the queue for a later try
                return None
            rec["path"] = path = job["path"]
        if why := job_refused(job):   # before any read of the app, which may be another instance's
            raise Unchecked(why)
        logs.status("policy", "failed" if decide.POLICY is None else "ok", config.POLICY_ERROR or "")
        unit = download_unit(job)
        fid, kind = int(job["file_id"]) if job.get("file_id") else None, unit.get("kind", "audio")
        handled = kind in ("audio", "video") and fid in unit.get("clean", [])   # that check ran on it with its download
        old, gone = job.get("moved_from") or path, None
        if fid not in unit.get("deleted", []) and not (path and os.path.exists(path)):
            try:
                new, gone, _ = moved(app, job)
            except Unseen as ex:   # the mount may be missing or slow, so the job waits for the file as for the API
                if time.time() - job["time"] > config.JOB_MAX_AGE:
                    raise Unchecked(f"{ex}. A day passed, so the import was never checked") from None
                wait = retry(name, job, claimed, str(ex))
                logs.log(dict(source="hook", app=app, job=name, path=path, result="warning",
                              note=f"{ex}. The job goes back to the queue, and the next try runs in {wait} seconds"))
                return None
            if new:   # the app renamed or moved it meanwhile. The checks, Plex and a re-grab all use the new path.
                logs.log(dict(source="hook", app=app, job=name, result="file_moved", old_path=path, path=new, ids={"file_id": fid}))
                job.update(path=new, moved_from=old)
                put_job(name, job, claimed)   # a re-run starts at the new path and logs no second file_moved line
            rec["path"] = path = new or path
        if fid in unit.get("deleted", []):
            rec.update(outcome="deleted_with_download", fault=kind, result=f"skipped, deleted with its download for {report.FAULTS[kind][0]}")
        elif gone:
            path = None
            rec.update(outcome="file_gone", result="dropped, the file is gone", note=gone)
        elif time.time() - job["time"] > config.JOB_MAX_AGE:
            rec.update(outcome="job_stale", result="dropped, the job is older than a day")
        elif decide.POLICY is None:   # alert once per distinct error, then skip. The file keeps its flags.
            rec.update(outcome="no_policy", result=f"skipped, no policy: {config.POLICY_ERROR}")
            policy = dict(app=app, source="hook", label="the policy file", path=config.CFG.policy_file, findings=[{
                "kind": "policy", "file": config.CFG.policy_file, "error": config.POLICY_ERROR, "action": {"code": "no_policy", "file": os.path.basename(path)}}])
            rec["alert_result"] = logs.alert_findings(policy, hashlib.sha1((config.POLICY_ERROR or "").encode()).hexdigest())
        else:
            with locked(shared, content.Deadline(config.LOCK_WAIT, f"gave up after waiting {config.LOCK_WAIT} seconds for the lock")) as lock:
                # A re-grab may have run while this job waited. It holds the lock exclusive, so what is read now stays true.
                now = download_unit(job)
                if shared and (now != unit or not os.path.exists(path)):
                    raise Replan("its download or its file changed while the job waited for the lock")
                unit, kind = now, now.get("kind", "audio")   # a re-run reads them here, under its exclusive lock
                handled = kind in ("audio", "video") and fid in unit.get("clean", [])
                if fid in unit.get("deleted", []):
                    rec.update(outcome="deleted_with_download", fault=kind, result=f"skipped, deleted with its download for {report.FAULTS[kind][0]}")
                elif not os.path.exists(path):
                    rec.update(outcome="file_gone", result="dropped, the file is gone", note="the file went while the job waited for the lock")
                else:
                    config.DEADLINE.start(config.BUDGET)
                    label, original, runtime, want, kids, ctx = apps.ARR[app].item(int(job["owner"]), fid)
                    ids = {"app_id": job.get("owner"), "file_id": job.get("file_id"), "episode_ids": job.get("episode_ids"), "guids": want["guids"],
                           "slug": ctx.get("slug")}   # the slug links the alerts to the item, see apps.App.page()
                    run = process.Ctx(
                        app, path, label, original, runtime, job=job, audio=not (handled and kind == "audio"), video=not (handled and kind == "video"),
                        kids=kids, release=job.get("release") or "", ids=ids, item=ctx, lock=lock,
                        shared=shared and types.SimpleNamespace(exclusive=lambda st: exclusive(lock, path, st, name, job, unit), settle=lambda: settle(name),
                                                                turn=lambda: wait_turn(name, job), reshare=lambda st: reshared(lock, path, st)))
                    rec = process.process(run)
                    rec["job"] = name   # process() gives a new record, and the summary line names the job, see report.render()
                    if handled:
                        rec.setdefault("notes", []).append(f"{kind} already checked with its download")
                    if path != old:
                        rec.update(moved_from=old, reasons=["file_moved"] + rec.get("reasons", []))
    except Replan as ex:
        again = ex.args[0] if ex.args else str(ex)
    except Unchecked as ex:
        rec.update(outcome="error", result=f"error: {ex}"[:500])
    except Exception as ex:   # the time limit, a timeout, a bad probe: record it and go on with the next job
        if isinstance(ex, subsync.Broken):   # only a run with AMG_INVARIANTS=1 raises one, and a test run must see it
            raise
        rec.update(outcome="error", result=f"error: {type(ex).__name__}: {ex}"[:500])
        rec["trace"] = traceback.format_exc(limit=3)[-800:]
    finally:
        config.DEADLINE.stop()
    if again:
        logs.log(dict(source="hook", app=rec.get("app"), job=name, path=path, result="warning", note=f"re-planned with the file lock exclusive: {again}"))
        settle(name, False)    # the re-run may re-grab, so younger jobs of the download wait for it again
        wait_turn(name, job)   # and it goes after the older jobs, as its exclusive step would
        return run_job(name, pending, claimed=claimed)
    rec = logs.decision(regrab.restore_log(rec, job, pending, rec.get("app")), started)
    path = path and rec.get("path")   # a conversion renames the file
    edited = process.changed(rec) and os.path.exists(path or "")   # a re-grab may have deleted it
    if edited and want:   # outside the file lock, so a slow Plex never holds up the next file
        pending.append(plex.plex_after(app, "hook", rec, want))
    held, deep = run and run.held, None   # the alerts the import held for the deep analysis, see process.alerts()
    if "tracks" in rec:   # process() ran: the deep analysis of the file, docs/design.md, "Subtitle match"
        with contextlib.suppress(OSError, sqlite3.Error):
            deep = queue_deep_analysis(job, rec, dict(label=label, original=original, runtime=runtime, want=want, kids=kids, ctx=ctx,
                                                      release=job.get("release") or ""), held)
    if held and not deep:   # a held alert is never lost
        with contextlib.suppress(OSError):
            logs.log(dict(source="hook", app=rec.get("app"), job=name, path=rec.get("path"), result="warning", held_result=logs.post_held(rec["app"], rec["path"], held),
                          note="the deep analysis of the file did not queue, so the alerts held for it posted now"))
    drop_job(name, claimed)
