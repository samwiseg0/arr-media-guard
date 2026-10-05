# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Sonarr and Radarr behind one interface, ARR[app] for each instance app, with the HTTP client, the API keys and the path maps."""
import contextlib, contextvars, datetime, json, os, re, sys, time, urllib.parse, urllib.request

from . import config, convert, decide, logs, plex, regrab, vault


def http(url, method="GET", body=None, headers=None, timeout=15):
    """One HTTP call. Its timeout is cut to the job's time left. A call that ends past it raises OutOfTime."""
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "arr-media-guard", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=config.DEADLINE.bound(timeout)) as r:
            raw = r.read()
    finally:
        config.DEADLINE.check()
    return json.loads(raw) if raw.strip()[:1] in (b"{", b"[") else raw


def app_dir(app):
    """The app's own folder, RADARR_DIR or SONARR_DIR. The script reads config.xml there."""
    return config.CFG.apps[app].dir


def api_key(app):
    """The app's API key: <APP>_API_KEY from the env file, else the key in config.xml in app_dir(). The key alone serves
    every mode."""
    return config.CFG.apps[app].api_key or \
        re.search(r"<ApiKey>([^<]+)</ApiKey>", open(os.path.join(app_dir(app), "config.xml")).read()).group(1)


# The app URLs that examples/arr-media-guard.env, docker/arr-media-guard.env and docker/compose.yml hold. A URL the user
# left as shipped sets up no app by itself, see no_key(). The image writes both env files into a new env file, so a
# one-off container with no app has both URLs. A Radarr-only user may leave the Sonarr lines of compose.yml as shipped.
SHIPPED_URLS = {"radarr": ("http://127.0.0.1:7878", "http://radarr:7878", "http://CHANGE_ME:7878"),
                "sonarr": ("http://127.0.0.1:8989", "http://sonarr:8989", "http://CHANGE_ME:8989")}


def no_key(app, ex):
    """The line for an app that the user set up in part, from ex, the FileNotFoundError of api_key(). Its <APP>_URL is
    the user's own, but no API key reads. None for an app that the user did not set up: no <APP>_API_KEY, no
    config.xml in app_dir(), and no URL or one of SHIPPED_URLS. A one-shot run says nothing of such an app."""
    key, url = config.env_key(app), config.CFG.apps[app].url.rstrip("/")
    return f"{key}_URL is set, but the {app.capitalize()} API key does not read: {config.mask(str(ex))[:150]}. Set {key}_API_KEY." \
        if url and url not in SHIPPED_URLS.get(app, ()) else None


def app_list(app):
    """The first list call of a backfill or a scan, Radarr's movies or Sonarr's series. A run for an app that is not set
    stops here with one line that names the settings."""
    try:
        return arr(app, ARR[app].kind)
    except FileNotFoundError as ex:   # api_key() found no key and no config.xml
        sys.exit(no_key(app, ex) or f"{app.capitalize()} is not set up. Set {config.env_key(app)}_URL and {config.env_key(app)}_API_KEY.")


def hunter_urls(app):
    """{"sab": (SABnzbd's API URL, None), "hydra": (the Newznab indexer's API URL, None)} of the subtitle hunter, from
    the Radarr instance app. Each is the first of its kind by id, as the app stores them. SABNZBD_URL and NEWZNAB_URL
    take the place of the address the app saved, as for a container that does not resolve it. The API path stays. A
    kind the app has not saved gives (None, why), as "Radarr has no Newznab indexer". One saved with no address gives
    ("", why), as "Radarr's SABnzbd download client has no host". The hunter and the start check use it, see
    arr_subhunt.creds() and runner.hunter_checks()."""
    c, name = config.CFG, ARR[app].name
    first = lambda kind, impl: min((p for p in arr(app, kind) if p.get("implementation") == impl), key=lambda p: p["id"], default=None)
    sab, hyd = ({f["name"]: f.get("value") for f in p.get("fields") or []} if p else None
                for p in (first("downloadclient", "Sabnzbd"), first("indexer", "Newznab")))
    if sab is None:
        s = None, f"{name} has no SABnzbd download client"
    elif not c.sabnzbd_url and not (sab.get("host") and sab.get("port")):
        s = "", f"{name}'s SABnzbd download client has no {'host' if not sab.get('host') else 'port'}"
    else:
        s = (c.sabnzbd_url or f'{"https" if sab.get("useSsl") else "http"}://{sab["host"]}:{sab["port"]}{sab.get("urlBase") or ""}').rstrip("/") + "/api", None
    if hyd is None:
        n = None, f"{name} has no Newznab indexer"
    elif not (c.newznab_url or hyd.get("baseUrl")):
        n = "", f"{name}'s Newznab indexer has no base URL"
    else:
        n = (c.newznab_url or hyd["baseUrl"]).rstrip("/") + (hyd.get("apiPath") or "/api"), None
    return {"sab": s, "hydra": n}


