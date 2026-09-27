# arr-media-guard, a Sonarr and Radarr import hook that sets default tracks and catches broken files.
# Copyright (C) 2026 samwiseg0
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Metadata checks for arr-media-guard. They decide whether a language or runtime alert can be trusted, and
whether the file is wrong enough to re-grab. The rule is that a correct file is never deleted.

- expected_languages() asks TMDB which languages the item has, so a wrong original language in Radarr or Sonarr
  never reads as a wrong file.
- trusted_duration() trusts a duration only when two sources in the file agree. A header may be off by hours.
- runtime_verdict() and year_verdict() judge the file against the item.
- wrong_content_evidence() adds the signals up. A re-grab needs REGRAB_POINTS.

Network failure is "unknown", never "wrong". The only I/O is TMDB, the cache file and ffprobe in last_packet().
No handler here catches OutOfTime, the exception the hook's time limit must raise.
docs/design.md, "Metadata checks", has the rules and the reasons behind them.
"""
import contextlib, fcntl, functools, http.client, json, os, re, subprocess, threading, time, urllib.error, urllib.parse, urllib.request

import arr_decide

TMDB = "https://api.themoviedb.org/3"
CACHE = "/var/lib/arr-media-guard/tmdb.json"
CACHE_DAYS = 30
TMDB_RETRY = 600   # seconds after a failed TMDB call before the next try, so an outage costs one timeout per run
# The last TMDB failure (its code and text), and "answered": the time of the last live HTTP answer from TMDB.
DOWN = {"until": 0.0, "code": "", "why": "", "answered": 0.0}
KEY_BROKEN = ("tmdb_token_missing", "tmdb_token_rejected")
KEY_ALERT_EVERY = 86400   # seconds between two "TMDB key not working" alerts of one host
# The default token is Radarr's bundled one, read from its DLL. TMDB_TOKEN in the env file overrides it with a key
# of your own.
RADARR_DLL = "/opt/Radarr/Radarr.Common.dll"
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
    """The hook's time limit. Its time_up() must raise this, never TimeoutError: a TimeoutError is an OSError, and
    the network handlers here would swallow it and let the job run on with no alarm left."""


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
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        DOWN["answered"] = time.time()
        return body
    except (OSError, ValueError, http.client.HTTPException) as ex:   # a bad token raises UnicodeEncodeError, a ValueError
        code = ex.code if isinstance(ex, urllib.error.HTTPError) else None
        if code: DOWN["answered"] = time.time()
        if code != 404:   # a token urllib cannot send (UnicodeEncodeError) is as broken as one TMDB refuses
            key = code in (401, 403) or isinstance(ex, UnicodeError)
            _down("tmdb_token_rejected" if key else "tmdb_unavailable", f"{type(ex).__name__}: {ex}")
        raise


def _down(code, why):
    DOWN.update(until=time.time() + TMDB_RETRY, code=code, why=why[:200])


def tmdb_state(expected):
    """(code, why) for the decision log and the Loki line. The code is ok, no_record, tmdb_unavailable (network,
    timeout, 5xx), tmdb_token_missing (no token) or tmdb_token_rejected (TMDB answered 401 or 403)."""
    if expected: return "ok", ""
    if time.time() < DOWN["until"]: return DOWN["code"], DOWN["why"]
    return "no_record", "TMDB has no record for the item"


def key_alert(code, state_dir, now=None):
    """(title, text) for one amber Discord embed when the TMDB key is missing or rejected, else None. At most one
    per KEY_ALERT_EVERY per host: the stamp file tmdb-key-alert under state_dir holds the time of the last one."""
    if code not in KEY_BROKEN: return None
    now, stamp = now or time.time(), os.path.join(state_dir, "tmdb-key-alert")
    try:
        with open(stamp + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)   # the hook's job processes run side by side, and one of them posts
            with contextlib.suppress(OSError):
                if now - os.path.getmtime(stamp) < KEY_ALERT_EVERY: return None
            open(stamp, "w").close()
            os.utime(stamp, (now, now))
    except OSError:
        return None   # no stamp means an alert per file, so none at all. The decision log still names the code.
    what = "TMDB rejected the key." if code == "tmdb_token_rejected" else "No TMDB key: TMDB_TOKEN is empty and Radarr's DLL gave none."
    return "TMDB key not working", (f"{what} The metadata checks run without TMDB, so no wrong-content re-grab relies on it. "
                                    "Set TMDB_TOKEN in the env file, or check Radarr's install.")


def tmdb_day_status(records):
    """The nightly audit's TMDB status from a day of decision records: "key broken", "unavailable n times", "ok", or
    "no checks" when no record carries a tmdb code."""
    codes = [r["tmdb"] for r in records if r.get("tmdb")]
    if any(c in KEY_BROKEN for c in codes): return "key broken"
    n = codes.count("tmdb_unavailable")
    return f"unavailable {n} time{'' if n == 1 else 's'}" if n else "ok" if codes else "no checks"


def _cached(cache, key, fetch, now):
    """The value under key when younger than CACHE_DAYS, else fetch() and store it. None is never stored."""
    try:
        data = json.load(open(cache))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    # an entry without a numeric "at" or a "v" counts as stale, so a damaged entry is dropped at the next write
    fresh = lambda e: isinstance(e, dict) and isinstance(e.get("at"), (int, float)) and "v" in e and now - e["at"] < CACHE_DAYS * 86400
    if fresh(data.get(key)):
        return data[key]["v"]
    v = fetch()
    if v is None:
        return None
    data = {k: e for k, e in data.items() if fresh(e)}
    data[key] = {"at": now, "v": v}
    try:   # a lost write costs one more request later. The rename keeps a concurrent reader off a half file.
        tmp = f"{cache}.{os.getpid()}.{threading.get_ident()}.tmp"   # the workers of a dry run and of a conversion backfill are threads
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, cache)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
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


def expected_languages(app, ids, token=None, cache=CACHE, now=None):
    """What TMDB says about the item, or None when it cannot be known. None means unknown to every caller, never wrong.

    ids: {"tmdb", "imdb"} for a Radarr movie, {"tmdb", "tvdb"} for a Sonarr series. The app's tmdb id comes first,
    /find by imdb or tvdb id is the fallback. token: TMDB_TOKEN from the env file, else radarr_token().
    Returns {"original": 639-2/B code, "spoken": [codes], "source", "fetched", "runtime" (movie
    minutes, 0 for a series), "title", "year", "special" (a TV movie or stand-up), "unmapped": TMDB codes with no
    639-2 code}. The result is cached for CACHE_DAYS in the cache file.
    """
    token, now = token or radarr_token(), now or time.time()
    if not token:
        _down("tmdb_token_missing", "TMDB_TOKEN is empty and Radarr's DLL gave no token")
        return None
    try:
        if app == "radarr":
            tid = ids.get("tmdb") or _find("imdb_id", ids.get("imdb"), "movie_results", token, cache, now)
            return _facts("movie", tid, token, cache, now) if tid else None
        tid = ids.get("tmdb") or _find("tvdb_id", ids.get("tvdb"), "tv_results", token, cache, now)
        return _facts("tv", tid, token, cache, now) if tid else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):   # urllib errors are OSErrors
        return None


def last_packet(path, timeout=30):
    """Seconds to the last video packet: ffprobe seeks to the last index point and reads to the end. None when that
    fails. Video only, because a stray subtitle event may set the header an hour past the end of the film.
    A file without an index would be read whole, so the timeout ends it."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-select_streams", "v:0", "-show_entries", "packet=pts_time:format=start_time",
                            "-read_intervals", "999999999%", path], capture_output=True, text=True, errors="replace", timeout=timeout)
        d = json.loads(r.stdout or "{}")
        pts = [float(p["pts_time"]) for p in d.get("packets") or [] if p.get("pts_time") not in (None, "N/A")]
        return round(max(pts) - float((d.get("format") or {}).get("start_time") or 0), 3) if pts else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
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
    src = {"header": arr_decide.duration(probe)}
    if av and all(fresh(p) and p.get("tag_duration") for p in av):
        src["streams"] = max(arr_decide.tag_seconds(p["tag_duration"]) for p in av)
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


