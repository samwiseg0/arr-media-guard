# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The checks and the flag edit of one file, process(), with the metadata checks it runs."""
import contextlib, dataclasses, fcntl, os, sqlite3, statistics, subprocess, time, uuid

from . import apps, checks, cli, config, content, convert, decide, logs, proof, regrab, remux, report, runner, subsync, subtitles, vault


def metadata(app, path, j, size, d, original, release, item, fresh=False, asked=None, nfo=None):
    """The checks of content.py on one probed file against its item, item from movie_item() or episode_item(). Returns
    {"trusted", "expected", "other", "evidence", "tmdb"}. A TMDB failure reads as unknown. TMDB's status goes to
    status.json only after a live answer, because a cache hit says nothing about the key. fresh is a second check
    before a re-grab, see tmdb_ask(). asked is the tmdb_ask() the decision already made, so TMDB is asked once. nfo is
    the episode title of the release's NFO, see release_nfo_title(). It wins over the title in the release name."""
    trusted = content.trusted_duration(j, size, content.last_packet(path))
    token, ids = config.CFG.tmdb_token or None, item.get("ids") or {}
    cache = "tmdb-recheck" if fresh else "tmdb"
    expected, t0 = asked or checks.tmdb_ask(app, item, fresh)
    code, why = content.tmdb_state(expected)
    if code not in ("found", "no_record") or content.DOWN.get("answered", 0) >= t0:
        logs.status("tmdb", code, why)
    kind = "episode" if config.program(app) == "sonarr" else "special" if (expected or {}).get("special") else "movie"
    name = release or os.path.basename(path)
    args = (d["tracks"], original, expected, trusted, item.get("listed") or 0, name, kind, item.get("year"), item.get("titles") or ())
    ev, other = content.wrong_content_evidence(*args, episode=episode_title(app, item, nfo, release, path) if kind == "episode" else None), None
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

    lock is the held file lock. It is released after the edit, because the metadata checks only read, and taken again
    before a wrong-content re-grab deletes anything. shared means lock is held shared, in a job process of coordinate()
    or a backfill worker, and the checks run under it. shared.exclusive(st) then takes the lock exclusive before a
    re-grab or an edit, and raises Replan when the file or its download changed meanwhile, see exclusive().
    shared.settle() tells younger jobs of the download that this one re-grabs nothing more. A conversion of an import
    builds the new file under the shared lock and takes the lock exclusive for its swap, see convert(). Everything after
    it runs under the exclusive lock. Any other repack raises Replan at once, so it and everything after it run under
    the exclusive lock. The language hearing runs without the lock, and shared.reshare(st) takes it shared again after
    it, see reshared().

    The steps set the state they hand on, such as rec, the decision log record, and the probe j, st and size, see
    refresh(). path follows a conversion's new name."""
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
        self.source = "recheck" if self.recheck else {"import": "hook", "deep": "deep_analysis"}.get(self.mode, "backfill")   # of the decision log
        self.background = self.mode == "deep" or bool(self.recheck)
        self.depth = self.recheck or self.mode

    def rearm(self):
        """Start the job's time limit again after a step turned it off. Only an import has one."""
        if self.mode == "import":
            config.DEADLINE.start(config.BUDGET)

def process(ctx):
    """Probe one file and decide its flags, see Ctx. Hear the audio language when the decision needs it, check the
    subtitles, check the audio and the video (import only), edit the flags when the rules say so, and verify. Then run
    the metadata checks and alert. A step that ends the run returns True. Returns the decision log record, which the
    caller writes with decision()."""
    for step in STEPS:
        if step(ctx):
            break
    return ctx.rec


def refresh(ctx, duration=True):
    """Probe the file at the start and again after a step replaced it: its stat, its size and mkvmerge -J. The record
    takes the new size, mtime and duration. duration False keeps the record's duration, as after a subtitle remux."""
    was, ctx.st = getattr(ctx, "st", None), os.stat(ctx.path)
    if was:   # a remux of AMG's own keeps the mark of a burn-in job, see runner.muted()
        runner.mute_mark(ctx.path, was)
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


