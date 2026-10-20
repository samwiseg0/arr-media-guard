# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The checks and the one write of one file, process(), with the metadata checks it runs."""
import contextlib, dataclasses, fcntl, os, sqlite3, statistics, subprocess, time, uuid

from . import apps, checks, cli, config, content, convert, decide, logs, proof, regrab, remux, report, runner, store, subsync, subtitles, vault


def metadata(app, path, j, size, d, original, release, item, fresh=False, asked=None, nfo=None):
    """The checks of content.py on one probed file against its item, item from movie_item() or episode_item(). Returns
    {"trusted", "expected", "other", "evidence", "tmdb"}. A TMDB failure reads as unknown. TMDB's status goes to
    status.json only after a live answer, because a cache hit says nothing about the key. fresh is a second check
    before a re-grab, see tmdb_ask(). asked is the tmdb_ask() the decision already made, so TMDB is asked once. nfo is
    the episode title of the release's NFO, see release_nfo_title(). It wins over the title in the release name."""
    trusted = content.trusted_duration(j, size, content.last_packet(path, probe=j))
    token, ids = config.CFG.tmdb_token or None, item.get("ids") or {}
    cache = "tmdb-recheck" if fresh else "tmdb"
    expected, t0 = asked or checks.tmdb_ask(app, item, fresh)
    code, why = content.tmdb_state(expected)
    if code not in ("found", "no_record") or content.DOWN.get("answered", 0) >= t0:
        logs.status("tmdb", code, why)
    kind = "episode" if config.program(app) == "sonarr" else "special" if (expected or {}).get("special") else "movie"
    name = release or os.path.basename(path)
    args = (d["tracks"], original, expected, trusted, item.get("listed") or 0, name, kind, item.get("year"), item.get("titles") or ())
    ev, other = content.wrong_content_evidence(*args, episode=episode_title(app, item, nfo, release, path) if kind == "episode" else None,
                                               season=item.get("season")), None
    if kind != "episode" and ev["points"] and trusted.get("seconds"):   # another film matters only for a movie with a point already
        other = content.other_film(name, ids.get("tmdb"), trusted["seconds"], token=token, cache=cache)
        if other:
            ev = content.wrong_content_evidence(*args, other)
    return {"trusted": trusted, "expected": expected, "other": other, "evidence": ev, "tmdb": ev["tmdb"]}


def episode_title(app, item, nfo, release, path):
    """content.episode_title_verdict() of an episode file, or None when neither the NFO, the release name nor the
    file name gives a title. Only then does it read the series' episodes, unless item holds them, as in a backfill. A
    failed read is verdict "unknown", so the other checks still run. A file with no scene name has Sonarr's own name,
    and the alert says so, because Sonarr wrote that name in the order it had at the import."""
    name = release or os.path.basename(path)
    said, title = ("the release's NFO", nfo) if nfo else \
        ("the release name" if release else "the file name", content.release_episode_title(name))
    if not title or not item.get("series_id"):
        return None
    try:
        eps = item.get("episodes") or apps.arr(app, f"episode?seriesId={item['series_id']}")
    except Exception as ex:
        return {"kind": "episode_title", "verdict": "unknown", "points": 0, "why": config.mask(f"the episode list did not read: {type(ex).__name__}: {ex}")[:200]}
    return content.episode_title_verdict(title, eps, item.get("episode_ids") or (), [t for t, _ in item.get("titles") or ()], said,
                                          item.get("anime"))


NFO_MAX = 64 << 10   # bytes of a scene NFO that nfo_title() reads


def release_nfo_title(source, release=""):
    """The episode title of the scene NFO beside source, the file Sonarr imported from, or None. The NFO counts when its
    name is the video's, or when it is the one NFO in a folder named like the video or the release. A download folder
    that holds the NFOs of other releases gives none. The hook reads it at the event, because a usenet download folder
    can be gone when the worker runs. Any error gives None, so the import never fails on it."""
    try:
        folder, name = os.path.split(source)
        stem, nfos = os.path.splitext(name)[0].lower(), [n for n in os.listdir(folder) if n.lower().endswith(".nfo")]
        own = [n for n in nfos if n[:-4].lower() == stem] or \
            (nfos if len(nfos) == 1 and os.path.basename(folder).lower() in (stem, release.lower()) else [])
        return nfo_title(os.path.join(folder, own[0])) if own else None
    except Exception:
        return None


def library_nfo_title(path):
    """The episode title of the release NFO that Sonarr copied beside the video at path, or None. Sonarr 4 names it
    <name>.nfo, see ExtraFileManager.ImportFile(). Before a metadata writer such as Kodi writes its own <name>.nfo,
    Sonarr renames the release NFO to <name>.nfo-orig, see OtherExtraFileRenamer. So a Kodi .nfo, by Sonarr.metadata(),
    gives the .nfo-orig. It reads as release_nfo_title() does. Any error gives None."""
    nfo = os.path.splitext(path)[0] + ".nfo"
    try:
        return nfo_title(nfo if os.path.isfile(nfo) and not apps.ARR["sonarr"].metadata(nfo) else nfo + "-orig")
    except Exception:
        return None


def nfo_title(path):
    """content.nfo_episode_title() of the first NFO_MAX bytes of the NFO at path, or None when it is no plain file."""
    if not os.path.isfile(path):   # open() of a FIFO would block the app's import
        return None
    with open(path, "rb") as f:
        raw = f.read(NFO_MAX)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:   # a scene NFO is code page 437
        text = raw.decode("cp437")
    return content.nfo_episode_title(text)


def burn_ready():
    """Why the burned-in subtitle check cannot run, or None. It runs burnin.py in the venv of language detection, so it
    needs that install, see checks.lid_ready(), and the models burnin.ready() checks. A host install without
    burnin.py has no check."""
    if not checks.lid_ready():
        return "language detection is not installed, and the burned-in subtitle check runs in its venv"
    try:
        from . import burnin
        ok, why = burnin.ready(os.path.join(config.CFG.lid_dir, "models"))
    except ImportError:
        return "the burned-in subtitle check is not installed"
    except Exception as ex:   # the start check and an import go on without the check
        return config.mask(f"its text models did not read: {type(ex).__name__}: {ex}")[:200]
    return None if ok else why or "its text models did not pass their check"


def burn_probe(path, j):
    """(the tracks of decide.classify(), whether a video track is there, the duration) of path for the burned-in
    subtitle check, from j, its mkvmerge -J probe. mkvmerge reads no track of an ASF or WMV file and no duration of an
    AVI file, so ffprobe then gives what j lacks, see proof.ff_streams(). That run ends at the job's time limit, see
    checks.run_bounded(). A failed ffprobe gives no track and duration 0, and the check then finds nothing."""
    video = lambda tracks: any(t.get("type") == "video" for t in tracks)
    if j.get("tracks") and decide.duration(j):
        return decide.classify(j), video(j["tracks"]), decide.duration(j)
    try:
        _, streams, duration = proof.ff_streams(path)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        streams, duration = [], 0.0
    if j.get("tracks"):
        return decide.classify(j), video(j["tracks"]), duration
    return (decide.classify({"streams": streams}), any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                                                       for s in streams), duration)


def burn_run(path, index, duration, flags, timeout, queue=None):
    """The answer of burnin.py on ffmpeg audio stream index of path, in the venv of language detection, see
    checks.lid_cli(): burnin.quick() with --quick, burnin.full() with --full. duration is burn_probe()'s. The run and
    its ffmpeg end after timeout seconds. A full check yields when an import waits in the queue of the store at queue.
    It takes no turn at the language model, so no hearing makes it yield. Raises RuntimeError with the reason when the
    run gives no answer."""
    got = checks.lid_cli(path, index, {"format": {"duration": duration}}, (), timeout, False, yield_to=(None, queue), burn=flags)
    if "error" in got or "why" in got:
        raise RuntimeError(got.get("error") or got["why"])
    return got


def tmdb_key_alert(app, code):
    """The ops embed "TMDB key not working", at most once a day per host, or None."""
    k = content.key_alert(code, token=config.CFG.tmdb_token, mark=report.bold)
    return logs.post(app, logs.embed(app, k[0], k[1], "amber", [("App", logs.app_name(app))])) if k else None


MODES = ("import", "backfill", "sub_check", "sub_time", "convert", "deep")   # the runs of process(), see Ctx


@dataclasses.dataclass
class Ctx:
    """One run of process() on one file: its inputs, and the state its steps hand on.

    mode names the run, one of MODES. import is a hook job, backfill the flag backfill, sub_check and convert a backfill
    with --sub-check or --convert, sub_time is --sub-time, and deep the deep analysis of an import. recheck makes the
    run a recheck job of the background queue, see runner.queue_rechecks(). It names the run of the saved result the
    recheck repeats, import, sub_check, sub_time or deep. depth is the run whose subtitle checks this run makes, see
    subtitles.SUB_RUNS. It is recheck when set, else mode. So a recheck never checks deeper than the result it repeats. A
    recheck of an import runs as sub_check at the import's depth. background is a deep analysis or a recheck: it checks
    only the subtitles, keeps the flags, and stops for an import that waits. Its subtitle check follows SUBTITLES, see
    subtitles.sub_fixes(). An import re-grabs
    a file with a certain audio or video fault in place of the edit, and so does a conversion that shows the original
    damaged. A kind REGRAB does not list only says it would re-grab. Wrong content re-grabs after the edit, or only
    says it would. A file that is not .mkv converts in an import at CONVERT and in a convert run. A .mkv file that holds
    another container converts in every run.

    lock is the held file lock. It is released after the write, because the metadata checks only read, and taken again
    before a wrong-content re-grab deletes anything. shared means lock is held shared, in a job process of coordinate()
    or a backfill worker, and the checks run under it. shared.exclusive(st) then takes the lock exclusive before a
    re-grab or the write, see write(), and raises Replan when the file or its download changed meanwhile, see
    exclusive().
    shared.settle() tells younger jobs of the download that this one re-grabs nothing more. A conversion of an import
    builds the new file under the shared lock and takes the lock exclusive for its swap, see convert(). Everything after
    it runs under the exclusive lock. A header repair that the checks need first raises Replan at once, so it and
    everything after it run under the exclusive lock, see header(). The language hearing runs without the lock, and shared.reshare(st) takes it shared again after
    it, see reshared().

    The steps set the state they hand on, such as rec, the decision log record, and the probe j, st and size, see
    refresh(). path follows a conversion's new name.

    check is the "check" of job in a --check run, see cli.check(). That run is an import of one file with no download,
    with source check. Its re-grabs only say what an import would do, in a dry run too, see regrab.regrab(). Its
    conversion keeps the original, see convert.convert(). "listed" is a file an app lists. "outside" is one no app
    lists. It gets no rescan, and its conversion writes the new file beside the original, because no app knows the file.
    With no app it has no item either, so the content checks and the language alert are left out, and act() keeps the
    flags. A deep analysis of --check runs in the foreground, see cli.check(), so it never yields to an import, and
    yields is False. A job a backfill queued has check and "backfill" in job, see runner.deep_analysis(). Its source
    is backfill_job, so its conversion keeps no original, as an import's does. waits is True for it, so it stops for an
    import that waits before a conversion and before a remux, until a step replaced the file, see refresh()."""
    app: str; path: str; label: str; original: str; runtime: object   # the file, and its item's original language and runtime
    mode: str = "import"; job: dict = None   # job is the queued hook job of an import
    save: object = None   # stores job in the queue, see runner.run_job()
    apply: bool = True; post: bool = True   # post sends the alerts
    audio: bool = True; video: bool = True   # False skips a check the file already had with its download
    kids: bool = False; release: str = ""   # the item context of the decision
    ids: dict = None; item: dict = None   # the app's ids of the item, and the metadata context of movie_item() or episode_item()
    keep_plan: bool = False   # adds the plan_record() as "plan"
    lock: object = None; shared: object = False
    pool: object = None   # the conversion pool of a backfill with several workers, see backfill_files(). It converts under the shared lock.
    force: str = None   # the refusal a person accepts, see convert()
    recheck: str = None
    held: list = None   # the alerts an import holds for the deep analysis of its file, see alerts()

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"no mode {self.mode}")
        self.check = (self.job or {}).get("check")
        queued = bool((self.job or {}).get("backfill"))   # a job a backfill queued, see runner.deep_analysis()
        self.source = "recheck" if self.recheck else "backfill_job" if queued else "check" if self.check else \
            {"import": "hook", "deep": "deep_analysis"}.get(self.mode, "backfill")   # of the decision log
        self.background = self.mode == "deep" or bool(self.recheck)
        self.yields = self.background and not self.check   # it stops for an import that waits, see runner.deep_waits()
        self.waits = self.yields or queued   # it stops for an import before a conversion and before a remux
        self.depth = self.recheck or self.mode

    def rearm(self):
        """Start the job's time limit again after a step turned it off. Only an import has one."""
        if self.mode == "import":
            config.DEADLINE.start(config.BUDGET)