def own_key(who):
    """The key of the own map of who, an instance or "plex": SONARR_PATH_MAP, SONARR_4K_PATH_MAP, PLEX_PATH_MAP."""
    return f"{config.env_key(who)}_PATH_MAP"


def map_key(who):
    """The setting whose map who takes, an instance or "plex": its own, else PATH_MAP. None when neither has a pair."""
    return own_key(who) if config.CFG.own_map(who) else "PATH_MAP" if config.CFG.path_map else None


def map_fix(who):
    """What a message tells the user to change when the map of who does not fit: fix a map that is set, else set one."""
    key = map_key(who)
    return f"fix {key}" if key == own_key(who) else f"fix PATH_MAP, or set {own_key(who)}" if key else f"set {own_key(who)} or PATH_MAP"


def mapped(x, who, back=False):
    """x with each path under a program prefix of who's map moved to its local prefix, in every string of a list or dict.
    who is an instance or "plex". back maps a local path to the program's path. The longest prefix wins, and a
    prefix matches whole folder names only."""
    pairs = config.CFG.map_of(who)
    if not pairs:
        return x
    if isinstance(x, dict):
        return {k: mapped(v, who, back) for k, v in x.items()}
    if isinstance(x, list):
        return [mapped(v, who, back) for v in x]
    for a, b in sorted(pairs, key=lambda m: -len(m[back])) if isinstance(x, str) else ():
        src, dst = (b, a) if back else (a, b)
        if x == src or x.startswith(src.rstrip("/") + "/"):
            return dst.rstrip("/") + x[len(src.rstrip("/")):] or "/"
    return x


def app_query(path, app):
    """An API path whose query values are local paths, with those values mapped to the app's paths."""
    if not config.CFG.map_of(app) or "?" not in path:
        return path
    base, q = path.split("?", 1)
    return base + "?" + urllib.parse.urlencode([(k, mapped(v, app, True)) for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True)])


ARR_TIMEOUT = contextvars.ContextVar("arr_timeout", default=60)   # seconds a GET of arr() waits. The Test checks set less.


def arr(app, path):
    """GET from the local Sonarr or Radarr. The paths in the answer are the ones this script sees, see mapped()."""
    return mapped(http(f"{config.CFG.apps[app].url}/api/v3/{app_query(path, app)}", headers={"X-Api-Key": api_key(app)},
                       timeout=ARR_TIMEOUT.get()), app)


def arr_write(app, path, method, body=None):
    """A write to the local Sonarr or Radarr. The re-grabs and the rescan after a repack use it."""
    return mapped(http(f"{config.CFG.apps[app].url}/api/v3/{app_query(path, app)}", method, mapped(body, app, True),
                       {"X-Api-Key": api_key(app)}, timeout=60), app)


DONE = ("completed", "failed", "aborted", "cancelled", "orphaned")   # the end states of an app command


def movie_file(m, app):
    """The file record a Radarr movie of the instance app names by movieFileId, or {}. The API fills movieFile from a
    join on the movie id (MovieRepository, LeftJoin on MovieFiles.MovieId), so while a movie has two file records it can
    show the other one.
    After a conversion's ManualImport the original's record stays until a rescan drops it, and movieFile can show it.
    The conversion then read as refused and rolled back, though Radarr had assigned the new file. A
    shape with no movieFileId, as in older fakes, keeps movieFile."""
    if "movieFileId" not in m:
        return m.get("movieFile") or {}
    fid, f = m.get("movieFileId"), m.get("movieFile") or {}
    if not fid:
        return {}
    return f if f.get("id") == fid else (arr(app, f"moviefile/{fid}") or {})