def conversion(ctx):
    """The conversion into Matroska, see convert(). mkvpropedit edits Matroska only. A file that did not convert ends
    the run there. An import of a file that is not .mkv goes on with the original instead, so its audio and video checks
    still run. A convert run ends after the conversion too."""
    rec, j, st, job = ctx.rec, ctx.j, ctx.st, ctx.job
    container = (j.get("container") or {}).get("type") or "Matroska"   # a probe without a type reads as Matroska
    ctx.unconverted = ctx.damaged = None
    if not ((ctx.mkv and container != "Matroska") or (not ctx.mkv and ctx.conv)):
        return
    if ctx.shared and ctx.apply and not ctx.pool and ctx.mode != "import":
        raise runner.Replan("the file needs a repack")
    was = ctx.path
    got, result, rec["repack"], ctx.path = convert.convert(ctx.app, ctx.path, j, st, ctx.apply, rec["ids"], ctx.lock, ctx.pool, ctx.force,
                                                           ctx.subs_on and subtitles.sub_fixes(ctx.source), decide.codes(ctx.original),
                                                           shared=bool(ctx.shared or ctx.pool), source=ctx.source)
    path = ctx.path
    # A relink gives the file a new id: the new name, or the original that convert_undo() imported again. The new name
    # always replaces the old id. Its id is None when an undo found the new name listed but could not read the id.
    renamed = got == "repacked" and path != rec["path"]
    fid = (rec["repack"]["relink"] if renamed else ((rec["repack"].get("restored") or {}).get("import") or {})).get("file_id")
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
            if not os.path.exists(rec["path"]):   # exclusive() runs the job again, so the list keeps this run's result now
                convert.convert_list(ctx.app, dict(rec, outcome=got, result=result))
            ctx.shared.exclusive(st)
        ctx.shared = False
    elif ctx.lock is not None and not ctx.pool and got in ("repacked", "repack_failed", "repack_source_changed"):   # convert() let the lock go, see there
        runner.gated(ctx.lock, fcntl.LOCK_EX)
    if "warnings" in rec["repack"]:
        ctx.rearm()   # convert() ran the remux with the time limit off, whatever came of it
    rec["container"] = container
    if got == "repacked":
        if path != rec["path"]:   # the app lists the new name now, with the new file id above
            rec["path"], ctx.mkv = path, True
        logs.log(dict(rec, outcome="repacked", result="repacked"))   # a repack cannot be undone, so its record is on disk before anything else runs
        checks.lid_carry(path, st, was)   # the proof shows the same audio, so the subtitle check's words move to the new file
        if ctx.mode == "convert":   # the language backfill decides the flags later, from its own cache
            rec["repack"]["rescan"] = "after the run"
            ctx.rec = dict(rec, outcome="repacked", result="repacked", reasons=["repacked"], **{"class": "repacked"})
            return True
        refresh(ctx)
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
    if not would:
        convert.convert_list(ctx.app, rec)
    damage = rec["repack"].get("damage")
    if damage and ctx.mode == "import" and ctx.apply:   # the original is damaged: it re-grabs like broken audio
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
    """The header check of a Matroska file, and its repair, see header_of() and repack()."""
    rec, j, path = ctx.rec, ctx.j, ctx.path
    ctx.asked = checks.tmdb_ask(ctx.app, ctx.item)   # the forced-flag clear of an English original needs TMDB's spoken languages. metadata() reuses it.
    ctx.hp = ctx.vpre = None   # header_probe(), and the video check of a file with a header issue, which looks for real damage first
    if ctx.mkv and config.CFG.header_repair and ctx.video and not ctx.background:
        ctx.hp = checks.header_of(path, j)
    hp = ctx.hp
    if not (hp and hp["issue"]):
        return
    cut = None
    if hp.get("trim") or hp.get("remove"):   # the runtime decides a removal, and whether the file may be cut
        listed = ctx.runtime or (ctx.app in apps.ARR and apps.ARR[ctx.app].film and (ctx.asked[0] or {}).get("runtime")) or 0   # minutes, the app's else TMDB's
        trim, remove, cut = decide.subtitle_plan(hp["trim"], hp["remove"], hp["subtitles"], hp["streams"], listed)
        hp = ctx.hp = dict(hp, trim=trim, remove=remove, listed=listed)
    if ctx.shared and ctx.apply and not hp["blocked"] and not cut and not remux.repack_skip(path, ctx.st):
        raise runner.Replan("the file needs a header repair")
    ctx.vpre = checks.video_check(path, j=j, hp=hp)
    h = ctx.vpre[2].get("header") or {}
    if cut and h.get("repairable"):   # the subtitles of a cut file run to the full length and are good: keep the file
        code, result, info = "subtitle_file_may_be_cut", f"not repaired, the file may be cut: {cut}", {"listed": hp["listed"], "streams": h["streams"]}
    elif h.get("repairable"):
        code, result, info = remux.repack(path, j, ctx.st, ctx.apply, h)
        if "warnings" in info:
            ctx.rearm()   # repack() ran mkvmerge with the time limit off, whatever came of it
    elif hp.get("unfixable"):
        code, result, info = "subtitle_overrun_unfixable", "not repaired, a subtitle runs past the end: " + "; ".join(h.get("blocked") or []), {}
    else:
        code, result, info = "header_not_repaired", "not repaired: " + "; ".join(
            h.get("blocked") or [f'the video check failed: {ctx.vpre[2].get("error")}']), {}
    rec["header_repair"] = dict(info, issue=hp["issue"], result=result, code=code)
    if code in config.REPAIRED:
        logs.log(dict(rec, outcome=code, result=result))   # the original is kept a while, so its record is on disk before anything else runs
        refresh(ctx)