def process(ctx):
    """Probe one file and decide its flags, see Ctx. Hear the audio language when the decision needs it, check the
    subtitles, check the audio and the video (import only), then write the file once with the flag edits and any remux
    the checks planned, see write(), and verify. Then run the metadata checks and alert. A step that ends the run returns True. Returns the decision log record, which the
    caller writes with decision(). Once a stop is asked, see runner.stop_asked(), no further step runs, see stop_line()."""
    for i, step in enumerate(STEPS):
        if i and runner.stop_asked():
            stop_line(ctx, STEPS[i:])
            break
        if step(ctx):
            break
    return ctx.rec


def stop_line(ctx, left):
    """The record of a run that a stop ended before the steps left, so its decision line comes at once. "not_run" names
    those steps. A run that stopped before its decision gets the outcome stopped, with the reason codes of what it
    wrote, so the Plex analyze of a changed file still runs. A job goes back to the queue, see runner.job_process(),
    and its next run makes the rest. A backfill ends after the file.

    No later run finds a re-grab that acted, or the subtitles a conversion took out. So the findings keep ctx.first and
    the unreadable notice, and they post, with the change posts of the writes that finished, see
    logs.alert_findings(). A finding that the stop caused posts nothing. The app rescan of a write waits. A job's next
    run sends it, see after_edit(), and a backfill sends it at its end. A deep analysis or a recheck sends none."""
    rec = ctx.rec
    rec["not_run"] = [s.__name__ for s in left]
    if "outcome" not in rec:   # a conversion that failed or was skipped set one, so "repack" here is a conversion that ran
        wrote = (["repacked"] if "repack" in rec else []) + [c for c in [(rec.get("header_repair") or {}).get("code")] if c in config.REPAIRED]
        rec.update(outcome="stopped", result="not checked: the run was stopped", reasons=rec.get("reasons") or wrote)
    if after_edit in left and (parts := rescanned(ctx)):
        if ctx.mode == "import" and ctx.save:
            ctx.job["rescan"] = True
            with contextlib.suppress(sqlite3.Error):   # a busy store costs the rescan, and the app's own refresh reads the file later
                ctx.save()
        elif not (ctx.mode == "import" or ctx.background):
            for x in parts:
                x["rescan"] = "after the run"
    found = [f for f in [*getattr(ctx, "first", ()), getattr(ctx, "unreadable", None)] if f]   # faults() and conversion() set them
    rec.update(findings=found, alert_kinds=[f["kind"] for f in found])
    if ctx.post:
        rec["alert_result"] = logs.alert_findings(rec, os.path.getsize(ctx.path) if os.path.exists(ctx.path) else ctx.size)


def rescanned(ctx):
    """The parts of ctx.rec that name a write which replaced the file, so the app rescans it: a conversion that ran, a
    header repair and a subtitle remux. A conversion that hid extras settles them in place of a rescan, see after_edit()."""
    rec = ctx.rec
    return [x for x, wrote in ((rec.get("repack"), "repack" in rec and not ctx.unconverted and not rec["repack"].get("pending")),
                               (rec.get("header_repair"), (rec.get("header_repair") or {}).get("code") in config.REPAIRED),
                               (rec.get("subremux"), (rec.get("subremux") or {}).get("done"))) if wrote]


def refresh(ctx, duration=True):
    """Probe the file at the start and again after a step replaced it: its stat, its size and mkvmerge -J. The record
    takes the new size, mtime and duration. duration False keeps the record's duration, as after a subtitle remux."""
    was, ctx.st = getattr(ctx, "st", None), os.stat(ctx.path)
    if was:   # a remux of AMG's own keeps the mark of a burn-in job, see runner.muted()
        runner.mute_mark(ctx.path, was)
        ctx.waits = ctx.yields   # a job a backfill queued yields no more. A yield would lose the post and the decision line of the change,
        # and the job's import time limit bounds the rest. A deep analysis and a recheck have no time limit, so they still yield.
    ctx.size = ctx.st.st_size; ctx.j = checks.mkvmerge(ctx.path)
    ctx.rec.update(size=ctx.size, mtime=int(ctx.st.st_mtime), **({"file_duration": round(decide.duration(ctx.j))} if duration else {}))


def changed(rec, repack=True):
    """Whether the run of rec changed its file in a way Plex reads, so Plex analyzes it again: a flag edit, one a
    wrong-content re-grab followed too, or a remux that replaced it. repack False leaves out a conversion, as --sub-time
    does. An edit of the Original language flag alone does not count, because Plex does not read that flag."""
    edits = rec.get("edits") or []
    flag_only = bool(edits) and all(decide.prop(e) == decide.ORIGINAL_FLAG for e in edits)
    return ("edited" in (rec.get("result"), rec.get("edit_result")) and not flag_only) \
        or bool({*config.REPAIRED, *(["repacked"] if repack else [])} & set(rec.get("reasons", [])))


def wrote(rec):
    """Whether the run of rec wrote its file, or as a dry run plans to. It counts a flag edit, a header repair and a
    subtitle remux. It counts a conversion only as a dry run plans it. A backfill's --limit counts these files."""
    return rec["outcome"] in ("edited", "dry_run", "would_repack") \
        or bool({*config.REPAIRED, "would_repair_header", "would_remux_subtitles"} & set(rec.get("reasons", [])))


def start(ctx):
    """The record, the skip of a file that is not .mkv and does not convert, and the probe."""
    ctx.item = ctx.item or {"listed": ctx.runtime}
    ctx.rec = dict(id=uuid.uuid4().hex[:12], app=ctx.app, source=ctx.source, apply=ctx.apply, ids=ctx.ids or {}, label=ctx.label, path=ctx.path,
                   original=ctx.original, kids=ctx.kids, release=ctx.release)
    ctx.mkv, ctx.conv = ctx.path.lower().endswith(".mkv"), config.CFG.convert if ctx.mode == "import" else ctx.mode == "convert"
    ctx.subs_on = subtitles.sub_on(ctx.source, ctx.mode in ("sub_check", "sub_time"))
    if not ctx.mkv and ctx.mode != "import" and not ctx.conv:
        ctx.rec = dict(ctx.rec, outcome="not_mkv", result="skipped, not mkv")
        return True
    if ctx.apply and (ctx.mkv or ctx.conv):   # a SIGKILL during a repack leaves its temp file
        remux.sweep_repack_tmp(os.path.dirname(ctx.path), ctx.source)
    refresh(ctx)
    ctx.muted = runner.muted(ctx.path, ctx.st)   # a burn-in job turned off the English subtitles, see decision_fields()
    ctx.extra = []   # the reason codes faults() adds to the decision's, which decision_fields() keeps


