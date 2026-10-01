#!/usr/bin/env python3
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
"""The Webhook listener of arr-media-guard, for Sonarr and Radarr in Docker. The apps reach it over HTTP, so nothing
is installed in their containers.

  arr-media-guard --serve

It listens on port PORT. Radarr posts to /radarr and Sonarr to /sonarr, with HTTP basic auth. The user and password
are WEBHOOK_USER and WEBHOOK_PASSWORD in the env file. Without both, the listener does not start. A Download event (an
import or an upgrade) becomes the job file that hook() writes from the Custom Script variables, through queue_job().
So the worker and the queue stay one code path. A Test event checks the policy, the API key and the root folders.
With KEEP_REPLACED on, a Grab event hard-links the files the grab may replace, see grab().

The body names the file by its id, and the app's API gives its path. The file record must belong to the item the body
names. The path in the body is never used. The old files of an upgrade must sit in the item's folder, and their copies
in the app's recycle bin, both as the API names them. The listener refuses anything else with a log line.

It serves THREADS requests at once, and each request has READ_TIMEOUT seconds for its headers and body together, so
a slow client never holds up an app. A request without the right path or credentials never reaches the decision log.
The listener counts them and prints one summary line a minute at most.

The listener also keeps a worker going. After each job, and every TICK seconds, it starts a worker when a job waits
and no worker holds worker.lock. The worker is a new program, arr-media-guard --serve --worker, so it reads the env
file and the policy again, and no thread of the listener goes into it. Once a day at AUDIT_TIME it runs the nightly audit of each app, which also removes kept
originals older than KEEP_ORIGINALS_DAYS, and it rotates the decision log with logrotate. SIGTERM stops the listener.
It sends SIGTERM to the worker and waits for it, so a flag edit is never cut. See docs/design.md, "Webhook".

The host script is passed in as h, as arr_subhunt.py gets it. This module needs only the stdlib.
"""
import base64, concurrent.futures, contextlib, datetime, hmac, http.server, json, os, signal, socket, subprocess, sys, threading, time
import urllib.error

PORT = 8484            # the listener's port in the container. Docker maps it to any host port.
MAX_BODY = 1 << 20     # bytes a Webhook body may hold. An import body holds a few kilobytes.
READ_TIMEOUT = 10      # seconds a client has for its whole request, the headers and the body together
THREADS = 32           # requests the listener serves at once. An idle connection holds one until READ_TIMEOUT.
BACKLOG = 64           # connections that may wait for a thread. The listener closes each one past that at once.
QUIET = 60             # seconds between two summary lines of the requests refused before the credentials
CONTROL = str.maketrans({c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7f, 0xa0))})   # a control character, escaped for a log
MAX_ID = 200           # characters a download id may hold
POLL = 2               # seconds the listener waits for a request before it looks for a stop
TICK = 60              # seconds between two looks for a waiting job with no worker, and for the daily jobs
NAMES = {"radarr": "Radarr", "sonarr": "Sonarr"}
ROTATE = """{log} {{
    weekly
    rotate 52
    compress
    delaycompress
    missingok
    notifempty
}}
"""   # the decision log's rotation, as the README asks of a native install


class Refused(Exception):
    """A request the listener refuses. code is the HTTP status of the answer."""
    def __init__(self, code, why):
        super().__init__(why)
        self.code = code


def plain(path):
    """Whether path is a plain absolute path: normalized, and with no control character, NUL included."""
    return isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) == path and path == path.translate(CONTROL)


def inside(path, folder):
    """Whether path is a plain absolute path under folder, with no '|'. The job joins the old files with '|'."""
    return plain(path) and "|" not in path and bool(folder) and path.startswith(folder.rstrip("/") + "/")