class App:
    """Sonarr or Radarr behind one interface, ARR[app]. arr() and arr_write() carry every call. The class fields hold
    what differs: the endpoints, the id fields and the commands. film is true when a file holds one film (Radarr), and
    false when it holds episodes of one series (Sonarr). app is the instance, and name its label in messages and alerts."""

    def __init__(self, app):
        self.app, self.name = app, app.capitalize()   # Radarr, Sonarr, Sonarr-4k

    def rescan_body(self, owner):
        return {"name": self.rescan_name, self.owner_key: int(owner)}

    def rescan(self, owner, wait=0):
        """Ask the app to rescan one movie or series, so its size, media info and file records follow a change. Returns
        "sent", or with wait the end state of command(). A scan that could not be sent returns why."""
        if not owner: return "no item id"
        try:
            if wait:
                return self.command(self.rescan_body(owner), wait, cancel=False)
            arr_write(self.app, "command", "POST", self.rescan_body(owner))
            return "sent"
        except Exception as ex:
            return config.mask(f"failed: {type(ex).__name__}: {ex}")[:200]

    def command(self, body, wait=None, cancel=True):
        """POST one command, then wait for it to end, wait seconds at most, COMMAND_WAIT by default. Returns its end state,
        "sent" when the app gave it no id, or "waited" when it did not end in time. With cancel, such a command is
        cancelled and returns "timed out", so a queued ManualImport never runs after the hook gave up on it. The app
        cannot cancel one that started, so the caller reads the app before it deletes anything, see convert_undo()."""
        cmd = self.wait(arr_write(self.app, "command", "POST", body) or {}, convert.COMMAND_WAIT if wait is None else wait)
        if cmd.get("status") in DONE:
            return cmd["status"]
        if cancel and cmd.get("id"):
            with contextlib.suppress(Exception):
                arr_write(self.app, f'command/{cmd["id"]}', "DELETE")
            return "timed out"
        return "waited" if cmd.get("id") else "sent"

    def wait(self, cmd, seconds):
        """Read the command cmd, the app's answer to its POST, every 2 seconds until it ends or seconds pass. Returns the
        last answer. The subtitle hunter waits for its ManualImport here too."""
        end = time.monotonic() + seconds
        while cmd.get("id") and cmd.get("status") not in DONE and time.monotonic() < end:
            time.sleep(2)
            cmd = arr(self.app, f'command/{cmd["id"]}')
        return cmd

    def remonitor(self, items):
        """Monitor the movies or episodes items again. A delete through the API may unmonitor them."""
        arr_write(self.app, self.monitor, "PUT", {self.ids_key: items, "monitored": True})

    def page(self, slug):
        """The URL of the item slug in the app's web UI, or None. The base is <KEY>_LINK alone, because the address a
        browser opens often differs from <KEY>_URL behind a reverse proxy or in Docker. An empty <KEY>_LINK, no slug or a
        base that does not read gives None. The URL keeps only the scheme, host, port and path of the base, so it never
        holds a user, a password or a query. A base whose "@" urlsplit does not place in the host part may hide a
        password in its path or fragment, so it gives None too. The slug is the titleSlug of the movie or series record.
        Sonarr routes /series/:titleSlug and Radarr /movie/:titleSlug, the names in kind (frontend/src/App/AppRoutes.tsx,
        line 69 in Sonarr v4.0.20.3014, line 70 in Radarr v5.28.0.10274 and v6.4.4.10685). Radarr's titleSlug is the TMDB
        id (src/Radarr.Api.V3/Movies/MovieResource.cs:151 at both tags)."""
        base = config.CFG.apps[self.app].link
        try:
            u = urllib.parse.urlsplit(base)
            host, port = u.hostname or "", u.port
        except ValueError:   # a bad IPv6 address or port
            return None
        if not slug or u.scheme not in ("http", "https") or base.count("@") != u.netloc.count("@") or \
                not re.fullmatch(r"[a-z0-9.-]+|[0-9a-f:.]+", host):
            return None
        host = f"[{host}]" if ":" in host else host
        path = urllib.parse.quote(u.path.rstrip("/"), safe="/%")
        return f'{u.scheme}://{host}{f":{port}" if port else ""}{path}/{self.kind}/{urllib.parse.quote(str(slug), safe="")}'


