# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The command line: main(), the backfill, --sub-time, --check, the audit and the scans."""
import argparse, collections, concurrent.futures, contextlib, datetime, fcntl, json, os, re, signal, sqlite3, subprocess, sys, threading, time, types, uuid

from . import apps, checks, config, content, convert, decide, logs, plex, process, regrab, report, runner, store, subsync, subtitles, vault


def plan_record(app, label, path, d):
    """One plan as a JSON line for --plan-out: its class, edits, and the tracks the audit needs to re-check the invariants."""
    return {"app": app, "label": label, "path": path, "class": decide.plan_class(d), "edits": d["edits"], "undecided": d.get("undecided"),
            "dropped": d.get("dropped", []), "cls": d.get("cls"), "orig": d.get("orig", []),
            "tracks": [{k: t[k] for k in ("kind", "sel", "pos", "lang", "role", "extra", "default")} for t in d.get("tracks", [])]}


def spread(plans, n):
    """n plans with edits, taken round-robin across their classes, so a canary covers every class it can."""
    by = {}
    for r in plans:
        by.setdefault(r["class"], []).append(r)
    out, queues = [], sorted(by.values(), key=len, reverse=True)
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n: out.append(q.pop(0))
    return out


def selected(path):
    """A backfill reads a file with two or more audio tracks, any subtitle track, a main audio track whose tag the heard
    language may change (und among them), a tag form to fix, or a container other than Matroska, which a repack
    fixes. One audio track and no subtitle plays that track whatever its flag says, and the hook still sets a missing
    default flag on import. The probe runs at the backfill's priority. A file whose probe fails is taken, so process()
    logs why. A file mkvmerge does not recognize is left out."""
    try:
        j = checks.mkvmerge(path)
    except Exception:
        return True
    ts = decide.classify(j); au = [t for t in ts if t["kind"] == "a"]; r = decide.retag(j, table=checks.langs())
    return len(au) > 1 or len(ts) > len(au) or bool(r["edits"] or r["ask"]) or (j.get("container") or {}).get("type") not in (None, "Matroska")


def upgrade(lock, path, st):
    """exclusive() for a backfill file, which has no time limit and no download. It trades the shared file lock for the
    exclusive one before an edit, and raises Replan when the file changed since the checks started from st."""
    fcntl.flock(lock, fcntl.LOCK_UN)
    runner.gated(lock, fcntl.LOCK_EX)
    runner.same_file(path, st)


def backfill_file(app, f, info, a, keep_plan, pool=None, job=None, save=None):
    """One file of a backfill, as (the seconds it took, the decision record), or None when its own tracks leave it out.
    The selection probe and the checks hold the file lock shared, so the workers of a dry run read side by side and an
    import never waits for a file's language detection. An edit takes the lock exclusive, see upgrade(). A file that
    changed meanwhile, or needs a repack, runs again with the lock exclusive from the start, as a job process does.
    pool is the conversion pool of a --convert apply. The file then holds a slot of it throughout, taken before the
    lock, and its conversion runs under the shared lock, see convert(). A re-run converts under the exclusive lock.
    --convert takes every file that is not .mkv, so selected() does not apply. With --sub-check a file whose subtitle
    check is saved and asks for nothing more is skipped, and the result is "checked", unless --recheck is given, see
    subtitles.sub_cached(). A file with a sidecar is taken then, whatever its tracks.

    job is the job of a --check run, see check(). The file then runs as an import, under an import's time limit, or as
    the deep analysis after it when job holds deep. An apply posts its alerts when an app names the file. The caller
    ends the time limit. job["run"] keeps the process.Ctx of the run, as runner.run_job() reads its own. save stores job
    in the queue after a conversion, see process.Ctx. A job a backfill queued yields to an import, see
    runner.deep_analysis(), so subtitles.Yielded passes through."""
    label, original, runtime, want, kids, ctx = info
    started, path, ids = time.time(), f["path"], file_ids(f, want, ctx)
    mode = ("deep" if job.get("deep") else "import") if job else "convert" if a.convert else "sub_time" if a.sub_time else "sub_check" if a.sub_check else "backfill"
    def run(lock, shared, pool):
        if mode == "import":   # an import's time limit, as runner.run_job() starts it
            config.DEADLINE.start(config.BUDGET)
        c = process.Ctx(app, path, label, original, runtime, mode=mode, apply=a.apply, post=bool(job and app) and a.apply, kids=kids,
                        release=f.get("sceneName") or "", keep_plan=keep_plan, ids=ids, item=ctx, lock=lock, shared=shared, pool=pool,
                        force=a.force.get(path), job=job, save=save)
        if job:
            job["run"] = c
        return process.process(c)
    try:
        with pool.slots if pool else contextlib.nullcontext():   # the slot first, so no lock waits for one
            try:
                with runner.locked(shared=True) as lock:
                    if a.sub_check and not a.recheck and subtitles.sub_cached(path):   # --sub-time takes each file it names
                        return "checked"
                    if not (job or a.sub_time or a.plan_from or a.convert or selected(path) or (a.sub_check and subtitles.side_stats(path))):
                        return None
                    rec = run(lock, types.SimpleNamespace(exclusive=lambda st: upgrade(lock, path, st), settle=lambda: None, turn=lambda: None,
                                                          reshare=lambda st: runner.reshared(lock, path, st)), pool)
            except runner.Replan as ex:
                with runner.locked() as lock:
                    rec = subtitles.swept_before(run(lock, False, None), ex)
    except Exception as ex:
        if isinstance(ex, (subsync.Broken, subtitles.Yielded)):   # only a run with AMG_INVARIANTS=1 raises Broken, and a test run must see it
            raise
        rec = dict(app=app, source="check" if job else "backfill", apply=a.apply, ids=ids, label=label, path=path, outcome="error",
                   result=config.mask(f"error: {type(ex).__name__}: {ex}")[:300])
    return time.time() - started, rec


def file_ids(f, want, ctx):
    """The ids of the file record f of App.library(), with its Plex lookup want and its metadata context ctx. --sub-time
    may name no item. The slug links the audit post to the item."""
    return {"app_id": f.get("movieId") or f.get("seriesId"), "file_id": f.get("id"), "guids": (want or {}).get("guids", []),
            "slug": (ctx or {}).get("slug")}


def logged_refusals(paths):
    """{path: the last refusal of its conversion in the decision log, as the result text of a failed repack, or ""}
    for paths. The store keeps the decision lines, see logs.decision(). A forced conversion's line counts with the
    refusal it overrode. --force-convert forces a file only for that refusal, so a file that now fails for another
    reason stays."""
    out = dict.fromkeys(paths, "")
    for path in out:
        for raw, in store.read("SELECT rec FROM decisions WHERE path = ? ORDER BY rowid", path):
            r = json.loads(raw)
            rp = r.get("repack") or {}
            if rp.get("forced"):
                out[path] = config.mask(f"repack failed: {rp['forced']}")[:500]
            elif r["outcome"] == "repack_failed":
                out[path] = r["result"]
    return out


HALT = threading.Event()   # SIGTERM or Ctrl+C in a backfill sets it, so a run of several instances stops whole


def unset(app):
    """Whether the user did not set up the instance app, see apps.no_key(). A backfill of every instance skips it."""
    try:
        apps.api_key(app)
    except FileNotFoundError as ex:
        return not apps.no_key(app, ex)
    except (OSError, AttributeError):   # app_list() fails on it, so the run names it
        pass
    return False


def backfill_args(argv):
    """The options of --backfill, checked, see backfill(). a.queue is True for a backfill that queues, see
    backfill_queue(). main() reads it, because a dry run that queues writes no status.json."""
    ap = argparse.ArgumentParser(prog="arr-media-guard --backfill", description="Run the hook's checks and decision over the "
                                 "library of one instance, or of every instance in turn, at nice 19 and idle I/O priority. With no "
                                 "pass flag and no plan option, --apply puts one job per file in the background queue. The worker runs "
                                 "each as --check --apply runs a file, after the imports. Otherwise a backfill prints its alerts and "
                                 "never posts them.")
    ap.add_argument("app", nargs="?", choices=sorted(config.CFG.apps), help="the instance. With none the run takes every instance in turn")
    ap.add_argument("--apply", action="store_true", help="edit the files. Without it the run is dry and changes nothing")
    ap.add_argument("--ids", type=int, nargs="+", default=[], help="only these items: movie ids for radarr, series ids for sonarr")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files that need an edit. A scan checks N files, and a "
                    "queue takes N files")
    scan_kind = ap.add_mutually_exclusive_group()
    scan_kind.add_argument("--check-audio", action="store_true", help="scan for broken audio. A scan never edits or re-grabs, "
                           "resumes where the last run stopped, and stops cleanly on SIGTERM or Ctrl+C")
    scan_kind.add_argument("--check-video", action="store_true", help="scan for corrupt video, as --check-audio does")
    ap.add_argument("--restart", action="store_true", help="start a new pass of the scan or of the queue")
    ap.add_argument("--workers", type=int, help="files at a time in a dry run or a scan, SCAN_WORKERS by default, and in a "
                    "--convert --apply, CONVERT_WORKERS by default. An apply edits one file at a time")
    ap.add_argument("--plan-out", metavar="FILE", help="write each file's plan as a JSON line")
    ap.add_argument("--plan-from", metavar="FILE", help="apply only to the files the plan of a dry run edits")
    ap.add_argument("--canary", type=int, default=0, help="apply to N files of the plan per instance, spread across its classes")
    ap.add_argument("--only-undecided", action="store_true", help="take the files the plan left undecided, for language detection")
    ap.add_argument("--convert", action="store_true", help="convert the files that are not .mkv into Matroska")
    ap.add_argument("--plex-later", action="store_true", help="list the folders a conversion changed for --plex-flush")
    ap.add_argument("--force-convert", nargs="+", default=[], metavar="PATH", help="convert these files past the refusal the "
                    "decision log names, and keep each original")
    ap.add_argument("--sub-check", action="store_true", help="run the subtitle match and timing check too")
    ap.add_argument("--paths", nargs="+", default=[], metavar="PATH", help="only these files")
    ap.add_argument("--recheck", action="store_true", help="with --sub-check, check each file again, whatever its saved result says")
    ap.add_argument("--drop-queued", action="store_true", help="drop the jobs that a backfill queued and that wait or run, of the "
                    "instance or of every instance, and do nothing else")
    a = ap.parse_args(argv)
    a.sub_time = False   # see sub_time()
    a.queue = not (a.convert or a.sub_check or a.check_audio or a.check_video or a.plan_out or a.plan_from)   # see backfill_queue()
    if a.drop_queued:   # it runs no pass, so no check of the pass flags applies
        return a
    if not a.app and (a.ids or a.paths or a.force_convert):
        ap.error("--ids, --paths and --force-convert need an instance, because ids and paths belong to one")
    if a.recheck and not a.sub_check:
        ap.error("--recheck needs --sub-check")
    if a.sub_check and (a.convert or a.check_audio or a.check_video):
        ap.error("--sub-check runs with the flag backfill, not with --convert or a scan")
    if a.paths and (a.check_audio or a.check_video):
        ap.error("--paths limits the flag backfill, --convert and --sub-check, not a scan")
    if a.force_convert and not (a.convert and a.apply):
        ap.error("--force-convert needs --convert --apply")
    if a.force_convert and not config.CFG.keep_days:
        ap.error("--force-convert needs KEEP_ORIGINALS_DAYS above 0, so the original is kept")
    if a.plex_later and not (a.convert and a.apply):
        ap.error("--plex-later needs --convert --apply")
    if a.canary and not (a.plan_from and a.apply):
        ap.error("--canary needs --apply and --plan-from, the dry run's plan file")
    if a.only_undecided and not a.plan_from:
        ap.error("--only-undecided needs --plan-from, the dry run's plan file")
    if a.workers is not None and (a.workers < 1 or (a.workers > 1 and a.apply and not a.convert)):
        ap.error("--workers takes 1 or more, and more than 1 only in a dry run, a scan or --convert --apply")
    return a