def job_of(h, app, body):
    """The job hook() writes for the same import, from the body of a Webhook Download event. The file id and the item
    id come from the body. The path, the scene name and the episode ids come from the app's API by the file id. Sonarr's
    episodeFile.sourcePath, mapped like any app path, names the folder whose NFO gives the episode title. Raises
    Refused when the record does not belong to the item, or when this container cannot see the file."""
    radarr, name = app == "radarr", NAMES[app]
    item, f = body.get("movie" if radarr else "series"), body.get("movieFile" if radarr else "episodeFile")
    if not radarr and not f and body.get("episodeFiles"):
        raise Refused(400, "an On Import Complete event names no single file. Turn it off, and turn on On File Import and On File Upgrade")
    ids = [(x or {}).get("id") if isinstance(x, dict) else None for x in (item, f)]
    if not all(type(i) is int and i > 0 for i in ids):   # type(), because a JSON true is an int too
        raise Refused(400, f"the body names no {'movie' if radarr else 'series'} id and file id")
    owner, fid = ids
    kind = "moviefile" if radarr else "episodefile"
    try:
        rec = h.arr(app, f"{kind}/{fid}")
    except urllib.error.HTTPError as ex:
        if ex.code != 404:
            raise
        raise Refused(400, f"{name} has no file {fid}") from None
    if rec.get("movieId" if radarr else "seriesId") != owner:
        raise Refused(400, f"{name} file {fid} belongs to {'movie' if radarr else 'series'} {rec.get('movieId' if radarr else 'seriesId')}, "
                           f"and the body names {owner}")
    path = rec.get("path")
    if not plain(path):
        raise Refused(400, f"{name} lists file {fid} at {path!r}, which is no plain absolute path")
    if not os.path.isfile(path):
        raise Refused(500, f"{name} lists file {fid} at {path}, and this container does not see it there. Mount the media at "
                           f"the app's paths, or {h.map_fix(app)}")
    eps = None
    if not radarr:
        eps = ",".join(str(i) for i in sorted(e["id"] for e in h.arr(app, f"episode?episodeFileId={fid}")))
    down = body.get("downloadId") or ""
    if not isinstance(down, str) or len(down) > MAX_ID:
        raise Refused(400, f"the downloadId is no text of at most {MAX_ID} characters")
    deleted, recycled = old_files(h, app, body.get("deletedFiles"), owner)
    posted = h.mapped(f.get("path"), app)
    if posted and posted != path:   # the app may have renamed it since. The API wins.
        note(h, app, "warning", f"the body names {posted!r}, and {name} lists file {fid} at {path}. The job takes the path of the API")
    job = {"app": app, "event": "Download", "time": time.time(), "path": path, "owner": str(owner), "file_id": str(fid),
           "episode_ids": eps, "download_id": down, "release": rec.get("sceneName") or "", "deleted": deleted, "recycled": recycled}
    if not radarr:   # the NFO beside the file Sonarr imported from, read now, see release_nfo_title()
        source, pairs = h.mapped(f.get("sourcePath"), app), h.MAPS.get(app, h.PATH_MAP)
        mapped = not pairs or any(inside(source, local) or source == local for _, local in pairs)   # with a map, only its folders
        job["nfo_title"] = h.release_nfo_title(source, job["release"]) if plain(source) and mapped else None
    return job


def old_files(h, app, files, owner):
    """The values of <app>_deletedpaths and <app>_deletedrecyclebinpaths for the old files of an upgrade, pipe-joined
    in one order, or (None, None) when the import replaced nothing. Each old path must sit in the item's folder, and
    each recycle bin path in the app's recycle bin. An empty recycle bin path means the app kept no copy."""
    if not files:
        return None, None
    if not isinstance(files, list) or not all(isinstance(d, dict) for d in files):
        raise Refused(400, "deletedFiles is no list of files")
    home = h.arr(app, f"{'movie' if app == 'radarr' else 'series'}/{owner}").get("path") or ""
    rbin = h.arr(app, "config/mediamanagement").get("recycleBin") or ""
    old, rb = [], []
    for d in files:
        o, r = h.mapped(d.get("path"), app), h.mapped(d.get("recycleBinPath") or "", app)
        if not inside(o, home):
            raise Refused(400, f"the old file {o!r} is not in the folder {home!r} of {NAMES[app]}'s item {owner}")
        if r and not inside(r, rbin):
            raise Refused(400, f"the recycle bin copy {r!r} is not in {NAMES[app]}'s recycle bin {rbin!r}")
        old.append(o)
        rb.append(r)
    return "|".join(old), "|".join(rb)


