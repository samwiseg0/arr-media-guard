# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Subtitle hunter: replace a Radarr movie file that has no English subtitle with a release that has one.

  arr-media-guard-subhunt radarr --ids ID [ID ...] [--apply] [--force]

Some foreign films have no English subtitle track. Radarr deletes the old file when it imports a new one. Its recycle
bin may keep it, and the hunter never relies on that. So the hunter downloads outside Radarr,
checks the file, and imports only a file that passes.
It works on one movie at a time.

1. It skips a movie whose file has a full English subtitle. It also skips a movie an earlier run gave up on, or
   whose import failed. --force hunts those again.
2. It sends one NZBHydra2 query by IMDb id. Radarr's parser and the movie's profile filter the results. See rank().
   The rest rank by how likely they carry English subtitles, then by Radarr's format score, then by date.
3. At most 3 candidates per movie go to SABnzbd with no category, so Radarr never sees them. SABnzbd fetches every
   NZB at once, because a Hydra link dies after about 30 minutes. The first job downloads, the others wait paused.
4. A download passes with a full English subtitle track, the listed runtime within 10 percent, audio in the original
   language, and audio that decodes. A ManualImport in copy mode then replaces the file, under the hook's file lock.
   A hard link keeps the old file until the new one is in place.
5. A rejected download is deleted and recorded in the hunter's state in the store, never in Radarr's blocklist. When
   every candidate is rejected, the current file stays and one Discord embed goes to DISCORD_WEBHOOK.

Dry by default. A dry run makes the one Hydra query, prints the ranked candidates and downloads nothing. Every step
writes a decision line with source "subhunt". The hunter uses the package arr_media_guard: the settings, the app adapter
and its command wait, the probe, the locks, the decision log and the Discord post. The package never imports the hunter.
It handles Radarr only. An episode needs its own search.
"""
import argparse, datetime, email.utils, os, re, shutil, signal, sys, time, urllib.parse, uuid

from arr_media_guard import apps, checks, config, decide, logs, report, runner, store

CAP = 3                      # rejected candidates per movie. Then the movie is no_subbed_release until --force.
GRAB_WAIT = 120              # seconds SABnzbd may take to fetch the NZBs. It retries a dead link without pause.
DOWNLOAD_WAIT = 3 * 3600     # seconds one download may take
IMPORT_WAIT = 1800           # seconds Radarr's ManualImport may take
POLL = 30                    # seconds between two SABnzbd status reads
PACE = 3                     # seconds between two movies, so the Hydra queries never come in a burst
RUNTIME_SLACK = 0.10         # a download may run 10 percent off the listed runtime
VETO = -100                  # a format the profile scores this low rejects a release. A softer score only ranks it lower.
FLOOR = {1080: 15, 720: 8}   # MiB a minute. A 1080p film under 15 is not 1080p.
SUB_ROLES = ("full", "sdh")  # the English subtitle roles that count. A forced or signs track translates only a few lines.
VIDEO = (".mkv", ".mp4", ".m4v", ".avi")
SAFE_LABELS = ("DUPLICATE", "ALTERNATIVE", "PROPAGATING")   # pause labels a resume may override. ENCRYPTED or TOO LARGE reject.
LID_TIMEOUT = 120            # seconds one lid.py run may take, the same limit the hook gives it
HARDSUB = re.compile(r"(?<![a-z])(hc|hcsubs?|hardsubs?|hardcoded|korsubs?)(?![a-z])", re.I)
SIGNALS = (   # (pattern on the release name after its year, points, label). A dry run prints the labels.
    (r"multi.?subs?|(?<![a-z])(en|eng|english)[ ._-]?(subs?|subtitles?)(?![a-z])", 4, "English subtitles tag"),   # "NL subs" is not
    (r"(?<![a-z])(nf|amzn|atvp|dsnp|max|hmax|hulu|pcok|pmtp|it)(?![a-z])", 3, "streaming source"),
    (r"criterion", 2, "Criterion"),
    (r"(?<![a-z])(multi|dual)(?![a-z])", 1, "multi audio"),
    (r"(?<![a-z])(nordic|swedish|danish|norwegian|finnish|french|truefrench|vff|vfq|german|italian|spanish|castellano|dutch|polish"
     r"|czech|russian|turkish)(subs?)?(?![a-z])", -2, "local release"),
)
def norm(title):
    """A release name reduced to letters and digits, so two posts of one release compare equal."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def scrub(text):
    """An error text without secrets. A Hydra NZB link and every SABnzbd call carry a key in the URL."""
    return re.sub(r"(apikey=)[^&\s'\"]+", r"\1<key>", config.mask(str(text)))[:300]