class Radarr(App):
    kind, file_kind, owner_key, ids_key, film = "movie", "moviefile", "movieId", "movieIds", True
    monitor, search, rescan_name, parsed, body_file = "movie/editor", "MoviesSearch", "RescanMovie", "parsedMovieInfo", "movieFile"

    def item(self, owner, fid=None):
        """movie_item() of the movie owner. The movie has one file, so fid adds nothing."""
        return movie_item(arr(self.app, f"movie/{owner}"), kids_profiles(self.app))

    def files(self, owner, items, read=True):
        """({file id: path}, {item id: (monitored, file id)}) of the items of owner, as the app lists them now. An item with
        no file has the file id None, on Sonarr 0 or None, and no path. The movie owner is its one item. read False skips
        the reads of file records. A movie needs none."""
        m = arr(self.app, f"movie/{owner}")
        f = movie_file(m, self.app)
        return {f.get("id"): f.get("path")}, {int(owner): (m.get("monitored"), f.get("id"))}

    def record(self, ids):
        """(the app's record of the item's file, {item id: monitored}, the item's folder) for a conversion. movie/<id>
        leaves the custom format score out of its movieFile, moviefile/<id> holds it."""
        m = arr(self.app, f"movie/{ids['app_id']}")
        f = movie_file(m, self.app)
        return (arr(self.app, f"moviefile/{f['id']}") or f) if f.get("id") else {}, {int(ids["app_id"]): m.get("monitored")}, m.get("path") or ""

    def extra_rows(self, owner):
        """[(relative path, file record id, type)] of every extra file Radarr tracks for the movie owner, from its
        extrafile API. type is subtitle, metadata or other."""
        return [(x.get("relativePath"), x.get("movieFileId"), x.get("type")) for x in arr(self.app, f"extrafile?movieId={int(owner)}") or []]

    def extras(self, owner, fid, path, home, items):
        """The paths of the files Radarr tracks as extras of its file record fid. Sonarr and Radarr move them to the
        recycle bin when that record goes for any reason but NoLinkedEpisodes, which only Sonarr's rescan gives, and only
        while the file is still on disk (ExtraFileService, MediaFileTableCleanupService). A conversion takes the file away,
        so the rescan that drops the old record would move every extra to the bin. Metadata files stay out: both apps run
        the Kodi metadata writer, which writes the .nfo and the images again under the same names on that rescan."""
        return sorted({os.path.join(home, r) for r, f, t in self.extra_rows(owner) if f == fid and r and t != "metadata"})

    def original(self, owner, old):
        """The original download path of the file record old, or None. Radarr's API shows it on the record."""
        return old.get("originalFilePath")

    def unit_files(self, imported, fids, job):
        out = {int(job["file_id"]): {"path": job["path"], "items": [int(job["owner"])], "eps": []}}
        for mid in sorted({h["movieId"] for h in imported} | {int(job["owner"])}):
            m = arr(self.app, f"movie/{mid}"); f = movie_file(m, self.app)
            if f.get("id") in fids:
                out.setdefault(f["id"], {"path": f["path"], "items": [], "eps": [], "owner": mid})["items"].append(mid)
                out[f["id"]]["runtime"] = m.get("runtime") or 0
        return out

    def grab_paths(self, owner, eps):
        """The paths of the files a grab may replace, see keep_grab(). A movie with no file gives None."""
        return [movie_file(arr(self.app, f"movie/{owner}"), self.app).get("path")]

    def other(self, f, owner):
        return "another film"

    def import_item(self, owner, items, old):
        return {"movieId": int(owner)}

    def library(self, movies, ids=(), paths=None):
        """([(file record, item)] of each movie in movies, the list call's answer, the count of their files). ids limits
        it to those movies, paths to the movies whose movieFile names one. A record of paths holds its movie id."""
        out, count = [], 0
        for m in movies:
            f = movie_file(m, self.app) if paths is None or (m.get("movieFile") or {}).get("path") in paths else {}
            count += bool(f)
            if f and (not ids or m["id"] in ids):
                out.append((f if paths is None else dict(f, movieId=m["id"]), movie_item(m, kids_profiles(self.app))))
        return out, count

    def scan_label(self, f, info):
        return info[0]