def backfill(a):
    """--backfill: backfill_app() for one instance, or for every instance in config.CFG.apps order. An instance that
    fails ends with one line, and the run goes on to the next one and exits non-zero at the end. See backfill_args()."""
    if a.drop_queued:
        n = runner.drop_backfill(a.app)
        print(f"dropped {n} job{'s' if n != 1 else ''} that a backfill queued", flush=True)
        return
    os.nice(19)   # a full pass probes every file on the NAS, so it yields to imports and to Plex
    subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False)
    HALT.clear()
    todo = [a.app] if a.app else config.CFG.apps
    a.plans = {app: plan_rows(a.plan_from, app) for app in todo} if a.plan_from else {}   # before --plan-out empties the file, which may be the same
    if a.plan_out:   # as /config/plans/, which the docs name
        os.makedirs(os.path.dirname(os.path.abspath(a.plan_out)), exist_ok=True)
    failed, out = [], open(a.plan_out, "w") if a.plan_out else None   # one plan file for every instance
    try:
        for app in todo:
            if HALT.is_set():
                break
            if not a.app and unset(app):
                print(f"== {app}: not set up, skipped", flush=True)
                continue
            if not a.app:
                print(f"== {app}", flush=True)
            try:
                backfill_app(app, a, out)
            except (Exception, SystemExit) as ex:   # app_list() exits for an instance set up in part
                if a.app or isinstance(ex, subsync.Broken):   # only a run with AMG_INVARIANTS=1 raises Broken, and a test run must see it
                    raise
                why = config.mask(str(ex) if isinstance(ex, SystemExit) else f"{type(ex).__name__}: {ex}")[:300]
                print(f"{app}: the backfill stopped: {why}", flush=True)
                logs.log(dict(app=app, source="backfill", result="error", note=why))
                failed.append(app)
    finally:
        if out: out.close()
    if failed:
        sys.exit(f"the backfill failed for {', '.join(failed)}")


def backfill_app(app, a, out):
    """The backfill of one instance. It writes the plans to out, the open --plan-out file or None."""
    if a.check_audio or a.check_video:
        a.workers = a.workers or config.CFG.scan_workers   # an explicit --workers wins over SCAN_WORKERS
        return scan(app, a, "audio" if a.check_audio else "video")
    work, library = apps.ARR[app].library(apps.app_list(app), set(a.ids))   # (file, (label, original, runtime, Plex lookup, kids, metadata context)), all files
    # The app lists the files. Each file's own tracks select it, see selected(), because the app's stored mediaInfo goes
    # stale and can miss a subtitle track.
    unconverted = [w for w in work if not w[0]["path"].lower().endswith(".mkv")]   # --convert takes only these, docs/design.md, "Conversion"
    work = [w for w in work if a.queue or w[0]["path"].lower().endswith(".mkv")]   # a queue takes every file
    if a.convert:
        work = unconverted
        with contextlib.suppress(OSError):   # a new pass starts a new list of refused and failed conversions
            open(os.path.join(config.CFG.state_dir, f"convert-{app}.txt"), "w").close()
    if a.plan_from:   # only the files the dry run planned edits for, or left undecided. Each one is probed and planned again.
        listed = {w[0]["path"] for w in work}   # so a canary with --ids takes its sample from those items
        plans = [r for r in a.plans[app] if r["path"] in listed
                 and (r["undecided"] if a.only_undecided else r["edits"] or r.get("outcome") == "would_repack" or r.get("header_repair") == "would_repair_header")]
        keep = {r["path"] for r in (spread(plans, a.canary) if a.canary else plans)}
        work = [w for w in work if w[0]["path"] in keep]
    for given in filter(None, (a.paths, a.force_convert)):   # the run takes only the listed files
        listed = {os.path.abspath(p) for p in given}
        for p in sorted(listed - {w[0]["path"] for w in work}):
            print(f"not in this run's work list, skipped: {p}", flush=True)
        work = [w for w in work if w[0]["path"] in listed]
    if a.queue:
        return backfill_queue(app, a, work)
    a.force = logged_refusals({w[0]["path"] for w in work}) if a.force_convert else {}   # path: the refusal a person checked, docs/design.md, "Conversion"
    for p in sorted(p for p, why in a.force.items() if not why):
        print(f"no proof refusal in the decision log, so {'it is not' if apps.ARR[app].film else 'only its name check is'} forced: {p}",
              flush=True)
    # A dry run reads SCAN_WORKERS files at a time, like a scan, and --workers wins. An apply edits one file at a time, so
    # --limit, --canary and the Plex pace stay exact. A conversion apply converts CONVERT_WORKERS files at a time through
    # the slots of a pool, see backfill_files().
    if a.apply:
        workers = (a.workers or config.CFG.convert_workers) if a.convert else 1
    else:
        workers = a.workers or config.CFG.scan_workers
    print(f"{len(work)} {'files that are not .mkv' if a.convert else 'mkv files, each read for its own tracks'}, "
          f"{('APPLY' + (f', {workers} at a time' if workers > 1 else '')) if a.apply else f'dry run, {workers} at a time'}", flush=True)
    counts, skipped, needed = {}, set(), 0   # skipped: plex_analyze()'s sections
    burst = {}   # plex_analyze()'s rows of idle checks, see plex_step()
    scans, rescans, settles, stop = {}, set(), {}, threading.Event()   # scans: folder -> plex_folder_job(), rescans and settles: per item, at the end
    if a.apply and a.convert:   # a stopped run left these
        convert.pending_recover(app, True)
    converted = [0]   # a list, so backfill_files() reads the count as it grows
    checks.langs()   # the language table, read once before any worker
    pool = types.SimpleNamespace(slots=threading.BoundedSemaphore(workers), imports=convert.Imports()) if a.apply and workers > 1 else None
    one = lambda w: backfill_file(app, w[0], w[1], a, bool(out), pool)
    ex = concurrent.futures.ThreadPoolExecutor(workers) if workers > 1 and not pool else None
    # An apply goes through backfill_files(): a pool yields its files in the order they end, and SIGTERM lets the files in
    # flight end first and write their decision lines. A dry run keeps the file order.
    results = backfill_files(work, lambda *w: one(w), 2 * workers if pool else 1, stop, converted) if a.apply else \
        zip(work, ex.map(one, work) if ex else map(one, work))
    subs = collections.Counter()   # the --sub-check summary
    for (f, (label, original, runtime, want, kids, ctx)), got in results:
        if got is None or got == "checked":
            key = "not_selected" if got is None else "subtitles_cached"
            counts[key] = counts.get(key, 0) + 1
            continue
        took, rec = got
        if out and "plan" in rec:
            out.write(json.dumps(rec.pop("plan")) + "\n")
        counts[rec["outcome"]] = counts.get(rec["outcome"], 0) + 1
        for code in {"repacked", *config.REPAIRED, "would_repair_header"} & set(rec.get("reasons", [])) - {rec["outcome"]}:   # each file once
            counts[code] = counts.get(code, 0) + 1
        if rec.get("edits") or rec.get("findings") or rec.get("heard") or "repack" in rec or "header_repair" in rec or rec.get("subcheck") \
                or rec.get("flash") or rec.get("garbled") or rec["outcome"] in ("undecided", "dropped", "error", "verify_failed", "edit_failed", "read_only",
                                                                                 "hardlinked"):
            print(report.render(rec, "cli"), flush=True)
        if a.apply and process.changed(rec) and want and config.CFG.plex_url:
            p = plex.plex_after(app, "backfill", rec, want)
            if (p["want"] or {}).get("folder") and a.plex_later:   # --plex-flush scans them later, one section at a time
                plex.plex_later(app, p["path"])
            elif (p["want"] or {}).get("folder"):   # the episodes of a season share one folder, so it is scanned once
                scans[p["path"]] = p
            else:
                rec["plex"], rec["plex_reason"] = plex.plex_analyze(p, skipped, burst=burst)
                rec["plex_section"] = p["section"]   # --plex-flush and the worker read it, see after_analyze()
        logs.decision(rec, time.time() - took)   # every file, dry runs too, so a run can be reviewed later
        if "plex_section" in rec:   # after the decision line, so a folder scan elsewhere sees this analyze at once
            time.sleep(config.PLEX_PACE)
        if "after the run" in ((rec.get("repack") or {}).get("rescan"), (rec.get("header_repair") or {}).get("rescan"),
                               (rec.get("subremux") or {}).get("rescan")):
            rescans.add(rec["ids"]["app_id"])
        if a.sub_check:
            sub_count(subs, rec, took)
        if (rec.get("repack") or {}).get("pending"):
            settles.setdefault(rec["ids"]["app_id"], []).append(rec["repack"]["pending"])
        owner = (rec.get("ids") or {}).get("app_id")
        if apps.ARR[app].film and owner in rescans:   # at once: until then movie/<id> shows the old record, see movie_file()
            rescans.discard(owner)
            rescan_item(app, owner, settles.pop(owner, None))
        converted[0] += a.apply and "repacked" in rec.get("reasons", [])
        if converted[0] >= config.CFG.convert_max and not stop.is_set():
            print(f"stopped after {converted[0]} conversions, the cap of one run (CONVERT_MAX_FILES)", flush=True)
            stop.set()
        needed += rec["outcome"] in ("edited", "dry_run", "would_repack") \
            or bool({*config.REPAIRED, "would_repair_header", "would_remux_subtitles"} & set(rec.get("reasons", [])))
        if a.limit and needed >= a.limit: stop.set()
        if stop.is_set() and not pool: break   # a pool lets its files in flight end, see backfill_files()
    if ex: ex.shutdown(cancel_futures=True)
    for owner in sorted(rescans):   # the size and media info follow the edits, and the record of a converted original goes
        rescan_item(app, owner, settles.get(owner))
    # After every file of the run is in place, with no lock and no worker. A --convert run takes only files that are not
    # .mkv, which all change their name, so it sends no analyze and plex_pass()'s PLEX_SCAN_AFTER never applies here. A
    # run stopped by Ctrl+C leaves them to Plex's own nightly scan.
    for p in scans.values():
        done, code = plex.plex_analyze(p, skipped)
        logs.log(dict(app=app, source="backfill", label=p["label"], path=p["path"], result="plex", plex=done, plex_reason=code, decision_id=p["decision_id"]))
        time.sleep(config.PLEX_PACE)
    print("summary:", json.dumps(counts))
    if a.sub_check:
        print(sub_summary(subs, library), flush=True)


