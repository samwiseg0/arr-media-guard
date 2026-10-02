# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The Plex analyze after an edit and the folder scans, each after two idle checks of the library section."""
import os, re, sys, time, urllib.parse

from . import apps, config, logs, store


def plex_get(path, **q):
    q["X-Plex-Token"] = config.CFG.plex_token
    return apps.http(f'{config.CFG.plex_url}{path}?{urllib.parse.urlencode(q)}', headers={"Accept": "application/json"})["MediaContainer"]


def plex_find(path, want):
    """ratingKeys of the Plex items whose media part is this file, and whether Plex has the item with another file.

    want is {"guids": ["tmdb://100", "imdb://tt0000100"], "title": "Film A", "show": False}. Plex has no lookup
    by external id, so the search goes by title in the section that holds the path. Any of the app's ids picks the
    item. When none matches, the first few title hits are checked by path alone. Plex can match a series folder to a
    same-name title that shares no id with Sonarr's series. For a show every episode is read, because the numbering
    can differ. Sonarr's S02E10 can be Plex's S05E03. The file path is the final gate. path is a local path, and
    PLEX_PATH_MAP gives the path Plex lists it at, see mapped(). The third value is the key of the library section
    that lists the item.
    """
    path = apps.mapped(path, "plex", True)
    parts = lambda v: [p.get("file") for m in v.get("Media", []) for p in m.get("Part", [])]
    leaves = lambda v: plex_get(f'/library/metadata/{v["ratingKey"]}/allLeaves').get("Metadata", []) if want.get("show") else [v]
    t = want["title"]
    queries = dict.fromkeys(q for q in (t, re.sub(r"\s*\(.*?\)", "", t), t.split(":")[0], (t.split() or [t])[0]) if q.strip())
    keys, other, loose, where = [], False, [], None
    for d in plex_get("/library/sections").get("Directory", []):
        if not any(path.startswith(loc["path"].rstrip("/") + "/") for loc in d.get("Location", [])): continue
        for q in queries:
            hits = plex_get(f'/library/sections/{d["key"]}/all', title=q, includeGuids=1).get("Metadata", [])
            found = [v for v in hits if set(want["guids"]) & {g["id"] for g in v.get("Guid", [])}]
            if hits and not loose: loose, where = hits[:3], d["key"]
            if not found: continue
            for v in found:
                mine = [i["ratingKey"] for i in leaves(v) if path in parts(i)]
                keys += mine
                other = other or not mine
            return keys, other, d["key"]
    # No id matched. The path alone still proves which item holds this exact file.
    return [i["ratingKey"] for v in loose for i in leaves(v) if path in parts(i)], other, where


def plex_activities(cache):
    """GET /activities once per cache dict. The worker passes one dict per pass, so a pass reads it once for every
    pending analyze, and a slow Plex costs one timeout per pass. A failure is kept and raised for each caller."""
    if "acts" not in cache:
        try:
            cache["acts"] = plex_get("/activities").get("Activity", [])
        except Exception as ex:
            cache["acts"] = ex
    if isinstance(cache["acts"], Exception):
        raise cache["acts"]
    return cache["acts"]


def plex_watches():
    """Whether Plex may scan a folder by itself when a file in it changes, its "Scan my library automatically"
    setting. Then a backfill's edit can start a scan of the edited item, so the analyze takes its own two idle checks.
    A missing setting or a failed read counts as on."""
    try:
        prefs = {s.get("id"): s.get("value") for s in plex_get("/:/prefs").get("Setting", [])}
    except Exception:
        return True
    return prefs.get("FSEventLibraryUpdatesEnabled") is not False


def plex_scan(section, acts):
    """The title of a Plex activity that may cover this library section, or "" when none does. acts is the
    /activities list. Plex 1.43 lists a scan there as type library.update.section, with the section in
    Context.librarySectionID. A scan names its section about a second after it starts. A single-item refresh
    (library.refresh.items, "Checking files") re-reads media parts too, and its Context names only the item. So a
    busy type with a missing, null or empty section counts for every section."""
    if section is None:
        raise LookupError("no library section for the item")
    for a in acts:
        sid = (a.get("Context") or {}).get("librarySectionID")
        if a.get("type") in config.PLEX_BUSY_TYPES and (not sid or str(sid) == str(section)):
            return f'{a.get("title") or "Scanning"} {a.get("subtitle") or ""}'.strip()
    return ""