def grab(h, app, body):
    """Keep the files a Grab event may replace, see keep_grab() in the host script. The body names the item and the
    episodes by id, and the app's API gives the files. An error logs a line, and the answer is still ok, so the app's
    grab never fails on the hook."""
    try:
        radarr = app == "radarr"
        item, eps, down = body.get("movie" if radarr else "series"), body.get("episodes") or [], body.get("downloadId") or ""
        owner = item.get("id") if isinstance(item, dict) else None
        ids = [] if radarr or not isinstance(eps, list) else [e.get("id") if isinstance(e, dict) else None for e in eps]
        if not (type(owner) is int and owner > 0 and all(type(i) is int and i > 0 for i in ids) and isinstance(down, str) and len(down) <= MAX_ID):
            raise Refused(400, f"the Grab body has no {'movie' if radarr else 'series'} id, episode ids or downloadId that the hook can read")
        rec = h.keep_grab(app, owner, down, ids) or {}
        n = len(rec.get("kept") or [])
        note(h, app, "grab", f"kept {n} file{'' if n == 1 else 's'}." + (f" {rec['note'][:1].upper()}{rec['note'][1:]}" if rec.get("note") else ""), logged=False)
    except Exception as ex:
        note(h, app, "error", f"the grab kept nothing: {type(ex).__name__}: {ex}")
    return "arr-media-guard: Grab ok"


def test_event(h, app):
    """Why the setup cannot work, or None: the policy, the app's API and key, and a root folder this container does not
    see."""
    if h.arr_decide.POLICY is None:
        return h.policy_help()
    try:
        roots = [r["path"] for r in h.arr(app, "rootfolder")]
    except Exception as ex:
        return h.mask(f"the {NAMES[app]} API did not answer: {type(ex).__name__}: {ex}")[:300]
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        return f"this container does not see the root folders {', '.join(missing)}. Mount the media at the app's paths, or {h.map_fix(app)}"
    return None


def note(h, app, result, text, logged=True, **more):
    """One line on stdout for docker logs. With logged, the same text goes to the decision log, source webhook. Both
    hide the secrets of the env file."""
    text = h.mask(text)
    print(f"arr-media-guard: {app or '-'} {result}: {text}", flush=True)
    if not logged:
        return
    try:
        h.log(dict(source="webhook", app=app, result=result, note=text[:500], **more))
    except OSError as ex:
        print(f"arr-media-guard: the decision log did not take the line: {ex}", flush=True)


class Refusals:
    """The requests refused before the credentials passed: a wrong path, wrong credentials, a malformed request, or one
    the deadline cut. A flood of them never reaches the decision log. flush() prints one summary line a minute at most."""
    def __init__(self):
        self.mu, self.n, self.last, self.at = threading.Lock(), 0, None, float("-inf")

    def add(self, client):
        with self.mu:
            self.n, self.last = self.n + 1, client

    def flush(self, now=None):
        now = time.monotonic() if now is None else now
        with self.mu:
            if not self.n or now - self.at < QUIET:
                return None
            line = (f"arr-media-guard: refused {self.n} request{'s' if self.n != 1 else ''} without the right path or credentials, "
                    f"the last from {self.last}")
            self.n, self.at = 0, now
        print(line, flush=True)
        return line


REFUSALS = Refusals()
WRITES = threading.Lock()   # queue_job() runs under it, so the threads write one job file at a time