def backfill_queue(app, a, work):
    """A backfill with no pass flag and no plan option (docs/commands.md, "Dry runs and the backfill"). Its apply puts one
    job per file of work in the background queue, see runner.queue_listed(), and the worker runs each one as --check
    --apply runs a file, see runner.deep_analysis(). A file with a job of its path that waits or runs keeps that job.
    The pass takes the files in file id order and keeps its place in the state a scan keeps, see advance(). So --limit
    N queues the next N files, and a run after the pass is done queues nothing. --restart starts a new pass. An --ids or
    --paths run keeps a state of its own, which starts again when they change. A dry run writes no state, probes no
    file and names the next files it would queue. Both print the count and the hours of video, from the runtime the
    app lists."""
    name = f"queue-{app}" + ("-ids" if a.ids or a.paths else "")
    state, todo, order = pass_left(name, work, lambda w: w[0]["id"], a.restart, sorted(a.ids) + sorted(a.paths))
    if not todo:
        print(f"{app}: the pass is done, so nothing was queued. --restart starts a new pass.", flush=True)
        return
    n = minutes = 0
    for f, info in todo:
        if a.limit and n >= a.limit:
            break
        with contextlib.suppress(OSError):   # a file that went since the app listed it, or whose mount is away, stays in the pass
            got = runner.queue_listed(app, f, info, backfill=True) if a.apply else not runner.still_queued(runner.deep_name(f["path"]))
            if got:
                n, minutes = n + 1, minutes + info[2]
                if not a.apply:
                    print(f"would queue: {f['path']}", flush=True)
            # The job of the path, queued now or before, takes the file. A recheck repeats only the subtitle check, so it does not.
            if a.apply and store.read(f"SELECT 1 FROM jobs WHERE name = ? AND {runner.RECHECK} = 0", runner.deep_name(f["path"])):
                advance(state, order, f["id"])
                store.put("scan", name, state)
    print(f"{app}: {n} file{'s' if n != 1 else ''} {'queued' if a.apply else 'to queue, dry run'}, {minutes / 60:.1f} hours of video, "
          f"{len(set(order) - set(state['done']))} of {len(work)} files left in this pass. They wait in the background queue behind the "
          "imports and run one at a time.", flush=True)


def sub_count(c, rec, took):
    """Add one file's subtitle check to the counts of a --sub-check backfill."""
    rs = rec.get("subcheck") or {}
    c["all"] += 1
    c["all_seconds"] += took
    if not rs:
        return
    c["files"] += 1
    c["cpu"] += sum({r["audio"]: r.get("cpu") or 0 for r in rs.values() if not r.get("cached")}.values())
    for r in rs.values():
        c[r["verdict"]] += 1
        c["fixes"] += bool((r.get("timing") or {}).get("fix"))


def sub_summary(c, library):
    """The last line of a --sub-check backfill: what it checked, what it found, the CPU time, and an estimate for the
    whole library at this run's pace."""
    each = c["all_seconds"] / c["all"] if c["all"] else 0
    return (f'subtitle check: {c["files"]} files checked, {c["match"]} tracks match, {c["mismatch"]} do not, {c["unknown"]} unknown, '
            f'{c["fixes"]} timing fixes, {c["cpu"]:.0f} CPU seconds for the hearing. '
            + (f'At {each:.1f} seconds a file, all {library} files of the library take about {each * library / 3600:.1f} hours.'
               if c["all"] else "No file ran, so there is no estimate."))


def sub_time(argv):
    """--sub-time: the subtitle check of each Matroska file named by path, with the reference timing and the whole-file
    timing (docs/design.md, "Subtitle match"). It runs the check of --backfill --sub-check on the file, past its cached
    verdicts, and writes them to the cache. It is dry unless --apply is given, and it acts as a backfill does: the file
    lock, hide_dir, keep_dir, the proof, and a Plex analyze after an edit. Alerts print and never post. An app names
    the item only for its original language and the other inputs of the check, see App.library(). A path no app
    lists runs with no item, so no original language is known, and it keeps every flag except those a subtitle
    verdict turns off, see process(). A lookup that fails, by an error, a timeout or an app restart, is not the same:
    --apply then stops before any change, and a dry run says so. An app the user set up in part fails the same way, see
    no_key()."""
    ap = argparse.ArgumentParser(prog="arr-media-guard --sub-time", description="The subtitle check of each Matroska file, with the "
                                 "reference timing and the whole-file timing, past the cache. See docs/subtitles.md.")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--apply", action="store_true", help="make the changes. Without it the run is dry and changes nothing")
    a = ap.parse_args(argv)
    a.sub_check, a.sub_time, a.recheck, a.plan_from, a.convert, a.force = True, True, True, None, False, {}
    os.nice(19)   # it hears and reads files on the NAS, so it yields to imports and to Plex
    subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False)
    checks.langs()
    left, work, failed, unset = [os.path.abspath(p) for p in a.paths], [], [], []
    for app in config.CFG.apps:
        try:
            found = [(app, f, i) for f, i in apps.ARR[app].library(apps.arr(app, apps.ARR[app].kind), paths=left)[0]] if left else []
        except FileNotFoundError as ex:   # no API key and no config.xml, so the app lists nothing
            why = apps.no_key(app, ex)
            if why:   # the user set the app up in part, so it fails as a lookup does
                failed.append(why)
                print(why, flush=True)
            else:   # the user did not set the app up
                unset.append(app)
            continue
        except Exception as ex:   # an error, a timeout or a restart: the app could not say what it lists
            failed.append(config.mask(f"the {app} lookup failed: {type(ex).__name__}: {ex}")[:200])
            print(failed[-1], flush=True)
            continue
        work += found
        left = [p for p in left if p not in {f.get("path") for _, f, _ in found}]
    if failed and a.apply and left:   # a failed lookup never applies: it would decide with no original language
        sys.exit(f"--sub-time --apply stops, and nothing changed: {'; '.join(failed)}")
    if len(unset) == len(config.CFG.apps):   # one line for every path
        print("no Sonarr or Radarr is set up, so this run knows no original language. Set SONARR_URL and SONARR_API_KEY, or RADARR_URL "
              "and RADARR_API_KEY, so an app can name the original language.", flush=True)
    for p in left:
        if len(unset) < len(config.CFG.apps):
            print(f"{'no app could be asked, so this dry run' if failed else 'no app lists it, so it'} knows no original language: {p}", flush=True)
        work.append((None, {"path": p}, (os.path.basename(p), None, 0, None, False, {"listed": 0})))
    skipped, burst, missed, absent = set(), {}, [], []
    for app, f, info in work:
        if not os.path.isfile(f["path"]):
            print(f"no file at the path, skipped: {f['path']}", flush=True)
            absent.append(f["path"])
            continue
        if not f["path"].lower().endswith(".mkv"):
            print(f"not a Matroska file, skipped: {f['path']}", flush=True)
            continue
        took, rec = backfill_file(app, f, info, a, False)
        missed += [f'{f["path"]}: {why}'] if a.apply and (why := report.missed(rec)) else []
        if a.apply and info[3] and config.CFG.plex_url and process.changed(rec, repack=False):
            rec["plex"], rec["plex_reason"] = plex.plex_analyze(plex.plex_after(app, "backfill", rec, info[3]), skipped, burst=burst)
        rec = logs.decision(rec, time.time() - took)
        print(report.sub_time_report(rec, report.tense_of(rec)), flush=True)
        if why := unheard(rec):
            print(f"  {why}", flush=True)
        owner = (rec.get("ids") or {}).get("app_id")
        if owner and "after the run" in ((rec.get("subremux") or {}).get("rescan"), (rec.get("header_repair") or {}).get("rescan")):
            rescan_item(app, owner, None)
    if missed:   # a change the apply planned did not happen
        print("--sub-time --apply did not make every change:\n  " + "\n  ".join(missed), flush=True)
        sys.exit(SUB_TIME_MISSED)
    if absent:
        sys.exit(SUB_TIME_NO_FILE)