def other_film(release_name, item_tmdb, seconds, token=None, cache=CACHE, now=None):
    """The TMDB film that the release name's title and year name, when it is not the item and its runtime matches the
    file. Only the top 5 search results within 1 year of the release year count, because TMDB's year matches any
    release date, so a search for a remake may return the older film first. A release with an edition word is skipped,
    because TMDB lists some cuts as films of their own. Returns the _facts() record plus "tmdb", or None.
    Network failure is None."""
    title, year, _ = split_release(release_name)
    token, now = token or radarr_token(), now or time.time()
    if not (title and year and seconds and token) or EDITION.search(release_name): return None
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
    """("ok" | "wrong" | "unknown", why) for the main audio of arr_decide.classify() tracks.

    The item's languages are English, the app's original, TMDB's original, and TMDB's spoken languages when TMDB's
    original is not English. English is always allowed, as in decide(): a foreign original may play its English dub
    (a kids anime dub). An English original's spoken list names dubs as well as real dialogue. So audio in that list is
    unknown, never wrong. Unknown without TMDB, with untagged main audio, or with an unmapped code.
    """
    main = [t for t in tracks if t["kind"] == "a" and t["role"] == "main"]
    if not main or any(t["lang"] in arr_decide.UNTAGGED for t in main):
        return "unknown", "the main audio is untagged or missing"
    if not expected or expected.get("unmapped"):
        return "unknown", "TMDB gave no usable languages"
    want = {expected["original"]} | (set(expected["spoken"]) if expected["original"] != "eng" else set())
    allowed = arr_decide.codes(original).union({"eng"}, *(arr_decide.codes(c) for c in want if c))
    have = sorted({t["lang"] for t in main})
    if any(lang in allowed for lang in have):
        return "ok", f"the audio is {', '.join(have)}, one of the item's languages"
    if expected["original"] == "eng" and set(have) & set().union(*(arr_decide.codes(c) for c in expected["spoken"])):
        return "unknown", f"the audio is {', '.join(have)}, which TMDB lists as spoken in this English original"
    return "wrong", f"the audio is {', '.join(have)}, the item's languages are {', '.join(sorted(allowed))}"