class Sonarr(App):
    kind, file_kind, owner_key, ids_key, film = "series", "episodefile", "seriesId", "episodeIds", False
    monitor, search, rescan_name, parsed, body_file = "episode/monitor", "EpisodeSearch", "RescanSeries", "parsedEpisodeInfo", "episodeFile"
    KODI_NFO = re.compile(rb"<(movie|tvshow|episodedetails|artist|album|musicvideo)>")   # XbmcNfoDetector
    # The folders and files a scan leaves out, so Sonarr tracks no extra there (DiskScanService.cs:72-75, FilterPaths)
    SKIP_DIRS = re.compile(r"\..*|@eadir|plex versions|extras|extrafanart|behind the scenes|deleted scenes|featurettes|interviews|other|scenes"
                           r"|samples|shorts|trailers", re.I)
    SKIP_FILES = re.compile(r"^\.(_|unmanic|DS_Store$)|^Thumbs\.db$|-(trailer|other|behindthescenes|deleted|featurette|interview|scene|short)\.[^.]+$",
                            re.I)
    IMPORT_WINDOW = 60   # seconds between a file record's dateAdded and an import row with no fileId, see original()

    def episodes(self, ids):
        return (arr(self.app, "episode?" + urllib.parse.urlencode([("episodeIds", i) for i in ids])) or []) if ids else []

    def item(self, owner, fid=None):
        return episode_item(arr(self.app, f"series/{owner}"), arr(self.app, f"episode?episodeFileId={fid}") if fid else [])

    def files(self, owner, items, read=True):
        of = {e["id"]: (e.get("monitored"), e.get("episodeFileId")) for e in self.episodes(sorted(items))}
        fids = sorted({f for _, f in of.values()} - {0, None}) if read else []
        return {f: (arr(self.app, f"episodefile/{f}") or {}).get("path") for f in fids}, of

    def record(self, ids):
        eps = arr(self.app, f"episode?episodeFileId={ids['file_id']}")
        return arr(self.app, f"episodefile/{ids['file_id']}") or {}, {e["id"]: e.get("monitored") for e in eps}, arr(self.app, f"series/{ids['app_id']}").get("path") or ""

    def extra_rows(self, owner):
        """None. Sonarr 4 has no API for extra files, and a conversion needs none to settle. Sonarr drops the extras of a
        deleted file record in a handler that runs inside the command that deletes the record. The handler moves each
        extra on disk to the bin, then deletes the rows (ExtraFileService.cs:104-131). It is an IHandle (line 30), and
        EventAggregator.cs:86-100 runs an IHandle in the thread that publishes the event. MediaFileService.cs:59-67
        publishes it when a rescan drops a record (MediaFileTableCleanupService.cs:45-49) or a ManualImport replaces one
        at the same path (ImportApprovedEpisodes.cs:166-170). A command reads as completed only after its handlers end
        (CommandExecutor.cs:84-86). So a completed rescan that no longer lists the old record leaves no extra to move,
        see settle_extras(). Source paths are under src/NzbDrone.Core of Sonarr v4.0.20. Radarr runs its handler as a
        task after the command (IHandleAsync), so Radarr waits for the rows, see old_rows_left()."""
        return None

    def extras(self, owner, fid, path, home, items):
        """The paths of the extras of the file at path, the ones Radarr.extras() gives on Radarr. Sonarr 4 has no API for
        them, so they come from disk, by Sonarr's own rule. A rescan tracks each file under the series folder home that
        is no video, that its scan does not leave out, and whose name parses as the episodes of one file
        (ExistingOtherExtraImporter). So the walk takes each such file that other_episodes() gives to the episodes items.
        A file beside the video that extra_of() names needs no parse. Neither does a file that extra_of() names for another
        video in its folder that Sonarr lists, because its name parses as that video's episodes. In a long series that is
        nearly every sidecar. A file named for a video Sonarr does not list, such as a leftover, gets the parse. Metadata
        stays out, see metadata()."""
        folder, stem = os.path.split(os.path.splitext(path)[0])
        listed = {os.path.normpath(f["path"]) for f in arr(self.app, f"episodefile?seriesId={int(owner)}") or [] if f.get("path")}
        out = []
        for d, dirs, names in os.walk(home):   # a series folder on the NAS: a listing per folder, a stat per file
            config.DEADLINE.check()
            dirs[:] = sorted(x for x in dirs if not self.SKIP_DIRS.fullmatch(x))
            others = [os.path.splitext(n)[0] for n in names if os.path.normpath(os.path.join(d, n)) in listed - {os.path.normpath(path)}]
            for n in sorted(names):
                config.DEADLINE.check()
                p = os.path.join(d, n)
                if os.path.splitext(n)[1].lower() in regrab.VIDEO_EXT or not os.path.isfile(p):
                    continue
                mine = os.path.dirname(p) == folder and vault.extra_of(stem, n)
                if (not mine and any(vault.extra_of(v, n) for v in others)) or self.metadata(p):
                    continue
                if mine or (not self.SKIP_FILES.search(n) and not regrab.other_episodes(self.app, p, items)):
                    out.append(p)
        return out

    def metadata(self, p):
        """Whether Sonarr tracks the file p as metadata. Its metadata writers write those again on the rescan, so they
        stay out of the extras, as on Radarr. ExistingMetadataImporter asks each writer, on or off (FindMetadataFile).
        Kodi takes <base>-thumb.jpg or .png and an .nfo of at most 10 MB with a Kodi tag. Roksbox takes .xml and a .jpg
        outside a folder named metadata. WDTV takes .xml and .metathumb, and Kometa S01E02.jpg or .png."""
        n, ext = os.path.basename(p), os.path.splitext(p)[1].lower()
        if ext in (".xml", ".metathumb") or re.search(r"-thumb\.(png|jpg)", n, re.I) or re.match(r"S\d{2,}E\d{2,}\.(png|jpg)", n, re.I):
            return True
        if ext == ".jpg":
            return os.path.basename(os.path.dirname(p)).lower() != "metadata"
        if ext != ".nfo" or os.path.getsize(p) > 10 << 20:
            return False
        with open(p, "rb") as f:
            return bool(self.KODI_NFO.search(f.read()))

    def original(self, owner, old):
        """The download path of the import that made the file record old, or None. Sonarr 4's API leaves out the record's
        OriginalFilePath. The history of that import holds the same file name in data.droppedPath, under data.fileId
        (ImportApprovedEpisodes, HistoryService). Both exist for an import from a download only. An older Sonarr wrote
        the row with no fileId. Then the row of one of the file's episodes with no fileId and the date nearest the
        record's dateAdded counts, within IMPORT_WINDOW seconds. A row with the fileId of another file never counts. No
        such row gives None, as a record with no OriginalFilePath does. eventType 3 is DownloadFolderImported."""
        rows = [(h, h.get("data") or {}) for h in arr(self.app, f"history/series?seriesId={int(owner)}&eventType=3") or []]
        mine = [x.get("droppedPath") for _, x in rows if str(x.get("fileId")) == str(old.get("id"))]
        if mine or not old.get("dateAdded"):
            return mine[0] if mine else None
        eps = {e["id"] for e in arr(self.app, f"episode?episodeFileId={int(old['id'])}") or []}
        added = datetime.datetime.fromisoformat(old["dateAdded"])
        near = [(abs((datetime.datetime.fromisoformat(h["date"]) - added).total_seconds()), x.get("droppedPath")) for h, x in rows
                if not x.get("fileId") and h.get("episodeId") in eps and h.get("date")]
        return min((n for n in near if n[0] <= self.IMPORT_WINDOW), key=lambda n: n[0], default=(0, None))[1]

    def unit_files(self, imported, fids, job):
        mine = regrab.job_episodes(job)
        out = {int(job["file_id"]): {"path": job["path"], "items": mine, "eps": []}}
        for e in self.episodes(sorted({h["episodeId"] for h in imported} | set(mine))):
            fid = e.get("episodeFileId")
            if fid in fids and e.get("seriesId") != int(job["owner"]):
                logs.log(dict(app=self.app, source="hook", download_id=job.get("download_id"), result="warning",
                         note=f'file {fid} left out of the re-grab unit: episode {e.get("id")} is of series {e.get("seriesId")}, '
                              f'the job is of series {job["owner"]}'))
            elif fid in fids:
                if fid not in out:
                    out[fid] = {"path": arr(self.app, f"episodefile/{fid}")["path"], "items": [], "eps": []}
                if e["id"] not in out[fid]["items"]:   # the job's own episodes are in it from the start, and the app answers 500 to an id twice
                    out[fid]["items"].append(e["id"])
                out[fid]["eps"] += [] if any(x.get("id") == e["id"] for x in out[fid]["eps"]) else [e]
        return out

    def grab_paths(self, owner, eps):
        recs = self.episodes(eps)
        return [arr(self.app, f"episodefile/{fid}").get("path")
                for fid in sorted({e["episodeFileId"] for e in recs if e.get("episodeFileId") and e.get("seriesId") == int(owner)})]

    def other(self, f, owner):
        return "another series" if any(e.get("seriesId") != int(owner) for e in f["eps"]) else None

    def import_item(self, owner, items, old):
        return {"seriesId": int(owner), "episodeIds": sorted(items), "releaseType": old.get("releaseType") or "unknown"}

    def library(self, series, ids=(), paths=None):
        """The walk reads only a series with files, or with paths a series whose folder holds one. No other series costs a call."""
        out, count = [], 0
        for s in series:
            n = (s.get("statistics") or {}).get("episodeFileCount") or 0
            count += n
            mine = None if paths is None else [p for p in paths if s.get("path") and p.startswith(s["path"].rstrip("/") + "/")]
            if (ids and s["id"] not in ids) or not (n if mine is None else mine):
                continue
            eps = arr(self.app, f"episode?seriesId={s['id']}")
            out += [(f, episode_item(s, [e for e in eps if e.get("episodeFileId") == f["id"]], eps))
                    for f in arr(self.app, f"episodefile?seriesId={s['id']}") if mine is None or f.get("path") in mine]
        return out, count

    def scan_label(self, f, info):   # the series and the file name
        return f'{info[3]["title"]} | {f["relativePath"].rsplit("/", 1)[-1]}'