def languages(ctx):
    """The decision, with the languages the hearing and the subtitle text give, see hear() and retag()."""
    rec, j, path, mkv = ctx.rec, ctx.j, ctx.path, ctx.mkv
    spoken = ctx.spoken = checks.spoken_of(ctx.asked[0])
    d = decide.decide(j, ctx.original, ctx.kids, ctx.release, spoken=spoken) if mkv else {"edits": [], "notes": [], "reasons": [],
                                                                                         "wrong_language": False, "tracks": decide.classify(j)}
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
        if ctx.shared:   # a hearing waits for the host's one model, so it runs without the file lock and an edit never queues behind it
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
    """The subtitle match check, the reference timing and the flash check, and the one remux they ask for, see
    resub(). A track that does not match leaves the file, and a track whose times need a fix gets them. --sub-time and
    the deep analysis hear the whole file and time every line of each track that matched, see subtitles.sub_whole().
    When the remux fails, a track that does not match loses its flags instead. Sidecars beside the file are checked
    too, see sidecar_fix(). The decision then counts each track that does not match."""
    rec, j, d, path, mkv, subs_on = ctx.rec, ctx.j, ctx.d, ctx.path, ctx.mkv, ctx.subs_on
    timing = ctx.depth in ("sub_time", "deep")   # the reference timing and the whole-file timing
    sync, unmatched, fixes, sides = {}, set(), {}, {}   # the subtitle match check, docs/design.md, "Subtitle match"
    timed_by, others, items = {}, {}, {}   # the reference timing of --sub-time, its sidecars, and the word check's cues
    full = subs_on and ctx.depth != "import"   # --sub-check and --sub-time read a track the Cues do not index from the whole file
    unindexed = subtitles.cue_less(path, j) & set(subtitles.sub_codecs(j)) if mkv and subs_on and not full and subtitles.sub_codecs(j) else set()
    if unindexed:
        rec["unindexed"] = {"tracks": sorted(unindexed), "why": "the Cues index none of their blocks, and an import never reads the whole file, "
                                                               "so the subtitle checks skip them"}
    if mkv and subs_on and checks.lid_ready():
        sides = subtitles.mkv_sidecars(path, d)
        if subtitles.sub_targets(j, d) or sides:
            items = subtitles.sub_items(path, j, d, sides, full) if decide.duration(j) >= config.SUB_MIN_SECONDS else {}   # read under the file lock
            if ctx.shared:   # a hearing waits for the host's one model, so it runs without the file lock, as above
                fcntl.flock(ctx.lock, fcntl.LOCK_UN)
            before = (rec.get("repack") or {}).get("subcheck") or {}   # a conversion checked the tracks already: the same windows
            sync = subtitles.sub_match(path, j, d, {r["audio"]: r["starts"] for r in before.values() if r.get("starts")} or None, sides, ctx.known,
                                       items, full, line=not timing, deep=ctx.mode == "import" and config.CFG.subtitles == "deep",
                                       streams=(ctx.hp or {}).get("streams"))
            if timing and sync:   # the whole-file timing of each track that matched, see subtitles.sub_whole()
                # The planner: the timing proposes the moves. Whether the times post is the timing judge's, after the
                # write, see judge.outcomes().
                rec["whole"], rec["whole_facts"] = subtitles.sub_whole(path, j, {k: items[k] for k, r in sync.items() if r["verdict"] == "match" and k in items},
                                                                       sync, deep=ctx.background)
            if ctx.shared:
                ctx.shared.reshare(ctx.st)
        for p in unindexed & set(sync):   # an import skips the track, and says why
            sync[p] = dict(sync[p], why=f'{sync[p]["why"]}: {rec["unindexed"]["why"]}')
        if sync:
            rec["subcheck"] = sync
    left = config.DEADLINE.left()   # an import reads and fits while its time limit leaves SUB_RESERVE
    deadline = time.monotonic() + left - config.SUB_RESERVE if left else None
    stop = runner.deep_waits if ctx.background else (lambda: time.monotonic() > deadline) if deadline is not None else None
    if mkv and subs_on:   # the reference timing
        others = subtitles.ref_sidecars(path, d)
        timed_by, refs = subtitles.sub_reference(path, j, d, sync, items or {}, others, full=full, stop=stop, report=timing, whole=rec.get("whole"))
        if full and checks.lid_ready():   # the speech layout of the subtitles no word check reads and no reference fits
            if ctx.shared:   # the read of the whole audio track takes minutes for a film, so it runs without the file lock
                fcntl.flock(ctx.lock, fcntl.LOCK_UN)
            laid, rec_speech = subtitles.sub_layout(path, j, d, timed_by, others, stop, deep=ctx.background)
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
    waits = {p for p, g in garbled.items() if (sync.get(p) or {}).get("verdict") == "mismatch"}
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
    flashy = subtitles.flash_check(path, j, proof.sidecar_subs(path), full, stop) if mkv and subs_on else {}   # ends too short to read, any text track or sidecar
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
    side_moves = {n: x for n in {*blocks, *starts} if (n in sides or n in others) and (n not in starts or subtitles.same_cues(side_cue(n), items[n][2]))
                  and (x := plan(n, side_cue(n), timing_of(n).get("fix"), False))}
    st = ctx.st
    if (subtitles.FULL.get((path, st.st_size, st.st_mtime_ns)) or {}).get("tracks"):
        rec["full_read"] = {k: v for k, v in subtitles.FULL[(path, st.st_size, st.st_mtime_ns)].items() if k != "cues"}
    if fixes or remove or ends or moves or recode or strip:
        if ctx.background:   # a remux of a film takes minutes, so an import that waits goes first
            runner.deep_waits()
        if ctx.shared and ctx.apply:
            raise runner.Replan("a subtitle needs a remux", rec.get("whole_facts"))   # the facts of the hearing this pass made
        ids_of = [t.get("id") for t in j.get("tracks") or [] if t.get("type") == "subtitles"]
        by_id = lambda ps: [ids_of[int(p[1:]) - 1] for p in ps]
        code, result, info = remux.resub(path, j, st, ctx.apply, dict(zip(by_id(fixes), fixes.values())), by_id(remove), dict(zip(by_id(ends), ends.values())),
                                         **({"timed": dict(zip(by_id(moves), moves.values()))} if moves else {}),
                                         **({"recode": dict(zip(by_id(recode), recode.values()))} if recode else {}),
                                         **({"strip": dict(zip(by_id(strip), strip.values()))} if strip else {}))
        if "warnings" in info:
            ctx.rearm()   # resub() ran its remux with the time limit off, whatever came of it
        done = code == "subtitles_remuxed"
        rec["subremux"] = dict(info, result=result, done=done, fixed=sorted(fixes), ended=sorted(ends), remove=remove, removed=remove if done else [],
                               codes=[c for c, x in (("subtitle_retimed", fixes), ("subtitle_blocks_retimed", set(moves) - set(kept)), ("subtitle_ends_lengthened", ends),
                                                     ("subtitle_mismatch_removed", remove), ("subtitle_repaired", recode), ("subtitle_garbled_removed", strip))
                                                    if x] if done else [code],
                               **({"timed": sorted(set(moves) - set(kept))} if set(moves) - set(kept) else {}), **({"recoded": sorted(recode)} if recode else {}),
                               **({"stripped": {p: info["garbled_text"][str(i)] for p, i in zip(strip, by_id(strip))}} if strip else {}))
        if done:
            rec["subremux"]["tracks_before"] = logs.track_log(d["tracks"])   # the places the check named, before a removal moves the tracks up
            logs.log(dict(rec, outcome=code, result=result))   # the original is kept a while, so its record is on disk before anything else runs
            checks.lid_carry(path, st)   # the proof shows the same audio
            refresh(ctx, duration=False)
            if ctx.hp and ctx.hp["issue"]:   # ffmpeg wrote a new header, and a retime can end a subtitle overrun, see faults()
                ctx.hp = checks.header_of(path, ctx.j) or ctx.hp
            gone = set(remove) | set(strip)   # the tracks after a removed one move up one place, and a track that stays keeps its mismatch
            ctx.read, ctx.wrong, ctx.heard = subtitles.renumber(ctx.read, gone), subtitles.renumber(ctx.wrong, gone), subtitles.renumber(ctx.heard, gone)
            ctx.checked = subtitles.renumber(ctx.checked, gone)
            unmatched = set(subtitles.renumber(dict.fromkeys(unmatched), gone))
            ctx.tags = decide.retag(ctx.j, ctx.got, ctx.known, checks.langs(), ctx.said, ctx.read, ctx.content_lang, ctx.checked)
    stay = sorted({p for p, r in {**sync, **timed_by}.items() if p not in sides and p not in others and r["verdict"] == "mismatch"}
                  - set((rec.get("subremux") or {}).get("removed") or []))
    if stay and not subtitles.sub_fixes(ctx.source):   # the tracks that stay, by their place before any remux, see report.KEPT_BACK
        rec.setdefault("subremux", {}).update(kept_back="check")
    elif stay and not config.CFG.keep_days:
        rec.setdefault("subremux", {}).update(kept_back="keep_days")
    if unmatched or (rec.get("subremux") or {}).get("done"):
        d = decide.decide(ctx.j, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong, unmatched)
        if ctx.tags:
            d = decide.with_tags(d, ctx.tags)
    ctx.d, ctx.sync, ctx.timed_by, ctx.sides, ctx.others, ctx.unmatched, ctx.stay = d, sync, timed_by, sides, others, unmatched, stay
    ctx.fixes, ctx.remove, ctx.ends, ctx.flashy, ctx.moves, ctx.side_moves, ctx.recode = fixes, remove, ends, flashy, moves, side_moves, recode
    ctx.strip, ctx.later = strip, later
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
        if ctx.shared:   # a hearing waits for the host's one model, so it runs without the file lock, as in languages()
            fcntl.flock(ctx.lock, fcntl.LOCK_UN)
        heard, rec["flag_heard"] = checks.hear(ctx.path, ctx.j, ctx.d, ctx.original, only=rest)
        if ctx.shared:
            ctx.shared.reshare(ctx.st)
    if proven or heard:
        ctx.checked = {**ctx.checked, **proven, **heard}
        ctx.tags = decide.retag(ctx.j, ctx.got, ctx.known, checks.langs(), ctx.said, ctx.read, ctx.content_lang, ctx.checked)
        ctx.d = decide.with_tags(decide.decide(ctx.j, ctx.original, ctx.kids, ctx.release, ctx.heard, ctx.spoken, ctx.wrong, ctx.unmatched), ctx.tags)