def unheard(rec):
    """Why --sub-time checked no subtitle of the file of rec against the audio, when language detection is not
    installed or the file runs under SUB_MIN_SECONDS. Else None."""
    if not checks.lid_ready():
        return "The subtitle match and timing check skipped the file, because language detection is not installed. See README.md."
    if rec.get("file_duration", config.SUB_MIN_SECONDS) < config.SUB_MIN_SECONDS:
        return f"The subtitle match and timing check skipped the file, because it runs under {config.SUB_MIN_SECONDS // 60} minutes."
    return None


SUB_TIME_MISSED = 3   # the exit code of --sub-time --apply when a change it planned did not happen, see report.missed()
SUB_TIME_NO_FILE = 4   # the exit code of --sub-time when a path it was given holds no file


def check(argv):
    """--check runs the checks and the decision of an import on each file named by path (docs/commands.md, "Check one
    file"). Each file runs as an import of its own with no download, see backfill_file(). It runs under an import's time
    limit, with the file lock shared for the checks and exclusive for an edit. Every check the settings turn on runs.
    Then the full burned-in subtitle check and the deep analysis that an import queues run at once, see burn_later() and
    deep_later(). The run is dry unless --apply is given, and a dry run changes nothing. A re-grab only says what an
    import would do, see regrab.regrab(), so nothing deletes a file for a fault or searches. An apply edits the file as
    an import does, keeps the original of a conversion, posts the alerts, and asks Plex to analyze the file as a
    backfill does. The apps list the files, as for --sub-time. --app takes the item from that instance only, and the other
    instances are asked for the paths it does not list. A path another instance lists fails, because it would run as a
    file outside the library. The run takes the lowest CPU and disk priority, as a backfill does. A dry run writes no
    status.json, see logs.NO_STATUS.

    A path no app lists runs too, as an outside file, see process.Ctx. It gets no app write, and nothing posts without an
    app. Its conversion writes the new file beside the original. Its item comes from --app and --id, or from --tmdb, see
    outside_item(). Else it has none. The lines "not checked" name what an import would check and this run left out. A
    path with no file, a path another instance lists, a lookup that fails, an item that does not read and a run that
    ends in an error make the exit code 1. The other paths still run. --apply stops before any change when a lookup
    failed and a path is left, as --sub-time does, because the file may be that app's."""
    ap = argparse.ArgumentParser(prog="arr-media-guard --check", description="The checks and the decision of an import on each file. It is "
                                 "dry unless --apply, and it never re-grabs. See docs/commands.md.")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--app", choices=sorted(config.CFG.apps), help="take the item from this instance only. With --id, the instance of the item. "
                    "With --tmdb, the instance of the run when it takes TMDB's kind")
    named = ap.add_mutually_exclusive_group()
    named.add_argument("--id", type=int, help="the item of a path no app lists. It is a movie id for radarr and an episode id for sonarr, "
                       "and needs --app")
    named.add_argument("--tmdb", metavar="KIND/ID", help="the item of a path no app lists, from TMDB alone. KIND/ID is the end of TMDB's URL. "
                       "A film is movie/ID, as movie/603. An episode is tv/ID/SEASON/EPISODE, as tv/1396/1/3 for season 1, episode 3")
    ap.add_argument("--apply", action="store_true", help="edit the files as an import does, keep the original of a conversion, post the "
                    "alerts and ask Plex to analyze. Without it the run is dry and changes nothing")
    a = ap.parse_args(argv)
    if a.id is not None and not a.app:
        ap.error("--id needs --app")
    if a.tmdb and not re.fullmatch(r"movie/\d+|tv/\d+/\d+/\d+", a.tmdb):
        ap.error("--tmdb takes movie/ID or tv/ID/SEASON/EPISODE, as movie/603 or tv/1396/1/3")
    vars(a).update(CHECK_ARGS)
    logs.NO_STATUS = not a.apply   # main() sets it back, and records the policy of an apply
    os.nice(19)   # it hears and reads files on the NAS, so it yields to imports and to Plex
    subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False)
    checks.langs()
    given, work, lookups, unasked = [os.path.abspath(p) for p in a.paths], [], [], []
    left, failed = [p for p in given if os.path.isfile(p)], [f"no file at the path: {p}" for p in given if not os.path.isfile(p)]
    for line in failed:
        print(line, flush=True)
    for app in sorted(config.CFG.apps, key=lambda x: x != a.app):   # --app first, then the others for the paths it does not list
        try:
            found = apps.ARR[app].library(apps.arr(app, apps.ARR[app].kind), paths=left)[0] if left else []
        except FileNotFoundError as ex:   # no API key and no config.xml
            if why := apps.no_key(app, ex):   # the user set the app up in part. One the user did not set up stays silent.
                lookups.append(why)
                unasked.append(app)
                print(why, flush=True)
            continue
        except Exception as ex:   # an error, a timeout or a restart, so the app could not say what it lists
            lookups.append(config.mask(f"the {app} lookup failed: {type(ex).__name__}: {ex}")[:200])
            unasked.append(app)
            print(lookups[-1], flush=True)
            continue
        left = [p for p in left if p not in {f.get("path") for f, _ in found}]
        if a.app and app != a.app:   # with the wrong --app, the file would run as one outside the library, and convert beside itself
            for f, _ in found:
                failed.append(f"{app} lists it, so run it with --app {app}: {f['path']}")
                print(failed[-1], flush=True)
            continue
        work += [(app, f, i, None) for f, i in found]
        for f, _ in found if a.id is not None or a.tmdb else ():
            print(f"{app} lists it, so the run takes {app}'s item and not {'--id' if a.tmdb is None else '--tmdb'}: {f['path']}", flush=True)
    if (a.id is not None or a.tmdb) and len(left) > 1:
        ap.error(f"--id and --tmdb name the item of one file, and no app lists {len(left)} of the paths")
    if lookups and a.apply and left:
        sys.exit(f"--check --apply stops, and nothing changed: {'; '.join(lookups)}")
    failed += lookups
    if left:
        try:
            app, fields, info = outside_item(a)
        except Exception as ex:
            failed += [config.mask(f"the item of {p} did not read: {type(ex).__name__}: {ex}")[:300] for p in left]
            print("\n".join(failed[-len(left):]), flush=True)
            left = []
        else:   # what an import checks and this run leaves out, see NO_ITEM
            out = ([] if app else list(NO_ITEM)) + list(TMDB_LEFT.get((a.tmdb or "").partition("/")[0], ()))
    said = f"{', '.join(unasked)} could not be asked" if unasked else f"{a.app} does not list it" if a.app else "no app lists it"
    for p in left:
        print(f"{said}, so it runs {'as ' + info[0] if app else 'with no item'}: {p}", flush=True)
        work.append((app, dict(fields, path=p), (info[0] or os.path.basename(p), *info[1:]), out))
    skipped, burst = set(), {}   # plex_analyze()'s, as for --sub-time

    def done(app, info, out, took, rec, what=""):
        """The Plex analyze after a change, the decision line and the printed report of one run of a file."""
        if a.apply and info[3] and config.CFG.plex_url and process.changed(rec):   # plex_find() finds only a file Plex lists
            rec["plex"], rec["plex_reason"] = plex.plex_analyze(plex.plex_after(app, "check", rec, info[3]), skipped, burst=burst)
        if out is not None:   # a file no app lists counts in no app's audit, see logs.decision()
            rec["outside"] = True
        rec = logs.decision(rec, time.time() - took)
        print(what + report.check_report(rec, report.tense_of(rec)), flush=True)
        if rec["outcome"] == "error":
            failed.append(f'{rec["path"]}: {what}{rec["result"]}')
    for app, f, info, out in work:
        job = check_job(f, info, out is not None)
        took, rec = backfill_file(app, f, info, a, False, job=job)
        config.DEADLINE.stop()
        run = job.pop("run", None)
        rec["not_checked"] = (out or []) + check_left(rec)
        done(app, info, out, took, rec)
        if run and "tracks" in rec:   # the decision ran, so an import would queue these, see runner.run_job()
            for what, later in (("burned-in subtitle check, ", burn_later), ("deep analysis, ", deep_later)):
                if got := later(app, f, info, a, job, rec, run):
                    done(app, info, out, *got, what=what)
    if failed:
        sys.exit("--check did not check every file:\n  " + "\n  ".join(failed))


CHECK_ARGS = dict(sub_check=False, sub_time=False, recheck=False, plan_from=None, convert=False, force={})   # what backfill_file() reads of a run of check()


def check_job(f, info, outside=False):
    """The job of --check for the file record f of App.library(), with its info, see backfill_file(). outside is a file
    no app lists. A job a backfill queued runs with one too, see runner.deep_analysis()."""
    return {"check": "outside" if outside else "listed", "path": f["path"], "owner": f.get("movieId") or f.get("seriesId"), "file_id": f.get("id"),
            "episode_ids": ",".join(map(str, info[5].get("episode_ids") or ()))}