ARR = {app: (Radarr if a.program == "radarr" else Sonarr)(app) for app, a in config.CFG.apps.items()}


def path_warnings(per_app=True, plex_warned=False):
    """Why a path map does not fit, as warnings. Each app whose API key reads names its root folders. A root folder
    this script does not see warns, and so does one that no Plex library folder holds or sits in. --selftest and the
    listener start print them, and neither fails on them. A Plex that does not answer warns that the folders went
    unchecked. plex_warned leaves that line out, because the plex check of runner.service_checks() already said why.
    Plex libraries that hold no root folder never warn, so a music library or another host's library stays quiet.
    per_app=False leaves out an app that does not answer and a root folder this script does not see, because the
    listener's start check reports both."""
    out, roots = [], []
    for app in config.CFG.apps:
        name = ARR[app].name
        try:
            api_key(app)
        except (OSError, AttributeError):   # an app this host does not run has no key here
            continue
        try:
            local = [r["path"] for r in arr(app, "rootfolder")]
        except Exception as ex:
            if per_app:
                out.append(config.mask(f"{name} did not answer, so its root folders are not checked: {type(ex).__name__}: {ex}")[:300])
            continue
        for r in local:
            roots.append((name, r))
            if not os.path.isdir(r) and per_app:
                where = f"{name}'s root folder {r}" if mapped(r, app, True) == r else \
                    f"{r}, where {map_key(app)} puts {name}'s root folder {mapped(r, app, True)}"
                out.append(f"this {config.here()} does not see {where}. Mount the media there, or {map_fix(app)}.")
    if not config.CFG.plex_url or not roots:
        return out
    try:
        locs = [loc["path"].rstrip("/") for d in plex.plex_get("/library/sections").get("Directory", []) for loc in d.get("Location", [])]
    except Exception as ex:
        return out if plex_warned else out + [config.mask(f"Plex did not answer, so its library folders are not checked: {type(ex).__name__}: {ex}")[:300]]
    for name, r in roots:
        p = mapped(r, "plex", True).rstrip("/")
        if not any(p == x or p.startswith(x + "/") or x.startswith(p + "/") for x in locs):
            where, fix = "" if p == r.rstrip("/") else f", which {map_key('plex')} puts at {p} in Plex", map_fix("plex")
            out.append(f"no Plex library folder holds {name}'s root folder {r}{where}. {fix[0].upper()}{fix[1:]}, so Plex finds the "
                       "files arr-media-guard edits.")
    return out


