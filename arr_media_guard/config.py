# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The settings of arr-media-guard: the env file and the environment, read once into CFG, the policy, the tuning
constants and mask()."""
import collections, dataclasses, hashlib, json, os, re

from . import content, decide


ENV_FILE = os.environ.get("ARR_MEDIA_GUARD_ENV", "/etc/arr-media-guard.env")
BUDGET = 300          # seconds a hook run may take from the lock to mkvpropedit
LOCK_WAIT = 3600      # seconds a hook waits for the lock, so a holder stuck on the NAS never blocks later imports forever
DEADLINE = content.DEADLINE   # the job's time limit, see content.Deadline. run_job() starts it at BUDGET.
APPS = {"radarr": 7878, "sonarr": 8989}   # each program, the name of its default instance, and its default port
TAKEN = ("PLEX", "STATE", "LID", "SABNZBD", "NEWZNAB")   # other settings use these with _URL, _API_KEY or _DIR, so no instance may
HOME = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # the folder of the launchers, this package and examples/
SCRIPT = os.path.join(HOME, "arr-media-guard")   # the launcher, the path a Custom Script connection runs
LIB = tuple(f"arr_media_guard/{n}" for n in sorted(os.listdir(os.path.join(HOME, "arr_media_guard"))) if n.endswith(".py")) + \
    ("arr-media-guard-subhunt", "arr_subhunt.py")   # the code files besides the launcher, relative to HOME
COLORS = {"red": 0xD64541, "amber": 0xF0A020}   # red: a re-grab fault. amber: other alerts.
# The outcome code of a wrong-content verdict per regrab() code, so the outcome says what happened: would_regrab only when
# the grab record, the cap and the second check all passed. report.VERDICTS words each one.
CONTENT_CODES = {"regrabbed": "wrong_content", "would_regrab": "would_regrab", "unconfirmed": "wrong_content_unconfirmed", "capped": "regrab_capped",
                 "no_grab": "regrab_no_grab", "failed": "regrab_failed", "restored": "wrong_content", "searched": "wrong_content",
                 "deleted": "wrong_content"}
PLEX_WAITS = (0, 15, 30, 60, 120, 180, 195)   # seconds before each Plex lookup of a new import, 10 minutes in all
PLEX_QUIET = 15       # seconds between two idle checks of the item's library section before an analyze goes out
PLEX_BUSY_WAIT = 30   # seconds between two checks of a busy section
PLEX_BUSY_CAP = 1800  # seconds of a busy section, then the analyze is skipped. Plex's own scan reads the changed file.
PLEX_BUSY_TYPES = ("library.update.section", "library.refresh.items")   # Plex activities that read or change media parts
JOB_MAX_AGE = 86400   # a queued job older than a day is dropped
POLL = 5              # seconds the worker sleeps between queue checks while Plex lookups wait
SCAN_PACE = 2         # seconds each worker of an audio or video scan pauses before a file
PLEX_PACE = 2         # seconds between two analyze requests in a backfill
LID_TIMEOUT = 120     # seconds lid.py may take for all tracks of one file, well under BUDGET
LID_RESERVE = 60      # seconds of the job's time limit that detection always leaves, for the audio samples before the edit
LID_MIN_SECONDS = 60  # a shorter file is never heard
VIDEO_RESERVE = 30    # seconds of the job's time limit the video check leaves for the steps up to mkvpropedit
ZERO_SECS = 10        # seconds the zero probe may need. A file with long zero runs takes the longest. A window may need decide.VIDEO_TIMEOUT.
WINDOW_POLL = 0.5     # seconds between two checks of a video window's time and bytes read
REPACK_SIZE = 0.03    # a header repair's new file may differ from the original's size by this share
REPACK_TMP = b".repack-tmp"   # the end of a repack's temp file name, see repack_tmp(). A header repair uses it too.
HEADER_READ = 64 << 10                # bytes header_probe() reads at the start of a file, and at a second SeekHead
HEADER_TAIL = (16 << 20, 128 << 20)   # bytes it reads at the end for the stream ends. The second when one Cluster is longer.
TAIL_MIN = 1 << 20                    # bytes past the Segment end that make a header issue. A few stray bytes there are
                                      # common and harmless. Over 1 MiB is a header issue.
CUES_MAX = 16 << 20                   # a larger Cues element is not read. A film's Cues are far smaller.
TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA")   # subtitle codecs whose text subtitle_read() reads. PGS and VobSub are pictures.
TEXT_CUES = 200                       # blocks subtitle_read() reads at most per track
TEXT_BLOCK = 64 << 10                 # bytes of one subtitle block it reads at most
TEXT_AHEAD = 16                       # blocks whose reads it starts at once
SUB_CODECS = TEXT_CODECS + ("S_TEXT/WEBVTT",)   # the text subtitle codecs the subtitle match check reads
CUE_MAX = 5000                        # blocks subtitle_cues() reads at most per track. A film's text track holds far fewer.
PICTURE_CODECS = ("S_HDMV/PGS", "S_VOBSUB")   # the picture subtitle codecs the reference timing of --sub-time reads
PICTURE_MAX = 12000                   # blocks picture_cues() reads at most per track. A PGS cue takes a block that shows it and one that
                                      # clears it, so a PGS track holds twice the blocks of its text.
SUB_MAX = 1800                        # seconds subtitle_ends() may take with no time limit, as in a scan or a backfill
SUB_RESERVE = 150                     # seconds of the job's time limit it leaves for the zero probe and the windows
LID_WHEN = {"original_missing_bare_tag", "untagged_may_be_original", "wrong_language", "audio_untagged_or_missing"}   # reason codes


SUB_TIMEOUT = 90       # seconds the subtitle check may hear one file, the model load included
SUB_MIN_SECONDS = 300  # a shorter file is never checked: its two windows would sit too close for a timing fit
STEP_ALERT = 1.0       # seconds the windows of a piecewise result must differ by to alert. A smaller step goes to the log only.
SUB_ROLES = ("full", "sdh", "dub")   # the subtitle roles the check takes. A forced or commentary track is never checked.
# Languages written with no spaces between words. Their cue text reads as one long word, so the check can give no
# verdict and never hears them.
NO_SPACES = frozenset({"jpn", "chi", "tha"})
REPAIRED = ("header_repaired", "subtitle_trimmed", "subtitle_removed", "tail_removed", "subtitle_retimed", "subtitle_mismatch_removed",
            "subtitle_ends_lengthened")   # the reason codes of a remux that replaced the file
CRASH_TRIES = 3                                  # a job whose process died this often is dropped, see requeue()
SCHEMA = 2                                       # the decision log's line format. Raise it when a field changes meaning.
HOST = os.uname().nodename
SERVE = False   # True in the listener and the worker and daily jobs it starts, all as --serve. serve.main() sets it.
REGRAB_KINDS = ("audio", "video", "content", "damage")   # broken audio, corrupt video, wrong content, a damaged source
# What the subtitle check of an import does, docs/design.md, "Subtitle match". off: no check. check: it reads, reports
# and alerts, and changes nothing. fix: it also removes, retimes and lengthens. deep: fix, and a deep analysis after the
# import, see deep_analysis(). --sub-check and --sub-time ignore it, see sub_on() and sub_fixes().
SUBTITLES_LEVELS = ("off", "check", "fix", "deep")
# PATH_MAP='/tv:/media/tv|/movies:/media/movies' pairs the path an app uses with the path this script sees, as in Docker.
# SONARR_PATH_MAP, RADARR_PATH_MAP and PLEX_PATH_MAP pair the paths of one program the same way. Each one that is empty
# or missing takes PATH_MAP, see Settings.map_of(). An instance of APP_INSTANCES has its own, see env_key().
MAP_KEYS = {"sonarr": "SONARR_PATH_MAP", "radarr": "RADARR_PATH_MAP", "plex": "PLEX_PATH_MAP"}


def env_key(app):
    """The start of the keys of the instance app: sonarr-4k reads SONARR_4K_URL, SONARR_4K_API_KEY and so on."""
    return app.upper().replace("-", "_")


def program(app):
    """The program of the instance app, radarr or sonarr. A value that is no instance, such as None in --sub-time, stays."""
    return CFG.apps[app].program if app in CFG.apps else app


@dataclasses.dataclass(frozen=True)
class AppSettings:
    """The settings of one instance: <KEY>_URL, <KEY>_API_KEY, <KEY>_DIR and the pairs of <KEY>_PATH_MAP, see env_key()."""
    url: str
    api_key: str    # empty: the key in config.xml, see api_key()
    dir: str        # the app's own folder
    path_map: list
    program: str    # radarr or sonarr, the program the instance runs


@dataclasses.dataclass(frozen=True)
class Settings:
    """The env file and the environment, read once by settings(). A field holds the key of its name in upper case, or the key its comment
    names. errors holds one sentence for each value the script cannot read. Such a value takes the safest reading.
    --selftest fails on errors, and the worker logs them."""
    instance: str            # the name of this host in logs and alerts
    log: str                 # the decision log
    state_dir: str
    policy_file: str
    lid_dir: str
    name: str                # the syslog tag and the name of the hidden folders
    plex_url: str            # empty: no Plex call at all
    plex_token: str
    plex_path_map: list
    path_map: list           # the map of each program whose own map has no pair
    map_error: str           # every map's error, or None. --serve refuses to start on it.
    discord_webhook: str
    tmdb_token: str
    webhook_user: str
    webhook_password: str
    sabnzbd_api_key: str     # the subtitle hunter's SABnzbd key, which Radarr's API masks
    newznab_api_key: str     # the subtitle hunter's indexer key, which Radarr's API masks
    sabnzbd_url: str         # the hunter's SABnzbd address. Empty: the one Radarr has saved.
    newznab_url: str         # the hunter's indexer address. Empty: the one Radarr has saved.
    audit_time: str          # as written, see serve.listen_config()
    regrab: frozenset        # the kinds that re-grab. Another kind only alerts "would re-grab".
    regrab_cap: int          # re-grabs of every kind per app in 24 hours, a download counts once. Then alerts only.
    restore: bool            # off: the plain re-grab, and a manual import only alerts
    keep_replaced: bool      # on: a Grab event hard-links the files an upgrade may replace, see keep_grab()
    keep_days: int           # KEEP_ORIGINALS_DAYS, the days a repack keeps the original it replaced. 0 drops it.
    header_repair: bool      # off: a header issue is logged and never remuxed
    repack_max: float        # REPACK_MAX_GB in bytes. A larger file that is not Matroska is skipped. A header repair too.
    subtitles: str           # one of SUBTITLES_LEVELS
    convert: bool            # the hook's conversion of an import. A backfill converts with --convert only.
    convert_max: int         # CONVERT_MAX_FILES, the conversions one backfill run applies. Then it stops, for the NAS load.
    convert_workers: int
    scan_workers: int
    hook_workers: int        # job processes of the worker. 1 runs each job in the worker itself.
    apps: dict               # instance name: AppSettings. The default instances radarr and sonarr come first, then APP_INSTANCES.
    errors: list
    from_env: tuple          # the keys the environment gave, sorted. --selftest names them.
    keep_dir = property(lambda s: f".{s.name}-originals")   # hidden, so the apps and Plex never scan it
    recycle_dir = property(lambda s: f".{s.name}-recycle")   # hidden too, see replaced_root()
    hide_dir = property(lambda s: f".{s.name}-convert")   # beside an extra, which waits there while the app drops the old record

    def own_map(self, who):
        """The pairs of the own map of who, an instance or plex: <KEY>_PATH_MAP or PLEX_PATH_MAP."""
        return self.plex_path_map if who == "plex" else self.apps[who].path_map

    def map_of(self, who):
        """The pairs that map the paths of who: its own map when it has a pair, else PATH_MAP."""
        return self.own_map(who) or self.path_map

    def secrets(self):
        """{key: value} of each secret setting that a URL, an error text or a log line may hold, for mask(). mask() skips
        a value under MASK_MIN characters, so a short WEBHOOK_PASSWORD never breaks a path. Radarr's TMDB key, from its DLL
        or the built-in copy, is a secret too, see content.expected_languages()."""
        return {"PLEX_TOKEN": self.plex_token, "DISCORD_WEBHOOK": self.discord_webhook, "TMDB_TOKEN": self.tmdb_token,
                "RADARR_TMDB_TOKEN": content.RADARR_TOKEN, "RADARR_DLL_TOKEN": content.radarr_token() or "",
                "WEBHOOK_PASSWORD": self.webhook_password,
                **{f"{env_key(app)}_API_KEY": a.api_key for app, a in self.apps.items()},
                "SABNZBD_API_KEY": self.sabnzbd_api_key, "NEWZNAB_API_KEY": self.newznab_api_key}


def env_file(path):
    """{KEY: value} of the KEY='value' lines of the env file at path, {} when it does not read."""
    out = {}
    try:
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"): out[k] = v.strip("'\"")
    except OSError:
        pass
    return out


def path_map(key, value):
    """(pairs, error) of the map setting key with value. error is None when every pair is two absolute paths. Each path
    loses its trailing slashes, so a folder written as "/mnt/TV/" still maps itself. "/" stays "/"."""
    pairs = [pair.split(":") for pair in value.split("|") if pair]
    good = [tuple(x.rstrip("/") or "/" for x in p) for p in pairs if len(p) == 2 and p[0].startswith("/") and p[1].startswith("/")]
    side = "PLEX_PATH" if key == "PLEX_PATH_MAP" else "APP_PATH"
    return good, None if len(good) == len(pairs) else \
        f"{key} takes pairs {side}:LOCAL_PATH of absolute paths, joined by '|'. A pair that is not one is left out."


class Lookup(collections.ChainMap):
    """The environment over the env file. A read of a key that the environment holds adds it to taken."""
    def __getitem__(self, key):
        if key in self.maps[0]: self.taken.add(key)
        return super().__getitem__(key)


def settings(path, environ=os.environ):
    """The Settings of the env file at path. A key in environ wins over the file, an empty one too. settings() reads
    only the keys of the settings, so any other variable of environ is ignored. A missing key takes its default."""
    env, errors = Lookup(environ, env_file(path)), []
    env.taken = set()

    def number(key, default, bad, why, least=None, cast=int):
        """key by cast, default when missing or empty. A bad value is bad, one under least is least, and why says so."""
        raw = env.get(key, "")
        try:
            n = cast(raw or default)
            if n != n: raise ValueError(n)   # nan would pass every size check
        except ValueError:
            n = None
        if n is None or (least is not None and n < least):
            n = bad if n is None else least
            errors.append(f"{key} {raw!r} {why.format(n)}")   # why holds {} for the value taken
        return n

    def switch(key, default, why):
        """key as true or false, default when missing or empty. Another value is default too, and why says so."""
        raw = env.get(key, "").strip().lower()
        if raw and raw not in ("true", "false"):
            errors.append(f"{key} {raw!r} is no switch value, so {why}. The values are true and false.")
        return raw == "true" if raw in ("true", "false") else default

    def level(key, default, levels, bad, why):
        """key as one of levels, default when missing. Another value, an empty one too, is bad, and why says so."""
        raw = env.get(key, default).strip().lower()
        if raw not in levels:
            errors.append(f"{key} {raw!r} is no level, so {why}. The levels are {', '.join(levels)}.")
        return raw if raw in levels else bad

    def api_key(key):
        """key as an API key. The placeholder CHANGE_ME of docker/compose.yml counts as empty, so that app is not set up."""
        raw = env.get(key, "")
        return "" if raw == "CHANGE_ME" else raw

    def kinds(key, default, known):
        """The kinds of known that key lists by commas. 'none' lists none. An empty list or an unknown kind is an error,
        and an unknown kind is left out, so it never re-grabs."""
        listed = [k.strip().lower() for k in env.get(key, default).split(",") if k.strip()]
        out = frozenset(k for k in listed if k in known)
        if listed == ["none"]:
            listed = []
        elif not listed:
            errors.append(f"{key} is blank, so nothing re-grabs. Write {key}='none' to turn every re-grab off.")
        if set(listed) - out:
            errors.append(f"{key} names {', '.join(sorted(set(listed) - out))}, which is no re-grab kind, so it is left out. "
                          f"The kinds are {', '.join(known)}.")
        return out

    # These run in this order, so the errors keep the order the worker logs them in.
    hook_workers = number("HOOK_WORKERS", 1, 1, "is not a whole number of 1 or more, so {} runs", least=1)
    regrab_cap = number("REGRAB_CAP", 30, 0, "is no whole number, so the cap is {} and every fault only alerts.")
    name = env.get("NAME", "arr-media-guard")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):   # NAME names folders, see Settings.keep_dir
        errors.append(f"NAME {name!r} holds other characters than letters, digits, '.', '_' and '-', so the script uses arr-media-guard.")
        name = "arr-media-guard"
    regrab = kinds("REGRAB", "audio,video", REGRAB_KINDS)
    repack_max = number("REPACK_MAX_GB", 30, 0, "is no number, so the cap is {} GB and every remux is skipped.", cast=float) * 1e9
    subtitles = level("SUBTITLES", "fix", SUBTITLES_LEVELS, "check", "the subtitle check of an import runs as check")
    maps = {key: path_map(key, env.get(key, "")) for key in ("PATH_MAP", *MAP_KEYS.values())}   # {key: (pairs, error)}
    apps = {app: AppSettings(env.get(f"{app.upper()}_URL", f"http://127.0.0.1:{port}"), api_key(f"{app.upper()}_API_KEY"),
                             env.get(f"{app.upper()}_DIR") or f"/var/lib/{app}", maps[f"{app.upper()}_PATH_MAP"][0], app) for app, port in APPS.items()}
    # APP_INSTANCES='sonarr-4k:sonarr,radarr-4k:radarr' adds instances. A bad entry is left out, so no event reaches it.
    for entry in (e.strip() for e in env.get("APP_INSTANCES", "").split(",") if e.strip()):
        app, sep, prog = (x.strip() for x in entry.partition(":"))
        key, prog = env_key(app), prog.lower()
        why = ("is no name:program pair" if not sep else
               "has other characters than letters, digits and '-' in its name" if not re.fullmatch(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*", app) else
               "names no program. The programs are radarr and sonarr" if prog not in APPS else
               "has the name of another instance" if key in map(env_key, apps) else
               f"has a name whose keys {key}_URL and the like belong to other settings" if key in TAKEN else
               f"has no {key}_URL" if not env.get(f"{key}_URL") else None)
        if why:
            errors.append(f"The APP_INSTANCES entry {entry!r} {why}, so it is left out.")
            continue
        maps[f"{key}_PATH_MAP"] = path_map(f"{key}_PATH_MAP", env.get(f"{key}_PATH_MAP", ""))
        apps[app] = AppSettings(env[f"{key}_URL"], api_key(f"{key}_API_KEY"), env.get(f"{key}_DIR") or f"/var/lib/{app}",
                                maps[f"{key}_PATH_MAP"][0], prog)
    map_errors = [e for _, e in maps.values() if e]
    errors += map_errors
    whole = "is not a whole number of 1 or more, so it counts as {}."
    return Settings(
        instance=env.get("INSTANCE", os.uname().nodename), log=env.get("LOG", "/var/log/arr-media-guard.jsonl"),
        state_dir=env.get("STATE_DIR", "/var/lib/arr-media-guard"), policy_file=env.get("POLICY_FILE") or "/etc/arr-media-guard.policy.json",
        lid_dir=env.get("LID_DIR", "/opt/arr-media-guard-lid"), name=name, plex_url=env.get("PLEX_URL", ""), plex_token=env.get("PLEX_TOKEN", ""),
        plex_path_map=maps["PLEX_PATH_MAP"][0], path_map=maps["PATH_MAP"][0], map_error=" ".join(map_errors) or None,
        discord_webhook=env.get("DISCORD_WEBHOOK", ""), tmdb_token=env.get("TMDB_TOKEN", ""), webhook_user=env.get("WEBHOOK_USER", ""),
        webhook_password=env.get("WEBHOOK_PASSWORD", ""), sabnzbd_api_key=env.get("SABNZBD_API_KEY", ""),
        newznab_api_key=env.get("NEWZNAB_API_KEY", ""), sabnzbd_url=env.get("SABNZBD_URL", ""), newznab_url=env.get("NEWZNAB_URL", ""),
        audit_time=env.get("AUDIT_TIME", "07:30"), regrab=regrab, regrab_cap=regrab_cap,
        keep_replaced=switch("KEEP_REPLACED", False, "the hook keeps nothing at a grab"),
        restore=switch("RESTORE", True, "a re-grab of a broken upgrade puts the old file back"),
        header_repair=switch("HEADER_REPAIR", True, "the hook repairs a broken header"),
        convert=switch("CONVERT", False, "the hook converts no import"),
        keep_days=number("KEEP_ORIGINALS_DAYS", 7, 7, "is not a whole number of 0 or more, so it counts as {}.", least=0),
        repack_max=repack_max, subtitles=subtitles, convert_max=number("CONVERT_MAX_FILES", 200, 200, whole, least=1),
        convert_workers=number("CONVERT_WORKERS", 1, 1, whole, least=1), scan_workers=number("SCAN_WORKERS", 1, 1, whole, least=1),
        hook_workers=hook_workers, errors=errors, apps=apps, from_env=tuple(sorted(env.taken)))


CFG = settings(ENV_FILE)


def own_version():
    """The first 12 hex of the sha256 of the launcher and of each file in LIB that is deployed. It changes only when
    the code does. A file that is not there is left out, so one missing optional file never hides the version."""
    h = hashlib.sha256()
    for path in [SCRIPT] + [os.path.join(HOME, n) for n in LIB]:
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


VERSION = own_version()
POLICY_ERROR = None
try:   # the policy data. A missing or malformed file never stops the script here, see run_job().
    decide.set_policy(json.load(open(CFG.policy_file)))
except Exception as ex:
    POLICY_ERROR = f"{type(ex).__name__}: {ex}"[:200]


def policy_help():
    """Why no policy loaded. For a missing file it adds the command that creates it from the example."""
    text = f"no policy loaded from {CFG.policy_file}: {POLICY_ERROR}"
    if not os.path.exists(CFG.policy_file):
        example = os.path.join(HOME, "examples", "policy.json")
        text += f". The file is missing. Create it from the example: install -m 0644 {example} {CFG.policy_file}"
    return text


MASK_MIN = 12   # characters a secret needs for mask(). A shorter one may be a word of a path, and every line would lose it.


def mask(text):
    """An error text with the Plex token and the webhooks replaced. urllib puts the whole URL into some errors."""
    for k, v in CFG.secrets().items():
        if len(v) >= MASK_MIN: text = text.replace(v, f"<{k}>")
    return text