def deep_drop(ctx):
    """A deep analysis or a recheck ends here when the app replaced or removed its file during the run. The app holds
    no file lock, so this happens during a remux, which then fails with "the original changed". The record becomes the
    quiet drop of deep_analysis(), so no stale edit, cache or alert follows. The reference is ctx.st, the run's own last stat,
    so a change of its own never counts."""
    if ctx.background and (drop := runner.deep_replaced({"path": ctx.path, "key": runner.file_key(ctx.st)})):
        ctx.rec = dict(app=ctx.app, source=ctx.source, path=ctx.path, **drop)
        return True


def decision_fields(ctx):
    """The decision in the record, and the plan of a dry run, see plan_record()."""
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
    rec["class"] = decide.plan_class(d)
    if ctx.keep_plan:
        rec["plan"] = cli.plan_record(ctx.app, ctx.label, ctx.path, d)
        if "header_repair" in rec:
            rec["plan"]["header_repair"] = rec["header_repair"]["code"]


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
    if ctx.tags and ctx.tags["mismatch"]:
        rest.append({"kind": "sublang", "mismatch": ctx.tags["mismatch"], "muted": [n for n in d["notes"] if " loses its default and forced flags, " in n]})
    code = rec.get("header_repair", {}).get("code") if hp and hp["issue"] else None   # a subtitle remux can end the issue
    if code in ("header_repair_failed", "subtitle_file_may_be_cut"):
        why = rec["header_repair"]["result"].partition(": ")[2]
        rest.append({"kind": "header", "why": why[:200]} if code == "header_repair_failed" else {"kind": "cut", "why": why})
    if code == "subtitle_overrun_unfixable" and hp.get("unfixable"):   # the alerts name the places before a removal, see report.track_langs()
        rm = rec.get("subremux") or {}
        gone = [*(rm.get("removed") or []), *(rm.get("stripped") or {} if rm.get("done") else [])]   # the remux took both out
        rest.append({"kind": "subtitle", "issue": hp["issue"], "tracks": [dict(x, track=place_before(x["track"], gone)) for x in hp["unfixable"]]})
    if ctx.mode == "import" and ctx.audio:
        ctx.certain, doubts, samples = checks.check_audio(ctx.path, ctx.j, d["edits"], ctx.runtime)
        rec["audio"] = logs.audio_summary(ctx.certain, doubts, samples)
        if doubts:   # uncertain: one alert per file, because the alerts of one kind share a marker
            rest.append({"kind": "audio", "doubts": doubts})
    if ctx.mode == "import" and ctx.video and not ctx.certain:   # before the edit, under the job's time limit, see check_video()
        ctx.vcertain, doubts, ctx.vfields = ctx.vpre or checks.video_check(ctx.path, j=ctx.j, hp=hp)
        rec["video"] = checks.video_summary(ctx.vcertain, doubts, ctx.vfields)
        if "code" in ctx.vfields:
            rec["reasons"] = rec["reasons"] + [ctx.vfields["code"]]
        if doubts:
            rest.append({"kind": "video", "doubts": doubts})