def release_languages(release_name):
    """639-2 codes of the language words outside the title position of a release name: "[Arabic]", "GERMAN"."""
    rest = split_release(release_name)[2].lower()
    return {c for w, c in arr_decide.LANGWORDS.items() if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", rest)}


def wrong_content_evidence(tracks, original, expected, trusted, listed_minutes, release_name, item_type, item_year,
                           alt_titles_years=(), other=None):
    """Each signal that the file holds the wrong content, its points, and whether they justify a re-grab.

    tracks: arr_decide.classify() of the file. original: the app's original language. expected: expected_languages().
    trusted: trusted_duration(). listed_minutes, release_name, item_type: see runtime_verdict(). item_year and
    alt_titles_years: see year_verdict(). other: other_film() for a movie.

    One point each: the wrong language, the release name naming that language (movies), a short or long runtime, a
    year mismatch, and the runtime of another film the release name names. Two points: a movie (never a special)
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
        add("release_language", "wrong", 1, f"the release name says {', '.join(sorted(named))}")
    rv, secs = runtime_verdict(trusted, listed_minutes, release_name, item_type), (trusted or {}).get("seconds")
    shown = max(listed_minutes or [0]) if isinstance(listed_minutes, (list, tuple)) else listed_minutes
    tmdb_rt = (expected or {}).get("runtime") if movie else 0
    points = int(rv in ("short", "long"))
    why = f'it runs {secs / 60:.0f} minutes ({trusted["trust"]}), the listing says {shown}' if secs else \
        f'the duration is not trusted ({(trusted or {}).get("trust")})'
    if points and tmdb_rt and runtime_verdict(trusted, tmdb_rt, release_name, item_type) != rv:
        rv, points, why = "unknown", 0, f"the app lists {listed_minutes} minutes and TMDB {tmdb_rt}, so the listing is in doubt"
    elif rv == "short" and item_type == "movie" and tmdb_rt and trusted["trust"] == "agree" and listed_minutes >= VERY_SHORT_MIN_LISTED \
            and secs / 60 < VERY_SHORT * listed_minutes:
        points = 2
    add("runtime", rv, points, why)
    yv = year_verdict(release_name, item_year, alt_titles_years)
    add("year", yv, int(yv == "mismatch"), f"the release name's year is {split_release(release_name, [t for t, _ in alt_titles_years])[1]}"
                                           f", the item's {item_year}")
    own = [m for m in ((listed_minutes, tmdb_rt) if movie else ()) if m]
    if other and movie and secs and own and not any(matches(m, secs) for m in own):
        add("other_film", "match", 1, f'the file runs like {other["title"]} ({other["year"]}), {other["source"]}, {other["runtime"]} minutes')
    total, (code, reason) = sum(s["points"] for s in sig), tmdb_state(expected)
    return {"signals": sig, "points": total, "regrab": total >= REGRAB_POINTS, "why": "; ".join(s["why"] for s in sig if s["points"]),
            "tmdb": code, "tmdb_why": reason}


def hms(seconds):
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


def header_alert(trusted):
    """The duration alert sentence when the header disagrees with the file, else None. A conflict alerts too, so
    a header that is off by hours keeps its alert with no trusted duration."""
    s = trusted.get("sources") or {}
    if not s.get("header") or (trusted.get("header_ok") is not False and trusted.get("trust") != "conflict"):
        return None
    if trusted.get("seconds"):
        return f'The container says {hms(s["header"])}, but the streams run {hms(trusted["seconds"])}.'
    other = next((s[k] for k in ("last_packet", "streams", "bitrate") if s.get(k)), None)
    return f'The container says {hms(s["header"])}, but the file suggests {hms(other)}. Neither can be trusted.' if other else None