class Server(http.server.HTTPServer):
    """The listener's server. It serves each request on a bounded pool of THREADS threads."""
    request_queue_size = BACKLOG   # the kernel's queue of connections not yet accepted

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.pool = concurrent.futures.ThreadPoolExecutor(THREADS)
        self.slots = threading.BoundedSemaphore(THREADS + BACKLOG)
        self.open, self.mu = set(), threading.Lock()   # the connections in a thread or waiting for one
        self.queued = True   # a job was queued since the last look. True: the first look finds work a stopped container left.

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):   # a flood of connections: this one closes at once
            REFUSALS.add(client_address[0])
            self.shutdown_request(request)
            return
        with self.mu:
            self.open.add(request)
        self.pool.submit(self.serve, request, client_address)

    def serve(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except ConnectionError:   # a client that went away before its answer, as an idle one does
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            with self.mu:
                self.open.discard(request)
            self.shutdown_request(request)
            self.slots.release()

    def server_close(self):
        """Stop at once: an idle connection loses its thread now, never at its deadline, so the exit waits for none."""
        super().server_close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.mu:
            for request in self.open:
                with contextlib.suppress(OSError):
                    request.shutdown(socket.SHUT_RDWR)


class Handler(http.server.BaseHTTPRequestHandler):
    """One request. h and auth are set by main(): the host module, and the Authorization header value that passes. A
    request stays quiet, counted in REFUSALS and never logged, until its credentials pass."""
    h, auth = None, None
    server_version = "arr-media-guard"
    timeout = READ_TIMEOUT   # each read. The deadline in setup() limits the whole request.

    def setup(self):
        super().setup()
        self.quiet = True
        self.deadline = threading.Timer(READ_TIMEOUT, self.expire)
        self.deadline.start()

    def expire(self):
        """READ_TIMEOUT passed before the whole request arrived. Its reads end at once."""
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def finish(self):
        self.deadline.cancel()
        if self.quiet:
            REFUSALS.add(self.client_address[0])
        with contextlib.suppress(OSError):   # a connection the deadline cut
            super().finish()

    def answer(self, code, text, headers=()):
        data = (text + "\n").encode()
        with contextlib.suppress(OSError):   # a client that went away, or a connection the deadline cut
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_GET(self):
        """The healthcheck. It needs no credentials and tells nothing."""
        self.deadline.cancel()
        self.quiet = self.path != "/health"
        self.answer(200, "ok") if self.path == "/health" else self.answer(404, "not found")

    def do_POST(self):
        h, app = self.h, self.path.strip("/")
        try:
            if self.path not in ("/radarr", "/sonarr"):
                return self.answer(404, "arr-media-guard: nothing listens here. Radarr posts to /radarr, and Sonarr to /sonarr")
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(errors="replace"), self.auth):
                return self.answer(401, "arr-media-guard: the user or the password is wrong or missing",
                                   [("WWW-Authenticate", 'Basic realm="arr-media-guard"')])
            self.quiet = False
            size = self.headers.get("Content-Length")
            if size is None:
                raise Refused(411, "the request has no Content-Length")
            if not (size.isascii() and size.isdigit()):   # isdigit() alone takes "²"
                raise Refused(400, "the Content-Length is no number")
            if int(size) > MAX_BODY:   # refused before a byte of the body is read
                raise Refused(413, f"the body holds {size} bytes, over the {MAX_BODY} this listener takes")
            try:
                raw = self.rfile.read(int(size))
            except TimeoutError:
                raw = b""
            self.deadline.cancel()   # the whole request is in. The checks below may wait for the app's API.
            if len(raw) < int(size):   # the deadline cut the body short
                raise Refused(408, f"the body did not arrive in {READ_TIMEOUT} seconds")
            try:
                body = json.loads(raw)
            except RecursionError:
                raise Refused(400, "the body nests too deep") from None
            except ValueError:
                raise Refused(400, "the body is no JSON") from None
            if not isinstance(body, dict):
                raise Refused(400, "the body is no JSON object")
            event = body.get("eventType")
            if event == "Test":
                why = test_event(h, app)
                if why:
                    raise Refused(500, why)
                warnings = h.bin_warnings(app)
                for w in warnings:
                    note(h, app, "warning", w, logged=False)
                self.answer(200, " ".join(["arr-media-guard: Test ok.", *(f"Warning: {w}" for w in warnings)]))
                note(h, app, "test", "Test ok", logged=False)
            elif event == "Download":
                job = job_of(h, app, body)
                with WRITES:
                    name = h.queue_job(job)
                self.server.queued = True   # main() starts a worker once the answer went out
                note(h, app, "queued", f"{job['path']} as {name}", logged=False)
                self.answer(200, f"arr-media-guard: queued {job['path']}")
            elif event == "Grab" and h.KEEP_REPLACED:
                self.answer(200, grab(h, app, body))
            else:   # a trigger the connection was not meant to send, as hook() treats it
                self.answer(200, f"arr-media-guard: {str(event)[:50]} ignored")
        except Refused as ex:
            note(h, app, "refused", str(ex), code=ex.code, client=self.client_address[0])
            self.answer(ex.code, f"arr-media-guard: {ex}")
        except Exception as ex:   # the app's API failed, or the queue did not take the job
            note(h, app, "error", f"{type(ex).__name__}: {ex}", client=self.client_address[0])
            self.answer(502, h.mask(f"arr-media-guard: {type(ex).__name__}: {ex}")[:300])

    do_PUT = do_POST   # the Webhook connection may use PUT

    def log_message(self, fmt, *args):
        """One line per request on stdout, with control characters escaped. Never the Authorization header, and never a
        quiet request."""
        if not self.quiet:
            print(f"arr-media-guard: {self.client_address[0]} {(fmt % args).translate(CONTROL)}", flush=True)