def plex_job(app, source, label, path, want, decision_id):
    """One analyze that waits, as plex_step() advances it. due, edited, since and quiet are time.monotonic() values."""
    now = time.monotonic()
    return dict(due=now, edited=now, tries=0, app=app, source=source, label=label, path=path, want=want, other=False,
                decision_id=decision_id, keys=None, section=None, since=None, quiet=None, scan="", deferrals=0, logged=None)


def plex_step(p, waits=config.PLEX_WAITS, cache=None, ready=None, burst=None):
    """One step of p, a plex_job(). First the lookup, again on the waits schedule until Plex lists the file. Then two
    checks of its library section PLEX_QUIET seconds apart, both idle, because the app's own Plex connection can start
    a scan just after the edit. Only then the analyze. Plex 1.43 crashes when an analyze races a scan of the same
    section. A busy section or a failed check waits PLEX_BUSY_WAIT and starts the two checks over. After
    PLEX_BUSY_CAP seconds of that the analyze is skipped. cache is plex_activities()'s dict, shared by one worker pass.
    The check right before the PUT never uses it. Returns (the final message or None, a reason code or None). A code
    without a message is a deferral. ready, from a folder scan of --plex-flush or the worker, gives the seconds the
    send must still wait for another reason, checked right after the two idle checks. A wait restarts them.

    burst, from a backfill only, maps a section to the times of the first and the last idle check in a row. A check
    at most PLEX_QUIET after the last one continues the row. Once the row spans PLEX_QUIET, each further analyze needs
    only its own fresh idle check. A busy or failed check ends the row, and so does "Scan my library automatically"
    when it is on, missing or does not read, see plex_watches(). See docs/design.md, "The burst"."""
    if not p["keys"]:
        try:
            p["keys"], p["other"], p["section"] = plex_folder_scan(p) if (p["want"] or {}).get("folder") else plex_find(p["path"], p["want"])
        except Exception as ex:   # Plex restarting or busy: try again at the next wait
            p["other"] = config.mask(f" Last error: {type(ex).__name__}: {ex}")[:200]
        p["tries"] += 1
        if not p["keys"]:
            if p["tries"] >= len(waits):
                return plex_gave_up(p["other"], sum(waits)), "plex_not_found"
            p["due"] = p["edited"] + sum(waits[:p["tries"] + 1])
            return None, None
    row = (burst or {}).get(p["section"]) if p["quiet"] is None else None
    if row and plex_watches():   # read before each file, so a change in the middle of a run counts
        del burst[p["section"]]
        row = None
    now = time.monotonic()   # after the lookup and the setting, so the times below are those of the check
    if row and now - row[1] <= config.PLEX_QUIET:
        p["quiet"] = row[0]
    # The check that can release the PUT reads /activities fresh. A shared read can be stale by then: another item's
    # lookup ran meanwhile, and a scan may have started. One GET per analyze sent.
    fresh = cache is None or (p["quiet"] is not None and now - p["quiet"] >= config.PLEX_QUIET)
    try:
        p["scan"], code = plex_scan(p["section"], plex_activities({} if fresh else cache)), "plex_section_busy"
    except Exception as ex:
        p["scan"], code = config.mask(f"check failed: {type(ex).__name__}: {ex}")[:200], "plex_check_failed"
    p["since"] = p["since"] or now
    if p["scan"]:
        p["quiet"] = None
        (burst or {}).pop(p["section"], None)
        if now - p["since"] >= config.PLEX_BUSY_CAP:
            return (f'section {p["section"]} was not idle for {config.PLEX_BUSY_CAP} seconds, last: {p["scan"]}. No analyze, '
                    "Plex's own scan reads the new flags."), "plex_analyze_skipped_busy"
        p["due"], p["deferrals"] = now + config.PLEX_BUSY_WAIT, p["deferrals"] + 1
        return None, code
    p["quiet"] = p["quiet"] or now
    if burst is not None:
        burst[p["section"]] = (p["quiet"], now)
    if now - p["quiet"] < config.PLEX_QUIET:
        p["due"] = p["quiet"] + config.PLEX_QUIET
        return None, None
    wait = ready() if ready else 0
    if wait > 0:   # an analyze in the section too recent: two new idle checks after the wait
        p["quiet"], p["due"], p["scan"] = None, now + wait, f"an analyze {PLEX_SCAN_AFTER - wait:.0f} s ago"
        return None, "plex_scan_after_analyze"
    if (p["want"] or {}).get("folder"):
        return plex_folder_scan(p, send=True)
    try:
        for k in p["keys"]:
            apps.http(f'{config.CFG.plex_url}/library/metadata/{k}/analyze?{urllib.parse.urlencode({"X-Plex-Token": config.CFG.plex_token})}', "PUT")
    except Exception as ex:
        return config.mask(f"analyze failed for {', '.join(p['keys'])}: {type(ex).__name__}: {ex}")[:200], "plex_analyze_failed"
    return f"analyze sent for {', '.join(p['keys'])}", "plex_analyze_sent"


