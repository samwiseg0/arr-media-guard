# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Metadata checks for arr-media-guard. They decide whether a language or runtime alert can be trusted, and
whether the file is wrong enough to re-grab. The rule is that a correct file is never deleted.

- expected_languages() asks TMDB which languages the item has, so a wrong original language in Radarr or Sonarr
  never reads as a wrong file.
- trusted_duration() trusts a duration only when two sources in the file agree. A header may be off by hours.
- runtime_verdict() and year_verdict() judge the file against the item.
- episode_title_verdict() finds a release whose episode title names another episode of the series. It alerts only.
- wrong_content_evidence() adds the signals up. A re-grab needs REGRAB_POINTS.

Network failure is "unknown", never "wrong". The only I/O is TMDB, its cache in the state store and ffprobe in last_packet().
DEADLINE is the hook job's time limit. Its waits here end at it, and no handler here catches its OutOfTime.
docs/design.md, "Metadata checks", has the rules and the reasons behind them.
"""
import contextlib, difflib, fcntl, functools, http.client, json, math, re, select, sqlite3, subprocess, time, unicodedata, urllib.error, urllib.parse, urllib.request

from . import decide

TMDB = "https://api.themoviedb.org/3"
CACHE_DAYS = 30
KINDS = {"radarr": ("movie", "imdb", "movie_results"), "sonarr": ("tv", "tvdb", "tv_results")}   # per app: TMDB's kind, the id /find takes, its list
TMDB_RETRY = 600   # seconds after a failed TMDB call before the next try, so an outage costs one timeout per run
# The last TMDB failure (its code and text), and "answered": the time of the last live HTTP answer from TMDB.
DOWN = {"until": 0.0, "code": "", "why": "", "answered": 0.0}
KEY_BROKEN = ("tmdb_token_missing", "tmdb_token_rejected")
KEY_ALERT_EVERY = 86400   # seconds between two "TMDB key not working" alerts of one host
# The default token is Radarr's bundled one, read from its DLL, else the copy below. TMDB_TOKEN overrides it with a
# key of your own.
RADARR_DLL = "/opt/Radarr/Radarr.Common.dll"
RADARR_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIxYTczNzMzMDE5NjFkMDNmOTdmODUzYTg3NmRkMTIxMiIsInN1YiI6IjU4NjRmNTkyYzNhMzY4MGFiNjAxNzUzNCIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.gh1BwogCCKOda6xj9FRMgAAj_RYKMMPC3oNlcBtlmwk"   # AuthToken in src/NzbDrone.Common/Cloud/RadarrCloudRequestBuilder.cs of Radarr
# ISO 639-1 (TMDB) to ISO 639-2/B (Matroska tags), from Debian's iso-codes. TMDB also uses cn for Cantonese and sh.
ISO1 = dict(p.split(":") for p in (
    "aa:aar ab:abk ae:ave af:afr ak:aka am:amh an:arg ar:ara as:asm av:ava ay:aym az:aze ba:bak be:bel bg:bul bh:bih "
    "bi:bis bm:bam bn:ben bo:tib br:bre bs:bos ca:cat ce:che ch:cha co:cos cr:cre cs:cze cu:chu cv:chv cy:wel da:dan "
    "de:ger dv:div dz:dzo ee:ewe el:gre en:eng eo:epo es:spa et:est eu:baq fa:per ff:ful fi:fin fj:fij fo:fao fr:fre "
    "fy:fry ga:gle gd:gla gl:glg gn:grn gu:guj gv:glv ha:hau he:heb hi:hin ho:hmo hr:hrv ht:hat hu:hun hy:arm hz:her "
    "ia:ina id:ind ie:ile ig:ibo ii:iii ik:ipk io:ido is:ice it:ita iu:iku ja:jpn jv:jav ka:geo kg:kon ki:kik kj:kua "
    "kk:kaz kl:kal km:khm kn:kan ko:kor kr:kau ks:kas ku:kur kv:kom kw:cor ky:kir la:lat lb:ltz lg:lug li:lim ln:lin "
    "lo:lao lt:lit lu:lub lv:lav mg:mlg mh:mah mi:mao mk:mac ml:mal mn:mon mr:mar ms:may mt:mlt my:bur na:nau nb:nob "
    "nd:nde ne:nep ng:ndo nl:dut nn:nno no:nor nr:nbl nv:nav ny:nya oc:oci oj:oji om:orm or:ori os:oss pa:pan pi:pli "
    "pl:pol ps:pus pt:por qu:que rm:roh rn:run ro:rum ru:rus rw:kin sa:san sc:srd sd:snd se:sme sg:sag si:sin sk:slo "
    "sl:slv sm:smo sn:sna so:som sq:alb sr:srp ss:ssw st:sot su:sun sv:swe sw:swa ta:tam te:tel tg:tgk th:tha ti:tir "
    "tk:tuk tl:tgl tn:tsn to:ton tr:tur ts:tso tt:tat tw:twi ty:tah ug:uig uk:ukr ur:urd uz:uzb ve:ven vi:vie vo:vol "
    "wa:wln wo:wol xh:xho yi:yid yo:yor za:zha zh:chi zu:zul cn:chi sh:srp").split())
TV_MOVIE, STANDUP = 10770, "stand-up comedy"   # TMDB genre id and keyword that make a movie a special

# Two duration sources agree when they differ by at most the larger of these.
DURATION_SLACK = 30         # seconds
DURATION_TOLERANCE = 0.02   # share of the longer one
# Runtime bands: the file's trusted minutes over the listed minutes.
MIN_LISTED = 10                          # a listed runtime under this says nothing
SHORT, LONG = 0.6, 1.4                   # a movie, the band the hook alerted on before
EDITION_SHORT, EDITION_LONG = 0.5, 2.0   # an edition word in the name. A TV cut may run near twice the listing.
SPECIAL_SHORT, SPECIAL_LONG = 0.5, 1.8   # stand-up, a TV movie: the listing may count the broadcast slot
EPISODE_SHORT = 0.6                      # an episode is judged on the short side only
EPISODE_MIN_LISTED = 20                  # TVDB lists some short cartoons at a longer slot, so a short listing says little
VERY_SHORT = 0.5                         # under half a feature-length movie listing is not the film. A special never
                                         # scores two on runtime, its listing may count the broadcast slot.
VERY_SHORT_MIN_LISTED = 40
MATCH_SLACK, MATCH_TOLERANCE = 3, 0.05   # a runtime matches the file within 3 minutes or 5 percent, PAL speed-up included
REGRAB_POINTS = 2
EDITION = re.compile(r"extended|director'?s.?cut|(?<![a-z])(dc|cut|version|edition|restored)(?![a-z])|unrated|uncut|theatrical"
                     r"|special.?edition|final.?cut|imax|ultimate|redux|remastered", re.I)   # "TV.Cut" too
MULTI_CUT = re.compile(r"(?<![0-9])[2-5].?in.?1(?![0-9])", re.I)   # "3in1": every cut in one file, the long side is not judged
# The title position of a release name ends at the episode number or the first quality word.
QUALITY = re.compile(r"(?<![a-z0-9])(s\d{1,2}(e\d{1,3})?|season.?\d+|\d{3,4}[pi]|4k|uhd|blu-?ray|bdrip|brrip|remux|web-?dl"
                     r"|web-?rip|hdtv|dvdrip|hdrip|xvid|[xh].?26[45]|hevc)(?![a-z0-9])", re.I)
# A year, never the year of a daily show's air date (2024.01.15) or of a season number (S2024E01).
YEAR = re.compile(r"(?<![0-9a-z])(?:19|20)[0-9]{2}(?![0-9])(?![._ -][01][0-9][._ -][0-3][0-9])", re.I)


class OutOfTime(Exception):
    """The hook's time limit ran out, see Deadline. It is no TimeoutError: a TimeoutError is an OSError, and the network
    handlers here would swallow it and let the job run on."""


LOCK_POLL = 0.2   # seconds between two tries of a lock under a Deadline. fcntl.flock has no timeout.


class Deadline:
    """A time limit: a monotonic end time and the text of its OutOfTime. end is None while no limit runs. DEADLINE holds
    the hook job's. A wait under it takes its bound from bound() or lock(), and a long loop calls check(). When the time
    is up, each raises OutOfTime once and ends the limit. So a handler that swallows it lets the job run on with no
    limit."""

    def __init__(self, secs=None, why=None):
        self.end = None
        if secs is not None:
            self.start(secs, why)

    def start(self, secs, why=None):
        self.end, self.why = time.monotonic() + secs, why or f"stopped after {secs} seconds"

    def stop(self):
        self.end = None

    def left(self):
        """The seconds left, None with no limit."""
        if self.end is None:
            return None
        left = self.end - time.monotonic()
        if left <= 0:
            self.out()
        return left

    def out(self):
        """Raise OutOfTime and end the limit. A wait that the limit bound calls it when the wait ends, because the
        wait's own timer may stop a little before the limit's end."""
        self.end = None
        raise OutOfTime(self.why)

    def check(self):
        self.left()

    def bound(self, secs):
        """secs, cut to the seconds left."""
        left = self.left()
        return secs if left is None else min(secs, left)

    @contextlib.contextmanager
    def paused(self):
        """No limit inside the block, which gets the seconds left, None with no limit. After it the limit has them again,
        so a wait with a limit of its own costs the job none of its time."""
        left = self.left()
        self.end = None
        try:
            yield left
        finally:
            if left is not None:
                self.end = time.monotonic() + left

    def lock(self, f, op):
        """fcntl.flock(f, op). While a limit runs it tries with LOCK_NB every LOCK_POLL seconds, else it blocks. A limit
        that ran out before the first try raises, as after a wait_turn() that waited it all."""
        if self.end is None:
            return fcntl.flock(f, op)
        self.check()
        while True:
            try:
                return fcntl.flock(f, op | fcntl.LOCK_NB)
            except BlockingIOError:
                self.check()
                select.select([], [], [], LOCK_POLL)   # a real wait. The hook's tests fake time.sleep.


DEADLINE = Deadline()   # the time limit of the hook job that runs in this process


@functools.lru_cache(maxsize=1)
def radarr_token(dll=RADARR_DLL):
    """Radarr's bundled TMDB read token from its installed DLL, or None. .NET keeps the string in UTF-16."""
    try:
        raw = open(dll, "rb").read()
    except OSError:
        return None
    # re.A: the byte pair after the token decodes as a CJK letter, which a Unicode \w would take into the token
    m = re.search(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", raw.decode("utf-16-le", "ignore") + " " + raw[1:].decode("utf-16-le", "ignore"), re.A)
    return m.group(0) if m else None


def _get(path, token, **params):
    """One TMDB GET. A failure other than 404 stops every call for TMDB_RETRY seconds. DOWN keeps the reason:
    tmdb_token_rejected for a 401, a 403 or a token urllib cannot send, tmdb_unavailable for anything else.
    DOWN["answered"] is set on every live HTTP answer, an error status such as 404 or 401 included. A cached answer
    never sets it, so the hook can report TMDB's status from live answers only."""
    if time.time() < DOWN["until"]:
        raise OSError(f'TMDB is paused after a failure: {DOWN["why"]}')
    url = f"{TMDB}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "arr-media-guard"})
        with urllib.request.urlopen(req, timeout=DEADLINE.bound(10)) as r:
            body = json.loads(r.read())
        DEADLINE.check()
        DOWN["answered"] = time.time()
        return body
    except (OSError, ValueError, http.client.HTTPException) as ex:   # a bad token raises UnicodeEncodeError, a ValueError
        DEADLINE.check()   # the job's time ran out during the call: OutOfTime, and TMDB stays unpaused
        code = ex.code if isinstance(ex, urllib.error.HTTPError) else None
        if code: DOWN["answered"] = time.time()
        if code != 404:   # a token urllib cannot send (UnicodeEncodeError) is as broken as one TMDB refuses
            key = code in (401, 403) or isinstance(ex, UnicodeError)
            _down("tmdb_token_rejected" if key else "tmdb_unavailable", f"{type(ex).__name__}: {ex}")
        raise


def _down(code, why):
    DOWN.update(until=time.time() + TMDB_RETRY, code=code, why=why[:200])


def tmdb_state(expected):
    """(code, why) for the decision log and the Loki line. The code is found (TMDB returned the item's record),
    no_record, tmdb_unavailable (network, timeout, 5xx) or tmdb_token_rejected (TMDB answered 401 or 403). Older
    records may hold tmdb_token_missing, from before RADARR_TOKEN."""
    if expected: return "found", ""
    if time.time() < DOWN["until"]: return DOWN["code"], DOWN["why"]
    return "no_record", "TMDB has no record for the item"


def key_name(token=None):
    """The TMDB key a call sends, as the key alert names it. token is TMDB_TOKEN, then Radarr's key from its DLL, then
    the copy of it in this file, in the order of expected_languages()."""
    return "the key in TMDB_TOKEN" if token else f"Radarr's key in {RADARR_DLL}" if radarr_token() else "the built-in copy of Radarr's key"


def key_alert(code, now=None, token=None, mark=str):
    """(title, text) for one amber Discord embed when TMDB rejects the key, else None. token is TMDB_TOKEN, and the text
    names the key that failed, see key_name(). mark marks that name, as report.bold() does for the embed. At most one
    per KEY_ALERT_EVERY per host: the store holds the time of the last one."""
    from . import store   # here, because store reads config, and config reads DEADLINE from this module as it loads
    if code != "tmdb_token_rejected": return None
    now = now or time.time()
    try:
        with store.tx():   # the hook's job processes run side by side, and one of them posts
            if now - store.get("mark", "tmdb-key-alert", -math.inf) < KEY_ALERT_EVERY: return None
            store.put("mark", "tmdb-key-alert", now)
    except (sqlite3.Error, OSError):
        return None   # no stamp means an alert per file, so none at all. The decision log still names the code.
    return "TMDB key not working", (f"TMDB rejected {mark(key_name(token))}. Until it's fixed, the checks for wrong content run without TMDB, "
                                    "and no re-grab relies on it.")


def tmdb_day_status(records):
    """The nightly audit's TMDB status from a day of decision records: "key broken", "unavailable n times", "ok", or
    "no checks" when no record carries a tmdb code. "ok" is the day's health: found, the old ok and no_record all count
    as TMDB working."""
    codes = [r["tmdb"] for r in records if r.get("tmdb")]
    if any(c in KEY_BROKEN for c in codes): return "key broken"
    n = codes.count("tmdb_unavailable")
    return f"unavailable {n} time{'' if n == 1 else 's'}" if n else "ok" if codes else "no checks"


def _cached(cache, key, fetch, now):
    """The value under key in cache, a part of the store, when younger than CACHE_DAYS, else fetch() and store it. None
    is never stored. A write drops the stale values."""
    from . import store   # here, as in key_alert()
    v = store.get(cache, key, newer=now - CACHE_DAYS * 86400)
    if v is not None:
        return v
    v = fetch()
    if v is None:
        return None
    with contextlib.suppress(sqlite3.Error, OSError), store.tx():   # a lost write costs one more request later
        store.drop(cache, older=now - CACHE_DAYS * 86400)
        store.put(cache, key, v, now)
    return v


def _facts(kind, tid, token, cache, now):
    """The trimmed TMDB record of a movie or a tv series."""
    def fetch():
        d = _get(f"/{kind}/{tid}", token, append_to_response="keywords")
        spoken = [s.get("iso_639_1") for s in d.get("spoken_languages") or []] + list(d.get("languages") or [])
        kw = d.get("keywords") or {}
        date = d.get("release_date") or d.get("first_air_date") or ""
        return {"original": ISO1.get(d.get("original_language") or ""), "spoken": sorted({ISO1[c] for c in spoken if c in ISO1}),
                "unmapped": sorted({c for c in [d.get("original_language")] + spoken if c and c not in ISO1}),
                "runtime": d.get("runtime") or 0, "title": d.get("title") or d.get("name"), "year": int(date[:4]) if date[:4].isdigit() else None,
                "special": TV_MOVIE in [g.get("id") for g in d.get("genres") or []]
                           or STANDUP in [k.get("name") for k in kw.get("keywords", kw.get("results", []))],
                "source": f"tmdb {kind} {tid}", "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    return _cached(cache, f"{kind}/{tid}", fetch, now)


def _find(source, xid, kind, token, cache, now):
    """The TMDB id for an imdb or tvdb id. A miss is stored too, as {"id": None}."""
    if not xid: return None
    got = _cached(cache, f"find/{source}/{xid}", lambda: {"id": next((r["id"] for r in _get(f"/find/{xid}", token, external_source=source)
                                                                    .get(kind) or []), None)}, now)
    return (got or {}).get("id")


def expected_languages(app, ids, token=None, cache="tmdb", now=None):
    """What TMDB says about the item, or None when it cannot be known. None means unknown to every caller, never wrong.

    ids: {"tmdb", "imdb"} for a Radarr movie, {"tmdb", "tvdb"} for a Sonarr series. The app's tmdb id comes first,
    /find by imdb or tvdb id is the fallback. token: TMDB_TOKEN, else radarr_token(), else RADARR_TOKEN.
    Returns {"original": 639-2/B code, "spoken": [codes], "source", "fetched", "runtime" (movie
    minutes, 0 for a series), "title", "year", "special" (a TV movie or stand-up), "unmapped": TMDB codes with no
    639-2 code}. The result is cached for CACHE_DAYS in cache, a part of the state store.
    """
    token, now = token or radarr_token() or RADARR_TOKEN, now or time.time()
    try:
        kind, other, found = KINDS.get(app, KINDS["sonarr"])
        tid = ids.get("tmdb") or _find(f"{other}_id", ids.get(other), found, token, cache, now)
        return _facts(kind, tid, token, cache, now) if tid else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):   # urllib errors are OSErrors
        return None


def last_packet(path, timeout=30):
    """Seconds to the last video packet: ffprobe seeks to the last index point and reads to the end. None when that
    fails. Video only, because a stray subtitle event may set the header an hour past the end of the film.
    A file without an index would be read whole, so the timeout ends it."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-select_streams", "v:0", "-show_entries", "packet=pts_time:format=start_time",
                            "-read_intervals", "999999999%", path], capture_output=True, text=True, errors="replace",
                           timeout=DEADLINE.bound(timeout))
        d = json.loads(r.stdout or "{}")
        pts = [float(p["pts_time"]) for p in d.get("packets") or [] if p.get("pts_time") not in (None, "N/A")]
        return round(max(pts) - float((d.get("format") or {}).get("start_time") or 0), 3) if pts else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        DEADLINE.check()   # a read the job's time limit cut raises OutOfTime
        return None


def trusted_duration(probe, size, last_packet=None):
    """The file's duration when two sources agree. probe is mkvmerge -J or ffprobe JSON, size in bytes, last_packet
    from last_packet().

    Sources: the container header, the stream durations, the size over the summed bitrate, and the last packet. The
    stream durations and bitrates are mkvmerge's statistics tags, used only when the app that wrote the file wrote
    them, because ffmpeg copies stale tags from its source. An ffprobe probe gives stream durations instead.
    trust: "agree" (the header and another source agree), "bitrate" (the header is off, two other sources agree),
    "header" (the header alone) or "conflict" (no two sources agree, or two pairs disagree). seconds is None unless
    two sources agree.
    header_ok is False when the header disagrees with the trusted duration.
    """
    c = (probe.get("container") or {}).get("properties") or {}
    app = c.get("writing_application") or ""
    props = [t.get("properties") or {} for t in probe.get("tracks") or []]
    av = [t.get("properties") or {} for t in probe.get("tracks") or [] if t.get("type") in ("audio", "video")]
    fresh = lambda p: app.startswith("mkvmerge") and p.get("tag__statistics_writing_app", app) == app
    src = {"header": decide.duration(probe)}
    if av and all(fresh(p) and p.get("tag_duration") for p in av):
        src["streams"] = max(decide.tag_seconds(p["tag_duration"]) for p in av)
    if av and all(fresh(p) and str(p.get("tag_bps", "")).isdigit() for p in av):   # subtitles count when tagged
        bps = sum(int(p["tag_bps"]) for p in props if fresh(p) and str(p.get("tag_bps", "")).isdigit())
        src["bitrate"] = (size - sum(a.get("size") or 0 for a in probe.get("attachments") or [])) * 8 / bps if bps else 0
    ff = [float(s["duration"]) for s in probe.get("streams") or [] if s.get("codec_type") in ("audio", "video") and s.get("duration")]
    if ff: src["streams"] = max(ff)
    src["last_packet"] = last_packet
    src = {k: round(v, 3) for k, v in src.items() if v}
    agree = lambda a, b: abs(a - b) <= max(DURATION_SLACK, DURATION_TOLERANCE * max(a, b))
    groups = [[k for k in src if agree(src[k], src[x])] for x in src]
    best = max(groups, key=lambda g: (len(g), "header" in g), default=[])
    tie = any(len(g) == len(best) and not set(g) & set(best) for g in groups)   # two pairs that disagree are a conflict
    if len(best) < 2 or tie:
        return {"seconds": None, "trust": "header" if list(src) == ["header"] else "conflict", "sources": src, "header_ok": None}
    ok = "header" in best
    return {"seconds": src["header"] if ok else sorted(src[k] for k in best)[len(best) // 2], "trust": "agree" if ok else "bitrate",
            "sources": src, "header_ok": ok}


def runtime_verdict(trusted, listed_minutes, release_name="", item_type="movie"):
    """"ok", "short", "long" or "unknown" for the trusted duration against the listed runtime.

    item_type: "movie", "special" (stand-up, a TV movie, see expected_languages) or "episode". An episode is judged
    on the short side only, because premieres and double episodes run long. For an episode, pass the listed
    runtimes of the file's episodes: a multi-episode file is judged against the longest one, because a segment show
    lists every segment at the whole slot. An edition word in the
    release name widens the band, a multi-cut file ("3in1") is never long. Unknown when the duration is not trusted
    or the listing is missing or under MIN_LISTED, EPISODE_MIN_LISTED for an episode.
    """
    if isinstance(listed_minutes, (list, tuple)):
        listed_minutes = max(listed_minutes) if listed_minutes and all(listed_minutes) else 0
    secs = (trusted or {}).get("seconds")
    if not secs or not listed_minutes or listed_minutes < MIN_LISTED:
        return "unknown"
    r = secs / 60 / listed_minutes
    if item_type == "episode":
        return "unknown" if listed_minutes < EPISODE_MIN_LISTED else "short" if r < EPISODE_SHORT else "ok"
    short, long = (SPECIAL_SHORT, SPECIAL_LONG) if item_type == "special" else (SHORT, LONG)
    if EDITION.search(release_name or ""): short, long = min(short, EDITION_SHORT), max(long, EDITION_LONG)
    if MULTI_CUT.search(release_name or ""): long = float("inf")
    return "short" if r < short else "long" if r > long else "ok"


def split_release(name, titles=()):
    """(title, year, rest) of a release or file name. The year is the first 4-digit year in the title position that
    is not part of a known title, such as a title that is a year, starts with one or ends with one. rest is the text
    after it, plus a leading [tag] such as "[Arabic]"."""
    name = re.sub(r"\.(mkv|mp4|m4v|avi|ts|wmv)$", "", name or "", flags=re.I)
    tags = re.match(r"(\s*\[[^\]]*\]\s*)*", name).group(0)
    body = name[len(tags):]
    m = QUALITY.search(body); head = body[:m.start()] if m else body
    own = {y for t in titles for y in YEAR.findall(t or "")}
    y = next((y for y in YEAR.finditer(head) if y.group(0) not in own), None)
    clean = lambda s: re.sub(r"[._\s()\[\]]+", " ", s).strip()
    if not y:
        return clean(head), None, tags + body[len(head):]
    return clean(head[:y.start()]), int(y.group(0)), tags + body[y.end():]


def year_verdict(release_name, item_year, alt_titles_years=()):
    """"ok", "mismatch" or "unknown". A mismatch is a year in the title position more than 1 year off the item's year
    and every other year in alt_titles_years. That is a list of (title, year) pairs, either may be None: the item's
    titles, which mark years that belong to a title, and other years of the item (Radarr's secondaryYear).
    A year in the release's title position counts as well, because many films carry their own year in an alternate
    title ("Film A 2020"), and split_release() then skips it."""
    title, year, _ = split_release(release_name, [t for t, _ in alt_titles_years])
    years = {item_year} | {y for _, y in alt_titles_years if y}
    if item_year and any(abs(int(r) - y) <= 1 for r in YEAR.findall(title) + ([str(year)] if year else []) for y in years):
        return "ok"
    return "mismatch" if year and item_year else "unknown"


def matches(minutes, seconds):
    """The listed minutes and the file's seconds match within MATCH_SLACK minutes or MATCH_TOLERANCE."""
    return bool(minutes and seconds) and abs(seconds / 60 - minutes) <= max(MATCH_SLACK, MATCH_TOLERANCE * minutes)


def other_film(release_name, item_tmdb, seconds, token=None, cache="tmdb", now=None):
    """The TMDB film that the release name's title and year name, when it is not the item and its runtime matches the
    file. Only the top 5 search results within 1 year of the release year count, because TMDB's year matches any
    release date, so a search for a remake may return the older film first. A release with an edition word is skipped,
    because TMDB lists some cuts as films of their own. Returns the _facts() record plus "tmdb", or None.
    Network failure is None."""
    title, year, _ = split_release(release_name)
    token, now = token or radarr_token() or RADARR_TOKEN, now or time.time()
    if not (title and year and seconds) or EDITION.search(release_name): return None
    try:
        ids = _cached(cache, f"search/{title.lower()}/{year}",
                      lambda: [r["id"] for r in (_get("/search/movie", token, query=title, year=year).get("results") or [])[:5]], now)
        for tid in ids or []:
            f = tid != item_tmdb and _facts("movie", tid, token, cache, now)
            if f and f["year"] and abs(f["year"] - year) <= 1 and matches(f["runtime"], seconds):
                return dict(f, tmdb=tid)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        return None
    return None


def language_verdict(tracks, original, expected):
    """("ok" | "wrong" | "unknown", why) for the main audio of decide.classify() tracks.

    The item's languages are English, the app's original, TMDB's original, and TMDB's spoken languages when TMDB's
    original is not English. English is always allowed, as in decide(): a foreign original may play its English dub
    (a kids anime dub). An English original's spoken list names dubs as well as real dialogue. So audio in that list is
    unknown, never wrong. Unknown without TMDB, with untagged main audio, or with an unmapped code.
    """
    main = [t for t in tracks if t["kind"] == "a" and t["role"] == "main"]
    if not main or any(t["lang"] in decide.UNTAGGED for t in main):
        return "unknown", "the main audio is untagged or missing"
    if not expected or expected.get("unmapped"):
        return "unknown", "TMDB gave no usable languages"
    want = {expected["original"]} | (set(expected["spoken"]) if expected["original"] != "eng" else set())
    allowed = decide.codes(original).union({"eng"}, *(decide.codes(c) for c in want if c))
    have = sorted({t["lang"] for t in main})
    if any(lang in allowed for lang in have):
        return "ok", f"the audio is {', '.join(have)}, one of the item's languages"
    if expected["original"] == "eng" and set(have) & set().union(*(decide.codes(c) for c in expected["spoken"])):
        return "unknown", f"the audio is {', '.join(have)}, which TMDB lists as spoken in this English original"
    return "wrong", f"the audio is {decide.lang_names(have)}, but it should be {decide.lang_names(allowed, 'or')}"


def release_languages(release_name):
    """639-2 codes of the language words outside the title position of a release name: "[Arabic]", "GERMAN"."""
    rest = split_release(release_name)[2].lower()
    return {c for w, c in decide.LANGWORDS.items() if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", rest)}


# The episode tag of a release name: S04E15, S04E15a (one segment of a DVD order), S21.E13, S01E01E02, S01E01-02,
# E172 (an absolute number), a daily show's date, or Part04.
EPISODE_TAG = re.compile(r"(?<![a-z0-9])(?:s\d{1,4}[ ._-]?e\d{1,4}[ab]?(?:[ ._-]?e\d{1,4}|-\d{1,4}(?![0-9pi]))*|e\d{1,4}"
                         r"|(?:19|20)\d\d[ ._-]\d\d[ ._-]\d\d|part[ ._-]?\d{1,3})(?![a-z0-9])", re.I)
EPISODE_TITLE_POINTS = 0   # points of a release title that names another episode. 0: it alerts, and never re-grabs.
TITLE_NEAR = 0.8           # difflib's ratio of two title keys that name one episode, as "Reunion Part 2" and "The Reunion Part 2"
# Words that end the episode title of a release name, beside the QUALITY words: release flags and sources. A flag in
# title case is a title word ("Internal Affairs"), because a release writes REPACK, iNTERNAL or repack. A language word
# ends the title only in capitals ("GERMAN"), because a title may hold one ("Chinese New Year").
TITLE_END = {"repack", "proper", "rerip", "internal", "readnfo", "dirfix", "nfofix", "multi", "dual", "nordic", "eng", "hebsub", "hebdub",
             "pdtv", "tvrip", "sdtv", "dvd", "ac3", "subbed", "dubbed", "vostfr"}
CONNECTORS = {"and", "amp"}   # words between two episode titles in one release name. "amp" is what an escaped "&" leaves.


def release_episode_title(name):
    """The episode title in a release name, between the episode tag and the first QUALITY or tag word, or None. A tag
    word is a TITLE_END flag, or a language word in capitals. In a name all in capitals the words cannot tell a tag from a
    title ("FRENCH WEEK"), so only the tag words right before the QUALITY word or the end are cut. In a name with no
    QUALITY word, a last "-GROUP" with no dot is the release group."""
    tag = EPISODE_TAG.search(name or "")
    if not tag:
        return None
    rest = name[tag.end():].split("[")[0]
    end = QUALITY.search(rest)
    words = [w for w in re.split(r"[ ._]+", rest[:end.start()] if end else re.sub(r"-[^-.\s]*$", "", rest)) if w]
    flag = lambda w: (w.lower().strip("-") in TITLE_END and not w.istitle()) or (w.isupper() and w.lower() in decide.LANGWORDS)
    if "".join(words).isupper():
        while words and flag(words[-1]):
            words.pop()
    else:
        words = words[:next((i for i, w in enumerate(words) if flag(w)), len(words))]
    return " ".join(words).strip(" -") or None


def nfo_episode_title(text):
    """The episode title of a scene NFO, from its "Episode Title" line, else its "Title" line, or None. A value that
    holds an episode tag gives the words after the tag."""
    found = {}
    for line in (text or "").splitlines():
        label, sep, value = line.partition(":")
        key = re.sub(r"[^a-z]", "", label.lower())
        if sep and key in ("title", "episodetitle", "episodename", "eptitle") and value.strip(" .:"):
            found.setdefault(key != "title", value.strip(" .:\t"))
    value = found.get(True) or found.get(False)
    tag = value and EPISODE_TAG.search(value)
    return (value[tag.end():].strip(" .-_") if tag else value) or None


def title_key(text):
    """text for a title match: lower case, no accents, no apostrophes, '&' as "and", every other mark a space."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", re.sub(r"['`]", "", text)).split())


def title_keys(title, series=()):
    """The keys of title and of each of its segments, which "/" or "+" join. A key that only names the series is left
    out, and a series title in front of the episode title is dropped."""
    out = set()
    for part in [title] + re.split(r"\s*[/+]\s*", title or ""):
        k = title_key(part)
        for s in series:
            if s and k.startswith(s + " "):
                k = k[len(s) + 1:]
        if k and k not in series:
            out.add(k)
    return out


def tag_numbers(name):
    """The numbers of the release name's episode tag: ("season", season, {episodes}), ("absolute", None, {numbers}) for a
    bare E172 or a Part04, or None for a date or no tag."""
    tag = EPISODE_TAG.search(name or "")
    t = tag.group().lower() if tag else ""
    if t.startswith("s"):
        season, rest = re.match(r"s(\d+)(.*)", t).groups()
        return "season", int(season), {int(n) for n in re.findall(r"\d+", rest)}
    if t.startswith(("e", "part")):
        return "absolute", None, {int(re.search(r"\d+", t).group())}
    return None


def covered(key, owners):
    """The episode keys of owners that cover key from end to end, with only CONNECTORS between them, or []. A release
    name joins two titles with no "/", as in "Show.S04E15.First.Title.Second.Title.1080p". A connector never starts or
    ends the key, so "And Then" never reads as the title "Then"."""
    words, starts, found = key.split(), {}, {0: []}   # found: the end of a covered start of words, and its keys
    for k in owners:
        starts.setdefault(k.split()[0], []).append(k.split())
    for i, w in enumerate(words):
        if i in found:
            if w in CONNECTORS and found[i] and i + 1 < len(words):
                found.setdefault(i + 1, found[i])
            for kw in starts.get(w, ()):
                if words[i:i + len(kw)] == kw:
                    found.setdefault(i + len(kw), found[i] + [" ".join(kw)])
    return found.get(len(words), [])


PART = re.compile(r"(.*?)(?: (?:part |pt )?(\d{1,2}|one|two|three|four|five|six|i{1,3}|iv|v|vi))?")   # a key and its part number


def parts(a, b):
    """Whether two keys name two parts of one story: the same words with another part number, or none on one side, as
    "versus the ring" and "versus the ring part 2". Neither names the other then, so a swap of two parts shows."""
    (base_a, part_a), (base_b, part_b) = PART.fullmatch(a).groups(), PART.fullmatch(b).groups()
    return base_a == base_b and part_a != part_b


def episode_title_verdict(title, episodes, imported, release_name="", series_titles=(), said="the release name", anime=False):
    """Whether the episode title of a release names another episode of the series than the file was imported as.

    title: release_episode_title() or nfo_episode_title(). episodes: Sonarr's episodes of the series. imported: the
    ids of the file's episodes. release_name: the release, whose tag_numbers() Sonarr imported by. A title, or one of
    its segments, matches an episode whose title has the same key. So do the titles that cover it, see covered(). The
    verdict is one of these:
    - "imported": a key is within TITLE_NEAR of an imported episode's title, or one holds the other's words in a row,
      such as a title that leaves out "Part Two", or "Inferno (4)" for "The Romans: Inferno (4)".
    - "mapped": Sonarr imported the release to other numbers than its tag, through its scene numbering, which already
      maps the release's own order. The title then follows the release's order, so it says nothing.
    - "other": the title matches another episode, one that no other episode shares the title with. A special counts
      only for a special, because a special often repeats the title of a regular episode.
    - "none": no episode matches.
    said names where the title comes from, such as "the release's NFO". anime adds each episode's absolute number.
    Returns a signal of wrong_content_evidence(): {"kind", "verdict", "points", "why", "episodes", "names", "imported",
    "said", "title"}. names holds the matched episodes as the alert names them, imported the [number, Sonarr's title] of
    each imported episode, see episode_why(). None when no episode points to the file, because the check then has
    nothing to compare.
    """
    series = {title_key(t) for t in series_titles if t}
    mine, wanted, owners = [e for e in episodes if e["id"] in set(imported)], title_keys(title, series), {}
    if not mine:
        return None
    for e in episodes:
        for k in title_keys(e.get("title"), series):
            owners.setdefault(k, set()).add(e["id"])
    wanted |= {k for w in list(wanted) for k in covered(w, owners)}
    near = lambda a, b: not parts(a, b) and (difflib.SequenceMatcher(None, a, b).ratio() >= TITLE_NEAR or f" {a} " in f" {b} " or f" {b} " in f" {a} ")
    tag, found = tag_numbers(release_name), []
    if any(near(k, o) for e in mine for o in title_keys(e.get("title"), series) for k in wanted):
        verdict = "imported"
    elif tag and mine and not tag[2] & ({e.get("episodeNumber") for e in mine if tag[0] == "absolute" or e.get("seasonNumber") == tag[1]}
                                          | {e.get("absoluteEpisodeNumber") for e in mine if tag[0] == "absolute"}):
        verdict = "mapped"
    else:
        ids = {i for k in wanted for i in owners.get(k, ()) if len(owners[k]) == 1}
        special = any(e.get("seasonNumber") == 0 for e in mine)
        found = sorted((e for e in episodes if e["id"] in ids and (e.get("seasonNumber") or special)),
                       key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
        verdict = "other" if found else "none"
    names, ours = and_join([episode_name(e, anime) for e in found]), [[episode_name(e, anime), e.get("title") or None] for e in mine]
    return {"kind": "episode_title", "verdict": verdict, "points": EPISODE_TITLE_POINTS if found else 0, "episodes": [e["id"] for e in found],
            "names": names, "imported": ours, "said": said, "title": title,
            "why": episode_why(ours, said, title, names) if found else f'{said} calls it "{title}"'}


def and_join(xs):
    """Words as a person lists them: "a", "a and b", "a, b and c"."""
    return f"{', '.join(xs[:-1])} and {xs[-1]}" if len(xs) > 1 else "".join(xs)


def episode_why(imported, said, title, names, quote=lambda s: f'"{s}"'):
    """The words of an episode title that names another episode, as in: imported as S01E02 "Overnight". The release
    name calls it "Anxious Times at Show Alpha", which is S01E03. imported holds the [number, Sonarr's title or None] of
    each imported episode. quote marks a title. The Discord embed bolds the titles with it, see report.quote()."""
    ours = and_join([n + (f" {quote(t)}" if t else "") for n, t in imported])
    return f"imported as {ours}. {said[:1].upper()}{said[1:]} calls it {quote(title)}, which is {names}"


def episode_name(e, anime=False):
    """S04E21 of a Sonarr episode. anime adds its absolute number, by which anime releases count."""
    return f'S{e.get("seasonNumber") or 0:02d}E{e.get("episodeNumber") or 0:02d}' + \
        (f' (absolute {e["absoluteEpisodeNumber"]})' if anime and e.get("absoluteEpisodeNumber") else "")


def wrong_content_evidence(tracks, original, expected, trusted, listed_minutes, release_name, item_type, item_year,
                           alt_titles_years=(), other=None, episode=None):
    """Each signal that the file holds the wrong content, its points, and whether they justify a re-grab.

    tracks: decide.classify() of the file. original: the app's original language. expected: expected_languages().
    trusted: trusted_duration(). listed_minutes, release_name, item_type: see runtime_verdict(). item_year and
    alt_titles_years: see year_verdict(). other: other_film() for a movie. episode: episode_title_verdict() for an
    episode whose release names a title.

    One point each: the wrong language, the release name naming that language (movies), a short or long runtime, a
    year mismatch, and the runtime of another film the release name names. An episode title that names another
    episode scores EPISODE_TITLE_POINTS, 0, so it alerts and never re-grabs. Two points: a movie (never a special)
    under VERY_SHORT of a feature-length listing, with the header confirmed and a TMDB runtime known. A re-grab needs
    REGRAB_POINTS, so one signal alone never deletes a file, except a very short movie.
    Returns {"signals": [{"kind", "verdict", "points", "why"}], "points", "regrab", "why", "tmdb", "tmdb_why"}.
    """
    sig, movie = [], item_type != "episode"
    add = lambda kind, verdict, points, why: sig.append({"kind": kind, "verdict": verdict, "points": points, "why": why})
    lv, why = language_verdict(tracks, original, expected)
    add("language", lv, int(lv == "wrong"), why)
    named = release_languages(release_name) & {t["lang"] for t in tracks if t["kind"] == "a" and t["role"] == "main"}
    if lv == "wrong" and movie and named:   # the uploader's label confirms the tag. A series may speak another language in some episodes.
        add("release_language", "wrong", 1, f"the release name says {decide.lang_names(named)}")
    rv, secs = runtime_verdict(trusted, listed_minutes, release_name, item_type), (trusted or {}).get("seconds")
    shown = max(listed_minutes or [0]) if isinstance(listed_minutes, (list, tuple)) else listed_minutes
    tmdb_rt = (expected or {}).get("runtime") if movie else 0
    points = int(rv in ("short", "long"))
    why = f'it runs {secs / 60:.0f} minutes, but the listed runtime is {shown} minutes' if secs else \
        f'the duration is not trusted ({(trusted or {}).get("trust")})'
    if points and tmdb_rt and runtime_verdict(trusted, tmdb_rt, release_name, item_type) != rv:
        rv, points, why = "unknown", 0, f"the app lists {listed_minutes} minutes and TMDB {tmdb_rt}, so the listing is in doubt"
    elif rv == "short" and item_type == "movie" and tmdb_rt and trusted["trust"] == "agree" and listed_minutes >= VERY_SHORT_MIN_LISTED \
            and secs / 60 < VERY_SHORT * listed_minutes:
        points = 2
    add("runtime", rv, points, why)
    yv = year_verdict(release_name, item_year, alt_titles_years)
    add("year", yv, int(yv == "mismatch"), f"the release name says {split_release(release_name, [t for t, _ in alt_titles_years])[1]}"
                                           f", but the listed year is {item_year}")
    own = [m for m in ((listed_minutes, tmdb_rt) if movie else ()) if m]
    if other and movie and secs and own and not any(matches(m, secs) for m in own):
        add("other_film", "match", 1, f'the release name matches {other["title"]} ({other["year"]}), and the file runs as long as its '
                                      f'{other["runtime"]} minutes')
    sig += [episode] if episode else []
    total, (code, reason) = sum(s["points"] for s in sig), tmdb_state(expected)
    return {"signals": sig, "points": total, "regrab": total >= REGRAB_POINTS, "why": "; ".join(s["why"] for s in sig if s["points"]),
            "tmdb": code, "tmdb_why": reason}


def hms(seconds):
    """Seconds as a player shows them, see decide.clock()."""
    return decide.clock(seconds)


def header_alert(trusted):
    """The duration alert sentence when the header disagrees with the file, else None. A conflict alerts too, so
    a header that is off by hours keeps its alert with no trusted duration."""
    s = trusted.get("sources") or {}
    if not s.get("header") or (trusted.get("header_ok") is not False and trusted.get("trust") != "conflict"):
        return None
    if trusted.get("seconds"):
        return f'The file says it runs {hms(s["header"])}, but the video and audio stop at {hms(trusted["seconds"])}. Players may show the wrong length.'
    k = next((k for k in ("last_packet", "streams", "bitrate") if s.get(k)), None)
    real = "its size and bitrate point to" if k == "bitrate" else "the video and audio stop at"
    return f'The file says it runs {hms(s["header"])}, but {real} {hms(s[k])}. The runtime check was skipped.' if k else None