PROFILES = {}
def kids_profiles(app):
    """The quality profile ids of the Radarr instance app named in the policy's kids profiles, read once per process."""
    if app not in PROFILES:
        names = decide.POLICY["kids"]["profiles"]
        PROFILES[app] = {p.get("id"): p.get("name") for p in arr(app, "qualityprofile") if p.get("name") in names}
    return PROFILES[app]


def guids(x, kinds):
    return [f"{k}://{x[k + 'Id']}" for k in kinds if x.get(k + "Id")]


def titles(x):
    """(title, year) pairs of a movie or series for content.year_verdict(): the title, the original title, the alternate titles."""
    pairs = [(x.get("title"), x.get("year")), (x.get("originalTitle"), None)] + [(a.get("title"), None) for a in x.get("alternateTitles") or []]
    return [p for p in pairs if p[0]]


def movie_item(m, kids_profiles_by_id=None):
    """(label, original language, listed runtime, Plex lookup, kids, ctx) for a Radarr movie. ctx is the metadata
    context of content.py: the ids, the year, the listed minutes, and the titles with the other years of the movie. Its
    slug names the movie's page in Radarr, see App.page()."""
    profile = (kids_profiles_by_id or {}).get(m.get("qualityProfileId"))
    years = [m.get("secondaryYear")] + [int(d[:4]) for d in (m.get(k) or "" for k in ("inCinemas", "digitalRelease", "physicalRelease"))
                                         if d[:4].isdigit()]
    ctx = {"ids": {"tmdb": m.get("tmdbId"), "imdb": m.get("imdbId")}, "year": m.get("year"), "listed": m.get("runtime") or 0,
           "titles": titles(m) + [(None, y) for y in years if y], "slug": m.get("titleSlug")}
    return (f'{m["title"]} ({m.get("year")})', (m.get("originalLanguage") or {}).get("name"), m.get("runtime") or 0,
            {"guids": guids(m, ("tmdb", "imdb")), "title": m["title"], "show": False},
            decide.kids_title("radarr", m.get("genres"), profile, m.get("studio")), ctx)


