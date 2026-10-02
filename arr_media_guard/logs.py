# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The decision log, its summary in syslog, and the Discord posts. report.py words them."""
import contextlib, datetime, hashlib, json, os, sqlite3, syslog, time, urllib.error

from . import apps, checks, config, content, decide, health, report, store


def log(rec):
    """One line in the decision log, in one O_APPEND write. The kernel appends each write whole, so the lines of the
    job processes, a backfill and a scan never interleave. Code never reads the log back, see log_facts(). Returns the
    time of the line."""
    now = datetime.datetime.now().astimezone()
    line = json.dumps(dict(time=now.isoformat(timespec="seconds"), instance=config.CFG.instance, **rec))
    fd = os.open(config.CFG.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        os.write(fd, (config.mask(line) + "\n").encode())
    finally:
        os.close(fd)
    return log_facts(rec, now.replace(microsecond=0).timestamp())


def log_facts(rec, at):
    """Keep in the store what the readers of the log need from its line rec of the time at: per path an editing line
    with no edited line after it, see checks.hook_edit_failed(), and per Plex section the time of the last analyze, see
    plex.after_analyze(). A store error loses the fact and never the caller, which expects an OSError at most. Returns
    at."""
    path = rec.get("path")
    with contextlib.suppress(sqlite3.Error):
        if path and rec.get("result") == "editing":
            store.put("editing", path, True, at)
        elif path and "edited" in (rec.get("result"), rec.get("edit_result")):
            store.drop("editing", path)
        if rec.get("plex_reason") in ("plex_analyze_sent", "plex_analyze_failed"):
            s = rec.get("section") or rec.get("plex_section")   # a line with no section counts for every section
            with store.tx():   # the latest time counts, as two processes may log out of order
                if at > store.get("plex-analyzed", "*" if s is None else str(s), 0.0):
                    store.put("plex-analyzed", "*" if s is None else str(s), at, at)
    return at


def status(key, code, detail="", touch=True):
    """Record one check for Zabbix in status.json. The time limit passes through. Any other failure logs a warning
    line and never stops the job, because the status file is a signal only."""
    try:
        _, note = health.record(config.CFG.state_dir, key, code, config.mask(detail or ""), touch=touch, flock=config.DEADLINE.lock)
    except (content.OutOfTime, TimeoutError):
        raise
    except Exception as ex:
        note = f"status.json: {type(ex).__name__}: {ex}"[:200]
    if note:
        try:
            log(dict(source="status", result="warning", note=config.mask(note)))
        except OSError:
            pass


def policy_hash():
    return hashlib.sha256(json.dumps(decide.POLICY, sort_keys=True).encode()).hexdigest()[:12] if decide.POLICY else None


def to_syslog(line):
    """One summary line to syslog. Under the listener it goes to stdout too, which is the container log."""
    syslog.openlog(config.CFG.name, 0, syslog.LOG_USER)
    syslog.syslog(syslog.LOG_INFO, line)
    if config.SERVE:
        print(line, flush=True)


# The fields of a decision line that the store keeps for the audit and for --force-convert, see store.decided()
KEPT_KEYS = ("app", "source", "apply", "outcome", "result", "edit_result", "label", "path", "class", "container", "abstain", "undecided",
             "invariants", "recheck", "heard", "original", "kids", "release", "ids", "tmdb", "edits", "edit_rules", "before", "repack",
             "header_repair")


def decision(rec, started):
    """Write one decision line for a file the hook, a backfill or an audit looked at, then its one-line summary to syslog,
    see report.render(). The summary says tmdb=not_asked when the run asked TMDB nothing, as in a conversion backfill.
    The store keeps the KEPT_KEYS of the line."""
    rec = report.render(dict(rec, took=round(time.time() - started, 2)), "log")
    store.decided(log(rec), rec.get("app"), rec.get("path"), config.mask(json.dumps({k: rec[k] for k in KEPT_KEYS if k in rec})))
    line = report.render(rec, "logfmt")
    to_syslog(line)
    return rec


def audio_summary(certain, doubts, samples):
    """The broken-audio verdict for the decision log, with the few numbers of each sample that decide it."""
    return {"certain": certain, "doubts": doubts, "samples": [{k: s.get(k) for k in ("at", "n", "max", "errors", "ran", "cut") + checks.AUDIO_MORE if k in s}
                                                            for s in samples]}


def track_log(ts):
    """The classification of every track, for the decision log. i is the track's position (a1, s3)."""
    return [{"i": t["pos"], "lang": t["lang"], "tag": t["tag"], "conf": t["conf"], "role": t["role"], "default": t["default"],
             "forced": t["forced_flag"], "sdh": t["sdh"], "ev": None if t["events"] is None else round(t["events"], 2), "ch": t["ch"],
             "codec": t["codec"], "name": t["title"], "heard": t.get("heard")} for t in ts]


def app_name(app):
    return f'{apps.ARR[app].name} {config.CFG.instance}'


EMBED_MAX = 5900   # Discord refuses an embed whose texts add up to more than 6000 characters


def embed(app, title, description, color, fields, footer=None):
    """A Discord embed: title, one or two sentences, the fields that have a value, a footer and a timestamp. The
    description is in Discord markdown, with its bold spans and its lines, see report.markdown(). The field names and
    values have their markdown escaped too. The longest field values are cut until all texts fit under Discord's
    6000-character limit."""
    e = {"title": title[:256], "description": report.markdown(description)[:2000], "color": config.COLORS[color],
         "fields": [{"name": report.escaped(str(k))[:256], "value": report.escaped(str(v))[:1024], "inline": False} for k, v in fields if v],
         "footer": {"text": footer or f"{config.CFG.name} on {config.CFG.instance}, {app_name(app).split()[0]}"},
         "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    size = lambda: len(e["title"]) + len(e["description"]) + len(e["footer"]["text"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
    while size() > EMBED_MAX:
        f = max(e["fields"], key=lambda f: len(f["value"]))
        f["value"] = f["value"][:max(1, len(f["value"]) - (size() - EMBED_MAX))]
    return e


def post(app, emb):
    """One embed to DISCORD_WEBHOOK, as the app ("Radarr <INSTANCE>"). A 429 waits retry_after and tries once more."""
    hook_url = config.CFG.discord_webhook
    if not hook_url: return "no webhook configured"
    body = json.loads(config.mask(json.dumps({"username": app_name(app), "embeds": [emb], "allowed_mentions": {"parse": []}})))
    try:
        try:
            apps.http(hook_url, "POST", body)
        except urllib.error.HTTPError as ex:   # Discord allows 5 posts per 2 seconds per webhook
            if ex.code != 429: raise
            time.sleep(config.DEADLINE.bound(min(float(json.loads(ex.read() or b"{}").get("retry_after", 2)), 30)))
            apps.http(hook_url, "POST", body)
        return "sent"
    except Exception as ex:
        return config.mask(f"failed: {type(ex).__name__}: {ex}")[:200]


def alert_findings(rec, size):
    """Post the findings of rec that report.posts() passes, one embed each, see report.alert_embed() and alert(). This is
    the one gate of the alerts. Returns what each finding gave, "log only" for one that stays in the decision log."""
    return [alert(rec["app"], f["kind"], rec["path"], size, e) if report.posts(f, rec) else "log only"
            for f, e in zip(rec["findings"], report.render(rec, "embed"))]


def alert(app, kind, path, size, emb):
    """Post the embed emb once per file, problem kind and size. A marker in the store remembers each one sent."""
    mark = hashlib.sha1(f"{kind}|{path}|{size}".encode()).hexdigest()
    try:   # the marker is claimed before the post, so two job processes never send the same embed
        if not store.add("alert", mark):
            return "already sent"
    except (sqlite3.Error, OSError):   # a lost marker costs one repeated embed, never the decision line
        mark = None
    sent = None
    try:
        sent = post(app, emb)
    finally:
        if sent != "sent" and mark:   # a later run tries again, also after a stop during the post
            with contextlib.suppress(sqlite3.Error, OSError):
                store.drop("alert", mark)
    return sent


def forced_note(repack):
    """What a person forced in a conversion: ", forced" for a proof refusal, ", name forced" for Sonarr's name check."""
    return (", forced" if repack.get("forced") else "") + (", name forced" if repack.get("forced_name") else "")


# The nightly audit's problems in plain words, per code of cli.audit()
UNDECIDED = {"untagged_may_be_original": "couldn't decide which audio should play first, because an untagged track may be the original language",
             "original_missing_bare_tag": "couldn't decide which audio should play first, because no track is in the original language",
             "sparse_full_title": "couldn't decide which subtitles should be on",
             "dense_forced_flag_english_only": "couldn't decide whether the forced English subtitles should be on"}
DROPPED = {"inv_audio_not_policy_target": "the wrong audio language would play first",
           "inv_audio_default_count": "the wrong number of audio tracks would play by default",
           "inv_extra_default": "a commentary or extra track would play by default",
           "inv_full_english_subtitle_on": "full English subtitles would stay on under English audio",
           "inv_only_english_subtitle_off": "the only English subtitles would be off"}
BROKEN = {"inv_audio_not_policy_target": "the wrong audio language plays first",
          "inv_audio_default_count": "the wrong number of audio tracks plays by default",
          "inv_extra_default": "a commentary or extra track plays by default",
          "inv_full_english_subtitle_on": "full English subtitles stay on under English audio",
          "inv_only_english_subtitle_off": "the only English subtitles are off"}
SKIPPED = {"header_repair_failed": "the file repair failed", "header_repair_skipped": "the file repair was skipped",
           "repack_failed": "the conversion to MKV failed", "repack_source_changed": "the app changed the file during the conversion to MKV",
           "repack_hardlinked": "the conversion to MKV was skipped, because the file has another hard link",
           "not_matroska": "the file isn't MKV, so its tracks weren't changed"}
AUDIT_CHARS = 1950   # the characters of the file list, under the 2000 that embed() keeps of a description


def audit_problem(what, r):
    """A problem that the nightly audit found in the decision line r, in plain words. what is its code, see cli.audit()."""
    code = what.partition(":")[2]
    if what == "undecided":
        return UNDECIDED.get(r.get("abstain"), "couldn't decide which tracks should play first")
    if what == "dropped":
        return "a planned track change wasn't made, because then " + report.and_list(DROPPED.get(c, c) for c in r.get("invariants") or [code])
    if what.startswith("broken:"):
        return f"after the change, {BROKEN.get(code, code)}"
    if what == "repair":
        return SKIPPED.get((r.get("header_repair") or {}).get("code"), "the file repair failed")
    if what == "convert":
        return SKIPPED.get(r.get("outcome"), "the conversion to MKV was skipped")
    return {"reprobe": "the file couldn't be read again for the check", "further": "a check after the change still finds tracks to change"}[what]


def audit_embed(app, seen, tmdb):
    """The nightly audit post, which cli.audit() sends only when a file has a problem. seen holds (the problem code or
    "changed", its decision line). One line per file, "<label>: OK" or its problems, the problems first. The lines past
    AUDIT_CHARS become "and N more". The footer names the host, and TMDB only when it had trouble that day, see
    content.tmdb_day_status()."""
    files = {}
    for what, r in seen:
        f = files.setdefault(r.get("path"), {"label": r.get("label") or "?", "problems": []})
        if what != "changed":
            f["problems"].append(audit_problem(what, r))
    lines = sorted((not f["problems"], f["label"], report.and_list(dict.fromkeys(f["problems"])) or "OK") for f in files.values())
    body, n = [], sum(not ok for ok, _, _ in lines)
    for k, (ok, label, text) in enumerate(lines):
        line = f"{report.bold(label)}: {text}"
        if sum(len(report.markdown(x)) + 1 for x in body + [line]) > AUDIT_CHARS:
            body.append(f"and {len(lines) - k} more" + (" OK" if all(x[0] for x in lines[k:]) else ""))
            break
        body.append(line)
    trouble = "TMDB rejected the key" if tmdb == "key broken" else f"TMDB didn't answer {tmdb.removeprefix('unavailable ')}" \
        if tmdb.startswith("unavailable") else None
    return embed(app, f"Audit check: {n} problem{'' if n == 1 else 's'} · {app_name(app)}", "\n".join(body), "amber", [],
                 " · ".join(x for x in (trouble, f"{config.CFG.name} on {config.CFG.instance}") if x))