def burn_later(app, f, info, a, job, rec, run):
    """(seconds, decision record) of the full burned-in subtitle check that an import queues after its quick check, run
    now as the burn-in job runs it, see runner.burn_act(), or None when the quick check of rec asks for none. run is the
    process.Ctx of the check, so the job decides with its inputs. It runs dry and never re-grabs, as the check does."""
    b = rec.get("burned_in") or {}
    if b.get("result") not in ("flagged", "unsure"):
        return None
    started = time.time()
    r = dict(id=uuid.uuid4().hex[:12], app=app, source="check", path=rec["path"], apply=a.apply, ids=rec.get("ids") or {}, label=rec.get("label"))
    inputs = dict(label=info[0], original=info[1], kids=info[4], release=f.get("sceneName") or "", plan=process.plan_inputs(run))
    try:
        r = runner.burn_act(None, dict(app=app, path=rec["path"], inputs=inputs, quick=b["result"], grab=job), r, [], post=run.post)
    except Exception as ex:
        if isinstance(ex, subsync.Broken):   # only a run with AMG_INVARIANTS=1 raises one, and a test run must see it
            raise
        r.update(outcome="error", result=config.mask(f"error: {type(ex).__name__}: {ex}")[:300])
    return time.time() - started, r


def deep_later(app, f, info, a, job, rec, run):
    """(seconds, decision record) of the deep analysis that an import queues for its file, see runner.deep_wanted(),
    run now with the inputs of the check, as runner.deep_analysis() runs it, or None. It has no time limit. The alerts
    that the check held for it post when it does not find them again, see runner.held_after()."""
    if not runner.deep_wanted(rec):
        return None
    deep = dict(job, deep=True, path=rec["path"])
    took, r = backfill_file(app, dict(f, path=rec["path"], id=(rec.get("ids") or {}).get("file_id")), info, a, False, job=deep)
    r["deep"] = True   # its line keeps source check, so the audit tells it apart by this, see a_result()
    if r["outcome"] == "error" and getattr(deep.get("run"), "rec", None):   # what the run changed before the error, as runner.deep_analysis() keeps it
        r.update({k: deep["run"].rec[k] for k in ("subremux", "sidecars") if k in deep["run"].rec})
    if run.held:
        r["held_result"] = runner.held_after(dict(app=app, path=rec["path"], held=run.held), r)
    return took, r


# What an import checks and --check leaves out for a file no app lists, with no item, and with an item of --tmdb by its
# kind, see outside_item(). The decision line keeps them in "not_checked", with those of check_left().
NO_ITEM = ("the track flags and language tags, which need the original language of the item. Only a subtitle verdict turns a flag off",
           "wrong content, with the runtime, too-long episode and episode title checks", "the audio language alert",
           "the cut-file test of the audio check, which needs the item's runtime")
TMDB_KIDS = "the kids quality profile, which only the app knows. The kids genres and studios count"
TMDB_SEASONS = "the episode titles of the other seasons, so the episode title check compares the season's own"
TMDB_LINK = "the link to the item in the app, which an alert gives"
TMDB_LEFT = {"movie": (TMDB_KIDS, "the other release years Radarr knows, so the year check takes TMDB's year only", TMDB_LINK),
             "tv": (TMDB_KIDS, TMDB_SEASONS, "the absolute episode numbers of an anime, which only Sonarr knows", TMDB_LINK)}
NO_LID = "the language hearing and the subtitle checks against the audio, because language detection is not installed"
NOT_CONVERTED = "the other checks of an import, which need the converted file"   # an .mkv that holds another container, see process.conversion()


def check_left(rec):
    """What an import checks and the --check run of rec left out, for its "not checked" lines. These are every check
    after a conversion that did not run, the burned-in subtitle check when it could not run, an extra read of the audio
    check that the time limit left out, the content checks that the time limit or an error stopped, and the checks that
    need language detection when it is not installed."""
    b = rec.get("burned_in") or {}
    return ([NOT_CONVERTED] if "repack" in rec and "tracks" not in rec else []) \
        + ([f'the burned-in subtitle check, because {b["why"]}'] if b.get("result") == "skipped" else []) \
        + [x["error"] for x in (rec.get("audio") or {}).get("samples") or [] if not x.get("ran", True) and " skipped: " in (x.get("error") or "")] \
        + ([f'wrong content and the runtime checks, which stopped: {rec["meta_error"]}'] if rec.get("meta_error") else []) \
        + ([NO_LID] if not checks.lid_ready() else [])


def outside_item(a):
    """(app, file record fields, item as App.library() gives it) for each path of --check that no app lists. --app and
    --id give the item of that instance, a movie id of Radarr or an episode id of Sonarr. --tmdb gives the item from
    TMDB alone, see content.tmdb_item(), with --app as the app when its program takes that kind, else the program's default
    instance. The app is then never asked.
    With neither the path has no item, and the app is None. The fields name the item's id for the decision line. Every
    call only reads."""
    if a.id is not None:
        x = apps.ARR[a.app]
        if x.film:
            return a.app, {"movieId": a.id}, x.item(a.id)
        e = apps.arr(a.app, f"episode/{a.id}")
        s = e["seriesId"]
        return a.app, {"seriesId": s}, apps.episode_item(apps.arr(a.app, f"series/{s}"), [e], apps.arr(a.app, f"episode?seriesId={s}"))
    if a.tmdb:
        kind, tid, *ep = a.tmdb.split("/")
        m, eps = content.tmdb_item(kind, tid, *ep[:1], token=config.CFG.tmdb_token or None)
        prog = "radarr" if kind == "movie" else "sonarr"
        app = a.app if config.program(a.app) == prog else prog
        if kind == "movie":
            return app, {}, apps.movie_item(m)
        mine = [e for e in eps if e["episodeNumber"] == int(ep[1])]
        if not mine:
            raise LookupError(f"TMDB lists no episode {ep[1]} in season {ep[0]} of tv/{tid}")
        return app, {}, apps.episode_item(m, mine, eps)
    return None, {}, (None, None, 0, None, False, {"listed": 0})


def burn_command(argv):
    """--burn-in: the burned-in subtitle check of each file named by path, or of each file of the items --ids names in
    --app (docs/design.md, "Burned-in subtitles"). It runs the quick check, the full check, and the second pass where
    the full check asks for one, see burn_file(). It prints one line per file, with what the burn-in job after an import
    would do at the current settings. It changes no file, posts nothing, and writes one decision line per file."""
    ap = argparse.ArgumentParser(prog="arr-media-guard --burn-in", description="The burned-in subtitle check of each file, as the "
                                 "background check after an import runs it. It changes nothing. See docs/commands.md.")
    ap.add_argument("paths", nargs="*", metavar="PATH")
    ap.add_argument("--app", choices=sorted(config.CFG.apps), help="the instance whose items --ids names")
    ap.add_argument("--ids", type=int, nargs="+", default=[], help="items of --app: movie ids for radarr, series ids for sonarr")
    a = ap.parse_args(argv)
    if bool(a.app) != bool(a.ids):
        ap.error("--app and --ids go together")
    if not (a.paths or a.ids):
        ap.error("name a PATH, or --app and --ids")
    if why := process.burn_ready():
        sys.exit(f"--burn-in stops: {why}")
    os.nice(19)   # it reads whole files on the NAS, so it yields to imports and to Plex
    subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False)
    work = [(None, os.path.abspath(p), os.path.basename(p), None, None) for p in a.paths]
    if a.ids:   # the item of a file, for the one re-grab of a burn-in in its own language, see runner.BURN_ONCE
        work += [(a.app, f["path"], info[0], info[1], runner.burn_item(a.app, f.get("movieId"), info[5].get("episode_ids") or ()))
                 for f, info in apps.ARR[a.app].library(apps.app_list(a.app), set(a.ids))[0]]
    for app, path, label, original, item in work:
        started, rec = time.time(), dict(app=app, source="backfill", apply=False, label=label, path=path)
        try:
            rec.update(burn_file(path, original, item))
        except Exception as ex:
            rec.update(outcome="error", result=config.mask(f"error: {type(ex).__name__}: {ex}")[:300])
        print(f'{rec["result"]} | {label} | {path}', flush=True)
        logs.decision(rec, started)


# What the burn-in job would do, per runner.burn_step(), as --burn-in prints it
BURN_WOULD = {"log": "the job would log it only", "alert": "the job would alert", "mute": "the job would turn off the English subtitles and alert",
              "regrab": "the job would re-grab it", "regrab_off": "the job would alert, re-grabs for burned-in subtitles are off"}
BURN_SECOND = {"disagreed": "disagrees", "unsure": "could not tell", "once": "agrees, and an earlier copy was re-grabbed for it"}   # else agrees


def burn_file(path, original=None, item=None):
    """The fields of the decision line of --burn-in for path: the quick check, and the passes of the full check on the
    audio that plays first, see runner.burn_passes(). The result says what the burn-in job would do, from the job's own
    runner.burn_plan() and runner.burn_step(). original and item are the item's original language and its
    runner.burn_item() key, when --app names it."""
    ts, _, duration = process.burn_probe(path, checks.mkvmerge(path))
    audio = process.burn_audio(ts)
    if not audio:
        return {"outcome": "burned_in_skipped", "result": "skipped, the file has no audio track"}
    quick = process.burn_run(path, audio["index"], duration, ["--quick"], config.BURN_TIMEOUT)
    facts, plan = runner.burn_passes(path, audio, duration, original, item)
    step = runner.burn_step(plan, path.lower().endswith(".mkv"), bool(runner.burn_on(ts)))
    would = "BURNED_IN is off, so the job would not run" if config.CFG.burned_in == "off" else BURN_WOULD[step]
    agree = f', the second pass {BURN_SECOND.get(plan, "agrees")}' if "second" in facts else ""
    return {"outcome": f'burned_in_{facts["first"]["label"]}', "burned_in": dict(facts, quick=quick, audio=audio, plan=plan),
            "result": f'quick check {quick["result"]}, {decide.lang_name(audio["lang"]) if decide.codes(audio["lang"]) else "untagged"} audio, {runner.burn_words(facts["first"])}{agree}, {would}'}