def waiting(h):
    """Whether work waits for a worker: a queued import or deep analysis job, a job a dead worker left claimed, or the
    Plex analyzes a stopped worker kept. A worker exits once no import waits, so a deep analysis job left by a stopped
    container waits for this check."""
    names = lambda d: [n for n in os.listdir(d) if n.endswith(".json") and not n.startswith(".")] if os.path.isdir(d) else []
    return bool(names(h.queue_dir()) or names(h.deep_analysis_dir()) or names(h.claimed_dir())
                or os.path.exists(os.path.join(h.CFG["STATE_DIR"], "plex-pending.json")))


def spawn(h, what):
    """Start arr-media-guard --serve --<what>, the worker or the daily jobs, as a new program in its own session. So a
    stop reaches its whole process group. A fork would copy the listener's threads and any lock one of them held."""
    return subprocess.Popen([sys.executable, os.path.realpath(h.__file__), "--serve", f"--{what}"], start_new_session=True)


def kick(h):
    """Start a worker when work waits and no worker holds worker.lock, as the first hook of a burst does. The worker
    takes the lock itself. Returns its Popen or None."""
    if not waiting(h):
        return None
    lock = h.try_lock("worker.lock")
    if not lock:
        return None
    lock.close()
    return spawn(h, "worker")


def daily_due(h, at, now=None):
    """Whether the daily jobs are due: the local time is past at (HH:MM) and they did not run today. It marks today
    first, so a failed run never repeats in a loop. A day the container was down at that time runs at the next start."""
    now = now or datetime.datetime.now()
    if not at or now.strftime("%H:%M") < at:
        return False
    mark, today = os.path.join(h.CFG["STATE_DIR"], "serve-daily"), now.date().isoformat()
    try:
        with open(mark) as f:
            if f.read().strip() == today:
                return False
    except OSError:
        pass
    with open(mark, "w") as f:
        f.write(today + "\n")
    return True


def apps_on(h):
    """The apps whose API key reads, from the env file or config.xml. A host with one app audits that one only."""
    out = []
    for app in h.APPS:
        try:
            h.api_key(app)
            out.append(app)
        except (OSError, AttributeError):   # no config.xml, or no key in it
            pass
    return out


def daily(h):
    """The nightly jobs of a native install: the audit of each app, which also prunes the kept originals, then the
    rotation of the decision log. Runs as arr-media-guard --serve --daily."""
    for app in apps_on(h):
        subprocess.run([sys.executable, os.path.realpath(h.__file__), "--audit", app, "--since", "24h", "--post"], check=False)
    conf = os.path.join(h.CFG["STATE_DIR"], "logrotate.conf")
    with open(conf, "w") as f:
        f.write(ROTATE.format(log=h.CFG["LOG"]))
    try:
        subprocess.run(["logrotate", "-s", os.path.join(h.CFG["STATE_DIR"], "logrotate.state"), conf], check=False)
    except FileNotFoundError:
        print("arr-media-guard: logrotate is not installed, so the decision log is not rotated", flush=True)