def place_before(p, gone):
    """The place before a subtitle remux of the track at place p after it. gone holds the places the remux removed.
    This is the reverse of subtitles.renumber()."""
    n, k = int(p[1:]), 0
    while n:
        k += 1
        n -= f"s{k}" not in gone
    return f"s{k}"


def act(ctx):
    """The exclusive lock, then the re-grab of a certain fault, or the flag edit, see regrab() and edit()."""
    rec, d, certain, vcertain, job = ctx.rec, ctx.d, ctx.certain, ctx.vcertain, ctx.job
    if ctx.shared and not (certain or vcertain or "content" in config.CFG.regrab):   # the checks found nothing to re-grab
        ctx.shared.settle()
    sides = {s["name"]: s for s in proof.sidecar_subs(ctx.path)} if ctx.flashy else {**ctx.sides, **ctx.others}   # the sidecars act alike
    ctx.sync_all = {**ctx.sync, **ctx.timed_by}
    ctx.sides, ctx.side_ends = sides, {n: v for n, v in ctx.flashy.items() if n in sides}   # at SUBTITLES check, sidecar_fix() only reports
    ctx.acts = [n for n in sides if (ctx.sync_all.get(n) or {}).get("verdict") == "mismatch" or ((ctx.sync_all.get(n) or {}).get("timing") or {}).get("fix")
                or n in ctx.side_ends or n in ctx.side_moves]
    if ctx.shared and ctx.apply and (certain or vcertain or (ctx.mkv and (d["edits"] or ctx.acts))):
        ctx.shared.exclusive(ctx.st)
    if certain:
        rec.update(outcome="broken_audio", result=f"broken audio: {certain}")
        ctx.first.append({"kind": "audio", "certain": certain, "action": regrab.regrab(ctx.app, job, certain, regrab.audio_probe(
            ctx.original, ctx.kids, ctx.release, ctx.runtime, ctx.heard, ctx.spoken)) if ctx.apply else {"code": "dry_run"}})
    elif vcertain:
        rec.update(outcome="corrupt_video", result=f"corrupt video: {vcertain}")
        ctx.first.append({"kind": "video", "certain": vcertain, "action": regrab.regrab(ctx.app, job, ctx.vfields["fault"], regrab.video_probe, "video")
                          if ctx.apply else {"code": "dry_run"}})
    elif not ctx.mkv:   # a conversion that did not happen set the result already
        if not ctx.unconverted:
            rec.update(outcome="not_mkv", result="skipped, not mkv")
    else:
        # The deep analysis and a recheck keep the flags an earlier run set, and --sub-time keeps them on a file no app
        # lists, as no item says the original language. Only a subtitle verdict changes a flag then: an unmatched track
        # loses its default and forced flags. That holds after a remux of their own too.
        keep = ctx.background or not ctx.app
        edits = [[t["sel"], 0, 1] for t in d["tracks"] if t["pos"] in ctx.unmatched and t["default"]] + \
            [[t["sel"], 0, 1, decide.FORCED_FLAG] for t in d["tracks"] if t["pos"] in ctx.unmatched and t["forced_flag"]] if keep else d["edits"]
        if keep and d["edits"] != edits:
            set_by = "an earlier run set the flags" if ctx.recheck else "the import set the flags"
            rec["flags_kept"] = (set_by if ctx.app else "no app lists the file, so no original language is known") + \
                ", and only a subtitle verdict changes a flag"
        rec = ctx.rec = edit(rec, ctx.j, edits, ctx.apply, replan=lambda after: muted_plan(replan(after, plan_inputs(ctx)), ctx.muted), ts=d["tracks"])
        # An abstained or dropped decision has no edits of its own, and its reason stays in "undecided" or "dropped". It
        # names the result only when nothing changed, so an edit of a subtitle verdict keeps "edited" and its Plex analyze.
        if d.get("undecided") and rec["outcome"] == "no_change":
            rec.update(outcome="undecided", result="undecided: " + d["undecided"])
        elif d.get("dropped") and rec["outcome"] == "no_change":
            rec.update(outcome="dropped", result="dropped: " + "; ".join(d["dropped"]))