def creds(app):
    """SABnzbd and NZBHydra2 URLs from the app's API, see apps.hunter_urls(), and their keys from SABNZBD_API_KEY and
    NEWZNAB_API_KEY, because the API masks both keys."""
    keys = {"SABNZBD_API_KEY": config.CFG.sabnzbd_api_key, "NEWZNAB_API_KEY": config.CFG.newznab_api_key}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} in the env file or the environment. {app.capitalize()}'s API hides the keys of its download "
                 "clients and indexers.")
    try:
        urls = apps.hunter_urls(app)
    except FileNotFoundError as ex:   # api_key() found no key and no config.xml, as apps.app_list() handles it
        sys.exit(apps.no_key(app, ex) or f"{app.capitalize()} is not set up. Set {config.env_key(app)}_URL and {config.env_key(app)}_API_KEY.")
    pair = (urls["sab"], urls["hydra"])
    whys = [why for url, why in pair if url is None] + [why for url, why in pair if url == ""]   # a kind not saved first, as before
    if whys:
        sys.exit(whys[0])
    return {"sab": urls["sab"][0], "sab_key": keys["SABNZBD_API_KEY"], "hydra": urls["hydra"][0], "hydra_key": keys["NEWZNAB_API_KEY"]}


def pubdate(d):
    """Hydra's JSON gives pubDate in epoch seconds or milliseconds. Returns an ISO UTC string, or the text as it came."""
    if isinstance(d, (int, float)):
        return datetime.datetime.fromtimestamp(d / 1000 if d > 1e11 else d, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return email.utils.parsedate_to_datetime(d).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return str(d or "")


def search(cred, m):
    """One fresh NZBHydra2 movie query, as a list of items. Hydra asks every indexer behind
    it, so this costs one API hit per indexer. The links in the answer live about 30 minutes, so nothing stores them."""
    q = {"t": "movie", "imdbid": m["imdbId"][2:]} if m.get("imdbId") else \
        {"t": "search", "cat": "2000", "q": f'{m["title"]} {m.get("year") or ""}'.strip()}   # 2000 is newznab's movie category
    data = apps.http(f'{cred["hydra"]}?{urllib.parse.urlencode(dict(q, apikey=cred["hydra_key"], o="json", limit=1000))}', timeout=180)
    items = (data.get("channel") or {}).get("item") or []
    out = []
    for it in [items] if isinstance(items, dict) else items:
        attrs = {x["name"]: x.get("value") for a in it.get("attr") or [] for x in [a.get("attributes") or a.get("@attributes") or {}] if "name" in x}
        enc = it.get("enclosure") or {}
        enc = enc.get("attributes") or enc.get("@attributes") or {}
        out.append({"title": it.get("title"), "size": int(attrs.get("size") or enc.get("length") or 0), "guid": it.get("guid"),
                    "link": it.get("link"), "pubDate": pubdate(it.get("pubDate")), "indexer": attrs.get("hydraIndexerName"), "attrs": attrs})
    return out


def tail(title):
    """The release name after its year, where the source and language tags sit. The movie's own title never scores."""
    m = re.search(r"(?<![0-9])(19|20)[0-9]{2}(?![0-9])", title)
    return title[m.end():] if m else title


def imdb_digits(x):
    return re.sub(r"\D", "", str(x or "")).lstrip("0")


def rank(app, m, results, tried, ctx):
    """(candidates best first, {skip reason: [names]}) for one movie. One post per name counts, the newest.

    Radarr's own parser decides the movie, the quality and the formats of each name, so the filters are the app's
    rules. A candidate is this movie, in a quality the profile allows, at no lower resolution than the current file.
    It has no format the profile scores VETO or lower, and its size fits the runtime. It is not hardcoded, not the
    current release, and not in tried."""
    prof = ctx["profiles"][m["qualityProfileId"]]
    allowed = {q["quality"]["id"] for i in prof["items"] if i.get("allowed") for q in ([i] if i.get("quality") else i.get("items") or [])}
    veto = {fi["format"]: fi["name"] for fi in prof.get("formatItems") or [] if fi["score"] <= VETO}
    f = apps.movie_file(m, app)   # the record movieFileId names, see apps.movie_file()
    have = ((f.get("quality") or {}).get("quality") or {}).get("resolution") or 0
    before, current, runtime = {norm(t["title"]) for t in tried}, norm(f.get("sceneName")), m.get("runtime") or 0
    newest, tags, ours = {}, {}, imdb_digits(m.get("imdbId"))
    for r in results:   # old posts die first on the provider
        k = norm(r.get("title"))
        if k and r.get("link") and (k not in newest or r["pubDate"] > newest[k]["pubDate"]):
            newest[k] = r
        tags.setdefault(k, set()).add(imdb_digits((r.get("attrs") or {}).get("imdb")))
    cands, skipped = [], {}
    for k, r in newest.items():
        why = "tried before" if k in before else "the current file" if k == current else "hardcoded subtitles" if HARDSUB.search(r["title"]) else None
        if not why:
            p = apps.arr(app, "parse?" + urllib.parse.urlencode({"title": r["title"]}))
            pm = p.get("parsedMovieInfo") or {}
            qq = (pm.get("quality") or {}).get("quality") or {}
            # An indexer's IMDb tag wins over the name, because Radarr may map a film to another film of the same name.
            # Indexers also disagree on one post, so a name is this movie when any of its posts carries this movie's tag.
            tagged = tags[k] - {""}
            mine = ours in tagged if tagged and ours else (p.get("movie") or {}).get("id") == m["id"]
            fmts = [veto[c["id"]] for c in p.get("customFormats") or [] if c.get("id") in veto]
            size = ctx["sizes"].get(qq.get("id")) or {}
            per_min = r["size"] / 2**20 / runtime if runtime else None
            lo, hi = max(size.get("minSize") or 0, FLOOR.get(qq.get("resolution"), 0)), size.get("maxSize")
            if not pm: why = "Radarr cannot parse the name"
            elif not mine: why = "another movie"
            elif qq.get("id") not in allowed: why = f'{qq.get("name")} is not in the profile'
            elif (qq.get("resolution") or 0) < have: why = f"below the current {have}p"
            elif fmts: why = "format " + fmts[0]
            elif per_min is not None and (per_min < lo or (hi and per_min > hi)): why = "the size does not fit the runtime"
            else:
                sig = [(pts, label) for rx, pts, label in SIGNALS if re.search(rx, tail(r["title"]), re.I)]
                cands.append(dict(title=r["title"], link=r["link"], indexer=r.get("indexer"), size=r["size"], pubDate=r["pubDate"],
                                  score=sum(pts for pts, _ in sig), signals=[label for _, label in sig], cf=p.get("customFormatScore") or 0,
                                  quality=qq.get("name"), resolution=qq.get("resolution") or 0))
        if why:
            skipped.setdefault(why, []).append(r["title"])
    cands.sort(key=lambda c: (c["score"], c["cf"], c["resolution"], c["pubDate"]), reverse=True)
    return cands, skipped


def brief(c):
    """A candidate for the decision log and Discord. Never the link, which carries Hydra's key."""
    return {k: c[k] for k in ("title", "indexer", "size", "pubDate", "score", "signals", "cf", "quality")}


def sab(cred, **q):
    return apps.http(f'{cred["sab"]}?{urllib.parse.urlencode(dict(q, output="json", apikey=cred["sab_key"]))}', timeout=30)


def add(cred, c, paused):
    """Hand one NZB link to SABnzbd with no category, so Radarr never sees the job. Returns the nzo id."""
    r = sab(cred, mode="addurl", name=c["link"], nzbname=c["title"], priority=-2 if paused else 0)
    if not (r.get("status") and r.get("nzo_ids")):
        raise RuntimeError(f"SABnzbd refused the NZB: {r.get('error') or r}")
    return r["nzo_ids"][0]


def job_state(cred, nzo):
    """("queue" or "history", slot) of a SABnzbd job, or (None, None) when SABnzbd no longer has it."""
    for where in ("queue", "history"):
        slots = (sab(cred, mode=where, nzo_ids=nzo, limit=100).get(where) or {}).get("slots") or []
        s = next((s for s in slots if s.get("nzo_id") == nzo), None)
        if s:
            return where, s
    return None, None


def drop(cred, nzo):
    """Delete a job from SABnzbd's queue and history, with its files."""
    for where in ("queue", "history"):
        sab(cred, mode=where, name="delete", value=nzo, del_files=1)


def wait_fetched(cred, nzos):
    """The jobs SABnzbd has not fetched after GRAB_WAIT seconds, deleted at once. SABnzbd retries a dead Hydra link
    without pause, and many such jobs can take Hydra down."""
    deadline = time.time() + GRAB_WAIT
    while True:
        stuck = [n for n in nzos if (job_state(cred, n)[1] or {}).get("status") == "Grabbing"]
        if not stuck or time.time() >= deadline:
            break
        time.sleep(5)
    for n in stuck:
        drop(cred, n)
    return stuck


def wait_download(cred, nzo):
    """(storage path, None, True) when the job completed. Otherwise (None, reason, final). final is False for a reason
    that may pass on a later run: a timeout, or a job SABnzbd lost. A paused job is resumed, which starts a waiting
    candidate. A job SABnzbd paused for a safety reason (ENCRYPTED, UNWANTED, TOO LARGE) is never resumed."""
    deadline, missed = time.time() + DOWNLOAD_WAIT, 0
    while time.time() < deadline:
        try:
            where, s = job_state(cred, nzo)
        except Exception:   # SABnzbd restarting or busy: read again at the next poll
            where, s = "queue", {}
        missed = missed + 1 if where is None else 0
        if missed >= 2:   # SABnzbd can miss a job for one read, between its queue and its history
            return None, "SABnzbd no longer has the job", False
        if where == "history" and s.get("status") == "Completed":
            return s.get("storage"), None, True
        if where == "history" and s.get("status") == "Failed":
            return None, f'SABnzbd failed the download ({s.get("fail_message") or "no message"})', True
        if where == "queue" and s.get("status") == "Paused":
            unsafe = [str(x) for x in s.get("labels") or [] if not str(x).upper().startswith(SAFE_LABELS)]
            if unsafe:
                return None, "SABnzbd paused the job as " + ", ".join(unsafe), True
            sab(cred, mode="queue", name="resume", value=nzo)
        time.sleep(POLL)
    return None, f"the download did not finish in {DOWNLOAD_WAIT // 3600} hours", False


def find_video(storage):
    """The largest video file of a finished download, or None. A sample never counts."""
    if os.path.isfile(storage):
        return storage if storage.lower().endswith(VIDEO) else None
    found = [os.path.join(d, n) for d, _, names in os.walk(storage) for n in names if n.lower().endswith(VIDEO) and "sample" not in n.lower()]
    return max(found, key=os.path.getsize, default=None)


def cleanup(cred, nzo, storage, title, roots):
    """Delete a download: the SABnzbd job, then the folder on disk. Returns what happened and never raises.
    The folder goes only when it sits outside the app's root folders, three levels deep, and carries the job's name."""
    try:
        drop(cred, nzo)
        if not storage or not os.path.exists(storage):
            return "deleted the SABnzbd job"
        p = os.path.realpath(storage)
        inside = any(p == r or p.startswith(r + "/") for r in map(os.path.realpath, roots))
        if inside or p.count("/") < 3 or norm(title)[:12] not in norm(os.path.basename(p)):
            return f"deleted the SABnzbd job and kept {storage}, which does not look like this download's folder"
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        return f"deleted the SABnzbd job and {storage}"
    except Exception as ex:
        return scrub(f"the delete failed with {type(ex).__name__}: {ex}")


def english(ts):
    """A full or SDH English subtitle track in decide's classification."""
    return any(t["kind"] == "s" and t["lang"] == "eng" and t["role"] in SUB_ROLES for t in ts)


def heard(path, index, j, want):
    """The language lid.py hears on one audio stream, or None when the install or a sure answer is missing.
    checks.lid_run() reads the install paths from the env file and kills the whole process group on a timeout."""
    try:
        return checks.lid_run(path, index, j, sorted(want), LID_TIMEOUT).get("lang")
    except Exception:   # a signal only, so no answer lets the track pass
        return None


def verdict(path, j, ts, original, runtime, kids, release):
    """What keeps a download out, as a list of reasons. Empty means it may replace the current file.

    One limit remains. An English subtitle with no forced flag, no title and no mkvmerge statistics counts as full.
    So a forced-only track of that kind passes. decide.classify() has no other signal for it."""
    out = []
    if not english(ts):
        out.append(f'no full English subtitle (subtitles: {", ".join(t["lang"] + " " + t["role"] for t in ts if t["kind"] == "s") or "none"})')
    minutes = decide.duration(j) / 60
    if runtime and abs(minutes - runtime) > RUNTIME_SLACK * runtime:
        out.append(f"it runs {minutes:.0f} minutes, and the listed runtime is {runtime}")
    want, au = decide.codes(original), [t for t in ts if t["kind"] == "a"]
    main = [(i, t) for i, t in enumerate(au) if t["role"] == "main"]
    if want and not any(t["lang"] in want for _, t in main):
        blind = [i for i, t in main if not t["conf"]]   # untagged, and no title names a language: unknown, not wrong
        lang = heard(path, blind[0], j, want) if blind else None
        if not blind:
            out.append(f'no {original} audio (audio: {", ".join(t["lang"] for t in au) or "none"})')
        elif lang and lang not in want:
            out.append(f"no {original} audio, the untagged track sounds like {lang}")
    if not out:   # three ffmpeg samples, only for a file that passes the rest
        edits = decide.decide(j, original, kids, release)["edits"] if path.lower().endswith(".mkv") else []
        certain = checks.check_audio(path, j, edits, runtime)[0]
        if certain:
            out.append(f"broken audio: {certain}")
    return out


def replace(app, m, video, remember):
    """ManualImport of the checked file over the current one, in copy mode. The caller holds the hook's file lock.
    Returns None when the new file is in place, else what went wrong as sentences for the alert.

    Radarr deletes the old file before it copies the new one, into its recycle bin when one is set. So a hard link
    keeps the old file until the new one is in place, and a failed import puts it back. The link has a hidden name and no video
    extension, so Radarr's disk scan and Plex skip it. remember(path) records the link in the hunter's state before the
    link exists, so a kill at any later point leaves a record. The caller clears the record once the link is gone.
    Copy mode only reads the download. A move also deletes it, and on a NAS that can fail after Radarr has deleted
    the old file."""
    old = apps.movie_file(m, app)
    now = apps.movie_file(apps.arr(app, f"movie/{m['id']}"), app)
    if now.get("id") != old["id"]:   # an upgrade during a long download. Never replace a file nobody checked.
        return "Radarr changed the movie file during the download, so the new release wasn't imported."
    items = apps.arr(app, "manualimport?" + urllib.parse.urlencode({"folder": os.path.dirname(video), "filterExistingFiles": "false"}))
    it = next((i for i in items if i.get("path") == video), None)
    if not it:
        return "Radarr does not list the file for import."
    path = now["path"]   # the path now, in case Radarr renamed the file during the download
    keep = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.subhunt-keep")
    unsure = f"A copy of the old file stays at {keep}."
    size = os.path.getsize(video)   # before the record, so a failed read never leaves a link that blocks the movie
    remember(keep)
    try:
        os.link(path, keep)
    except OSError as ex:
        return f"Couldn't keep a copy of the old file at {keep} ({type(ex).__name__}), so the new release wasn't imported."
    try:
        cmd = apps.arr_write(app, "command", "POST", {"name": "ManualImport", "importMode": "copy", "files": [
            {"path": video, "movieId": m["id"], "quality": it.get("quality"), "languages": [m["originalLanguage"]],
             "releaseGroup": it.get("releaseGroup") or ""}]})
    except Exception as ex:
        if isinstance(ex, TimeoutError) or isinstance(getattr(ex, "reason", None), TimeoutError):
            return f"The import command timed out, and Radarr may have queued it. {unsure}"   # nothing moves back
        return f"Radarr refused the import command with {type(ex).__name__}: {ex}. " + restore(path, keep)
    try:
        cmd = apps.ARR[app].wait(cmd, IMPORT_WAIT)
        new = apps.movie_file(apps.arr(app, f"movie/{m['id']}"), app)
    except Exception as ex:   # Radarr's state is unknown, so the link stays
        return f"Reading Radarr after the import failed with {type(ex).__name__}: {ex}. {unsure}"
    if cmd.get("status") not in apps.DONE:   # Radarr may still land the copy, so nothing moves back
        return f"Radarr was still importing after {IMPORT_WAIT // 60} minutes. {unsure}"
    if new.get("id") != old["id"] and new.get("size") == size:
        try:
            os.remove(keep)
        except OSError:   # the import landed. The record stays, and the caller names the link.
            pass
        return None
    return (f'The import command ended {cmd.get("status")}. '
            + (f'Radarr lists file id {new["id"]} with {new.get("size")} bytes. ' if new else "Radarr lists no movie file. ")
            + restore(path, keep))


def restore(path, keep):
    """Put the current file back from its keep link after a failed import. Returns one or two sentences."""
    try:
        if os.path.exists(path) and os.path.samefile(path, keep):
            os.remove(keep)
            return f"The current file is untouched at {path}."
        if os.path.exists(path):
            return f"Another file now sits at {path}. A copy of the old file stays at {keep}."
        os.rename(keep, path)
        return f"The current file is back at {path}. Radarr may not list it until its next rescan of the movie."
    except OSError as ex:
        return f"A copy of the old file stays at {keep}. Moving it back failed with {type(ex).__name__}."


def show(label, f, ts, results, cands, picks, skipped):
    subs = ", ".join(t["lang"] + " " + t["role"] for t in ts if t["kind"] == "s") or "none"
    print(f'{label}: {f.get("sceneName") or os.path.basename(f["path"])} has no full English subtitle (subtitles: {subs})')
    print(f"  {len(results)} results, {len(cands)} candidates, {len(picks)} to try")
    for i, c in enumerate(cands[:10], 1):
        print(f'  {"*" if c in picks else " "}{i:>2} {c["score"]:+d} cf {c["cf"]:+d} {c["quality"] or "?":<13} {c["size"] / 1e9:5.1f}G '
              f'{c["pubDate"][:10]} {(c["indexer"] or "")[:12]:<12} {c["title"][:90]} {", ".join(c["signals"])}')
    for why, names in sorted(skipped.items(), key=lambda kv: -len(kv[1])):
        print(f"  skipped {len(names)}: {why} (for example {names[0][:70]})")
    sys.stdout.flush()


def hunt(app, mid, a, cred, ctx):
    """One movie: skip it, list its candidates, or download, check and import them. Returns the last decision record."""
    started = time.time()
    m = apps.arr(app, f"movie/{mid}")
    label, original, runtime, want, kids, _ = apps.movie_item(m, apps.kids_profiles(app))
    f = apps.movie_file(m, app)
    base = dict(app=app, source="subhunt", apply=a.apply, label=label, path=f.get("path"), original=original,
                ids={"app_id": mid, "file_id": f.get("id"), "guids": want["guids"]})
    note = lambda code, result, t0=started, **kw: logs.decision(dict(base, id=uuid.uuid4().hex[:12], result=result, outcome=code, **kw), t0)
    item = store.get(f"subhunt-{app}", str(mid)) or {"tried": [], "status": "open"}

    def save():
        store.put(f"subhunt-{app}", str(mid), item)

    shown = report.link(label, apps.ARR[app].page(m.get("titleSlug")))   # the movie's name, a link to its page in Radarr

    def post(title, text, color, release):
        return logs.post(app, logs.embed(app, title, text, color, [("Title", shown), ("Release", release), ("App", logs.app_name(app))]))

    if item.get("keep") and os.path.exists(item["keep"]):   # an earlier run left the old file linked. Never go past it.
        text = (f'An earlier run left a copy of the old file of {label} at {item["keep"]}. Radarr may list the new file, the old '
                "file or no file. The search for English subtitles skips this movie while the copy is there.")
        return note("subhunt_stopped", f'subhunt stopped, the keep link {item["keep"]} from an earlier run is unresolved.',
                    alert_result=[post("Old file left by an earlier run", text, "red", item.get("release") or "")] if a.apply else [])
    item.pop("keep", None)   # recorded, but the link is gone
    if not f:
        return note("subhunt_skipped", "subhunt skipped, the movie has no file to replace.")
    if item["status"] == "no_subbed_release" and not a.force:
        return note("subhunt_skipped", "subhunt skipped, an earlier run found no release with English subtitles. --force hunts again.", tried=item["tried"])
    if item["status"] == "import_failed" and not a.force:
        return note("subhunt_skipped", f'subhunt skipped, an earlier import failed. The checked download is in {item.get("kept")}. --force hunts again.')
    ts = decide.classify(checks.mkvmerge(f["path"]))
    if english(ts):
        return note("subhunt_skipped", "subhunt skipped, the current file has a full English subtitle.", tracks=logs.track_log(ts))
    final = [t for t in item["tried"] if t.get("final", True)]   # a timeout or a lost job is not final, so --force retries it
    results = search(cred, m)
    cands, skipped = rank(app, m, results, final if a.force else item["tried"], ctx)
    picks = cands[:max(0, CAP if a.force else CAP - len(final))]
    show(label, f, ts, results, cands, picks, skipped)
    if not a.apply:
        return note("subhunt_dry_run", f"subhunt dry run: {len(cands)} candidates of {len(results)} results, would try {len(picks)}",
                    candidates=[brief(c) for c in picks], skipped={k: len(v) for k, v in skipped.items()}, tracks=logs.track_log(ts))
    if not cands:   # nothing to try yet. A new release may appear, so the movie stays open and nobody is alerted.
        return note("subhunt_stopped", f"subhunt stopped, no candidate among {len(results)} results. A later run searches again.",
                    skipped={k: len(v) for k, v in skipped.items()})

    def remember(keep):
        item["keep"] = keep
        save()

    def failed(c, why, t0, final, **kw):
        item["tried"].append({"title": c["title"], "indexer": c["indexer"], "why": why, "final": final, "time": int(time.time())})
        save()
        print(f'  rejected {c["title"][:80]}, {why}', flush=True)
        return note("subhunt_rejected", f'subhunt rejected {c["title"]}: {why}', t0, candidate=brief(c), final=final, **kw)

    jobs, done, passing = [], set(), 0   # done holds the jobs this run deleted, or kept on purpose
    try:
        for c in picks:   # every NZB now, while the Hydra links live. A network error stops the run and marks nothing.
            try:
                jobs.append((c, add(cred, c, paused=bool(jobs))))
            except RuntimeError as ex:   # SABnzbd answered and refused this NZB
                failed(c, scrub(str(ex)), time.time(), False)
                passing += 1
        if jobs:
            note("subhunt_queued", f"subhunt queued {len(jobs)} releases in SABnzbd", candidates=[brief(c) for c, _ in jobs])
        dead = wait_fetched(cred, [n for _, n in jobs])
        for c, nzo in jobs:
            t0 = time.time()
            if nzo in dead:
                done.add(nzo)
                failed(c, f"SABnzbd could not fetch the NZB in {GRAB_WAIT} seconds", t0, False)
                passing += 1
                continue
            storage, why, final = wait_download(cred, nzo)
            storage = apps.mapped(storage, app)   # SABnzbd's path, which the app's path map pairs with the local path
            video = find_video(storage) if storage else None
            if storage and not video:
                why, final = f"no video file in {storage} on this host", True
            ts = []
            if video:
                try:
                    j = checks.mkvmerge(video)
                    ts = decide.classify(j)
                    reasons = verdict(video, j, ts, original, runtime, kids, c["title"])
                except Exception as ex:   # a truncated file or a probe timeout rejects this candidate, never the movie
                    reasons = [scrub(f"the check failed with {type(ex).__name__}: {ex}")]
                if not reasons:
                    done.add(nzo)   # the download stays until the new file is in place
                    try:
                        with runner.locked():   # the hook's file lock. Its worker probes the new file after this block.
                            err = replace(app, m, video, remember)
                            gone = None if err else cleanup(cred, nzo, storage, c["title"], ctx["roots"])
                    except Exception as ex:
                        err = f"The import stopped with {type(ex).__name__}: {ex}."
                    if item.get("keep") and not os.path.exists(item["keep"]):
                        item.pop("keep")   # removed after the landing, or moved back
                    if err:
                        err = scrub(err)
                        item.update(status="import_failed", kept=storage, time=int(time.time()))
                        save()
                        text = f"Radarr did not replace the file of {label} with a checked release. {err} The download stays in {storage}."
                        return note("subhunt_import_failed", f"subhunt import failed: {err}", t0, candidate=brief(c), tracks=logs.track_log(ts),
                                    alert_result=[post("Release with English subtitles not imported", text, "red", c["title"])])
                    item.update(status="imported", release=c["title"], time=int(time.time()))
                    save()
                    text = (f"Replaced the file of {label} with a release that has full English subtitles. A copy of the old file "
                            f'stays at {item.get("keep")}, because its removal failed.')
                    # a fix that worked goes to the decision log only. A keep link left behind posts.
                    return note("subhunt_imported", f'subhunt imported {c["title"]}', t0, candidate=brief(c), tracks=logs.track_log(ts), cleanup=gone,
                                alert_result=[post("English subtitles found, old file left", text, "amber", c["title"]) if item.get("keep")
                                              else "log only"])
                why, final = ". ".join(reasons), True
            gone = cleanup(cred, nzo, storage, c["title"], ctx["roots"])
            done.add(nzo)
            failed(c, why, t0, final, tracks=logs.track_log(ts), cleanup=gone)
            passing += not final
        if passing:   # a timeout or a lost job may pass later, so the movie stays open
            return note("subhunt_stopped", f"subhunt stopped, {passing} of {len(picks)} releases failed for a reason that may pass. "
                        "A later run tries other releases, and --force retries these.")
        item.update(status="no_subbed_release", time=int(time.time()))
        save()
        tried = "\n".join(f'{t["title"]}, {t["why"]}' for t in item["tried"])
        text = (f"No release of {label} passed the check, so the current file stays. Later searches for English subtitles skip "
                "this movie.")
        sent = logs.post(app, logs.embed(app, "No release with English subtitles", text, "amber",
                                   [("Title", shown), ("Tried", tried), ("App", logs.app_name(app))]))
        return note("no_subbed_release", f'no_subbed_release: tried {len(item["tried"])}', tried=item["tried"], alert_result=[sent])
    finally:   # the paused jobs a pass never reached, and every job of a run that broke off, SIGTERM included
        for c, nzo in jobs:
            if nzo not in done:
                try:
                    storage = apps.mapped((job_state(cred, nzo)[1] or {}).get("storage"), app)
                except Exception:
                    storage = None
                print("  " + cleanup(cred, nzo, storage, c["title"], ctx["roots"]), flush=True)


def main(argv):
    """arr-media-guard-subhunt <app> --ids ID ... [--apply] [--force]."""
    ap = argparse.ArgumentParser(prog="arr-media-guard-subhunt", description="Replace the file of each Radarr movie that has no "
                                 "English subtitle with a release that has one. A dry run ranks the releases and downloads nothing. "
                                 "See docs/features.md.")
    ap.add_argument("app", choices=sorted(a for a in config.CFG.apps if config.program(a) == "radarr"), help="the Radarr instance")
    ap.add_argument("--ids", type=int, nargs="+", required=True, metavar="ID", help="the Radarr movie ids")
    ap.add_argument("--apply", action="store_true", help="download, check and import. Without it the run is dry and changes nothing")
    ap.add_argument("--force", action="store_true", help="hunt again for a movie that an earlier run gave up on, or whose import failed")
    a = ap.parse_args(argv)
    logs.status("policy", "failed" if decide.POLICY is None else "ok", config.POLICY_ERROR or "")   # as each mode of the core records it
    if decide.POLICY is None:
        sys.exit(config.policy_help())
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # a kill runs the finally cleanup, as Ctrl-C does
    run_lock = runner.try_lock("subhunt.lock") if a.apply else None   # held until main returns
    if a.apply and not run_lock:   # two runs would overwrite each other's state and download at once
        sys.exit("another subtitle hunter run is active")
    cred = creds(a.app)
    ctx = {"profiles": {p["id"]: p for p in apps.arr(a.app, "qualityprofile")},
           "sizes": {d["quality"]["id"]: d for d in apps.arr(a.app, "qualitydefinition")},
           "roots": [r["path"] for r in apps.arr(a.app, "rootfolder")]}
    print(f"subtitle hunter, {a.app}, {'APPLY' if a.apply else 'dry run'}, {len(a.ids)} movies", flush=True)
    for n, mid in enumerate(a.ids):
        if n:
            time.sleep(PACE)
        started = time.time()
        try:
            rec = hunt(a.app, mid, a, cred, ctx)
        except Exception as ex:   # one movie's failure never stops the next
            rec = logs.decision(dict(id=uuid.uuid4().hex[:12], app=a.app, source="subhunt", apply=a.apply, ids={"app_id": mid},
                                  result=scrub(f"error: {type(ex).__name__}: {ex}"), outcome="error"), started)
        print(f'  {mid}: {rec["result"][:200]}', flush=True)