def path_check(h):
    """Print the warnings of h.path_warnings(), for docker logs. The listener runs it in a thread at its start, so a
    slow app or Plex never delays the listener."""
    for w in h.path_warnings():
        print(f"arr-media-guard: warning: {w}", flush=True)


def config(h):
    """(Authorization header value, AUDIT_TIME) from the env file, or exit with what to fix."""
    user, pw = h.CFG.get("WEBHOOK_USER", ""), h.CFG.get("WEBHOOK_PASSWORD", "")
    if not user or not pw:
        sys.exit(f"arr-media-guard --serve: set WEBHOOK_USER and WEBHOOK_PASSWORD in {h.ENV_FILE}. The Webhook connection of each app "
                 "uses the same two")
    if ":" in user or not all(c.isascii() and c.isprintable() for c in user + pw):   # the apps send them as ISO-8859-1
        sys.exit("arr-media-guard --serve: WEBHOOK_USER and WEBHOOK_PASSWORD take printable ASCII only, and the user takes no ':'")
    if h.PATH_MAP_ERROR:
        sys.exit(f"arr-media-guard --serve: {h.PATH_MAP_ERROR}")
    at = h.CFG.get("AUDIT_TIME", "07:30")
    try:
        at = at and datetime.datetime.strptime(at, "%H:%M").strftime("%H:%M")
    except ValueError:
        sys.exit("arr-media-guard --serve: AUDIT_TIME takes HH:MM, or empty for no nightly audit")
    return b"Basic " + base64.b64encode(f"{user}:{pw}".encode()), at


def main(h, argv):
    if argv == ["--worker"]:   # started by kick()
        lock = h.try_lock("worker.lock")
        return h.worker(lock) if lock else None
    if argv == ["--daily"]:   # started by the listener once a day
        return daily(h)
    if argv:
        sys.exit("usage: arr-media-guard --serve")
    Handler.h, (Handler.auth, at) = h, config(h)
    for d in ("queue", "claimed", "alerts"):
        os.makedirs(os.path.join(h.CFG["STATE_DIR"], d), exist_ok=True)
    server = Server(("", PORT), Handler)
    server.timeout = POLL
    stop, kids, last = [], {}, 0.0   # kids: Popen -> "worker" or "daily"
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.append(True))
    maps = ", ".join(f"{who} {'|'.join(':'.join(p) for p in h.MAPS.get(who, h.PATH_MAP)) or 'none'}" for who in h.MAP_KEYS)
    print(f"arr-media-guard {h.VERSION}: listening on port {PORT}. Path maps: {maps}. Nightly audit: {at or 'off'}. "
          f"Apps with an API key: {', '.join(apps_on(h)) or 'none'}.", flush=True)
    for why in h.CONFIG_ERRORS:
        print(f"arr-media-guard: {why}", flush=True)
    threading.Thread(target=path_check, args=(h,), daemon=True).start()   # the apps and Plex may still be starting
    while not stop:
        server.handle_request()
        for p in [p for p in kids if p.poll() is not None]:   # a child that ended
            del kids[p]
        REFUSALS.flush()
        if server.queued or time.monotonic() - last >= TICK:
            server.queued, last = False, time.monotonic()
            if p := kick(h):   # a worker that still ends after its last job holds no lock, and exits when it sees this one
                kids[p] = "worker"
            if "daily" not in kids.values() and daily_due(h, at):
                kids[spawn(h, "daily")] = "daily"
    server.server_close()
    REFUSALS.flush(now=float("inf"))   # the count since the last summary line
    for p in kids:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
    for p in kids:   # the worker ends its flag edit or re-grab first, see no_stop() in the host script
        p.wait()
    print("arr-media-guard: stopped", flush=True)