def rescan_item(app, owner, keys):
    """A backfill's rescan of one item, with no lock: settle_extras() when conversions of the item hid extras (keys),
    else one rescan that is sent and not waited for. Logs it and prints it."""
    if keys:
        done = convert.settle_extras(app, owner, keys)
        logs.log(dict(app=app, source="backfill", ids={"app_id": owner}, result="settle", settle=done))
    else:
        done = apps.ARR[app].rescan(owner)
        logs.log(dict(app=app, source="backfill", ids={"app_id": owner}, result="rescan", rescan=done))
    print(f"rescan of {apps.ARR[app].kind} {owner}: {done}", flush=True)


def backfill_files(work, one, workers, stop, converted):
    """(work item, one(*item)) for each file of a backfill, in the order they end. One worker runs them in order in this
    thread. More run one() in threads, and submit no file once stop is set or the conversions done
    and in flight reach convert_max. A conversion backfill runs twice as many threads as the pool has slots, so a
    thread that waits for the app's import leaves its slot to another file. SIGTERM or Ctrl+C sets stop, and the
    files in flight end first: a stop never cuts a remux, a swap, an import or mkvpropedit, and every file that ran gets
    its decision line. A process that ignores SIGINT keeps it ignored, as remux.Swap does."""
    handlers = {s: signal.signal(s, lambda *_: (stop.set(), HALT.set())) for s in (signal.SIGTERM, signal.SIGINT)
                if s == signal.SIGTERM or signal.getsignal(s) != signal.SIG_IGN}
    try:
        if workers == 1:   # the file ends and writes its decision line. convert() handles a stop itself during the remux.
            for w in work:
                if stop.is_set():
                    return
                yield w, one(*w)
            return
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            left, running = iter(work), {}
            while True:
                while not stop.is_set() and len(running) < workers and converted[0] + len(running) < config.CFG.convert_max and (w := next(left, None)):
                    running[pool.submit(one, *w)] = w
                if not running:
                    return
                for fut in concurrent.futures.wait(running, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)[0]:
                    yield running.pop(fut), fut.result()
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)


def read_jsonl(*paths):
    """The JSON lines of the files that exist. A line cut short by a crash is skipped."""
    out = []
    for path in paths:
        if not os.path.exists(path): continue
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


PLAN_KEYS = ("app", "label", "path", "class", "edits", "undecided", "dropped", "cls", "orig", "tracks")   # every plan_record() key


def plan_rows(path, app):
    """The plan lines of one app. A line without a plan_record() key is skipped with a line on stdout, never a KeyError."""
    out = []
    for r in read_jsonl(path):
        if not isinstance(r, dict) or not all(k in r for k in PLAN_KEYS):
            print(f"skipped a plan line without {', '.join(k for k in PLAN_KEYS if not isinstance(r, dict) or k not in r)}: "
                  f"{str(r.get('label') if isinstance(r, dict) else r)[:100]}", flush=True)
        elif r["app"] == app:
            out.append(r)
    return out


def since_time(text):
    """"24h" means 24 hours ago. Anything else is an ISO date or time, local time when it has no zone."""
    if text.endswith("h") and text[:-1].isdigit():
        return datetime.datetime.now().astimezone() - datetime.timedelta(hours=int(text[:-1]))
    t = datetime.datetime.fromisoformat(text)
    return t if t.tzinfo else t.astimezone()


def lines_field(name, groups):
    """Embed fields from {key: [labels]}: one line per key, biggest first, with two sample labels. Cut to Discord's 1024 per field."""
    lines = [f"{len(v)} {k}" + (f" ({'; '.join(x for x in v[:2] if x)})" if any(v[:2]) else "")
             for k, v in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    fields, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > 1000:
            fields.append((name if not fields else f"{name}, more", cur)); cur = ""
        cur += ("\n" if cur else "") + line[:1000]
    return fields + ([(name if not fields else f"{name}, more", cur)] if cur else [])


RESULTS = ("hook", "check", "backfill", "backfill_job")   # the runs that decide the flags. A deep analysis and a recheck keep them.


def a_result(r):
    """Whether the decision line r decides the flags of its file, so it can be the file's latest result, see
    open_problem(). process.decision_fields() and process.conversion() set the class. --check runs the burned-in
    subtitle check and the deep analysis after its own checks, with source check. The deep analysis keeps the flags, and
    the burned-in check changes only the English subtitle flags. The class of a burn-in line and "deep" tell their lines
    apart, see runner.burn_act() and deep_later()."""
    return r.get("source") in RESULTS and "class" in r and not r.get("deep") and r["class"] not in runner.BURN_CLASSES.values()


def open_problem(what, r, result, edited_at):
    """Whether the nightly audit posts the problem what of the decision line r, see audit(). A file that is gone counts
    no more. A problem of the line counts only when the line is the file's latest result in result. A problem of the
    check after an edit counts unless a later result came after that edit. A failed conversion or a failed repair counts
    only when its alert did not go out, see process.conversion() and process.faults(). alert_result says "sent" or
    "already sent" for an alert that went out. A line without it, as of a backfill, keeps the problem."""
    path = r.get("path")
    if not os.path.exists(path or ""):
        return False
    if what in ("further", "reprobe") or what.startswith("broken:"):
        return edited_at[path] >= result.get(path, (-1,))[0]
    code = {"convert": r["outcome"], "repair": (r.get("header_repair") or {}).get("code")}.get(what)
    posted = dict(zip(r.get("alert_kinds") or (), r.get("alert_result") or ())).get({"repack_failed": "repack", "header_repair_failed": "header"}.get(code))
    return result.get(path, (None, None))[1] is r and posted not in ("sent", "already sent")


def audit(argv):
    """One summary of a dry run's plans (--plan-from) or of the open problems since a time (--since). It edits no file. --since
    also removes the kept files older than KEEP_ORIGINALS_DAYS, turns on On Grab where KEEP_REPLACED needs it, see
    regrab.grab_fix(), and queues the rechecks of a new version, see runner.queue_rechecks()."""
    ap = argparse.ArgumentParser(prog="arr-media-guard --audit", description="One summary of a dry run's plans or of the open "
                                 "problems since a time. It edits nothing. --since also removes the kept files older than KEEP_ORIGINALS_DAYS, "
                                 "turns on On Grab where KEEP_REPLACED needs it, and queues the rechecks of a new version.")
    ap.add_argument("app", choices=sorted(config.CFG.apps)); g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan-from", metavar="FILE", help="group the plans of a dry run by policy path and rule, and check the "
                   "invariants again")
    g.add_argument("--since", metavar="24h|DATE", help="read the decision log from this time on, 24h or an ISO date")
    ap.add_argument("--source", choices=["hook", "backfill", "check"], help="only the results of the hook, of a backfill or of --check")
    ap.add_argument("--post", action="store_true", help="post the summary to DISCORD_WEBHOOK, when there is anything to report")
    a = ap.parse_args(argv); app = a.app
    os.nice(19)
    subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False)
    if a.plan_from:
        classes, undecided, dropped, broken, skipped, repacked, headers = {}, {}, {}, {}, {}, {}, {}
        add = lambda d, k, label: d.setdefault(k, []).append(label)
        rows = plan_rows(a.plan_from, app)
        for r in rows:
            if r.get("header_repair") == "would_repair_header":   # beside its flag plan, so not in the chain below
                add(headers, "would repair header", r["label"])
            if r["undecided"]: add(undecided, r["undecided"], r["label"])
            elif r["dropped"]: add(dropped, "; ".join(r["dropped"]), r["label"])
            elif r.get("skipped"): add(skipped, f'{logs.SKIPPED.get(r.get("outcome"), r.get("outcome", "other"))} ({r["skipped"]})', r["label"])   # by reason, not by file
            elif r.get("repack"): add(repacked, r["repack"], r["label"])
            elif r["edits"]:
                add(classes, r["class"], r["label"])
                for _, x in decide.invariants(r["tracks"], r["edits"], r["cls"], set(r["orig"])): add(broken, x, r["label"])
        title = f"Plan audit: {logs.app_name(app)}"
        text = (f"{sum(map(len, classes.values()))} of {len(rows)} files would change, in {len(classes)} classes. "
                f"{sum(map(len, undecided.values()))} undecided, {sum(map(len, dropped.values()))} with a planned track change not made")
        worth = bool(undecided or dropped or skipped or broken)   # a plan that only changes files posts nothing
        n_broken, n_skipped, n_repacked, n_headers = (sum(map(len, x.values())) for x in (broken, skipped, repacked, headers))
        text += f", {n_broken} with a track problem after the planned change."
        text += f" {n_repacked} would be repacked into Matroska." if n_repacked else ""
        text += f" {n_headers} would get a new header from a remux." if n_headers else ""
        text += f" {n_skipped} skipped: the container is not Matroska, or a header repair failed or was skipped." if n_skipped else ""
        fields = (lines_field("Classes", classes) + lines_field("Undecided", undecided) + lines_field("Dropped", dropped)
                  + lines_field("Track problem after the change", broken) + lines_field("Repacked", repacked)
                  + lines_field("Header repaired", headers) + lines_field("Skipped", skipped))
    else:
        # seen: (a problem code, its decision line), see logs.audit_problem(). result: per path the place and line of its
        # latest result, and edited_at the place of its last edit, see open_problem().
        start, last, day, seen, result, edited_at = since_time(a.since), {}, [], [], {}, {}
        for n, (raw,) in enumerate(store.read("SELECT rec FROM decisions WHERE app = ? AND at >= ? ORDER BY rowid", app, start.timestamp())):
            r = json.loads(raw)   # a decision line of this app and window, as logs.decision() keeps it
            day.append(r)   # every source, for the TMDB status
            if (a.source and r.get("source") != a.source) or not (r.get("apply") or r.get("source") == "hook"):
                continue   # from the hook or an apply only. Dry runs change nothing.
            if a_result(r):
                result[r.get("path")] = n, r
            if (r.get("header_repair") or {}).get("code") in ("header_repair_failed", "header_repair_skipped"):
                seen.append(("repair", r))
            if r["outcome"] in ("undecided", "dropped"):
                seen.append((r["outcome"], r))
            elif r["outcome"] == "not_matroska" or r["outcome"].startswith("repack_"):   # a repack that failed or was skipped
                seen.append(("convert", r))
            elif "edited" in (r["outcome"], r.get("edit_result")):   # an edit before a wrong-content verdict too
                last[r["path"]], edited_at[r["path"]] = r, n
        for path, r in sorted(last.items()):   # the state each edited file was left in, from the check right after the edit
            check = r.get("recheck")
            if not check or "error" in check:   # a line without the check: probe the file now, and log that as an audit decision
                if not os.path.exists(path): continue
                started = time.time()
                try:
                    with runner.locked():
                        heard = {k: v["lang"] for k, v in (r.get("heard") or {}).items() if v.get("lang")}
                        d = decide.decide(checks.mkvmerge(path), r.get("original"), r.get("kids", False), r.get("release") or "", heard)
                        if runner.muted(path):   # a burn-in job turned off the English subtitles, and they stay off
                            d = process.muted_plan(d)
                except Exception:
                    seen.append(("reprobe", r)); continue
                check = {"edits": len(d["edits"]), "invariants": [c for c, _ in decide.invariants(d["tracks"], [], d["cls"], set(d["orig"]))]}
                logs.decision(dict(id=uuid.uuid4().hex[:12], app=app, source="audit", apply=False, ids=r.get("ids", {}), label=r.get("label"),
                              path=path, result="no change" if not d["edits"] else "dry run", outcome="dry_run" if d["edits"] else "no_change",
                              edits=d["edits"], reasons=d["reasons"],
                              tracks=logs.track_log(d["tracks"]), recheck=check, **{"class": decide.plan_class(d)}), started)
            if check["edits"]:
                seen.append(("further", r))
            seen += [(f"broken:{code}", r) for code in check.get("invariants", [])]
        tmdb = content.tmdb_day_status(day)
        # The nightly run drops the originals a repack kept, and the grab links, older than keep_days. At 0 it drops every
        # grab link and leaves the originals.
        say = lambda kind, text: print(f"arr-media-guard: {app} {kind}: {text}" if config.SERVE else text)   # as serve.note() writes a line
        with contextlib.suppress(Exception):   # an API that fails here fails the prune below too, and that warns
            regrab.grab_fix(app, "audit")
        roots = vault.prune_roots(config.CFG.keep_days > 0)   # a folder below a root folder, as on a mount per show or a fallback, too
        try:
            roots |= {f(os.path.join(r["path"], "x")) for r in apps.arr(app, "rootfolder") for f in ((vault.originals_root, vault.replaced_root) if config.CFG.keep_days else (vault.replaced_root,))}
        except FileNotFoundError as ex:   # api_key() found no key and no config.xml
            say("warning", apps.no_key(app, ex) or f"{app.capitalize()} is not set up, so the audit prunes no kept folder under its root folders.")
        except Exception as ex:
            say("warning", config.mask(f"kept folders under the root folders not pruned: {type(ex).__name__}: {ex}")[:200])
        for root in sorted(roots):
            gone = vault.prune_originals(root)
            if gone: say("audit", f"removed {len(gone)} kept folders older than {config.CFG.keep_days} days under {root}")
        with contextlib.suppress(sqlite3.Error):   # a busy store shrinks the next night
            store.shrink()
        runner.queue_rechecks()   # on a host the first nightly audit of a new version queues its rechecks
        # one summary line a night, so Loki sees each host even on a day without imports. It counts a file once per decision line.
        k = collections.Counter(what.partition(":")[0] for what, _ in seen)
        logs.to_syslog(report.logfmt([("arr", app), ("source", "audit"), ("outcome", "summary"), ("edited", len(last)),
                          ("further", k["further"] + k["reprobe"]), ("undecided", k["undecided"]), ("dropped", k["dropped"]),
                          ("broken", k["broken"]), ("tmdb", tmdb)]))
        files = logs.audit_files([(what, r) for what, r in seen if open_problem(what, r, result, edited_at)])
        title = f"Open problems: {len(files)} file{'' if len(files) == 1 else 's'} · {logs.app_name(app)}"
        text = f"{len(files)} file{'' if len(files) == 1 else 's'} with an open problem since {start.isoformat(timespec='minutes')}." + \
            "".join(f"\n{label}: {problems}" for label, problems, _ in files)
        fields = [("TMDB", tmdb)]   # the day's metadata checks: ok, unavailable n times, key broken, or no checks
        worth = bool(files)   # the post goes out only for an open problem, the syslog line every night
    if not config.SERVE:   # the container log takes the summary line above, one line per event
        print(title); print(text)
        for name, value in fields: print(f"\n{name}:\n{value}")
    if a.post and worth:
        print(f"arr-media-guard: {app} audit post:", logs.post(app, logs.embed(app, title, text, "amber", fields[:24] + [("App", logs.app_name(app))])
                                                              if a.plan_from else logs.audit_embed(app, title, files, tmdb)))