def plex_note(p, done, code):
    """A plex line in the decision log: the final outcome (result plex) with the count of deferrals, or a deferral
    (result plex_deferred) when its code differs from the last one logged for this item. A section busy for 30
    minutes then writes one line, not 60. Returns whether it wrote a line."""
    if not done and code == p["logged"]:
        return False
    p["logged"] = code
    logs.log(dict(app=p["app"], source=p["source"], label=p["label"], path=p["path"], result="plex" if done else "plex_deferred",
             plex=done or p["scan"], plex_reason=code, section=p["section"], deferrals=p["deferrals"], decision_id=p["decision_id"]))
    return True


def plex_gave_up(other, seconds):
    # The app's own Plex connection adds the item later, and Plex then reads the flags already fixed.
    extra = other if isinstance(other, str) else ""
    return ("Plex has the item but not this file" if other is True else "not in Plex") + f" after {seconds} seconds, no analyze." + extra


def plex_analyze(p, skipped, ready=None, burst=None):
    """The backfill's analyze: one lookup, then plex_step() with a sleep in between. The backfill waits while the
    section is busy, so the next file waits too. skipped holds the sections this run already gave up on. A file in
    one of them is skipped at its first busy check, so a section busy for hours costs one PLEX_BUSY_CAP per run, not
    one per file. An analyze sent clears the section. burst goes to plex_step(). Returns (message, reason code)."""
    while True:
        done, code = plex_step(p, waits=(0,), ready=ready, burst=burst)
        if code in ("plex_section_busy", "plex_check_failed") and p["section"] in skipped:
            done = f'section {p["section"]} is still busy ({p["scan"]}) after an earlier skip in this run. No analyze.'
            code = "plex_analyze_skipped_busy"
        if done:
            if code == "plex_analyze_skipped_busy": skipped.add(p["section"])
            elif code == "plex_analyze_sent": skipped.discard(p["section"])
            return done, code
        if code and plex_note(p, None, code):
            print(f'waiting for Plex section {p["section"]} ({p["scan"]}): {p["label"]}', flush=True)
        time.sleep(max(0, p["due"] - time.monotonic()))


def plex_folder_job(app, source, label, folder, decision_id):
    """A plex_job() for a partial scan of one folder instead of an analyze. A file whose name changed is not in Plex
    until Plex scans its folder, and an analyze reads only the part Plex knows. The scan waits for the same two idle
    checks of the section as an analyze, see plex_step(), and plex_pass() keeps it apart from an analyze of the section.
    want {"folder": True} marks it, so it passes the pipe of a job process and save_plex() like any plex_job()."""
    return plex_job(app, source, label, folder, {"folder": True}, decision_id)


def plex_later(app, folder):
    """Add folder to the folders of app that --plex-flush scans, in the store. At once, so a stopped run leaves its
    folders listed."""
    with store.tx():
        store.put("plex-later", app, store.get("plex-later", app, []) + [folder])


