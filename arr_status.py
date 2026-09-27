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
"""Health status of the import hook for a monitoring agent, in <state_dir>/status.json.

record() is the only writer. A monitoring agent reads the file, for example Zabbix with vfs.file.contents and
JSONPath preprocessing. docs/design.md, section "Status file", has the fields and the rules for the call sites. The
file holds two checks, tmdb and policy, with STATUSES below.

The file, one entry per check:
  {"version": 1, "last_hook_run": <epoch>,
   "checks": {"tmdb": {"status", "since", "checked", "error", "error_time", "last_24h": {status: n}, "hours"}}}
since is when the status last changed, checked is the last record() of the check. A check never recorded reads
"unknown". error keeps the last error text after a recovery, with error_time.
"""
import fcntl, json, os, re, tempfile, time

FILE = "status.json"
VERSION = 1
STATUSES = {"tmdb": ("ok", "unavailable", "token_missing", "token_rejected"), "policy": ("ok", "failed")}
# arr_meta.tmdb_state() codes. no_record means TMDB answered without the item, or there was no id to ask about.
ALIASES = {"no_record": "ok", "tmdb_unavailable": "unavailable", "tmdb_token_missing": "token_missing",
           "tmdb_token_rejected": "token_rejected"}
UNKNOWN = {"tmdb": "unavailable", "policy": "failed"}   # an unknown code still reads as a failure to a trigger
DAY = 86400
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,}")   # a TMDB key or a JWT segment. A path or a file name breaks at "/" and ".".


def time_limit(ex):
    """True for the hook's time limit, arr_meta.OutOfTime. TimeoutError stays for callers that raise it."""
    return isinstance(ex, TimeoutError) or type(ex).__name__ == "OutOfTime"


def blank(key):
    return {"status": "unknown", "since": 0, "checked": 0, "error": "", "error_time": 0,
            "last_24h": {s: 0 for s in STATUSES[key]}, "hours": {}}


def load(path):
    """The current file with every check present. A missing, broken or wrong-shape file starts fresh."""
    try:
        with open(path) as f:
            data = json.load(f)
        if data["version"] != VERSION: raise ValueError(data["version"])
        checks = {}
        for k in STATUSES:
            c = {**blank(k), **data["checks"].get(k, {})}
            c.update(status=str(c["status"]), error=str(c["error"]), since=int(c["since"]), checked=int(c["checked"]),
                     error_time=int(c["error_time"]),
                     hours={str(int(h)): {str(s): int(n) for s, n in b.items()} for h, b in c["hours"].items()})
            checks[k] = c
        return {"version": VERSION, "last_hook_run": int(data.get("last_hook_run", 0)), "checks": checks}
    except Exception as ex:
        if time_limit(ex): raise
        return {"version": VERSION, "last_hook_run": 0, "checks": {k: blank(k) for k in STATUSES}}


def record(state_dir, key, code, detail="", now=None, touch=True):
    """Record one result of check key in state_dir/status.json. Returns (written, note).

    code is one of STATUSES[key] or an arr_meta.tmdb_state() code. An unknown code records UNKNOWN[key] and keeps
    the code in the error text. detail is the error text, kept only for a status other than ok. It is cut to 200
    characters, and anything that looks like a token is replaced. note is empty when all went well. Otherwise it
    says why, for the hook's log. The time limit is the one exception record() raises.
    touch=False leaves last_hook_run alone. --selftest uses it, so a deploy tool's dry run never hides a stopped
    audit timer from a stale-data alert.
    """
    note = ""
    try:
        if key not in STATUSES:
            return False, f"arr_status: unknown check {key!r}"
        status = ALIASES.get(code, code)
        if status not in STATUSES[key]:
            status, note = UNKNOWN[key], f"arr_status: unknown {key} code {code!r}, recorded as {UNKNOWN[key]}"
            detail = f"{code}: {detail}"
        now = int(now or time.time())
        path = os.path.join(state_dir, FILE)
        with open(path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)   # the worker and a backfill never lose each other's change
            data = load(path)
            c = data["checks"][key]
            if c["status"] != status:
                c["status"], c["since"] = status, now
            c["checked"] = now
            if status != "ok":
                c["error"], c["error_time"] = TOKEN.sub("<redacted>", str(detail))[:200], now
            # ponytail: hourly buckets, so the window is 23 to 24 hours and only as fresh as the last write.
            hours = {h: n for h, n in c["hours"].items() if now - int(h) < DAY}
            bucket = hours.setdefault(str(now - now % 3600), {})
            bucket[status] = bucket.get(status, 0) + 1
            c["hours"] = hours
            c["last_24h"] = {s: sum(n.get(s, 0) for n in hours.values()) for s in STATUSES[key]}
            if touch:
                data["last_hook_run"] = now
            write(path, data)
        return True, note
    except TimeoutError:
        raise
    except Exception as ex:
        if time_limit(ex): raise
        return False, f"arr_status: {type(ex).__name__}: {ex}"[:200]


def write(path, data):
    """Write a temp file, fsync it and rename it over the old one. A reader sees one whole file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".status-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)   # mkstemp makes 0600, and a monitoring agent reads the file as its own user
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