def sub_outcome(rec):
    """(outcome, result) of a run whose subtitle step changed a file and no flag, or None when it changed none. The
    outcome is subtitles_remuxed after a remux, else sidecars_changed. The result names what happened to each track and
    sidecar, as "subtitles remuxed: s2 retimed, s1 lines moved, s3 repaired, Film.en.srt retimed". A partial shift of
    the speech layout adds how many lines kept their times, see subsync.shifted()."""
    rm = rec.get("subremux") or {}
    did = (("fixed", "retimed"), ("timed", "lines moved"), ("ended", "ends lengthened"), ("removed", "removed"), ("recoded", "repaired"),
           ("stripped", "taken out as garbled")) \
        if rm.get("done") else ()
    kept = lambda p: ((rec.get("subtime") or {}).get(p) or {}).get("timing", {}) or {}   # a partial shift names the lines it kept
    left = lambda p: f', {n} lines kept their times' if (n := kept(p).get("kept")) else ""
    what = [f"{p} {x}" + (left(p) if k == "fixed" else "") for k, x in did for p in rm.get(k) or []] + \
        [f'{e["name"]} {e["result"]}' + (left(e["name"]) if e["result"] == "retimed" else "") for e in rec.get("sidecars") or [] if e.get("result") in ("retimed", "moved")]
    code, head = ("subtitles_remuxed", "subtitles remuxed") if rm.get("done") else ("sidecars_changed", "sidecars changed")
    return (code, f"{head}: " + ", ".join(what)) if what else None