def conversion(ctx):
    """The conversion into Matroska, see convert(). mkvpropedit edits Matroska only. A file that did not convert ends
    the run there. An import of a file that is not .mkv goes on with the original instead, so its audio and video checks
    still run. A convert run ends after the conversion too. A conversion that removed a broken timed-text track, see
    convert.broken_tracks(), gives an unreadable finding, in a convert run too."""
    rec, j, st, job = ctx.rec, ctx.j, ctx.st, ctx.job
    container = (j.get("container") or {}).get("type") or "Matroska"   # a probe without a type reads as Matroska
    ctx.unconverted = ctx.damaged = ctx.unreadable = None
    ctx.places = {}
    review = not ctx.check or (ctx.apply and ctx.check == "listed")   # the list to review by hand takes no dry --check run and no file no app lists
    if not ((ctx.mkv and container != "Matroska") or (not ctx.mkv and ctx.conv)):
        return
    if ctx.shared and ctx.apply and not ctx.pool and ctx.mode != "import":
        raise runner.Replan("the file needs a repack")
    if ctx.waits:   # a conversion of a film takes minutes, so an import that waits goes first
        runner.deep_waits()
    was = ctx.path
    got, result, rec["repack"], ctx.path = convert.convert(ctx.app, ctx.path, j, st, ctx.apply, rec["ids"], ctx.lock, ctx.pool, ctx.force,
                                                           ctx.subs_on and subtitles.sub_fixes(ctx.source), decide.codes(ctx.original),
                                                           shared=bool(ctx.shared or ctx.pool), source=ctx.source, outside=ctx.check == "outside")
    path = ctx.path
    # A relink gives the file a new id: the new name, or the original that convert_undo() imported again. The new name
    # always replaces the old id. Its id is None when an undo found the new name listed but could not read the id, and
    # for a file outside the library, which no app relinks.
    renamed = got == "repacked" and path != rec["path"]
    fid = ((rec["repack"].get("relink") or {}) if renamed else ((rec["repack"].get("restored") or {}).get("import") or {})).get("file_id")
    if renamed or fid:
        rec["ids"]["file_id"] = fid
        if ctx.mode == "import":   # the queue gets the new id now, so a re-run asks the app for it after a Replan or a crash
            job.update(path=path, file_id=str(fid) if fid else None)
            if ctx.save:
                with contextlib.suppress(sqlite3.Error):   # a busy store keeps the old id in the queue, and this run goes on
                    ctx.save()
    if ctx.shared and ctx.apply and not ctx.pool:   # a hook job converted under the shared lock. The rest of the job runs exclusive.
        if got == "repacked":   # a re-run would lose the new file's record, so the job goes on, after the older jobs of its download
            ctx.shared.turn()
            runner.gated(ctx.lock, fcntl.LOCK_EX)
        else:   # the original stays. It and its download must still be as the checks found them, see exclusive().
            if not os.path.exists(rec["path"]) and review:   # exclusive() runs the job again, so the list keeps this run's result now
                convert.convert_list(ctx.app, dict(rec, outcome=got, result=result))
            ctx.shared.exclusive(st)
        ctx.shared = False
    elif ctx.lock is not None and not ctx.pool and got in ("repacked", "repack_failed", "repack_source_changed"):   # convert() let the lock go, see there
        runner.gated(ctx.lock, fcntl.LOCK_EX)
    if "warnings" in rec["repack"]:
        ctx.rearm()   # convert() ran the remux with the time limit off, whatever came of it
    rec["container"] = container
    if got == "repacked":
        if path != rec["path"]:   # the app lists the new name now, with the new file id above, unless it is outside the library
            rec["path"], ctx.mkv = path, True
        logs.log(dict(rec, outcome="repacked", result="repacked"))   # a repack cannot be undone, so its record is on disk before anything else runs
        checks.lid_carry(path, st, was)   # the proof shows the same audio, so the subtitle check's words move to the new file
        if broken := rec["repack"].get("broken"):
            ctx.unreadable = report.unreadable_finding(broken, rec["repack"].get("kept"), container)
        # The place in the original of each subtitle track of the new file that came from one of the original's, for
        # the notice of a remux that takes it out, see after_edit(). The conversion keeps their order, and its own
        # tracks come after them, see convert.convert_cmd().
        gone = {*(broken or ()), *([] if rec["repack"].get("tracks_kept_back") else rec["repack"].get("tracks_unmatched") or ())}
        sub_langs = lambda j: [(t.get("properties") or {}).get("language") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
        before = [(f"s{n}", lang) for n, lang in enumerate(sub_langs(j), 1) if f"s{n}" not in gone]
        if ctx.mode == "convert":   # the language backfill decides the flags later, from its own cache
            rec["repack"]["rescan"] = "after the run"
            ctx.rec = dict(rec, outcome="repacked", result="repacked", reasons=["repacked"], **{"class": "repacked"})
            if ctx.unreadable:
                ctx.rec.update(findings=[ctx.unreadable], alert_kinds=[ctx.unreadable["kind"]])
                if ctx.post:
                    ctx.rec["alert_result"] = logs.alert_findings(ctx.rec, rec["repack"]["new_size"])
            return True
        refresh(ctx)
        ctx.places = {f"s{k}": p for k, ((p, lang), now) in enumerate(zip(before, sub_langs(ctx.j)), 1) if lang == now}
        return
    would = got == "would_repack"
    kind = container if ctx.mkv else os.path.splitext(path)[1].lower().lstrip(".")
    rec.update(outcome=got, result=result, reasons=["not_matroska"], **{"class": f"would repack {kind}" if would else "not Matroska"})
    if ctx.keep_plan:   # outcome: --audit --plan-from groups the skipped files by it
        rec["plan"] = {"app": ctx.app, "label": ctx.label, "path": path, "class": rec["class"], "edits": [], "undecided": None, "dropped": [],
                       "cls": None, "orig": [], "tracks": [], "repack": result, "outcome": got,
                       **({} if would else {"skipped": f"not Matroska: {container}"})}
    failed = None
    if got == "repack_failed":   # one embed, like a failed flag edit. A skip, a dry run or a changed original posts nothing.
        failed = {"kind": "repack", "container": container, "why": result.partition(": ")[2][:200]}
    if not would and review:
        convert.convert_list(ctx.app, rec)
    damage = rec["repack"].get("damage")
    if damage and ctx.mode == "import" and (ctx.apply or ctx.check):   # the original is damaged: it re-grabs like broken audio
        action = regrab.regrab(ctx.app, job, damage["fault"], proof.damage_probe(damage, j), "damage")
        rec.update(outcome="damaged_source", result=f"damaged source: {damage['fault']}", regrab=action["code"])
        ctx.damaged, failed = {"kind": "damage", "fault": damage["fault"], "line": damage["line"], "refusal": damage.get("refusal"), "action": action}, None
    gone = ctx.damaged and (rec["regrab"] in ("regrabbed", "restored", "searched", "deleted") or not os.path.exists(path))
    if ctx.mkv or ctx.mode != "import" or gone:
        problem = ctx.damaged or failed
        if problem:
            rec.update(findings=[problem], alert_kinds=[problem["kind"]])
            if ctx.post:
                rec["alert_result"] = logs.alert_findings(rec, ctx.size)
        return True
    if ctx.damaged:
        ctx.rearm()   # regrab() turned the time limit off
    ctx.unconverted = (rec["result"], failed)   # an import goes on with the original, so its audio and video checks still run


def header(ctx):
    """The header check of a Matroska file, see header_of(). An issue the checks cannot read through is repaired here,
    before them, see repack(): no usable Cues, a header duration far from the streams, or a subtitle event far past
    them (hp["first"]). Any other issue waits for the one write of the run, see write(). ctx.head is then the header
    stage of check_video() that write() repairs. ctx.stage is the header stage of the file as it is now, for edit()."""
    rec, j, path = ctx.rec, ctx.j, ctx.path
    ctx.asked = checks.tmdb_ask(ctx.app, ctx.item)   # the forced-flag clear of an English original needs TMDB's spoken languages. metadata() reuses it.
    ctx.hp = ctx.vpre = ctx.head = None   # header_probe(), and the video check of a file with a header issue, which looks for real damage first
    ctx.ended = False   # a subtitle remux ended a header issue that stayed, see write()
    if ctx.mkv and config.CFG.header_repair and ctx.video and not ctx.background:
        ctx.hp = checks.header_of(path, j)
    hp = ctx.stage = ctx.hp
    if hp:   # each RFC 9559 fault by kind, which --audit counts
        rec["rfc"] = hp["rfc"]
    if not (hp and hp["issue"]):
        return
    cut = None
    if hp.get("trim") or hp.get("remove"):   # the runtime decides a removal, and whether the file may be cut
        listed = ctx.runtime or (ctx.app in apps.ARR and apps.ARR[ctx.app].film and (ctx.asked[0] or {}).get("runtime")) or 0   # minutes, the app's else TMDB's
        trim, remove, cut = decide.subtitle_plan(hp["trim"], hp["remove"], hp["subtitles"], hp["streams"], listed)
        hp = ctx.hp = dict(hp, trim=trim, remove=remove, listed=listed)
    if hp["first"] and ctx.shared and ctx.apply and not hp["blocked"] and not cut and not remux.repack_skip(path, ctx.st):
        raise runner.Replan("the file needs a header repair")
    ctx.vpre = checks.video_check(path, j=j, hp=hp)
    h = ctx.vpre[2].get("header") or {}
    ctx.stage = h or dict(hp, repairable=False)
    if cut and h.get("repairable"):   # the subtitles of a cut file run to the full length and are good: keep the file
        code, result, info = "subtitle_file_may_be_cut", f"not repaired, the file may be cut: {cut}", {"listed": hp["listed"], "streams": h["streams"]}
    elif h.get("repairable") and not hp["first"]:   # the one write of the run repairs it, with the flag edits
        ctx.head = h
        return
    elif h.get("repairable"):
        if ctx.waits:   # as before a subtitle remux
            runner.deep_waits()
        code, result, info = remux.repack(path, j, ctx.st, ctx.apply, h)
        if "warnings" in info:
            ctx.rearm()   # repack() ran mkvmerge with the time limit off, whatever came of it
    elif hp.get("unfixable"):
        code, result, info = "subtitle_overrun_unfixable", "not repaired, a subtitle runs past the end: " + "; ".join(h.get("blocked") or []), {}
    else:
        code, result, info = "header_not_repaired", "not repaired: " + "; ".join(
            h.get("blocked") or [f'the video check failed: {ctx.vpre[2].get("error")}']) + ". The issue: " + "; ".join(hp["issue"]), {}
    rec["header_repair"] = dict(info, issue=hp["issue"], result=result, code=code)
    if code in config.REPAIRED:
        logs.log(dict(rec, outcome=code, result=result))   # the original is kept a while, so its record is on disk before anything else runs
        refresh(ctx)
        ctx.stage = None   # the stage was of the original
    elif code == "header_repair_failed" or info.get("stopped"):   # edit() tries no second remux of the file in this run
        ctx.stage = dict(h, tried=result)


def languages(ctx):
    """The decision, with the languages the hearing and the subtitle text give, see hear() and retag()."""
    rec, j, path, mkv = ctx.rec, ctx.j, ctx.path, ctx.mkv
    spoken = ctx.spoken = checks.spoken_of(ctx.asked[0])
    d = decide.decide(j, ctx.original, ctx.kids, ctx.release, spoken=spoken) if mkv else {"edits": [], "notes": [], "reasons": [],
                                                                                         "wrong_language": False, "tracks": decide.classify(j, original=ctx.original)}
    heard, got, tags = {}, {}, None
    lid_when = config.LID_WHEN & set(d["reasons"])
    pre = decide.retag(j, table=checks.langs()) if mkv else {"ask": set(), "to_read": set()}
    ask = pre["ask"]   # the tracks whose tag only a heard language can change
    doubt = {t["pos"] for t in d["tracks"] if t["kind"] == "a" and t["role"] == "main" and t["conf"] < decide.AGREE} if lid_when else set()
    # The Original language flag changes only on a heard language or a read text, see decide.retag(). A run that keeps
    # the flags, see act(), needs neither. The audio waits for the word check, see flag_hearing().
    ctx.content_lang = decide.content_language(ctx.original, ctx.asked[0]) if mkv and ctx.app and not ctx.background else None
    ctx.flag_hear, flag_read = decide.flag_checks(j, ctx.content_lang)
    if (lid_when or ask) and decide.duration(j) >= config.LID_MIN_SECONDS and not ctx.background:   # the import heard the language
        if ctx.shared:   # a hearing waits for a turn at the host's model, so it runs without the file lock and an edit never queues behind it
            fcntl.flock(ctx.lock, fcntl.LOCK_UN)
        keep = ctx.subs_on and mkv and checks.lid_ready() and bool(any((t.get("properties") or {}).get("codec_id") in config.SUB_CODECS
                                                                       for t in j.get("tracks") or []) or subtitles.side_stats(path))
        then = subtitles.sub_jobs(path, j, d, subtitles.mkv_sidecars(path, d)) if keep else None   # the subtitle check shares the loaded model
        try:
            got, rec["heard"] = checks.hear(path, j, d, ctx.original, only=doubt | ask, keep=keep, then=then)
        finally:
            if then:
                with contextlib.suppress(OSError):
                    os.remove(then)
        if ctx.shared:
            ctx.shared.reshare(ctx.st)
    known, said = checks.item_languages(ctx.original, ctx.asked[0])
    checked, read = {}, {}   # checked: read for the Original language flag alone, see decide.retag()
    if mkv:   # the subtitles a player shows by itself, and those whose tag the text can change
        shown = decide.defaults(d["edits"])
        want = pre["to_read"] | {t["pos"] for t in d["tracks"] if t["kind"] == "s" and (shown.get(t["sel"], t["default"]) or t["forced_flag"])}
        got_text = subtitles.subtitle_read(path, j, want | flag_read) if want | flag_read else {}
        if got_text:
            rec["read"] = {p: {"lang": x[0], "conf": x[1], "why": x[2]} for p, x in got_text.items()}
        read = {p: x[0] for p, x in got_text.items() if x[0] and p in want}
        checked |= {p: x[0] for p, x in got_text.items() if x[0] and p not in want}
        tags = decide.retag(j, got, known, checks.langs(), said, read, ctx.content_lang, checked)
    lang = {t["pos"]: t["lang"] for t in d["tracks"]}
    fix = {p: x for p, x in (tags or {}).get("set", {}).items() if x != lang.get(p)}   # a new tag the decision does not read yet
    heard = {**fix, **got} if lid_when else fix   # outside those reasons a heard language counts only when the tag changes
    wrong = (tags or {}).get("wrong") or {}   # a subtitle whose text contradicts its tag, with nothing else to back a new tag
    if heard or wrong:
        d = decide.decide(j, ctx.original, ctx.kids, ctx.release, heard, spoken, wrong)
    if tags:
        d = decide.with_tags(d, tags)
    ctx.d, ctx.heard, ctx.got, ctx.tags, ctx.known, ctx.said, ctx.read, ctx.wrong = d, heard, got, tags, known, said, read, wrong
    ctx.checked = checked if mkv else {}


def subtitle_checks(ctx):
    """The subtitle match check, the reference timing and the flash check, and the plan of the one remux they ask for,
    see resub(). write() runs it. A track that does not match leaves the file, and a track whose times need a fix gets
    them. --sub-time and the deep analysis hear the whole file and time every line of each track that matched, see
    subtitles.sub_whole(). When the remux fails, a track that does not match loses its flags instead. Sidecars beside
    the file are checked too, see sidecar_fix(). The decision counts each track that does not match, and each garbled
    track the remux takes out."""
    rec, j, d, path, mkv, subs_on = ctx.rec, ctx.j, ctx.d, ctx.path, ctx.mkv, ctx.subs_on
    timing = ctx.depth in ("sub_time", "deep")   # the reference timing and the whole-file timing
    sync, unmatched, fixes, sides = {}, set(), {}, {}   # the subtitle match check, docs/design.md, "Subtitle match"
    timed_by, others, items = {}, {}, {}   # the reference timing of --sub-time, its sidecars, and the word check's cues
    full = subs_on and ctx.depth != "import"   # --sub-check and --sub-time read a track the Cues do not index from the whole file
    unindexed = subtitles.cue_less(path, j) & set(subtitles.sub_codecs(j)) if mkv and subs_on and not full and subtitles.sub_codecs(j) else set()
    if unindexed:
        rec["unindexed"] = {"tracks": sorted(unindexed), "why": "the Cues index none of their blocks, and an import never reads the whole file, "
                                                               "so the subtitle checks skip them"}
    # A garbled .srt sidecar is repaired or renamed, see subtitles.sidecar_fix(). Only a run that reads whole files
    # checks it, as for a garbled track below. One to rename leaves every other check. One to repair stays in them, as a
    # garbled track does, and what they find waits for its readable text, see later below.
    bad_sides = subtitles.garbled_sidecars(path) if mkv and full else {}
    gone_sides = {n for n, s in bad_sides.items() if not s["garbled"]["repair"]}
    if mkv and subs_on and checks.lid_ready():
        sides = {n: s for n, s in subtitles.mkv_sidecars(path, d).items() if n not in gone_sides}
        if subtitles.sub_targets(j, d) or sides:
            items = subtitles.sub_items(path, j, d, sides, full) if decide.duration(j) >= config.SUB_MIN_SECONDS else {}   # read under the file lock
            if ctx.shared:   # a hearing waits for a turn at the host's model, so it runs without the file lock, as above
                fcntl.flock(ctx.lock, fcntl.LOCK_UN)
            before = (rec.get("repack") or {}).get("subcheck") or {}   # a conversion checked the tracks already: the same windows
            sync = subtitles.sub_match(path, j, d, {r["audio"]: r["starts"] for r in before.values() if r.get("starts")} or None, sides, ctx.known,
                                       items, full, line=not timing, deep=ctx.mode == "import" and config.CFG.subtitles == "deep",
                                       streams=(ctx.hp or {}).get("streams"))
            if timing and sync:   # the whole-file timing of each track that matched, see subtitles.sub_whole()
                # The planner: the timing proposes the moves. Whether the times post is the timing judge's, after the
                # write, see judge.outcomes().
                rec["whole"], rec["whole_facts"] = subtitles.sub_whole(path, j, {k: items[k] for k, r in sync.items() if r["verdict"] == "match" and k in items},
                                                                       sync, deep=ctx.yields)
            if ctx.shared:
                ctx.shared.reshare(ctx.st)
        for p in unindexed & set(sync):   # an import skips the track, and says why
            sync[p] = dict(sync[p], why=f'{sync[p]["why"]}: {rec["unindexed"]["why"]}')
        if sync:
            rec["subcheck"] = sync
    left = config.DEADLINE.left()   # an import reads and fits while its time limit leaves SUB_RESERVE
    deadline = time.monotonic() + left - config.SUB_RESERVE if left else None
    stop = runner.deep_waits if ctx.yields else (lambda: time.monotonic() > deadline) if deadline is not None else None
    if mkv and subs_on:   # the reference timing
        others = {n: s for n, s in subtitles.ref_sidecars(path, d).items() if n not in gone_sides}
        timed_by, refs = subtitles.sub_reference(path, j, d, sync, items or {}, others, full=full, stop=stop, report=timing, whole=rec.get("whole"))
        if full and checks.lid_ready():   # the speech layout of the subtitles no word check reads and no reference fits
            if ctx.shared:   # the read of the whole audio track takes minutes for a film, so it runs without the file lock
                fcntl.flock(ctx.lock, fcntl.LOCK_UN)
            laid, rec_speech = subtitles.sub_layout(path, j, d, timed_by, others, stop, deep=ctx.yields)
            if ctx.shared:
                ctx.shared.reshare(ctx.st)
            timed_by.update(laid)
            if rec_speech:
                rec["speech"] = rec_speech
        if timed_by:
            rec["subtime"], rec["references"] = timed_by, refs
    # A SubRip track whose text is garbled, docs/design.md, "Garbled subtitle repair". Only a run that reads whole files
    # checks it, so a track the Cues do not index is read too. The word check read its garbled text, so a mismatch waits
    # for readable text, see later below. A cut read waits too, but it is never repaired, so it only alerts.
    all_sides = proof.sidecar_subs(path) if mkv and full else []
    garbled = subtitles.garbled_tracks(path, j, all_sides, full) if mkv and full else {}
    if garbled:
        rec["garbled"] = {p: {k: v for k, v in g.items() if k != "side"} for p, g in garbled.items()}
    waits = {p for p in [*garbled, *bad_sides] if (sync.get(p) or {}).get("verdict") == "mismatch"}
    for p in waits:
        sync[p] = dict(sync[p], verdict="unknown", why=f'{sync[p]["why"]}, but its text is garbled, so a later run judges it', held=True)
    if sync or timed_by:
        tracks = {p: r for p, r in {**sync, **timed_by}.items() if p not in sides and p not in others}
        unmatched = {p for p, r in tracks.items() if r["verdict"] == "mismatch"}
        fixes = {p: r["timing"]["fix"] for p, r in tracks.items() if (r.get("timing") or {}).get("fix")} if subtitles.sub_fixes(ctx.source) else {}
        unmatched = unmatched if subtitles.sub_fixes(ctx.source) else set()   # SUBTITLES check turns no flag off. The alerts read sync.
    blocks = {}
    # The new start of each line the whole-file timing moves, by its place in the subtitle, see subtitles.sub_whole()
    starts = {k: {i: m["start"] for i, m in e["moves"].items() if m["why"] != "end"} for k, e in (rec.get("whole") or {}).items()} \
        if subtitles.sub_fixes(ctx.source) else {}
    starts = {k: v for k, v in starts.items() if v}
    # A fix of the speech layout that keeps runs of lines at the file's ends, see subsync.shifted(), holds them where
    # they are with blocks against its fix. A remux or a sidecar rewrite then takes its plan.
    kept = {k: subsync.keep_blocks(r["timing"]) for k, r in timed_by.items() if ((r.get("timing") or {}).get("fix") and r["timing"].get("keep"))
            and (k in fixes or k in others)} if subtitles.sub_fixes(ctx.source) else {}
    blocks.update(kept)
    # A garbled track that cannot be repaired leaves the file as a removal does, and its bytes go beside the video, see
    # remux.resub(). A cut read never does, because the bytes past the cut were never read.
    strip = {p: g["tag"] for p, g in garbled.items() if not g["repair"] and not g.get("capped")} \
        if subtitles.sub_fixes(ctx.source) and config.CFG.keep_days else {}
    # A track whose every name for its bytes holds a file stays this run, so the remux still makes the other fixes.
    # Before the remux no bytes say whether one of those files holds them already, see remux.broken_plan().
    names = {p: {"lang": tag, "data": None, "ext": remux.TEXT_EXT[subtitles.sub_codecs(j)[p]]} for p, tag in strip.items()}
    remux.broken_plan(path, names)
    for p in [p for p, e in names.items() if e["reused"]]:   # with no bytes, a name that holds a file is taken
        del strip[p]
        for g in (garbled[p], rec["garbled"][p]):
            g.update(held=True, why=f'{g["why"]}, but the names for its bytes beside the video are taken')
    remove = sorted(unmatched - set(strip)) if config.CFG.keep_days else []   # a removal keeps the original, so KEEP_ORIGINALS_DAYS 0 keeps the track
    # A repair takes the track's place in the remux and keeps its times, so the track gets no other change in this run.
    # Its default flag goes when the decision turns it off, in a run whose flag edit acts on the decision, see act().
    keep = ctx.background or not ctx.app   # the runs that keep the flags, as act() does
    off, sel = {e[0] for e in d["edits"] if len(e) == 3 and e[1] == 0} if not keep else set(), {t["pos"]: t["sel"] for t in d["tracks"]}
    recode = {p: dict(codepage=g["codepage"], lang=g["tag"], default_off=sel.get(p) in off,
                      **({"sidecar": next(s for s in all_sides if s["name"] == g["sidecar"]), "side": g["side"], "filled": g["filled"]} if g.get("sidecar") else {}))
              for p, g in garbled.items() if g["repair"] and p not in remove} if subtitles.sub_fixes(ctx.source) else {}
    later = {p for p in recode if p in fixes or p in blocks or p in starts or p in waits}   # judged again after the repair, see after_edit()
    fixes = {p: f for p, f in fixes.items() if p not in recode and p not in strip}
    flashy = subtitles.flash_check(path, j, [s for s in proof.sidecar_subs(path) if s["name"] not in gone_sides], full, stop) if mkv and subs_on else {}   # ends too short to read, any text track or sidecar
    codecs = subtitles.sub_codecs(j)
    vtt = {p for p, c in codecs.items() if c == "S_TEXT/WEBVTT"} & set(flashy)   # the fix never rewrites WebVTT: report only
    if flashy:
        rec["flash"] = {k: {"cues": len(v), "median": round(statistics.median(o - a for a, _, o, _ in v), 3), "lengthened": sum(n != o for _, _, o, n in v),
                            "first": [[a, o, n] for a, _, o, n in v[:3]], **({"report_only": "WebVTT"} if k in vtt else {})} for k, v in flashy.items()}
    later |= set(recode) & set(flashy)
    for p in later:
        rec["garbled"][p]["later"] = True
    ends = {p: v for p, v in flashy.items() if p.startswith("s") and p[1:].isdigit() and p not in remove and p not in vtt and p not in recode
            and p not in strip} if subtitles.sub_fixes(ctx.source) else {}
    # The new times of each cue of a track or sidecar with blocks or new starts, with its fix and its flash ends, in one
    # plan that resub() or sidecar_fix() writes. The plan takes the cues in the order of the file's text. A sidecar's
    # new starts count only when its text gives the cues the word check read. A WebVTT track is never rewritten, so its
    # moves only report.
    plan = lambda k, cues, fix, ass: remux.time_plan(cues, fix, blocks.get(k, ()), [x[3] for x in flashy[k]] if k in flashy else None, ass, starts.get(k))
    cues_of = lambda p: items[p][2] if p in items else subtitles.layout_cues(path, p)   # a layout track's, as sub_layout() read them
    for k in list(kept):   # a plan that would put a line with no text out of order, or has no cues, writes nothing
        cs = subtitles.side_cues({**others, **sides}[k]) if k in others or k in sides else cues_of(k)
        if not cs or not subsync.keep_ordered(cs, timed_by[k]["timing"]):
            blocks.pop(k, None), kept.pop(k), fixes.pop(k, None)
            timed_by[k] = dict(timed_by[k], timing={"fix": None, "unfixed": timed_by[k]["timing"]["fix"]["offset"],
                                                    "why": f'{timed_by[k]["timing"]["why"]}, but a line with no text would pass a line that keeps its time, so the times stay'})
    moves = {p: x for p in {*blocks, *starts} if p not in sides and p not in others and p not in recode and p not in strip and codecs.get(p) in config.TEXT_CODECS
             and (x := plan(p, cues_of(p), fixes.get(p), codecs[p] in ("S_TEXT/ASS", "S_TEXT/SSA")))}
    timing_of = lambda n: ({**sync, **timed_by}.get(n) or {}).get("timing") or {}
    side_cue = lambda n: subtitles.side_cues({**others, **sides}[n])
    side_moves = {n: x for n in {*blocks, *starts} if (n in sides or n in others) and n not in bad_sides
                  and (n not in starts or subtitles.same_cues(side_cue(n), items[n][2])) and (x := plan(n, side_cue(n), timing_of(n).get("fix"), False))}
    # A repair of a sidecar's text takes the place of its other changes in this run, as for a track. Its verdicts and
    # times wait for a run that reads its readable text.
    side_later = {n for n in bad_sides if n in waits or timing_of(n).get("fix") or n in blocks or n in starts or n in flashy}
    for n in side_later:
        bad_sides[n]["garbled"]["later"] = True
    st = ctx.st
    if (subtitles.FULL.get((path, st.st_size, st.st_mtime_ns)) or {}).get("tracks"):
        rec["full_read"] = {k: v for k, v in subtitles.FULL[(path, st.st_size, st.st_mtime_ns)].items() if k != "cues"}
    # A cover picture stored as a video track goes into an attachment in the same remux, see remux.cover_tracks()
    covers = remux.cover_tracks(path, j) if mkv and subs_on and subtitles.sub_fixes(ctx.source) else {}
    if covers:
        rec["covers"] = {e["place"]: {k: e[k] for k in ("codec", "frames", "name", "copy", "there", "why") if e.get(k) is not None}
                         for e in covers.values()}
    covers = {i: e for i, e in covers.items() if not e.get("why")}
    ctx.subplan, ctx.sub_ids = None, [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
    if fixes or remove or ends or moves or recode or strip or covers:   # the one remux that write() runs, see resub()
        by_id = lambda ps: [ctx.sub_ids[int(p[1:]) - 1] for p in ps]
        ctx.subplan = dict(fixes=dict(zip(by_id(fixes), fixes.values())), drop=by_id(remove), ends=dict(zip(by_id(ends), ends.values())),
                           **({"timed": dict(zip(by_id(moves), moves.values()))} if moves else {}),
                           **({"recode": dict(zip(by_id(recode), recode.values()))} if recode else {}),
                           **({"strip": dict(zip(by_id(strip), strip.values()))} if strip else {}), **({"covers": covers} if covers else {}))
    # The tracks that stay, by their place before any remux, see report.KEPT_BACK. write() takes out the ones it removed.
    stay = sorted({p for p, r in {**sync, **timed_by}.items() if p not in sides and p not in others and r["verdict"] == "mismatch"})
    if stay and not subtitles.sub_fixes(ctx.source):
        rec.setdefault("subremux", {}).update(kept_back="check")
    elif stay and not config.CFG.keep_days:
        rec.setdefault("subremux", {}).update(kept_back="keep_days")
    if unmatched or strip:   # the remux takes out the tracks in strip too, so the decision gives them no flag either
        d = decide.decide(ctx.j, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong, unmatched | set(strip))
        if ctx.tags:
            d = decide.with_tags(d, ctx.tags)
    ctx.d, ctx.sync, ctx.timed_by, ctx.sides, ctx.others, ctx.unmatched, ctx.stay = d, sync, timed_by, sides, others, unmatched, stay
    ctx.fixes, ctx.remove, ctx.ends, ctx.flashy, ctx.moves, ctx.side_moves, ctx.recode = fixes, remove, ends, flashy, moves, side_moves, recode
    ctx.strip, ctx.later, ctx.bad_sides, ctx.covers, ctx.kept = strip, later | side_later, bad_sides, covers, kept
    ctx.starts = {k: sorted(c[0] for c in x[2]) for k, x in items.items()}   # the cue starts the timing judge counts, see judge.outcomes()


def flag_hearing(ctx):
    """The language check of the main audio tracks whose Original language flag their tag would change, see
    decide.flag_checks(). A subtitle whose words matched an audio track at the word check proves that the track speaks
    its language, the one the check heard it in. Such a track needs no hearing, and "flag_matched" logs it. A mismatch
    proves nothing. lid.py hears the rest, after the subtitle check, and "flag_heard" logs its answers. A missing
    install, a failed run or no time left gives no answer, so their flag stays. The flag edits are then planned
    again."""
    if not ctx.content_lang or not ctx.flag_hear:
        return
    rec, lang = ctx.rec, {t["pos"]: t["lang"] for t in ctx.d["tracks"] if t["kind"] == "a"}
    matched = {f'a{r["audio"] + 1}' for r in ctx.sync.values() if r.get("verdict") == "match" and r.get("audio") is not None}
    proven = {p: lang[p] for p in sorted(matched & ctx.flag_hear) if p in lang}
    rest = ctx.flag_hear - set(proven) - set(ctx.got) - set(ctx.checked)
    if proven:
        rec["flag_matched"] = proven
    heard = {}
    if rest and decide.duration(ctx.j) >= config.LID_MIN_SECONDS:
        if ctx.shared:   # a hearing waits for a turn at the host's model, so it runs without the file lock, as in languages()
            fcntl.flock(ctx.lock, fcntl.LOCK_UN)
        heard, rec["flag_heard"] = checks.hear(ctx.path, ctx.j, ctx.d, ctx.original, only=rest)
        if ctx.shared:
            ctx.shared.reshare(ctx.st)
    if proven or heard:
        ctx.checked = {**ctx.checked, **proven, **heard}
        ctx.tags = decide.retag(ctx.j, ctx.got, ctx.known, checks.langs(), ctx.said, ctx.read, ctx.content_lang, ctx.checked)
        ctx.d = decide.with_tags(decide.decide(ctx.j, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong,
                                               ctx.unmatched | set(ctx.strip)), ctx.tags)   # as subtitle_checks() decides


def deep_drop(ctx):
    """A deep analysis or a recheck ends here when the app replaced or removed its file during the run. The app holds
    no file lock, so this happens during a remux, which then fails with "the original changed". The record becomes the
    quiet drop of deep_analysis(), so no stale edit, cache or alert follows. The reference is ctx.st, the run's own last stat,
    so a change of its own never counts."""
    if ctx.background and (drop := runner.deep_replaced({"path": ctx.path, "key": runner.file_key(ctx.st)})):
        ctx.rec = dict(app=ctx.app, source=ctx.source, path=ctx.path, **drop)
        return True


def decision_fields(ctx):
    """The decision in the record, and the plan of a dry run, see plan_record(). write() runs it again after its
    write, so the record holds the decision of the file the run leaves and the reason codes of the write."""
    rec, d = ctx.rec, ctx.d
    if ctx.muted and (kept := muted_plan(d))["edits"] != d["edits"]:   # a run that keeps the flags plans none of these
        d = ctx.d = kept
        rec["flags_kept"] = "a burned-in subtitle check turned off the English subtitles, so they stay off"
    ctx.file_checks = decide.checks(ctx.j, ctx.size, ctx.runtime, shorter_only=config.program(ctx.app) == "sonarr")
    rec.update(notes=d["notes"], reasons=d.get("reasons", []), item_class=d.get("cls"), tracks=logs.track_log(d["tracks"]),
               undecided=d.get("undecided"), abstain=d.get("abstain"), dropped=d.get("dropped", []), invariants=d.get("invariants", []),
               edit_rules=d.get("edit_rules", []), policy_path=d.get("path"))
    if "repack" in rec:
        rec["reasons"] = ["not_matroska" if ctx.unconverted else "repacked"] + rec["reasons"]
    if "header_repair" in rec:
        rec["reasons"] = [rec["header_repair"]["code"]] + rec["reasons"]
    if (rec.get("subremux") or {}).get("codes"):
        rec["reasons"] = rec["subremux"]["codes"] + rec["reasons"]
    rec["reasons"] += ctx.extra
    rec["class"] = decide.plan_class(d)
    if ctx.keep_plan:
        rec["plan"] = cli.plan_record(ctx.app, ctx.label, ctx.path, d)
        if "header_repair" in rec:
            rec["plan"]["header_repair"] = rec["header_repair"]["code"]
        if checks.padding_only(ctx.hp):   # --only-header leaves it, see docs/design.md, "RFC 9559 check"
            rec["plan"]["padding_only"] = True


def faults(ctx):
    """The findings of the steps so far, and the audio and video checks of an import, see check_audio() and
    check_video(). report.FINDINGS words them. first holds the re-grab kinds, rest the doubts and edit failures."""
    rec, d, hp = ctx.rec, ctx.d, ctx.hp
    ctx.first, ctx.rest, ctx.certain, ctx.vcertain = [], [], None, None
    first, rest = ctx.first, ctx.rest
    if ctx.vpre:   # the check that ran before the header step. A repaired file is the same streams, so it holds for the new file.
        rec["video"] = checks.video_summary(*ctx.vpre)
    if ctx.damaged:
        first.append(ctx.damaged)
    if ctx.unconverted and ctx.unconverted[1]:
        rest.append(ctx.unconverted[1])
    if ctx.unreadable:
        rest.append(ctx.unreadable)
    ctx.at = len(rest)   # the place of the findings that write_findings() adds after the write
    if ctx.mode == "import" and ctx.audio:
        ctx.certain, doubts, samples = checks.check_audio(ctx.path, ctx.j, d["edits"], ctx.runtime)
        rec["audio"] = logs.audio_summary(ctx.certain, doubts, samples)
        if doubts:   # uncertain: one alert per file, because the alerts of one kind share a marker
            rest.append({"kind": "audio", "doubts": doubts})
    if ctx.mode == "import" and ctx.video and not ctx.certain:   # before the edit, under the job's time limit, see check_video()
        ctx.vcertain, doubts, ctx.vfields = ctx.vpre or checks.video_check(ctx.path, j=ctx.j, hp=hp)
        rec["video"] = checks.video_summary(ctx.vcertain, doubts, ctx.vfields)
        if "code" in ctx.vfields:
            ctx.extra.append(ctx.vfields["code"])
            rec["reasons"] = rec["reasons"] + [ctx.vfields["code"]]
        if doubts:
            rest.append({"kind": "video", "doubts": doubts})
        elif ctx.source == "hook" and not ctx.vcertain and (ctx.vfields.get("frame_rate") or {}).get("variable"):
            # Only an import posts it. A backfill job or --check logs video.frame_rate, so a library pass posts no alert per file.
            rest.append({"kind": "frame_rate", "off": ctx.vfields["frame_rate"]["off"]})


def write_findings(ctx):
    """The findings that the write can change, in their place in ctx.rest, see faults(): a subtitle whose text
    contradicts its tag, by its place in the file the run leaves, and a header issue that stays."""
    rec, hp, hr, found = ctx.rec, ctx.hp, ctx.rec.get("header_repair") or {}, []
    if ctx.tags and ctx.tags["mismatch"]:
        found.append({"kind": "sublang", "mismatch": ctx.tags["mismatch"], "muted": [n for n in ctx.d["notes"] if " loses its default and forced flags, " in n]})
    code = None if ctx.ended or hr.get("edit") or hr.get("remux") else hr.get("code")   # the edit or subtitle finding names the failure
    if code in ("header_repair_failed", "subtitle_file_may_be_cut"):
        why = hr["result"].partition(": ")[2]
        found.append({"kind": "header", "why": why[:200]} if code == "header_repair_failed" else {"kind": "cut", "why": why})
    if code == "subtitle_overrun_unfixable" and hp.get("unfixable"):   # the alerts name the places before a removal, see report.track_langs()
        rm = rec.get("subremux") or {}
        gone = [*(rm.get("removed") or []), *(rm.get("stripped") or {} if rm.get("done") else [])]   # the remux took both out
        found.append({"kind": "subtitle", "issue": hp["issue"], "tracks": [dict(x, track=place_before(x["track"], gone)) for x in hp["unfixable"]]})
    ctx.rest[ctx.at:ctx.at] = found


def place_before(p, gone):
    """The place before a subtitle remux of the track at place p after it. gone holds the places the remux removed.
    This is the reverse of subtitles.renumber()."""
    n, k = int(p[1:]), 0
    while n:
        k += 1
        n -= f"s{k}" not in gone
    return f"s{k}"


def act(ctx):
    """The exclusive lock, then the re-grab of a certain fault, or the one write of the run, see regrab() and write()."""
    rec, d, certain, vcertain, job = ctx.rec, ctx.d, ctx.certain, ctx.vcertain, ctx.job
    if ctx.shared and not (certain or vcertain or "content" in config.CFG.regrab):   # the checks found nothing to re-grab
        ctx.shared.settle()
    sides = {s["name"]: s for s in proof.sidecar_subs(ctx.path)} if ctx.flashy else {**ctx.sides, **ctx.others}   # the sidecars act alike
    sides.update(ctx.bad_sides)   # a garbled sidecar holds its verdict, see subtitles.broken_side()
    ctx.sync_all = {**ctx.sync, **ctx.timed_by}
    ctx.sides, ctx.side_ends = sides, {n: v for n, v in ctx.flashy.items() if n in sides}   # at SUBTITLES check, sidecar_fix() only reports
    ctx.acts = [n for n in sides if (ctx.sync_all.get(n) or {}).get("verdict") == "mismatch" or ((ctx.sync_all.get(n) or {}).get("timing") or {}).get("fix")
                or n in ctx.side_ends or n in ctx.side_moves or n in ctx.bad_sides]
    if ctx.shared and ctx.apply and (certain or vcertain or (ctx.mkv and (d["edits"] or ctx.acts or ctx.subplan or ctx.head))):
        ctx.shared.exclusive(ctx.st)
    if certain:
        rec.update(outcome="broken_audio", result=f"broken audio: {certain}")
        ctx.first.append({"kind": "audio", "certain": certain, "action": regrab.regrab(ctx.app, job, certain, regrab.audio_probe(
            ctx.original, ctx.kids, ctx.release, ctx.runtime, ctx.heard, ctx.spoken)) if ctx.apply or ctx.check else {"code": "dry_run"}})
    elif vcertain:
        rec.update(outcome="corrupt_video", result=f"corrupt video: {vcertain}")
        ctx.first.append({"kind": "video", "certain": vcertain, "action": regrab.regrab(ctx.app, job, ctx.vfields["fault"], regrab.video_probe, "video")
                          if ctx.apply or ctx.check else {"code": "dry_run"}})
    elif not ctx.mkv:   # a conversion that did not happen set the result already
        if not ctx.unconverted:
            rec.update(outcome="not_mkv", result="skipped, not mkv")
    else:
        if (ctx.background or not ctx.app) and d["edits"] != flag_edits(ctx, d, ctx.unmatched | set(ctx.strip)):
            set_by = "an earlier run set the flags" if ctx.recheck else "the import set the flags"
            rec["flags_kept"] = (set_by if ctx.app else "no app lists the file, so no original language is known") + \
                ", and only a subtitle verdict changes a flag"
        if write(ctx):
            return True
        rec, d = ctx.rec, ctx.d
        # An abstained or dropped decision has no edits of its own, and its reason stays in "undecided" or "dropped". It
        # names the result only when nothing changed, so an edit of a subtitle verdict keeps "edited" and its Plex analyze.
        if d.get("undecided") and rec["outcome"] == "no_change":
            rec.update(outcome="undecided", result="undecided: " + d["undecided"])
        elif d.get("dropped") and rec["outcome"] == "no_change":
            rec.update(outcome="dropped", result="dropped: " + "; ".join(d["dropped"]))
    write_findings(ctx)


def flag_edits(ctx, d, unmatched):
    """The flag edits of the decision d that the run makes. The deep analysis and a recheck keep the flags an earlier run
    set, and --sub-time keeps them on a file no app lists, as no item says the original language. Only a subtitle
    verdict changes a flag then: a track in unmatched loses its default and forced flags. That holds after a remux of
    their own too."""
    if ctx.app and not ctx.background:
        return d["edits"]
    return [[t["sel"], 0, 1] for t in d["tracks"] if t["pos"] in unmatched and t["default"]] + \
        [[t["sel"], 0, 1, decide.FORCED_FLAG] for t in d["tracks"] if t["pos"] in unmatched and t["forced_flag"]]


def write(ctx):
    """The one write of the run (docs/design.md, "Write stage"), under the exclusive lock act() holds. A planned remux
    runs once and carries the flag edits of the tracks it keeps as mkvmerge options: resub() when subtitle work is
    planned, with the header issue of ctx.head in the same remux, else repack() for ctx.head. A file that is not
    writable gets no edits carried, so edit() gives it read_only, as before. After the remux the file is probed and
    planned again, see replan(), and edit() makes what that plan still asks for: mkvmerge's statistics tags or a
    removed track can change it. rec["replan_edits"] counts those edits. With no remux planned, or when the remux
    fails or cannot run, edit() makes the edits of the decision without the garbled tracks the remux takes out, as
    before the remux was planned, and a dry run records them. After a failed remux, edit() keeps the flags of a file
    that a flag edit would enclose bytes in, see edit(). Returns True when the app replaced the file of a deep analysis
    or a recheck during the remux, see deep_drop()."""
    rec, j, path, sub, head = ctx.rec, ctx.j, ctx.path, ctx.subplan, ctx.head
    replan_of = lambda after: muted_plan(replan(after, plan_inputs(ctx)), ctx.muted)
    edits = flag_edits(ctx, ctx.d, ctx.unmatched | set(ctx.strip))
    if not (sub or head):
        return edited(ctx, edit(rec, j, edits, ctx.apply, replan=replan_of, ts=ctx.d["tracks"], hp=ctx.stage))
    if ctx.waits:   # a remux of a film takes minutes, so an import that waits goes first
        runner.deep_waits()
    gone, ids = {*sub["drop"], *sub.get("strip", ()), *sub.get("covers", ())} if sub else set(), remux.track_ids(j)
    sel = {t["pos"]: t["sel"] for t in ctx.d["tracks"]}
    off = {sel.get(p) for p, r in ctx.recode.items() if r["default_off"]}   # the repair turns these off itself, see subtitle_checks()
    carried = [e for e in needed(j, edits) if ids.get(e[0]) not in gone and not (e[0] in off and decide.prop(e) == "flag-default" and not e[1])] \
        if ctx.apply and os.access(path, os.W_OK) else []
    if carried and subsync.INVARIANTS:
        mute_rule(path, ctx.d["tracks"], carried)
    # Either remux drops the bytes past the Segment end, so edit_blocked stops it, as another Matroska file there
    hp = ctx.stage if ctx.stage is not None else checks.header_of(path, j) or {}
    blocked = "; ".join(hp.get("edit_blocked") or [])
    if sub:
        code, result, info = ("subtitle_remux_skipped", f"subtitle remux skipped, {blocked}", {}) if blocked else \
            remux.resub(path, j, ctx.st, ctx.apply, **sub, **({"edits": carried} if carried else {}), **({"hp": head} if head else {}))
        done = code == "subtitles_remuxed"
        fixes, ends, remove, moves, kept, recode, strip, covers = ctx.fixes, ctx.ends, ctx.remove, ctx.moves, ctx.kept, ctx.recode, ctx.strip, ctx.covers
        timed = set(moves) - set(kept)
        rec["subremux"] = {**rec.get("subremux", {}), **dict(
            info, result=result, done=done, fixed=sorted(fixes), ended=sorted(ends), remove=remove, removed=remove if done else [],
            codes=[c for c, x in (("subtitle_retimed", fixes), ("subtitle_blocks_retimed", timed), ("subtitle_ends_lengthened", ends),
                                  ("subtitle_mismatch_removed", remove), ("subtitle_repaired", recode), ("subtitle_garbled_removed", strip),
                                  ("cover_attached", covers)) if x] if done else [code],
            **({"covers": sorted(e["place"] for e in covers.values())} if covers else {}),
            **({"copies": c} if (c := sorted(e["place"] for e in covers.values() if e.get("copy"))) else {}),
            **({"timed": sorted(timed)} if timed else {}), **({"recoded": sorted(recode)} if recode else {}),
            **({"stripped": {p: (info.get("garbled_text") or {}).get(str(i)) for p, i in zip(strip, sub["strip"])}} if strip else {}),
            **({"tracks_before": logs.track_log(ctx.d["tracks"])} if done else {}))}   # the places the check named, before a removal moves the tracks up
        if head:   # the same remux repairs the header, see resub()
            tail = head.get("tail") or head.get("edit_tail")
            hcode, hresult = (("tail_removed", "tail removed") if tail else ("header_repaired", "header repaired")) if done else \
                ({"would_remux_subtitles": "would_repair_header", "subtitle_remux_skipped": "header_repair_skipped"}.get(code, "header_repair_failed"), result)
            rec["header_repair"] = dict(issue=head["issue"], result=hresult, code=hcode, remux="subtitles")
    else:
        code, result, info = ("header_repair_skipped", f"header repair skipped, {blocked}: {'; '.join(head['issue'])}", {}) if blocked else \
            remux.repack(path, j, ctx.st, ctx.apply, head, **({"edits": carried} if carried else {}))
        done = code in config.REPAIRED
        rec["header_repair"] = dict(info, issue=head["issue"], result=result, code=code)
    if not done:
        if deep_drop(ctx):   # "the original changed", and the drop logs no stale edit
            return True
        if ctx.strip:   # the remux took no garbled track out, so the decision gives its flags as before the plan
            ctx.d = decide.decide(j, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong, ctx.unmatched)
            ctx.d = muted_plan(decide.with_tags(ctx.d, ctx.tags) if ctx.tags else ctx.d, ctx.muted)
        # After a failed remux, edit() keeps the flags of a file it would enclose bytes in (held), and after a failed
        # header repair it tries no second one (tried). repack() gives header_not_repaired only to a stopped one.
        mark = {"subtitle_remux_failed": "held", "header_repair_failed": "tried", "header_not_repaired": "tried"}.get(code)
        stage = dict(hp, **{mark: result}) if mark else hp
        return edited(ctx, edit(ctx.rec, j, flag_edits(ctx, ctx.d, ctx.unmatched), ctx.apply, replan=replan_of, ts=ctx.d["tracks"], hp=stage))
    # The original is kept a while, so its record is on disk before anything else runs. It names the undo of the edits it carried.
    logs.log(dict(rec, outcome=code, result=result, **({"undo": ["mkvpropedit", path] + propedit_args(carried, 2)} if carried else {})))
    checks.lid_carry(path, ctx.st)   # the proof shows the same audio, so the words the checks heard move to the new file
    refresh(ctx, duration=bool(head))   # a subtitle remux alone keeps the record's duration
    if sub:
        gone = {*ctx.remove, *ctx.strip}   # the tracks after a removed one move up one place, and a track that stays keeps its mismatch
        ctx.read, ctx.wrong, ctx.heard = subtitles.renumber(ctx.read, gone), subtitles.renumber(ctx.wrong, gone), subtitles.renumber(ctx.heard, gone)
        ctx.checked = subtitles.renumber(ctx.checked, gone)
        ctx.unmatched = set(subtitles.renumber(dict.fromkeys(ctx.unmatched), gone))
        ctx.stay = [p for p in ctx.stay if p not in rec["subremux"]["removed"]]
        if ctx.hp and ctx.hp["issue"] and not head:   # mkvmerge wrote a new header, and a retime can end a subtitle overrun
            now = checks.header_of(path, ctx.j)
            ctx.ended, ctx.hp = bool(now and not now["issue"]), now or ctx.hp   # its places are of the new file, see write_findings()
    # The plan of the new file as it reads with no edit carried, as after a remux in 2.18.3. Its edits are the change
    # from the original. mkvpropedit makes those the remux did not, and takes back a carried edit the plan no longer asks for.
    probe = unedited(ctx.j, j, carried) if carried else ctx.j
    ctx.tags = decide.retag(probe, ctx.got, ctx.known, checks.langs(), ctx.said, ctx.read, ctx.content_lang, ctx.checked)
    ctx.d = muted_plan(decide.with_tags(decide.decide(probe, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong, ctx.unmatched),
                                        ctx.tags), ctx.muted)
    want = flag_edits(ctx, ctx.d, ctx.unmatched)
    asked = {(e[0], decide.prop(e)) for e in want}
    left = [[e[0], e[2], e[1], *e[3:]] for e in carried if (e[0], decide.prop(e)) not in asked] + decide.unapplied(ctx.j, want)
    out = edit(ctx.rec, ctx.j, left, True, replan=replan_of, ts=ctx.d["tracks"])
    out["replan_edits"] = len(out.get("edits") or [])
    if carried and not want and out["outcome"] == "edited":   # the replan took back every carried edit, so the flags are the original's
        out = dict({k: v for k, v in out.items() if k not in ("before", "edits", "undo", "after", "recheck")}, outcome="no_change", result="no change")
        with contextlib.suppress(sqlite3.Error):   # the edit of the take-back ended, see checks.hook_edit_failed()
            store.drop("editing", path)
    elif carried:   # the record names the change from the original, as for a flag edit
        out.update(before=runner.flags(probe, ctx.original), edits=want, undo=["mkvpropedit", path] + propedit_args(want, 2))
        if out["outcome"] == "no_change" and want:
            out.update(outcome="edited", result="edited", after=runner.flags(ctx.j, ctx.original))
            try:
                out["recheck"] = rechecked(replan_of(ctx.j))
            except Exception as ex:
                out["recheck"] = {"error": config.mask(f"{type(ex).__name__}: {ex}")[:200]}
    edited(ctx, out)


def edited(ctx, rec):
    """Hand on rec, the record of edit(), with the decision record, see decision_fields(). A repair in edit() replaces
    the file, so ctx then takes its probe and stat, see refresh(). The content checks and the quick check read them. A
    file the app removed or replaced keeps the old stat, so those checks skip it, as before."""
    was, ctx.rec = ctx.rec.get("header_repair"), rec
    if rec.get("header_repair") is not was and rec["header_repair"]["code"] in config.REPAIRED:   # a repair of edit()'s own
        st, ctx.st = ctx.st, None   # edit() moved the mark of a burn-in job already, see runner.mute_mark()
        try:
            refresh(ctx)
        except OSError:   # the app removed the file
            ctx.st = st
    decision_fields(ctx)


def unedited(new, old, edits):
    """new, the probe of the file a remux wrote with edits, as it reads with no edit carried. Each property an edit set
    takes its value in old, the probe of the original, again, on the tracks the edits name, by UID."""
    uids = {t["uid"] for t in decide.classify(old) if t["sel"] in {e[0] for e in edits}}
    keys = {k for e in edits for k in (remux.EDIT_OPTS[decide.prop(e)][1], *(["language"] if decide.prop(e) in (decide.LANG_EDIT, decide.LANG_IETF) else []))}
    was = {(t.get("properties") or {}).get("uid"): t.get("properties") or {} for t in old.get("tracks") or []}
    back = lambda p: {**{k: v for k, v in p.items() if k not in keys}, **{k: was[p["uid"]][k] for k in keys if k in was[p["uid"]]}} \
        if p.get("uid") in uids and p.get("uid") in was else p
    return dict(new, tracks=[dict(t, properties=back(t.get("properties") or {})) for t in new.get("tracks") or []])


def sub_outcome(rec):
    """(outcome, result) of a run whose subtitle step changed a file and no flag, or None when it changed none. The
    outcome is subtitles_remuxed after a remux, else sidecars_changed. The result names what happened to each track and
    sidecar, as "subtitles remuxed: s2 retimed, s1 lines moved, s3 repaired, Film.en.srt retimed". A remux that moved
    only a cover picture says "remuxed". A partial shift of the speech layout adds how many lines kept their times, see
    subsync.shifted()."""
    rm = rec.get("subremux") or {}
    rm = dict(rm, moved=[p for p in rm.get("covers") or [] if p not in (rm.get("copies") or [])])
    did = (("fixed", "retimed"), ("timed", "lines moved"), ("ended", "ends lengthened"), ("removed", "removed"), ("recoded", "repaired"),
           ("stripped", "taken out as garbled"), ("moved", "cover picture to an attachment"), ("copies", "removed as a copy of a cover picture")) \
        if rm.get("done") else ()
    kept = lambda p: ((rec.get("subtime") or {}).get(p) or {}).get("timing", {}) or {}   # a partial shift names the lines it kept
    left = lambda p: f', {n} lines kept their times' if (n := kept(p).get("kept")) else ""
    what = [f"{p} {x}" + (left(p) if k == "fixed" else "") for k, x in did for p in rm.get(k) or []] + \
        [f'{e["name"]} {e["result"]}' + (left(e["name"]) if e["result"] == "retimed" else "") for e in rec.get("sidecars") or []
         if e.get("result") in ("retimed", "moved", "renamed", "repaired")]
    head = "subtitles remuxed" if any(rm.get(k) for k, _ in did if k not in ("moved", "copies")) or not rm.get("covers") else "remuxed"
    code, head = ("subtitles_remuxed", head) if rm.get("done") else ("sidecars_changed", "sidecars changed")
    return (code, f"{head}: " + ", ".join(what)) if what else None


def quiet_result(rec, apply, source):
    """The result of a run of rec that changed no flag, when another step writes the file or a sidecar, else None. A dry
    run that plans a write says "dry run", see wrote(). A sidecar fix counts only where --apply makes it, see
    subtitles.sub_fixes(). An applied header repair gives its own result, as "header repaired". sub_outcome() names an
    applied subtitle remux and a sidecar change first."""
    if not apply:
        side = subtitles.sub_fixes(source) and any(e.get("result") == "dry run" for e in rec.get("sidecars") or [])
        return "dry run" if wrote(rec) or side else None
    hr = rec.get("header_repair") or {}
    return hr["result"] if hr.get("code") in config.REPAIRED else None


def after_edit(ctx):
    """The rescan after a change, the sidecars, the subtitle cache and alerts, then the lock goes, see sidecar_fix()."""
    rec, d = ctx.rec, ctx.d
    # After the edit, so the app's disk scan never probes a header mkvpropedit is rewriting. A backfill sends one rescan
    # per item at the end of its run, because Sonarr's rescan of a large series is slow.
    rescan_now = lambda: "none, no app lists the file" if ctx.check == "outside" else \
        apps.ARR[ctx.app].rescan((ctx.ids or {}).get("app_id")) if ctx.mode == "import" or ctx.background else "after the run"
    # One rescan after the last write that replaced the file. Each part of the record that names a remux holds what it gave.
    # A job whose stopped run wrote asks for it too, see stop_line(). The record then holds it.
    parts = rescanned(ctx) or ([rec] if (ctx.job or {}).get("rescan") else [])
    said = rescan_now() if parts else None
    for x in parts:
        x["rescan"] = said
    if ctx.acts and ctx.mkv and not (ctx.certain or ctx.vcertain):   # the sidecars beside the file, under the lock of the edit
        rec["sidecars"] = subtitles.sidecar_fix({n: ctx.sides[n] for n in ctx.acts}, ctx.sync_all, ctx.apply, ctx.app, ctx.source, ctx.side_ends,
                                                **({"timed": ctx.side_moves} if ctx.side_moves else {}))
    if rec["outcome"] == "no_change" and (named := sub_outcome(rec)):   # the subtitle step was the change, so the outcome names it
        rec.update(outcome=named[0], result=named[1])
    elif rec["outcome"] == "no_change" and (said := quiet_result(rec, ctx.apply, ctx.source)):   # the outcome stays, so every count stays
        rec["result"] = said
    found = subtitles.sub_findings(rec, ctx.sync_all, ctx.stay, ctx.starts)
    rm = rec.get("subremux") or {}
    # A cover move that failed or could not run. With subtitle fixes, the alert that says why the rewrite failed takes
    # one more sentence for it, so the run posts once. Only a sentence of a subtitle fix in this remux holds its result.
    if rm.get("covers") and not rm["done"] and ctx.apply:
        host = next((f for f in found if any(x.get("result") for x in f.get("lines") or []) and report.posts(f, rec)), None)
        if host:
            host["lines"].append({"code": "cover", "tracks": rm["covers"], "also": True})
        else:
            held = [(rec.get("covers") or {}).get(p) or {} for p in rm["covers"]]
            same = list(dict.fromkeys(e.get("name") for e in held)) if all(e.get("there") for e in held) else []
            found.append({"kind": "cover", "tracks": rm["covers"], "result": rm["result"], "name": held[0].get("name"), **({"same": same} if same else {})})
    remux = next((f for f in found if f["kind"] == "unreadable"), None)
    if remux and (rec.get("repack") or {}).get("new_size"):   # the run converted the file first, so one notice names both
        found.remove(remux)
        if ctx.places:   # the number in the original of each track it took out, see report.unreadable()
            remux["places"] = ctx.places
        if ctx.unreadable:   # in rest already, see faults()
            ctx.unreadable["remux"] = remux
        else:
            ctx.rest.append(report.unreadable_finding({}, rec["repack"].get("kept"), rec["container"], remux=remux))
    ctx.rest += found
    if ctx.mkv and ctx.subs_on and checks.lid_ready():   # a backfill with --sub-check skips the file while it stays as it is and needs nothing more
        planned = ctx.fixes or ctx.remove or ctx.ends or ctx.moves or ctx.recode or ctx.strip
        done = ctx.apply and rec["outcome"] in ("edited", "no_change", "subtitles_remuxed", "sidecars_changed") and (rec.get("subremux") or {}).get("done", not planned) \
            and all(e["result"] in ("moved", "retimed", "renamed", "repaired") for e in rec.get("sidecars") or [])
        # A repaired track whose times or words this run left out stays pending, so the next run judges its readable text.
        # So does a track whose whole-file hearing stopped part way, see subtitles.sub_whole(). Nothing queues a new
        # run, so a later --sub-time or deep analysis times it.
        stopped = any(e.get("stopped") for e in (rec.get("whole") or {}).values())
        # At SUBTITLES check a fix the check found stays unmade: new times, a repair, or a track that does not match the
        # audio. The result stays pending, so a later --sub-check --apply makes it. A dry run plans its fixes, so planned
        # counts them. A check that could not read what it judges, as a failed speech read, stays pending too.
        found = subtitles.sub_found(rec)
        unmade = not subtitles.sub_fixes(ctx.source) and (any(r.get("verdict") == "mismatch" for r in ctx.sync_all.values())
                                                          or any({"fix", "unfixable"} & set(f) for f in found.values()))
        unmade = unmade or any("unread" in f for f in found.values())
        # A flag edit that a dry run plans or that failed asks for an action too, so the next apply still takes the file,
        # see sub_cached(). A hard link or a read-only file refuses every edit, so it asks for none.
        flags = bool(rec.get("edits")) and (not ctx.apply or rec["outcome"] in ("edit_failed", "verify_failed"))
        subtitles.sub_cache(ctx.path, rec, ctx.depth, ctx.app, bool(planned or ctx.acts) and not done or flags or bool(ctx.later) or stopped or unmade)
    if rec["outcome"] in ("verify_failed", "edit_failed"):
        ctx.rest.append({"kind": "edit", "error": short_error(rec["result"], rec["path"]), "unread": rec.get("after_error"),
                         "on": None if "after" not in rec else [f'{t["pos"]} {t["lang"]}' for t in rec["after"] if t["default"]]})
    if ctx.lock is not None:   # the edit is done, and the checks below only read the file
        fcntl.flock(ctx.lock, fcntl.LOCK_UN)
    # A stopped run of the job left the extras of its conversion hidden, see stop_line(). This run settles them.
    pending = (rec.get("repack") or {}).get("pending")
    keys = [pending] if pending else convert.left_converted(ctx.app, ctx.path) if ctx.mode == "import" and ctx.apply else []
    if keys and (ctx.mode == "import" or ctx.background):   # two waited rescans, see settle()
        config.DEADLINE.stop()
        (rec["repack"] if pending else rec)["settle"] = convert.settle_extras(ctx.app, (ctx.ids or {}).get("app_id"), keys)


def short_error(text, path, limit=150):
    """text cut to limit characters. The file's path in it becomes its name first, cut as far as the limit needs, so the
    reason after the name stays."""
    name, room = os.path.basename(path), limit - len(text) + len(path)
    if path and path in text and room > 1:
        text = text.replace(path, name if len(name) <= room else name[:room - 1] + "…")
    return text[:limit]


def content_checks(ctx):
    """The metadata checks after the edit, and the re-grab of wrong content, see metadata(). The episode title comes
    from the NFO of the download folder, else from the NFO the app copied beside the video, see library_nfo_title()."""
    rec, job = ctx.rec, ctx.job
    ctx.meta = ctx.until = None   # until: the end of their limit, which quick_check() takes over
    # A re-grab may have deleted the file, or put a same-name old file back.
    if not ctx.background and os.path.exists(ctx.path) and os.stat(ctx.path).st_ino == ctx.st.st_ino:
        ctx.rearm()   # the checks get their own limit. The edit is done, so a timeout costs only the checks.
        try:
            if ctx.check and not ctx.app:   # no item to judge the content against. The finally still hands on the time left.
                return
            episode = ctx.app in apps.ARR and not apps.ARR[ctx.app].film   # metadata() reads an NFO title for an episode only
            nfo = (job or {}).get("nfo_title") or (library_nfo_title(ctx.path) if episode else None)
            meta = ctx.meta = metadata(ctx.app, ctx.path, ctx.j, ctx.size, ctx.d, ctx.original, ctx.release, ctx.item, asked=ctx.asked, nfo=nfo)
            rec.update(trusted=meta["trusted"], expected=meta["expected"], other_film=meta["other"], evidence=meta["evidence"],
                       tmdb=meta["tmdb"])
            ev = meta["evidence"]
            if ctx.post and (k := tmdb_key_alert(ctx.app, meta["tmdb"])):
                rec["key_alert"] = k
            if ev["regrab"] and ctx.mode == "import" and (ctx.apply or ctx.check) and not (ctx.certain or ctx.vcertain):
                if ctx.lock is not None and not ctx.check:   # a re-grab deletes files, so it waits for an edit in progress. --check deletes none.
                    # A backfill or the subtitle hunter can hold the lock far past BUDGET, so the wait has LOCK_WAIT.
                    wait = content.Deadline(config.LOCK_WAIT, f"stopped after {config.BUDGET} seconds")
                    if ctx.shared:   # and for the older jobs of its download, as the other exclusive steps do
                        ctx.shared.turn()
                    runner.gated(ctx.lock, fcntl.LOCK_EX, wait)
                    ctx.rearm()
                action = regrab.regrab(ctx.app, job, "wrong content", regrab.content_probe(ctx.app, job["owner"], ctx.original, ctx.kids,
                                                                                           ctx.release, ctx.item, ctx.heard), "content")
                rec["outcome"] = config.CONTENT_CODES[action["code"]]
                rec["edit_result"], rec["result"], rec["regrab"] = rec["result"], f'{report.VERDICTS[rec["outcome"]]}: {ev["why"]}', action["code"]
                ctx.first.append(dict(content_finding(ev), action=action))
            elif ev["regrab"]:
                ctx.first.append(content_finding(ev))
        except Exception as ex:   # the edit is done, so a failure or the time limit costs only the checks
            rec["meta_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:200]
        finally:
            ctx.until = config.DEADLINE.end
            config.DEADLINE.stop()
    if ctx.shared:   # every re-grab of this job is behind it
        ctx.shared.settle()


def burn_audio(ts, edits=()):
    """{"index": its ffmpeg audio index, "pos", "lang"} of the audio track that plays first, see decide.default_audio(),
    for the burned-in subtitle check, or None for a file with no audio. ts is decide.classify() of the file. lang is the
    track's tag, so a language the import only heard never turns subtitles off, see runner.burn_plan()."""
    play = decide.default_audio(ts, edits)
    return play and {"index": [t for t in ts if t["kind"] == "a"].index(play), "pos": play["pos"], "lang": play["tag"]}


def quick_check(ctx):
    """The quick burned-in subtitle check of an import whose file has a video track, in any container (docs/design.md,
    "Burned-in subtitles"), see burnin.quick(). It runs after the import's own checks and edits, on the audio that plays
    first. It only sorts the file and changes nothing. clean ends there. flagged and unsure queue the full check in the
    background, see runner.queue_burn_in(). It gets the time the metadata checks left of their limit, see
    content_checks(), and the run of burnin.py ends at that limit. With under QUICK_SECS left, or when the check fails,
    the file is unsure. The decision line keeps the result in "burned_in". A file whose probe holds no track, as an ASF
    or WMV file, or no duration, as an AVI file, gets them from ffprobe inside that limit, see burn_probe()."""
    rec, tracks = ctx.rec, ctx.j.get("tracks")
    if ctx.mode != "import" or config.CFG.burned_in == "off" or (tracks and not any(t.get("type") == "video" for t in tracks)):
        return
    try:   # a re-grab may have deleted the file, or put a same-name old file back
        if os.stat(ctx.path).st_ino != ctx.st.st_ino:
            return
    except OSError:
        return
    edited = "edited" in (rec.get("outcome"), rec.get("edit_result"))
    audio, why = burn_audio(ctx.d["tracks"], ctx.d["edits"] if edited else ()) if tracks else {}, burn_ready()   # {}: ffprobe tells below
    if audio is None or why:
        rec["burned_in"] = {"result": "skipped", "why": why or "the file has no audio track"}
        return
    left = ctx.until - time.monotonic() if ctx.until else 0
    if left < config.QUICK_SECS:
        rec["burned_in"] = dict(result="unsure", why=f"{max(left, 0):.0f} seconds of the time limit were left, and the quick check needs "
                                                     f"{config.QUICK_SECS}", **({"audio": audio} if audio else {}))
        return
    config.DEADLINE.start(left)   # the ffprobe of burn_probe() ends at the import's limit too
    try:
        ts, video, duration = burn_probe(ctx.path, ctx.j)
        audio = audio or burn_audio(ts)
        if not (video and audio):   # an ASF or WMV file, whose tracks only ffprobe reads
            rec["burned_in"] = {"result": "skipped", "why": f'the file has no {"audio" if video else "video"} track'}
            return
        rec["burned_in"] = dict(burn_run(ctx.path, audio["index"], duration, ["--quick"], config.DEADLINE.left()), audio=audio)
    except Exception as ex:
        rec["burned_in"] = dict(result="unsure", why=config.mask(f"the quick check failed: {type(ex).__name__}: {ex}")[:200],
                                **({"audio": audio} if audio else {}))
    finally:
        config.DEADLINE.stop()


def alerts(ctx):
    """The findings of the run in the record, and their alerts, posted unless post is False. The deep analysis and a
    recheck alert on the subtitles only, because the import alerted on the rest. The subtitles they took out are among
    them, see report.unreadable_finding(). An import whose file gets the deep
    analysis holds the subtitle alerts in held, because that analysis checks them again and posts what it still finds,
    see logs.alert_findings() and runner.queue_deep_analysis()."""
    rec = ctx.rec
    try:
        found = file_alerts(ctx.d, ctx.file_checks, ctx.meta, ctx.original, ctx.item)
        if ctx.check and not ctx.app:   # with no original language, audio in another language than English proves nothing
            found = [f for f in found if f["kind"] != "language"]
        problems = ctx.first + found + ctx.rest
    except Exception as ex:   # a finding must never cost the decision line or the Plex analyze of an edited file
        rec["alert_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:200]
        problems = ctx.first + ctx.rest
    rec["findings"] = [p for p in problems if not ctx.background or p["kind"] in (*logs.DEEP_KINDS, "unreadable", "cover")]
    rec["alert_kinds"] = [p["kind"] for p in rec["findings"]]
    if ctx.post:   # after the edit, so a failed post never blocks one
        ctx.held = [] if ctx.mode == "import" and runner.deep_wanted(rec) else None
        # The marker of each alert keys on the size of the file now, after any write, so a later run over it posts no alert twice
        size = os.path.getsize(ctx.path) if os.path.exists(ctx.path) else ctx.size
        rec["alert_result"] = logs.alert_findings(rec, size, ctx.held)


STEPS = (start, conversion, header, languages, subtitle_checks, flag_hearing, deep_drop, decision_fields, faults, act, after_edit, content_checks,
         quick_check, alerts)   # process() runs them in this order


def content_finding(ev):
    """The wrong-content finding: the reason and the kind of each signal that scored, and the points. report.posts()
    reads the kinds."""
    scored = [s for s in ev["signals"] if s["points"]]
    return {"kind": "content", "signals": [s["why"] for s in scored], "scored": [s["kind"] for s in scored], "points": ev["points"]}


def file_alerts(d, file_checks, meta, original, item):
    """The language, duration and runtime findings of one file. The metadata checks decide them when they ran. The
    language alert fires on a wrong verdict, or on the decision's wrong_language when TMDB cannot tell. The runtime
    alert needs a trusted duration. Without the checks, decide.checks() decides as before them."""
    ev = (meta or {}).get("evidence")
    verdict = {s["kind"]: s["verdict"] for s in (ev or {}).get("signals", [])}
    out = []
    if verdict.get("language") == "wrong" or (verdict.get("language", "unknown") == "unknown" and d["wrong_language"]):
        want = "English" if "eng" in decide.codes(original) else f"English or {original or 'the original language'}"
        out.append({"kind": "language", "want": want, "has": [t["lang"] for t in d["tracks"] if t["kind"] == "a"]})
    duration = [dict(f, kind=k) for k, f in file_checks if k == "duration"]
    header = content.header_alert(meta["trusted"]) if meta else None
    out += duration or ([{"kind": "duration", "why": header}] if header else [])
    if ev is None:
        out += [dict(f, kind=k) for k, f in file_checks if k == "runtime"]
    elif verdict.get("runtime") in ("short", "long"):
        listed = item.get("listed") or 0
        out.append({"kind": "runtime", "runs": content.hms(meta["trusted"]["seconds"]), "listed": max(listed) if isinstance(listed, list) else listed})
    out += [{"kind": "episode", **{k: s[k] for k in ("imported", "said", "title", "names")}} for s in (ev or {}).get("signals", [])
            if s["kind"] == "episode_title" and s["verdict"] == "other"]
    return out


def plan_inputs(ctx):
    """The inputs of the decision of ctx's file as JSON, so a later run plans the file as this run did, see replan(). A
    burn-in job keeps them, see runner.queue_burn_in()."""
    return dict(original=ctx.original, kids=ctx.kids, release=ctx.release, heard=ctx.heard, spoken=ctx.spoken, wrong=ctx.wrong,
                unmatched=sorted(ctx.unmatched), got=ctx.got, known=sorted(ctx.known), said=sorted(ctx.said), read=ctx.read,
                content_lang=ctx.content_lang, checked=ctx.checked)


def replan(after, p):
    """The decision and the tag edits of after, the probe of an edited file, from p, the plan_inputs() of the run that
    planned it. The import's check after its edit and a burn-in job's both use it."""
    return decide.with_tags(decide.decide(after, p["original"], p["kids"], p["release"], p["heard"], p["spoken"], p["wrong"], set(p["unmatched"])),
                            decide.retag(after, p["got"], set(p["known"]), checks.langs(), set(p["said"]), p["read"], p["content_lang"], p["checked"]))


def muted_edits(edits, ts):
    """edits less those that turn on an English subtitle track of ts, decide.classify() of the file, for a file whose
    English subtitles a burn-in job turned off, see runner.muted()."""
    english = {t["sel"] for t in ts if t["kind"] == "s" and "eng" in decide.codes(t["lang"])}
    return [e for e in edits if not (e[0] in english and decide.prop(e) in ("flag-default", decide.FORCED_FLAG) and e[1])]


KEPT_OFF = "English subtitles kept off by the burned-in subtitle check"   # the last rule of a plan muted_plan() changed


def muted_plan(plan, muted=True):
    """plan, a decision of a file, less the edits muted_edits() leaves out when muted. When it leaves one out, the edit
    rules name only the edits that are left, and the rules end with KEPT_OFF, so the class says why, see
    decide.plan_class()."""
    kept = muted_edits(plan["edits"], plan["tracks"]) if muted else plan["edits"]
    if kept == plan["edits"]:
        return plan
    rules = [r for e, r in zip(plan["edits"], plan["edit_rules"]) if e in kept]
    return dict(plan, edits=kept, edit_rules=rules, rules=sorted(set(rules)) + [KEPT_OFF])


def mute_rule(path, ts, edits):
    """The safety rule of the burned-in subtitle mute, see subsync.INVARIANTS and docs/development.md, "Safety
    self-checks". decide.invariants() keeps the only English subtitles on under audio in another language. A burn-in job
    is the one exception: it turns them off and marks the file, see runner.burn_act() and runner.muted(). So an edit that
    turns them off needs the mark, and an edit of a marked file never turns an English subtitle on. ts is the tracks of
    the plan, as decide.decide() or decide.classify() gives them. Raises subsync.Broken."""
    mark, final = runner.muted(path), {t["sel"]: t["default"] for t in ts} | decide.defaults(edits)
    a, english = decide.default_audio(ts, edits), [t for t in ts if t["kind"] == "s" and t["lang"] == "eng" and not t["extra"]]
    if mark and muted_edits(edits, ts) != edits:
        raise subsync.Broken("rule muted broken: the edit turns on English subtitles that a burn-in job turned off")
    if a and a["lang"] != "eng" and any(t["default"] for t in english) and not any(final[t["sel"]] for t in english) and not mark:
        raise subsync.Broken(f'rule only English subtitles off broken: the edit turns off the only English subtitles under {a["lang"]} '
                             "audio, and no burn-in job marked the file")


def propedit_args(edits, i):
    """mkvpropedit arguments that set the property of each edit (flag-default, flag-forced, flag-original, language or
    language-ietf) to its new (i=1) or old (i=2) value. None deletes the property: a track that had no BCP 47 tag or no
    Original language flag gets none back on undo."""
    return [a for e in edits for a in (("--edit", e[0], "--delete", decide.prop(e)) if e[i] is None
                                       else ("--edit", e[0], "--set", f"{decide.prop(e)}={e[i]}"))]


def needed(j, edits):
    """edits less the flag edits that j, the probe of the file, shows already, so a flag edit never runs as a no-op."""
    return [e for e in edits if decide.prop(e) not in ("flag-default", decide.FORCED_FLAG, decide.ORIGINAL_FLAG) or decide.unapplied(j, [e])]


def rechecked(p):
    """rec["recheck"] of the plan p of an edited file: its edit count and the invariants of the new state."""
    return {"edits": len(p["edits"]), "undecided": p.get("abstain"), "invariants": [c for c, _ in decide.invariants(p["tracks"], [], p["cls"], set(p["orig"]))]}


def edit(rec, j, edits, apply, replan=None, ts=None, hp=None):
    """mkvpropedit the planned flags and verify them with a second probe. Returns the record with the result.
    replan(after) plans the edited file again. Its edit count and the invariants of the new state go in "recheck",
    which the audit reads instead of probing the file again. ts is the tracks of the plan, for mute_rule().

    With HEADER_REPAIR on, two guards keep the Matroska layout whole (docs/design.md, "Write stage"). hp is the header
    stage of the file as it is, see header(), else header_of() reads it. A file whose bytes past the Segment end
    (edit_tail) or zero run in it (enclosed) a flag edit would put inside the Segment takes the edits in one repack()
    in place of mkvpropedit. The header check of such a file reads the stream ends and edit_blocked, and the video
    check looks for real damage first, as before any header repair. When that remux must not run (edit_blocked, a block
    of the video check), or another remux of the file failed in this run (hp["held"], hp["tried"]), nothing is
    written: the outcome is edit_failed, and the flags stay. A stop before the write gives the same, see
    runner.refuse(). After mkvpropedit, a header layout that breaks the Matroska rules, as when mkvpropedit moves
    Tracks past the Clusters, is repaired at once, see relayout(). That runs after no_stop() ends, so SIGTERM removes
    the temp file of the remux and leaves the edited file. header_repair records either remux."""
    edits = needed(j, edits)
    if not edits:
        return dict(rec, outcome="no_change", result="no change")
    path = rec["path"]
    rec = dict(rec, before=runner.flags(j, rec.get("original")), edits=edits, undo=["mkvpropedit", path] + propedit_args(edits, 2))
    before = os.stat(path)
    if vault.links(before) > 1:   # a hardlink shares the edit with the download client's copy
        return dict(rec, outcome="hardlinked", result="hardlinked, not edited")
    if not apply:
        return dict(rec, outcome="dry_run", result="dry run")
    if not os.access(path, os.W_OK):
        return dict(rec, outcome="read_only", result="read-only, not edited")
    if subsync.INVARIANTS:
        mute_rule(path, ts or decide.classify(j), edits)
    hp = (hp if hp is not None else checks.header_of(path, j) if config.CFG.header_repair else None) or {}
    remuxed = config.CFG.header_repair and bool(hp.get("edit_tail") or hp.get("enclosed") is not None)
    if remuxed:   # fix 2
        why, room = hp.get("tried") or hp.get("held"), f'the {hp["edit_tail"]} bytes past its end' if hp.get("edit_tail") else "a run of zero bytes"
        if not why and "repairable" not in hp:   # the video check looks for real damage first, as for a header issue
            issue = hp["issue"] or [f"a flag edit would put {room} inside the Matroska Segment"]
            hp = checks.video_check(path, j=j, hp=dict(hp, issue=issue))[2].get("header") or dict(hp, issue=issue, repairable=False)
        why = why or "; ".join(hp.get("edit_blocked") or []) or (None if hp.get("repairable") else "; ".join(hp.get("blocked") or ["the video check failed"]))
        if not why:
            code, result, info = remux.repack(path, j, before, True, hp, edits)
            rec["header_repair"] = dict(info, issue=hp["issue"], result=result, code=code)
            why = None if code in config.REPAIRED else result
        if why:   # one alert, the edit finding, names the repair that failed too, see write_findings()
            return dict(rec, outcome="edit_failed", result=f"not edited, as a flag edit would put {room} inside the Matroska Segment: {why}",
                        after=rec["before"], **({"header_repair": dict(rec["header_repair"], edit=True)} if "header_repair" in rec else {}))   # nothing changed
        logs.log(dict(rec, outcome=code, result=result))   # the original is kept a while, so its record is on disk before anything else runs
    else:
        try:
            with runner.no_stop():   # neither the time limit nor SIGTERM cuts mkvpropedit, which rewrites the header in place
                config.DEADLINE.stop()
                logs.log(dict(rec, result="editing"))   # the undo record exists before the file changes
                e = subprocess.run(["mkvpropedit", path] + propedit_args(edits, 1), capture_output=True, text=True, errors="replace")
        except runner.Stopped as ex:   # a stop came before the edit, so nothing changed
            return dict(rec, outcome="edit_failed", result=f"not edited: {ex}", after=rec["before"])
    checks.lid_carry(path, before)
    runner.mute_mark(path, before)
    if not remuxed and e.returncode not in (0, 1):   # it may still have written. A run can fail on "Tracks" and still set the flags.
        rec.update(outcome="edit_failed", result="mkvpropedit failed: " + ((e.stdout + e.stderr).strip()[-300:] or f"exit code {e.returncode}"))
        try:
            rec["after"] = runner.flags(checks.mkvmerge(path), rec.get("original"))
        except Exception as ex:
            rec["after_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:300]
        return rec
    after = checks.mkvmerge(path)
    if config.CFG.header_repair and not remuxed and not hp.get("tried") and checks.layout_of(path):   # fix 1
        after = relayout(rec, path, after)
    rec["after"] = runner.flags(after, rec.get("original"))
    if decide.unapplied(after, edits):
        return dict(rec, outcome="verify_failed", result="VERIFY FAILED, flags did not change")
    rec = dict(rec, outcome="edited", result="edited")
    if replan:
        try:
            rec["recheck"] = rechecked(replan(after))
        except Exception as ex:
            rec["recheck"] = {"error": config.mask(f"{type(ex).__name__}: {ex}")[:200]}
    return rec


def relayout(rec, path, j):
    """Repair at once the header layout that the flag edit of rec broke, with a remux, see repack(). j is the probe of
    the edited file. The video check of the edited file looks for real damage first, as for any header repair, and a
    subtitle that runs past the end leaves the repair to the next header check, which decides its cut. Sets
    rec["header_repair"] and returns the probe of the file the run leaves."""
    st, hp = os.stat(path), checks.header_of(path, j)
    if not (hp and hp["issue"]):   # the probe failed
        return j
    h = {} if hp.get("trim") or hp.get("remove") else checks.video_check(path, j=j, hp=hp)[2].get("header") or {}
    try:   # a stop removes the temp file and keeps the edit, see Swap.stop(). The run then writes the line of the edit.
        code, result, info = remux.repack(path, j, st, True, h) if h.get("repairable") else (None, None, {})
    except (SystemExit, KeyboardInterrupt):   # no handler of the process's own, as in --check. It keeps the stop for its next write.
        code, result, info = "header_not_repaired", "not repaired: the run was stopped. The issue: " + "; ".join(hp["issue"]), {}
    if code is None:
        code, result, info = "header_not_repaired", "not repaired: " + "; ".join(h.get("blocked") or [
            "a subtitle runs past the end, so the next header check repairs it" if hp.get("trim") or hp.get("remove") else "the video check failed"]) + \
            ". The issue: " + "; ".join(hp["issue"]), {}
    rec["header_repair"] = dict(info, issue=hp["issue"], result=result, code=code)
    if code not in config.REPAIRED:
        return j
    logs.log(dict(rec, outcome=code, result=result))   # the original is kept a while, so its record is on disk before anything else runs
    checks.lid_carry(path, st)
    runner.mute_mark(path, st)
    return checks.mkvmerge(path)