def plex_flush(argv):
    """--plex-flush <app>: one partial Plex scan per library location the listed folders sit in, instead of one per
    folder (a --convert --apply --plex-later run). Each goes through plex_folder_job() and plex_analyze(): two idle
    checks of the section back to back, then, right before the send, no analyze in the section for PLEX_SCAN_AFTER.
    An analyze that came meanwhile, as from a live import, means a wait and two new idle checks, again and again.
    A scan soon after an analyze is the race that crashes Plex, see plex_step(). Every analyze the decision
    log records counts, in either of its two shapes: the hook worker's line (result plex, plex_reason, section) and a
    backfill's decision line of an edited file (plex, plex_reason and plex_section on the line). A line with no section
    counts for every section. The subtitle hunter sends no analyze: Radarr's own Plex connection reads its import.
    The folders of a section leave the list only once its scan went out. So a stop in a wait of 5 or 30 minutes keeps
    the rest listed, and so do a failed scan and a folder no section holds."""
    app = argv[0] if argv and argv[0] in config.CFG.apps else sys.exit("usage: arr-media-guard --plex-flush <instance>")
    if not config.CFG.plex_url:
        sys.exit("PLEX_URL is empty in the env file, so there is no Plex to scan")
    plexed = lambda x: apps.mapped(x, "plex", True).rstrip("/") + "/"
    folders = sorted({plexed(x) for x in store.get("plex-later", app, [])})
    roots, left = {}, list(folders)   # (section, location) -> its folders
    for d in plex_get("/library/sections").get("Directory", []) if folders else []:
        for loc in d.get("Location", []):
            mine = [x for x in folders if x.startswith(loc["path"].rstrip("/") + "/")]
            if mine:
                roots[(d["key"], loc["path"])] = mine
                left = [x for x in left if x not in mine]
    skipped = set()
    for (section, root), mine in sorted(roots.items()):
        p = plex_folder_job(app, "backfill", f"section {section}", apps.mapped(root, "plex"), None)   # a local path, as every folder job holds
        done, code = plex_analyze(p, skipped, ready=lambda s=section: after_analyze(s))
        logs.log(dict(app=app, source="backfill", label=p["label"], path=root, result="plex", plex=done, plex_reason=code, section=section,
                 folders=len(mine)))
        print(f"section {section} at {root}, {len(mine)} folders: {done}", flush=True)
        if code == "plex_scan_sent":   # only now these folders leave the list, so a stop before keeps them listed
            with store.tx():
                store.put("plex-later", app, [x for x in store.get("plex-later", app, []) if plexed(x) not in mine])
        else:
            left += mine
    print(f"{len(folders) - len(left)} folders scanned, {len(left)} left on the list", flush=True)


def after_analyze(section):
    """Seconds a folder scan of section must still wait, so that PLEX_SCAN_AFTER passes after every analyze the decision
    log records by now. logs.log() keeps the time of the last one per section in the store. A line with no section
    counts for every section."""
    return max(0.0, PLEX_SCAN_AFTER - (time.time() - max(store.get("plex-analyzed", str(section), 0.0), store.get("plex-analyzed", "*", 0.0))))


def plex_folder_scan(p, send=False):
    """The two Plex calls of a plex_folder_job(), from plex_step(). Before the idle checks: the library section whose
    location holds the folder, as (keys, other, section) like plex_find(), ([], False, None) when none does. After them,
    with send: GET /library/sections/<section>/refresh?path=<folder>, Plex's partial scan, as (message, reason code).
    It scans that one folder, never the section."""
    folder = apps.mapped(p["path"], "plex", True).rstrip("/") + "/"
    if not send:
        for d in plex_get("/library/sections").get("Directory", []):
            if any(folder.startswith(loc["path"].rstrip("/") + "/") for loc in d.get("Location", [])):
                return ["folder"], False, d["key"]
        return [], False, None
    q = urllib.parse.urlencode({"path": apps.mapped(p["path"], "plex", True), "X-Plex-Token": config.CFG.plex_token})
    try:
        apps.http(f'{config.CFG.plex_url}/library/sections/{p["section"]}/refresh?{q}')
    except Exception as ex:
        return config.mask(f'scan failed for {p["path"]}: {type(ex).__name__}: {ex}')[:200], "plex_scan_failed"
    return f'scan sent for {p["path"]}', "plex_scan_sent"