def episode_item(s, eps, series_eps=None):
    """movie_item() for the episodes of one Sonarr file. ctx lists each episode's runtime, never their sum, because a
    segment show lists every segment at the whole slot. series_eps, all episodes of the series, saves the title check
    one list call per file, see episode_title()."""
    num = "".join(f'E{e["episodeNumber"]:02d}' for e in eps)
    label = f'{s["title"]} S{eps[0]["seasonNumber"]:02d}{num}' if eps else s["title"]
    want = {"guids": guids(s, ("tvdb", "tmdb", "imdb")), "title": s["title"], "show": True}
    ctx = {"ids": {"tmdb": s.get("tmdbId"), "tvdb": s.get("tvdbId")}, "year": s.get("year"),
           "listed": [e.get("runtime") or 0 for e in eps], "titles": titles(s), "series_id": s.get("id"), "episode_ids": [e["id"] for e in eps if "id" in e],
           "anime": s.get("seriesType") == "anime", "slug": s.get("titleSlug"),
           **({"episodes": series_eps} if series_eps else {})}
    return (label, (s.get("originalLanguage") or {}).get("name"), episode_runtime(eps), want, decide.kids_title("sonarr", s.get("genres")),
            ctx)


def episode_runtime(eps):
    """Listed runtime of a file's episodes. 0 when one is unknown, because the series runtime misleads on specials."""
    return sum(e["runtime"] for e in eps) if eps and all(e.get("runtime") for e in eps) else 0