def after_edit(ctx):
    """The rescans after a change, the sidecars, the subtitle cache and alerts, then the lock goes, see sidecar_fix()."""
    rec, d = ctx.rec, ctx.d
    # After the edit, so the app's disk scan never probes a header mkvpropedit is rewriting. A backfill sends one rescan
    # per item at the end of its run, because Sonarr's rescan of a large series is slow.
    rescan_now = lambda: apps.ARR[ctx.app].rescan((ctx.ids or {}).get("app_id")) if ctx.mode == "import" or ctx.background else "after the run"
    if "repack" in rec and not ctx.unconverted and not rec["repack"].get("pending"):   # a pending one settles below, with no lock
        rec["repack"]["rescan"] = rescan_now()
    if rec.get("header_repair", {}).get("code") in config.REPAIRED:
        rec["header_repair"]["rescan"] = rescan_now()
    if (rec.get("subremux") or {}).get("done"):
        rec["subremux"]["rescan"] = rescan_now()
    if ctx.acts and ctx.mkv and not (ctx.certain or ctx.vcertain):   # the sidecars beside the file, under the lock of the edit
        rec["sidecars"] = subtitles.sidecar_fix({n: ctx.sides[n] for n in ctx.acts}, ctx.sync_all, ctx.apply, ctx.app, ctx.source, ctx.side_ends,
                                                **({"timed": ctx.side_moves} if ctx.side_moves else {}))
    if rec["outcome"] == "no_change" and (named := sub_outcome(rec)):   # the subtitle step was the change, so the outcome names it
        rec.update(outcome=named[0], result=named[1])
    ctx.rest += subtitles.sub_findings(rec, ctx.sync_all, ctx.stay, ctx.starts)
    if ctx.mkv and ctx.subs_on and checks.lid_ready():   # a backfill with --sub-check skips the file while it stays as it is and needs nothing more
        muted = {t["sel"] for t in d["tracks"] if t["pos"] in ctx.unmatched}
        planned = ctx.fixes or ctx.remove or ctx.ends or ctx.moves or ctx.recode or ctx.strip
        done = ctx.apply and rec["outcome"] in ("edited", "no_change", "subtitles_remuxed", "sidecars_changed") and (rec.get("subremux") or {}).get("done", not planned) \
            and all(e["result"] in ("moved", "retimed") for e in rec.get("sidecars") or [])
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
        subtitles.sub_cache(ctx.path, rec, ctx.depth, ctx.app,
                            (bool(planned or ctx.acts) or any(e[0] in muted for e in d["edits"])) and not done or bool(ctx.later) or stopped or unmade)
    if rec["outcome"] in ("verify_failed", "edit_failed"):
        ctx.rest.append({"kind": "edit", "error": short_error(rec["result"], rec["path"]), "unread": rec.get("after_error"),
                         "on": None if "after" not in rec else [f'{t["pos"]} {t["lang"]}' for t in rec["after"] if t["default"]]})
    if ctx.lock is not None:   # the edit is done, and the checks below only read the file
        fcntl.flock(ctx.lock, fcntl.LOCK_UN)
    if (rec.get("repack") or {}).get("pending") and (ctx.mode == "import" or ctx.background):   # two waited rescans, see settle()
        config.DEADLINE.stop()
        rec["repack"]["settle"] = convert.settle_extras(ctx.app, (ctx.ids or {}).get("app_id"), [rec["repack"]["pending"]])


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
            episode = ctx.app in apps.ARR and not apps.ARR[ctx.app].film   # metadata() reads an NFO title for an episode only
            nfo = (job or {}).get("nfo_title") or (library_nfo_title(ctx.path) if episode else None)
            meta = ctx.meta = metadata(ctx.app, ctx.path, ctx.j, ctx.size, ctx.d, ctx.original, ctx.release, ctx.item, asked=ctx.asked, nfo=nfo)
            rec.update(trusted=meta["trusted"], expected=meta["expected"], other_film=meta["other"], evidence=meta["evidence"],
                       tmdb=meta["tmdb"])
            ev = meta["evidence"]
            if ctx.post and (k := tmdb_key_alert(ctx.app, meta["tmdb"])):
                rec["key_alert"] = k
            if ev["regrab"] and ctx.mode == "import" and ctx.apply and not (ctx.certain or ctx.vcertain):
                if ctx.lock is not None:   # a re-grab deletes files, so it waits for an edit in progress
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
    recheck alert on the subtitles only, because the import alerted on the rest. An import whose file gets the deep
    analysis holds the subtitle alerts in held, because that analysis checks them again and posts what it still finds,
    see logs.alert_findings() and runner.queue_deep_analysis()."""
    rec = ctx.rec
    try:
        problems = ctx.first + file_alerts(ctx.d, ctx.file_checks, ctx.meta, ctx.original, ctx.item) + ctx.rest
    except Exception as ex:   # a finding must never cost the decision line or the Plex analyze of an edited file
        rec["alert_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:200]
        problems = ctx.first + ctx.rest
    rec["findings"] = [p for p in problems if not ctx.background or p["kind"] in logs.DEEP_KINDS]
    rec["alert_kinds"] = [p["kind"] for p in rec["findings"]]
    if ctx.post:   # after the edit, so a failed post never blocks one
        ctx.held = [] if ctx.mode == "import" and runner.deep_wanted(rec) else None
        rec["alert_result"] = logs.alert_findings(rec, ctx.size, ctx.held)


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


def edit(rec, j, edits, apply, replan=None, ts=None):
    """mkvpropedit the planned flags and verify them with a second probe. Returns the record with the result.
    replan(after) plans the edited file again. Its edit count and the invariants of the new state go in "recheck",
    which the audit reads instead of probing the file again. ts is the tracks of the plan, for mute_rule()."""
    edits = [e for e in edits if decide.prop(e) not in ("flag-default", decide.FORCED_FLAG, decide.ORIGINAL_FLAG)
             or decide.unapplied(j, [e])]   # never a no-op flag edit
    if not edits:
        return dict(rec, outcome="no_change", result="no change")
    path = rec["path"]
    rec = dict(rec, before=runner.flags(j), edits=edits, undo=["mkvpropedit", path] + propedit_args(edits, 2))
    before = os.stat(path)
    if vault.links(before) > 1:   # a hardlink shares the edit with the download client's copy
        return dict(rec, outcome="hardlinked", result="hardlinked, not edited")
    if not apply:
        return dict(rec, outcome="dry_run", result="dry run")
    if not os.access(path, os.W_OK):
        return dict(rec, outcome="read_only", result="read-only, not edited")
    if subsync.INVARIANTS:
        mute_rule(path, ts or decide.classify(j), edits)
    with runner.no_stop():   # neither the time limit nor SIGTERM cuts mkvpropedit, which rewrites the header in place
        config.DEADLINE.stop()
        logs.log(dict(rec, result="editing"))   # the undo record exists before the file changes
        e = subprocess.run(["mkvpropedit", path] + propedit_args(edits, 1), capture_output=True, text=True, errors="replace")
    checks.lid_carry(path, before)
    runner.mute_mark(path, before)
    if e.returncode > 1:   # it may still have written. A run can fail on "Tracks" and still set the flags.
        rec.update(outcome="edit_failed", result="mkvpropedit failed: " + (e.stdout + e.stderr).strip()[-300:])
        try:
            rec["after"] = runner.flags(checks.mkvmerge(path))
        except Exception as ex:
            rec["after_error"] = config.mask(f"{type(ex).__name__}: {ex}")[:300]
        return rec
    after = checks.mkvmerge(path)
    rec["after"] = runner.flags(after)
    if decide.unapplied(after, edits):
        return dict(rec, outcome="verify_failed", result="VERIFY FAILED, flags did not change")
    rec = dict(rec, outcome="edited", result="edited")
    if replan:
        try:
            p = replan(after)
            rec["recheck"] = {"edits": len(p["edits"]), "undecided": p.get("abstain"),
                              "invariants": [c for c, _ in decide.invariants(p["tracks"], [], p["cls"], set(p["orig"]))]}
        except Exception as ex:
            rec["recheck"] = {"error": config.mask(f"{type(ex).__name__}: {ex}")[:200]}
    return rec
