# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The decision log, its summary in syslog, and the Discord posts. report.py words them."""
import collections, contextlib, datetime, hashlib, json, os, re, sqlite3, syslog, time, urllib.error

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
    """A Discord embed: title, one or two sentences, the fields that have a value, a footer and a timestamp.
    The longest field values are cut until all texts fit under Discord's 6000-character limit."""
    e = {"title": title[:256], "description": description[:2000], "color": config.COLORS[color],
         "fields": [{"name": str(k)[:256], "value": str(v)[:1024], "inline": False} for k, v in fields if v],
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


def footer_b(tmdb=None):
    """The footer of layout B: "TMDB ok · arr-media-guard on <INSTANCE>". tmdb is a decision line's TMDB
    code or content.tmdb_day_status(). Without it the footer names the host only."""
    words = {"found": "found the item", "no_record": "has no record", "tmdb_unavailable": "unavailable", "tmdb_token_missing": "key missing",
             "tmdb_token_rejected": "key rejected", "no checks": "not asked"}
    return " · ".join(([f"TMDB {words.get(tmdb, tmdb)}"] if tmdb else []) + [f"{config.CFG.name} on {config.CFG.instance}"])


def alert_findings(rec, size):
    """Post the findings of rec, one embed each, see report.alert_embed() and alert(). Returns what each post gave."""
    return [alert(rec["app"], f["kind"], rec["path"], size, e) for f, e in zip(rec["findings"], report.render(rec, "embed"))]


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


LANG_NAMES = {"eng": "English", "spa": "Spanish", "fre": "French", "ger": "German", "ita": "Italian", "por": "Portuguese", "jpn": "Japanese",
              "kor": "Korean", "chi": "Chinese", "rus": "Russian", "hin": "Hindi", "tur": "Turkish", "dut": "Dutch", "swe": "Swedish",
              "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish", "ara": "Arabic", "heb": "Hebrew", "tha": "Thai",
              "ind": "Indonesian", "vie": "Vietnamese", "gre": "Greek", "cze": "Czech", "hun": "Hungarian", "rum": "Romanian", "ukr": "Ukrainian",
              "tam": "Tamil", "tel": "Telugu", "tgl": "Tagalog", "may": "Malay", "und": "untagged"}


def forced_note(repack):
    """What a person forced in a conversion: ", forced" for a proof refusal, ", name forced" for Sonarr's name check."""
    return (", forced" if repack.get("forced") else "") + (", name forced" if repack.get("forced_name") else "")


def change_phrases(r):
    """The changes one decision line records, as (phrase, role) in the plain words of layout B: "English audio first",
    "English subs on" with the role of the subtitle turned on, "English subs off", "foreign subs off", "fake forced
    subs off", "audio default flag fixed", "converted to MKV from AVI" (with forced_note()), "header
    repaired". Each phrase once."""
    out, before = [], {t["sel"]: t for t in r.get("before") or []}
    words = {"audio default flag fixed": "audio default flag fixed", "English subtitle off": "English subs off",
             "foreign subtitle off": "foreign subs off", "forced flag cleared": "fake forced subs off"}
    for e, rule in zip(r.get("edits") or [], r.get("edit_rules") or []):
        lang = (before.get(e[0]) or {}).get("lang") or "und"
        if rule == "audio switched" and e[1]:
            out.append((f"{LANG_NAMES.get(lang, lang)} audio first", None))
        elif rule.endswith("English subtitle on"):
            out.append(("English subs on", {"sdh": "SDH"}.get(rule.split()[0], rule.split()[0])))
        elif rule in words:
            out.append((words[rule], None))
    if (r.get("repack") or {}).get("new_size"):
        out.append((f"converted to MKV from {r.get('container')}" + forced_note(r["repack"]), None))
    code = (r.get("header_repair") or {}).get("code")
    if code in config.REPAIRED:
        out.append(({"header_repaired": "header repaired", "subtitle_trimmed": "subtitles trimmed", "subtitle_removed": "a subtitle track removed"}[code],
                    None))
    return list(dict.fromkeys(out))


def counted(word, k):
    """A problem with its file count: "1 needs another edit", "2 need another edit"."""
    return f"{k} {word if k == 1 else word.replace('needs ', 'need ').replace('breaks ', 'break ')}"


def show_value(files, problems, n):
    """The plain sentence of one show's changes for layout B. files maps a path to its change_phrases(), problems maps a
    problem to its file count, and n is the show's file count. A phrase that not every file shares gets its count. The
    English subs turned on get their roles when the roles differ: "English subs on (16 full, 3 forced)"."""
    count, roles = collections.Counter(), {}
    for phrases in files.values():
        for phrase, role in phrases:
            count[phrase] += 1
            if role:
                roles.setdefault(phrase, collections.Counter())[role] += 1
    parts = [f"{p} ({', '.join(f'{v} {r}' for r, v in roles[p].most_common())})" if len(roles.get(p) or {}) > 1 else p + ("" if k == n else f" ({k})")
             for p, k in count.items()]
    parts += [counted(w, k) for w, k in problems.items()]
    text = ", ".join(parts) or "no change"
    return text[:1].upper() + text[1:]


def layout_b(app, start, seen, tmdb):
    """The nightly audit embed, layout B. The description says "All clean" or what needs a look, then
    the file count and the start of the window. One field per show or film, "<show> · <count>" with show_value(), the
    10 largest, then "and N more". footer_b() names the day's TMDB state and the host. No emoji and no check mark."""
    shows = {}
    for what, r in seen:
        g = shows.setdefault(re.sub(r" S\d+(E\d+)+$", "", r.get("label") or "?"), {"files": {}, "problems": {}, "paths": set()})
        g["paths"].add(r.get("path"))
        if what == "changed":
            g["files"][r.get("path")] = list(dict.fromkeys(g["files"].get(r.get("path"), []) + change_phrases(r)))
        else:
            g["problems"].setdefault(what, set()).add(r.get("path"))
    rows = sorted(((name, len(g["paths"]), show_value(g["files"], {w: len(p) for w, p in g["problems"].items()}, len(g["paths"])))
                   for name, g in shows.items()), key=lambda x: (-x[1], x[0]))
    trouble = collections.Counter(w for w, _ in {(w, r.get("path")) for w, r in seen if w != "changed"})
    head = ("**All clean.** Nothing needs another edit, nothing undecided, no rule broken." if not trouble else
            "**Needs a look.** " + ", ".join(counted(w, k) for w, k in sorted(trouble.items())) + ".")
    n = sum(x[1] for x in rows)
    fields = [(f"{name} · {k}", value) for name, k, value in rows[:10]]
    if len(rows) > 10:
        fields.append((f"and {len(rows) - 10} more", ", ".join(f"{name} · {k}" for name, k, _ in rows[10:])[:1000]))
    return embed(app, f"Edit audit · {app_name(app)}", f"{head}\n**{n} file{'' if n == 1 else 's'}** since {start:%a %d %b %H:%M}",
                 "amber" if trouble else "green", fields, footer_b(tmdb))