def library(app, ids):
    """Every file of the local app as (file id, path, label, original language, listed runtime, kids), in file id order."""
    a = apps.ARR[app]   # the movie_file() of a movie outside ids is never read, as its join can fail
    return sorted((f["id"], f["path"], a.scan_label(f, i), i[1], i[2], i[4]) for f, i in a.library([x for x in apps.app_list(app) if not ids or x["id"] in ids])[0])


def scan_file(kind, app, w, stop):
    """One file of scan(), in a worker thread: the pause, then the check under the shared file lock. It writes nothing.
    Returns (rec, certain, doubts, detail, started), or None when the scan stopped during the pause."""
    time.sleep(config.SCAN_PACE)
    if stop.is_set():
        return None
    fid, path, label, original, runtime, kids = w
    started = time.time()
    rec = dict(id=uuid.uuid4().hex[:12], app=app, source=f"{kind}_scan", apply=False, ids={"file_id": fid}, label=label, path=path,
               original=original, kids=kids)
    d = None
    try:
        with runner.locked(shared=True):
            if kind == "video":
                dur, hp = checks.video_inputs(path)
                certain, doubts, detail = checks.check_video(path, dur, hp=hp)
            else:
                j = checks.mkvmerge(path)
                d = decide.decide(j, original, kids, "") if path.lower().endswith(".mkv") else None
                certain, doubts, detail = checks.check_audio(path, j, d["edits"] if d else [], runtime)
        if d:
            rec.update(item_class=d["cls"], reasons=d["reasons"], tracks=logs.track_log(d["tracks"]), **{"class": decide.plan_class(d)})
    except Exception as ex:
        certain, doubts = None, [config.mask(f"the check failed: {type(ex).__name__}: {ex}")[:200]]
        detail = [] if kind == "audio" else {}   # the samples or the video stages, for the list line
    if not os.path.exists(path) and os.path.isdir(os.path.dirname(os.path.dirname(path))):   # the app replaced or deleted it meanwhile.
        rec["gone"] = True                                                                     # A lost mount keeps its list line.
    return rec, certain, doubts, detail, started


def pass_left(name, work, fid, new=False, ask=None):
    """(the state of the pass name, a scan's or a queue's, the items of work left in it in file id order, a deque of the
    ids above state["last"] for advance()). An item is left when its id fid(item) is above state["last"] and not in
    state["done"]. new, no state, or another ask, the ids and paths of the run, starts a new pass."""
    state = store.get("scan", name, {})
    if new or not state or state.get("ask") != ask:
        state = {"started": time.time(), "last": -1, "checked": 0, "done": [], "ask": ask}
    done = set(state.setdefault("done", []))   # a state of a version before done holds none
    todo = sorted((w for w in work if fid(w) > state["last"]), key=fid)
    return state, [w for w in todo if fid(w) not in done], collections.deque(map(fid, todo))


def advance(state, order, fid):
    """Count fid as checked in a scan state. order holds the pass's file ids above state["last"], in order, and last
    moves over every one of them that is checked. The ids checked above last wait in state["done"]. So a restart
    skips only checked files, even when the workers finish out of order and a stop drops the files in flight."""
    done = set(state["done"]) | {fid}
    while order and order[0] in done:
        state["last"] = order.popleft()
    state.update(done=sorted(i for i in done if i > state["last"]), checked=state["checked"] + 1)