def plex_after(app, source, rec, want):
    """The Plex step after a change to the file of a decision line: an analyze of its item, or for a file a conversion
    renamed, a scan of its folder, plex_folder_job(). Plex lists the old name, so no item holds the new one yet."""
    if "repacked" in rec.get("reasons", []) and (rec.get("repack") or {}).get("new_path"):
        return plex_folder_job(app, source, rec["label"], os.path.dirname(rec["path"]), rec.get("id"))
    return plex_job(app, source, rec["label"], rec["path"], want, rec.get("id"))


PLEX_KEEP = ("app", "source", "label", "path", "want", "decision_id")   # the plex_job() arguments


def save_plex(pending):
    """Keep the analyze requests a stopped worker still had in the store, for the next worker. Returns them."""
    kept = [{k: p[k] for k in PLEX_KEEP} for p in pending]
    if kept:
        store.put("plex", "pending", kept)
    return kept


def load_plex():
    """The analyze requests a stopped worker kept, as new plex_job() dicts. They leave the store once read."""
    with store.tx():
        rows = store.get("plex", "pending", [])
        store.drop("plex", "pending")
    return [plex_job(*(r[k] for k in PLEX_KEEP)) for r in rows if isinstance(r, dict) and all(k in r for k in PLEX_KEEP)]


def plex_pass(pending):
    """One step of each pending analyze or folder scan that is due. One /activities read serves the whole pass, see
    plex_step(). An analyze waits while a folder scan of its section is pending, see plex_scan_first(). With PLEX_URL
    empty there is no Plex, so the pending requests are dropped unsent."""
    if not config.CFG.plex_url:
        pending.clear()
        return
    now, cache = time.monotonic(), {}
    for p in [p for p in pending if p["due"] <= now]:
        last = PLEX_ANALYZED.get(p["section"])
        if (p["want"] or {}).get("folder") and last is not None and now - last < PLEX_SCAN_AFTER:   # its two idle checks come after the wait
            p["quiet"], p["due"] = None, last + PLEX_SCAN_AFTER
            plex_note(p, None, "plex_scan_after_analyze")
            continue
        if plex_scan_first(p, pending):   # not yet: its two idle checks start again after the scan
            p["quiet"] = None
        # a folder scan also waits for the analyzes of every other sender on this host, a backfill's among them
        done, code = plex_step(p, cache=cache, ready=(lambda p=p: after_analyze(p["section"])) if (p["want"] or {}).get("folder") else None)
        if done or code:
            plex_note(p, done, code)
        if code in ("plex_analyze_sent", "plex_analyze_failed"):   # a failed request may still have reached Plex
            PLEX_ANALYZED[p["section"]] = time.monotonic()
        if done:
            pending.remove(p)
            for q in pending:   # a scan and an analyze of one section never go out together: the other waits for two new idle checks
                if q["section"] == p["section"] and bool((p["want"] or {}).get("folder")) != bool((q["want"] or {}).get("folder")):
                    q["quiet"] = None


PLEX_SCAN_AFTER = 300   # seconds a folder scan waits after the last analyze sent in its section, then its two idle checks
PLEX_ANALYZED = {}      # library section -> time.monotonic() of the last analyze this worker sent there


def plex_scan_first(p, pending):
    """Whether the analyze p waits for a folder scan of its section that is still pending. Plex's own work after an
    analyze is no activity the gate can see, and in Plex 1.43 that work crashes Plex during a scan. So the scan
    goes out first, and the gate then holds each analyze until the scan ends. An analyze that went out before the scan
    was queued is no longer pending, so plex_pass() holds the scan PLEX_SCAN_AFTER seconds after the section's last
    analyze instead, then its two idle checks. That is a delay, so it never deadlocks. A request that failed may still
    have reached Plex, so any end of an item restarts the other kind's checks. Right before the send, the scan also
    waits PLEX_SCAN_AFTER after every analyze the decision log records, see after_analyze(). That covers a backfill, a
    stopped worker and this worker."""
    return not (p["want"] or {}).get("folder") and p["section"] is not None and \
        any(q["section"] == p["section"] and (q["want"] or {}).get("folder") for q in pending if q is not p)