def kill_children():
    """SIGKILL every child of this process: the ffmpeg, ffprobe and mkvmerge runs of the scan workers."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid == os.getpid():
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError, IndexError):   # gone meanwhile
            pass


def scan_class(r):
    """The class of a scan list line: BROKEN, CHECK, or HEADER for a header issue with no sign of damage. A HEADER line
    says whether a remux would repair it, which the hook and backfill --apply do."""
    return "BROKEN" if r["certain"] else "CHECK" if r["doubts"] else "HEADER"


def scan_text(r):
    if r["certain"] or r["doubts"]:
        return r["certain"] or "; ".join(r["doubts"])
    h = (r.get("video") or {}).get("header") or {}
    return "; ".join(r["header"]) + (". A remux repairs it" if h.get("repairable") else ". Not repairable: " + "; ".join(h.get("blocked") or []))


def scan(app, a, kind):
    """Scan the library for broken audio or corrupt video (kind), a.workers files at a time. Resumable and read-only. A
    run that finds a problem posts one Discord summary. A clean run prints its summary only. The video scan has no time
    limit, so every stage runs, and it reads the duration from ffprobe. The main thread writes every line and the state,
    so no two writes interleave. SIGTERM or Ctrl+C starts no new file, drops the files in flight, kills their processes
    and still ends with the summary. A restart checks the dropped files again."""
    # An --ids run keeps its own state and list, so it never moves the full pass or truncates its list. The store holds
    # where the pass stands and the problems it found. base.txt lists them for a person.
    name = f"{kind}-scan-{app}" + ("-ids" if a.ids else "")
    base, work = os.path.join(config.CFG.state_dir, name), library(app, set(a.ids))
    state, todo, order = pass_left(name, work, lambda w: w[0], a.restart or a.ids)
    if not todo:   # the pass is done, so a new one starts
        state, todo, order = pass_left(name, work, lambda w: w[0], True)
    if not state["checked"]:   # a new pass starts a new list
        store.drop(name)
    print(f"{kind} scan of {app}: {len(todo)} of {len(work)} files left in this pass, {a.workers} workers", flush=True)
    checked = found = gone = 0
    stop, left, running = threading.Event(), iter(todo[:a.limit] if a.limit else todo), {}
    handlers = {s: signal.signal(s, lambda *_: (stop.set(), HALT.set())) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
            while True:
                while not stop.is_set() and len(running) < a.workers and (w := next(left, None)):
                    running[pool.submit(scan_file, kind, app, w, stop)] = w
                if stop.is_set() or not running:
                    break
                for fut in concurrent.futures.wait(running, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)[0]:
                    (fid, path, label, *_), out = running.pop(fut), fut.result()
                    if out is None or stop.is_set():   # a stop may have cut its ffmpeg short, so it is checked again
                        continue
                    rec, certain, doubts, detail, started = out
                    if rec.get("gone"):   # counted, with no decision and no list line
                        gone += 1
                        print(f"GONE    {label}", flush=True)
                        advance(state, order, fid)
                        store.put("scan", name, state)
                        continue
                    summary = logs.audio_summary(certain, doubts, detail) if kind == "audio" else checks.video_summary(certain, doubts, detail)
                    logs.decision(dict(rec, result=f"{report.FAULTS[kind][0]}: {certain}" if certain else report.FAULTS[kind][1],
                                  outcome=regrab.RESULT_CODES[kind][0] if certain else regrab.RESULT_CODES[kind][1], **{kind: summary}), started)
                    checked += 1
                    header = (detail.get("header") or {}).get("issue") if kind == "video" else None   # listed, never repaired
                    if certain or doubts or header:
                        found += 1
                        line = dict(file_id=fid, label=label, path=path, certain=certain, doubts=doubts, header=header,
                                    **{"samples" if kind == "audio" else "video": detail})
                        store.put(name, str(fid), line)
                        print(f'{scan_class(line):<7} {label} | {scan_text(line)}', flush=True)
                    advance(state, order, fid)
                    store.put("scan", name, state)
            while not all(f.done() for f in running):   # a stop: end the files in flight
                kill_children()
                concurrent.futures.wait(running, timeout=0.2)
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
    rows = sorted(store.items(name).values(), key=lambda r: r["file_id"])
    with open(base + ".txt", "w") as f:
        f.writelines(f'{scan_class(r)}\t{r["label"]}\t{scan_text(r)}\t{r["path"]}\n' for r in rows)
    text = f"{checked} files checked this run, {state['checked']} of {len(work)} in this pass." + (" The run was stopped." if stop.is_set() else "") \
        + (f" Files that left the library during their check: {gone}." if gone else "")
    emb = logs.embed(app, f"{kind.capitalize()} scan: {logs.app_name(app)}", text, "amber",
                [("Problems this run", str(found)), ("Problems in this pass", str(len(rows))), ("List", f"{base}.txt"), ("App", logs.app_name(app))])
    print(f"{text} {found} with a problem this run, {len(rows)} in this pass. The list is in {base}.txt.", logs.post(app, emb) if found else "")


HELP = """arr-media-guard: set the default audio and subtitle tracks of an imported mkv, and flag broken files.

  arr-media-guard                     run by Sonarr or Radarr as a Custom Script connection
  arr-media-guard --backfill [<instance>] [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--restart]   queue each file for the checks of an import
  arr-media-guard --backfill [<instance>] --plan-out FILE [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--workers N]
  arr-media-guard --backfill <instance> --sub-check [--apply] [--ids ID ...] [--paths PATH ...]
  arr-media-guard --backfill [<instance>] --apply --plan-from FILE [--canary N]
  arr-media-guard --backfill [<instance>] --plan-from FILE --only-undecided [--apply] [--plan-out FILE]
  arr-media-guard --backfill <instance> --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]
  arr-media-guard --backfill <instance> --convert --apply --force-convert PATH [PATH ...] [--ids ID ...]
  arr-media-guard --backfill [<instance>] --drop-queued   drop the jobs a backfill queued
  arr-media-guard --plex-flush <instance>
  arr-media-guard --backfill <instance> --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]
  arr-media-guard --backfill <instance> --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]
  arr-media-guard --audit <instance> (--plan-from FILE | --since 24h|DATE) [--source hook|backfill|check] [--post]
  arr-media-guard --sub-time PATH [PATH ...] [--apply]
  arr-media-guard --check PATH [PATH ...] [--app <instance>] [--id ID | --tmdb KIND/ID] [--apply]
  arr-media-guard --burn-in [PATH ...] [--app <instance> --ids ID ...]   the burned-in subtitle check, report only
  arr-media-guard --serve             the Webhook listener for Sonarr and Radarr in Docker
  arr-media-guard --selftest
  arr-media-guard --test-discord      post a test message to DISCORD_WEBHOOK and print Discord's answer
  arr-media-guard-subhunt <radarr instance> --ids ID [ID ...] [--apply] [--force]   the subtitle hunter, a command of its own

<instance> is radarr, sonarr or a name in APP_INSTANCES, as sonarr-4k. A backfill or a scan with no instance runs every
instance. With no pass flag (--convert, --sub-check, --check-audio, --check-video) and no plan option (--plan-out,
--plan-from), --apply queues each file the app lists. The worker runs each one as --check --apply, after the imports.
--backfill, --audit, --sub-time, --check and --burn-in print their options with --help, as in arr-media-guard --backfill --help.
README.md says how to install and connect it. docs/commands.md explains each mode, and docs/design.md how it works."""


def main(argv):
    mode = argv[:1]
    # A deploy tool may run --selftest on every dry run, which must change nothing on the host. So the selftest records
    # the policy only with ARR_MEDIA_GUARD_RECORD=1. Its record never moves last_hook_run.
    # A dry run of --check, or of a backfill that queues, writes no status.json, see check().
    armed = mode != ["--selftest"] or os.environ.get("ARR_MEDIA_GUARD_RECORD") == "1"
    a = backfill_args(argv[1:]) if mode == ["--backfill"] else None
    dry = mode == ["--check"] and "--apply" not in argv or a and a.queue and not a.apply
    if mode in (["--selftest"], ["--backfill"], ["--audit"], ["--sub-time"], ["--check"]) and armed and not dry:
        logs.status("policy", "failed" if decide.POLICY is None else "ok", config.POLICY_ERROR or "", touch=mode != ["--selftest"])
    if mode == ["--selftest"]:
        print(f"keys from the environment: {', '.join(config.CFG.from_env) or 'none'}")   # the names only, as a value may be a secret
        if os.path.exists(store.path()):   # a selftest creates no store
            with contextlib.suppress(sqlite3.Error):
                print("background queue: {} jobs, {} of them queued by a backfill".format(*runner.backfill_jobs()))
        if config.CFG.errors:
            sys.exit("selftest failed: " + " ".join(config.CFG.errors))
        if decide.POLICY is None:
            sys.exit(f"selftest failed: {config.policy_help()}")
        for app in config.CFG.apps:   # a warning only: an app this host does not run has no key here
            try:
                apps.api_key(app)
            except FileNotFoundError as ex:   # no API key and no config.xml: an app with a URL of its own warns, as the start check does
                if why := apps.no_key(app, ex):
                    print(f"warning: {why}")
                continue
            except (OSError, AttributeError):
                continue
            for w in runner.app_check(app)[1]:   # path_warnings() below names an API or a root folder that fails it
                print(f"warning: {w}")
        results = runner.service_checks()
        for _, why in results:   # a warning only, as for an app
            if why:
                print(f"warning: {why}")
        for w in apps.path_warnings(plex_warned=bool(dict(results).get("plex"))):
            print(f"warning: {w}")
        print("selftest ok")
    elif mode == ["--test-discord"]:
        ok, line = logs.discord_test()
        if not ok:
            sys.exit(line)
        print(line)
    elif mode == ["--subhunt"]:
        print("arr-media-guard: the subtitle hunter is a command of its own. Run arr-media-guard-subhunt with the same arguments.",
              file=sys.stderr)
        sys.exit(2)
    elif mode in (["--backfill"], ["--audit"], ["--sub-time"], ["--check"]) and decide.POLICY is None and not {"-h", "--help"} & set(argv):
        sys.exit(config.policy_help())
    elif mode == ["--backfill"]:
        backfill(a)
    elif mode == ["--audit"]:
        audit(argv[1:])
    elif mode == ["--plex-flush"]:
        plex.plex_flush(argv[1:])
    elif mode == ["--sub-time"]:
        sub_time(argv[1:])
    elif mode == ["--check"]:
        try:
            check(argv[1:])
        finally:
            logs.NO_STATUS = False
    elif mode == ["--burn-in"]:
        burn_command(argv[1:])
    elif mode == ["--serve"]:
        from . import serve   # only this mode needs it
        serve.main(argv[1:])
    elif argv in (["-h"], ["--help"]):
        print(HELP)
    elif argv:
        print(HELP); sys.exit(2)
    else:
        try:
            runner.hook()
        except Exception as ex:   # hook mode never fails the import
            if isinstance(ex, subsync.Broken):   # only a run with AMG_INVARIANTS=1 raises one, and a test run must see it
                raise
