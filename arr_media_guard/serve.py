#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The Webhook listener of arr-media-guard, for Sonarr and Radarr in Docker. The apps reach it over HTTP, so nothing
is installed in their containers.

  arr-media-guard --serve

It listens on port PORT. Each instance posts to /<its name>, Radarr to /radarr and Sonarr to /sonarr, with HTTP basic
auth. The user and password are WEBHOOK_USER and WEBHOOK_PASSWORD, from the env file or the environment. When the
password is empty in the env file, the listener writes a new one at its start, see new_password(). Without a user, the
listener does not start. The body gives the same runner.Event that hook() reads from the Custom Script variables, with
the checks of runner.Event.from_webhook().
A Download event (an import or an upgrade) becomes its job through queue_job(), so the worker and the queue stay one
code path. A Test event runs runner.app_check(), as the hook's Test does. With KEEP_REPLACED on, a Grab event
hard-links the files the grab may replace, see grab(). The listener refuses a body that fails a check with a log line.
At its start it prints BANNER, and start_check() runs the same checks for each app.

It serves THREADS requests at once, and each request has READ_TIMEOUT seconds for its headers and body together, so
a slow client never holds up an app. A request without the right path or credentials never reaches the decision log.
The listener counts them and prints one summary line a minute at most.

The listener also keeps a worker going. After each job, and every TICK seconds, it starts a worker when a job waits
and no worker holds worker.lock, see runner.ensure_worker(). The worker is a new program, arr-media-guard --serve
--worker, so it reads the env file and the policy again, and no thread of the listener goes into it. The listener, the
worker and the nightly audit set config.SERVE, so the summary of each decision line goes to stdout too. Once a day at AUDIT_TIME it runs
the nightly audit of each app, which also removes kept originals older than KEEP_ORIGINALS_DAYS, and it rotates the
decision log with logrotate. SIGTERM stops the listener.
It sends SIGTERM to the worker and waits for it, so a flag edit is never cut. See docs/design.md, "Webhook".
"""
import base64, concurrent.futures, contextlib, datetime, hmac, http.server, json, os, secrets, signal, socket, sqlite3, subprocess, sys, tempfile, threading, time

from . import apps, cli, config, decide, logs, runner, store, vault

PORT = 8484            # the listener's port in the container. Docker maps it to any host port.
MAX_BODY = 1 << 20     # bytes a Webhook body may hold. An import body holds a few kilobytes.
READ_TIMEOUT = 10      # seconds a client has for its whole request, the headers and the body together
THREADS = 32           # requests the listener serves at once. An idle connection holds one until READ_TIMEOUT.
BACKLOG = 64           # connections that may wait for a thread. The listener closes each one past that at once.
QUIET = 60             # seconds between two summary lines of the requests refused before the credentials
POLL = 2               # seconds the listener waits for a request before it looks for a stop
TICK = 60              # seconds between two looks for a waiting job with no worker, and for the daily jobs
START_WAIT = 120       # seconds the start check asks again an app that does not answer, as one that starts beside the listener
USER = "arr-admin"     # the WEBHOOK_USER of the Docker env file. new_password() writes it when WEBHOOK_USER is empty.
BANNER = r"""
                                   _  _                                     _
 __ _ _ _ _ _  ___  _ __   ___  __| |(_) __ _  ___  __ _ _  _  __ _ _ _  __| |
/ _` | '_| '_||___|| '  \ / -_)/ _` || |/ _` ||___|/ _` | || |/ _` | '_|/ _` |
\__,_|_| |_|       |_|_|_|\___|\__,_||_|\__,_|     \__, |\_,_|\__,_|_|  \__,_|
                                                   |___/
"""[1:-1]   # the listener prints it once at its start, above the listening line
ROTATE = """{log} {{
    weekly
    rotate 52
    compress
    delaycompress
    missingok
    notifempty
}}
"""   # the decision log's rotation, as docs/monitoring.md asks of a host install


def download(app, body):
    """The job of a Webhook Download event, see runner.Event.from_webhook(). Raises Refused when the body fails a check.
    The job is queued even when the app's API fails or this container does not see the file, with one warning line, so
    no import is lost. When the API failed, the job holds the body, and runner.run_job() asks the app again. A file this
    container does not see marks the job unseen, and the worker looks for it again until JOB_MAX_AGE, see
    runner.moved()."""
    name = apps.ARR[app].name
    try:
        ev = runner.Event.from_webhook(app, body)
    except runner.Refused:
        raise
    except Exception as ex:
        note(app, "warning", f"the {name} API did not answer: {type(ex).__name__}: {ex}. The job holds the ids of the body, and the worker "
                             f"asks {name} again")
        return dict(runner.Event.from_webhook(app, body, ask=False).job(), webhook=body)
    job = ev.job()
    if not os.path.isfile(ev.path):
        note(app, "warning", f"{name} lists file {ev.file_id} at {ev.path}, and this container does not see it there. The job is queued, and "
                             f"the worker looks for the file again. Mount the media at the app's paths, or {apps.map_fix(app)}")
        job["unseen"] = True
    posted = apps.mapped(body[apps.ARR[app].body_file].get("path"), app)
    if posted and posted != ev.path:   # the app may have renamed it since. The API wins.
        note(app, "warning", f"the body names {posted!r}, and {name} lists file {ev.file_id} at {ev.path}. The job takes the path of the API")
    return job


def grab(app, body):
    """Keep the files a Grab event may replace, see vault.keep_grab(). The body names the item and the episodes by id,
    see runner.Event.from_webhook(), and the app's API gives the files. An error logs a line, and the answer is still
    ok, so the app's grab never fails on the hook."""
    try:
        ev = runner.Event.from_webhook(app, body)
        rec = vault.keep_grab(app, ev.owner, ev.download_id, ev.eps) or {}
        n = len(rec.get("kept") or [])
        note(app, "grab", f"kept {n} file{'' if n == 1 else 's'}." + (f" {rec['note'][:1].upper()}{rec['note'][1:]}" if rec.get("note") else ""), logged=False)
    except Exception as ex:
        note(app, "error", f"the grab kept nothing: {type(ex).__name__}: {ex}")
    return "arr-media-guard: Grab ok"


def note(app, result, text, logged=True, **more):
    """One line on stdout for docker logs. With logged, the same text goes to the decision log, source webhook. Both
    hide the secrets of the env file."""
    text = config.mask(text)
    print(f"arr-media-guard: {app or '-'} {result}: {text}", flush=True)
    if not logged:
        return
    try:
        logs.log(dict(source="webhook", app=app, result=result, note=text[:500], **more))
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
WRITES = threading.Lock()   # queue_job() runs under it, so the threads queue one job at a time


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
    """One request. auth is set by main(): the Authorization header value that passes. A
    request stays quiet, counted in REFUSALS and never logged, until its credentials pass."""
    auth = None
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
        app = self.path.strip("/")
        try:
            if self.path not in {f"/{a}" for a in config.CFG.apps}:   # the whole path names an instance, or it stays out of every log
                return self.answer(404, "arr-media-guard: nothing listens here. Each instance posts to /<its name>, as /radarr or /sonarr-4k")
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(errors="replace"), self.auth):
                return self.answer(401, "arr-media-guard: the user or the password is wrong or missing",
                                   [("WWW-Authenticate", 'Basic realm="arr-media-guard"')])
            self.quiet = False
            size = self.headers.get("Content-Length")
            if size is None:
                raise runner.Refused(411, "the request has no Content-Length")
            if not (size.isascii() and size.isdigit()):   # isdigit() alone takes "²"
                raise runner.Refused(400, "the Content-Length is no number")
            if int(size) > MAX_BODY:   # refused before a byte of the body is read
                raise runner.Refused(413, f"the body holds {size} bytes, over the {MAX_BODY} this listener takes")
            try:
                raw = self.rfile.read(int(size))
            except TimeoutError:
                raw = b""
            self.deadline.cancel()   # the whole request is in. The checks below may wait for the app's API.
            if len(raw) < int(size):   # the deadline cut the body short
                raise runner.Refused(408, f"the body did not arrive in {READ_TIMEOUT} seconds")
            try:
                body = json.loads(raw)
            except RecursionError:
                raise runner.Refused(400, "the body nests too deep") from None
            except ValueError:
                raise runner.Refused(400, "the body is no JSON") from None
            if not isinstance(body, dict):
                raise runner.Refused(400, "the body is no JSON object")
            event = body.get("eventType")
            if event == "Test":
                why, warnings = runner.app_check(app)
                if why:
                    raise runner.Refused(500, why)
                for w in warnings:
                    note(app, "warning", w, logged=False)
                self.answer(200, " ".join(["arr-media-guard: Test ok.", *(f"Warning: {w}" for w in warnings)]))
                note(app, "test", "Test ok", logged=False)
            elif event == "Download":
                job = download(app, body)
                with WRITES:
                    name = runner.queue_job(job)
                self.server.queued = True   # main() starts a worker once the answer went out
                what = job["path"] or f"{apps.ARR[app].name} file {job['file_id']}"   # no path while the API did not answer
                note(app, "queued", f"{what} as {name}", logged=False)
                self.answer(200, f"arr-media-guard: queued {what}")
            elif event == "Grab" and config.CFG.keep_replaced:
                self.answer(200, grab(app, body))
            else:   # a trigger the connection was not meant to send, as hook() treats it
                self.answer(200, f"arr-media-guard: {str(event)[:50]} ignored")
        except runner.Refused as ex:
            note(app, "refused", str(ex), code=ex.code, client=self.client_address[0])
            self.answer(ex.code, f"arr-media-guard: {ex}")
        except Exception as ex:   # the queue did not take the job
            note(app, "error", f"{type(ex).__name__}: {ex}", client=self.client_address[0])
            self.answer(502, config.mask(f"arr-media-guard: {type(ex).__name__}: {ex}")[:300])

    do_PUT = do_POST   # the Webhook connection may use PUT

    def log_message(self, fmt, *args):
        """One line per request on stdout, with control characters escaped. Never the Authorization header, and never a
        quiet request."""
        if not self.quiet:
            print(f"arr-media-guard: {self.client_address[0]} {(fmt % args).translate(runner.CONTROL)}", flush=True)


def spawn(what):
    """Start arr-media-guard --serve --<what>, the worker or the daily jobs, as a new program in its own session. So a
    stop reaches its whole process group. A fork would copy the listener's threads and any lock one of them held."""
    return subprocess.Popen([sys.executable, config.SCRIPT, "--serve", f"--{what}"], start_new_session=True)


def daily_due(at, now=None):
    """Whether the daily jobs are due: the local time is past at (HH:MM) and they did not run today. It marks today
    first, so a failed run never repeats in a loop. A day the container was down at that time runs at the next start."""
    now = now or datetime.datetime.now()
    if not at or now.strftime("%H:%M") < at:
        return False
    try:
        with store.tx():
            if store.get("mark", "serve-daily") == now.date().isoformat():
                return False
            store.put("mark", "serve-daily", now.date().isoformat())
    except sqlite3.Error:   # a busy store: the next tick asks again
        return False
    return True


def apps_on():
    """The instances whose API key reads, from the env file or config.xml. A host with one app audits that one only."""
    out = []
    for app in config.CFG.apps:
        try:
            apps.api_key(app)
            out.append(app)
        except (OSError, AttributeError):   # no config.xml, or no key in it
            pass
    return out


def daily():
    """The nightly jobs in Docker, which a host install runs from a timer and logrotate. It runs the audit of each app,
    which also prunes the kept originals, then the rotation of the decision log. Runs as arr-media-guard --serve --daily. Each audit runs as --serve --audit, so its
    summary lines reach the container log, see config.SERVE."""
    for app in apps_on():
        subprocess.run([sys.executable, config.SCRIPT, "--serve", "--audit", app, "--since", "24h", "--post"], check=False)
    conf = os.path.join(config.CFG.state_dir, "logrotate.conf")
    with open(conf, "w") as f:
        f.write(ROTATE.format(log=config.CFG.log))
    try:
        subprocess.run(["logrotate", "-s", os.path.join(config.CFG.state_dir, "logrotate.state"), conf], check=False)
    except FileNotFoundError:
        print("arr-media-guard: logrotate is not installed, so the decision log is not rotated", flush=True)


def path_check(per_app=True):
    """Print the warnings of apps.path_warnings(), for docker logs."""
    for w in apps.path_warnings(per_app):
        print(f"arr-media-guard: warning: {w}", flush=True)


def start_check():
    """path_check(), then the checks of the Test event for each app with an API key, see runner.app_check(), with one
    line per result for docker logs. An app that does not answer is asked again for START_WAIT seconds. A failed check
    warns, and the listener keeps running. An app with its own URL and no API key warns too, see apps.no_key(). The
    listener runs it in a thread at its start, so a slow app or Plex never delays the listener."""
    path_check(per_app=False)   # the start check below reports an app that does not answer and a root folder not seen
    if decide.POLICY is None:   # once, and each app is still checked
        print(f"arr-media-guard: warning: the start check failed: {config.policy_help()}", flush=True)
    until = time.monotonic() + START_WAIT
    for app in config.CFG.apps:
        try:
            apps.api_key(app)
        except FileNotFoundError as ex:   # no API key and no config.xml
            if why := apps.no_key(app, ex):
                note(app, "warning", why, logged=False)
            continue
        except (OSError, AttributeError):   # no key in config.xml, as for apps_on()
            continue
        why, warnings = runner.app_check(app, until, policy=False)
        for w in warnings:
            note(app, "warning", w, logged=False)
        note(app, "warning", f"the start check failed: {why}", logged=False) if why else note(app, "start check", "ok", logged=False)


def new_password(user):
    """(user, password) after a new WEBHOOK_PASSWORD went into the env file, in place of its empty line. An empty user
    becomes USER the same way. A temp file and a rename write the file, so a crash never leaves half a file. The file
    keeps its owner and mode. The log line says where the password is, and never shows it."""
    path, keys = config.ENV_FILE, {} if user else {"WEBHOOK_USER": USER}   # a user already set keeps its line
    keys["WEBHOOK_PASSWORD"] = secrets.token_urlsafe(24)   # 32 characters of A-Z, a-z, 0-9, '-' and '_'
    try:
        st = os.stat(path)
        with open(path) as f:   # each line of a key, as config.env_file() reads it
            lines = [f"{k}='{keys[k]}'" if (k := line.strip().partition("=")[0]) in keys else line for line in f.read().rstrip("\n").split("\n")]
        lines += [f"{k}='{v}'" for k, v in keys.items() if f"{k}='{v}'" not in lines]   # a key the file does not hold
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env-")
        try:
            with os.fdopen(fd, "w") as f:
                os.fchown(fd, st.st_uid, st.st_gid)
                os.fchmod(fd, st.st_mode & 0o7777)
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(fd)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except OSError as ex:
        sys.exit(f"arr-media-guard --serve: WEBHOOK_PASSWORD is empty, and the listener did not write a new one to {path}: {ex}. "
                 "Set WEBHOOK_USER and WEBHOOK_PASSWORD in it")
    print(f"arr-media-guard: generated a Webhook password and wrote it to {path} as WEBHOOK_PASSWORD. With docker/compose.yml, "
          "that is ./arr-media-guard/arr-media-guard.env on the host. In the Webhook connection of each app, set Username to "
          f"WEBHOOK_USER{' from the environment' if 'WEBHOOK_USER' in config.CFG.from_env else ''} and Password to WEBHOOK_PASSWORD "
          "from that file.", flush=True)
    return keys.get("WEBHOOK_USER", user), keys["WEBHOOK_PASSWORD"]


def listen_config():
    """(Authorization header value, AUDIT_TIME) from the settings, or exit with what to fix. An empty password gets a new
    one, see new_password(). A password from the environment never does, because the environment wins over the file."""
    user, pw = config.CFG.webhook_user, config.CFG.webhook_password
    if not pw and "WEBHOOK_PASSWORD" not in config.CFG.from_env and (user or "WEBHOOK_USER" not in config.CFG.from_env):
        user, pw = new_password(user)   # an empty user from the environment stops the start below, so nothing is written
    if not user or not pw:
        sys.exit(f"arr-media-guard --serve: set WEBHOOK_USER and WEBHOOK_PASSWORD in {config.ENV_FILE} or the environment. The Webhook connection of each app "
                 "uses the same two")
    if ":" in user or not all(c.isascii() and c.isprintable() for c in user + pw):   # the apps send them as ISO-8859-1
        sys.exit("arr-media-guard --serve: WEBHOOK_USER and WEBHOOK_PASSWORD take printable ASCII only, and the user takes no ':'")
    if config.CFG.map_error:
        sys.exit(f"arr-media-guard --serve: {config.CFG.map_error}")
    at = config.CFG.audit_time
    try:
        at = at and datetime.datetime.strptime(at, "%H:%M").strftime("%H:%M")
    except ValueError:
        sys.exit("arr-media-guard --serve: AUDIT_TIME takes HH:MM, or empty for no nightly audit")
    return b"Basic " + base64.b64encode(f"{user}:{pw}".encode()), at


def main(argv):
    config.SERVE = True   # each syslog summary goes to stdout too, see logs.to_syslog()
    if argv == ["--worker"]:   # started by runner.ensure_worker()
        lock = runner.try_lock("worker.lock")
        return runner.worker(lock) if lock else None
    if argv == ["--daily"]:   # started by the listener once a day
        return daily()
    if argv[:1] == ["--audit"]:   # started by daily()
        return cli.main(argv)
    if argv:
        sys.exit("usage: arr-media-guard --serve")
    Handler.auth, at = listen_config()
    store.wait = store.HOOK_WAIT   # the apps wait for each answer, see runner.queue_job()
    server = Server(("", PORT), Handler)
    server.timeout = POLL
    stop, kids, last = [], {}, 0.0   # kids: Popen -> "worker" or "daily"
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.append(True))
    maps = ", ".join(f"{who} {'|'.join(':'.join(p) for p in config.CFG.map_of(who)) or 'none'}"
                     for who in dict.fromkeys([*config.MAP_KEYS, *config.CFG.apps]))   # sonarr, radarr and plex first, as before
    print(BANNER, flush=True)
    print(f"arr-media-guard {config.VERSION}: listening on port {PORT}. Path maps: {maps}. Nightly audit: {at or 'off'}. "
          f"Apps with an API key: {', '.join(apps_on()) or 'none'}.", flush=True)
    for why in config.CFG.errors:
        print(f"arr-media-guard: {why}", flush=True)
    threading.Thread(target=start_check, args=(), daemon=True).start()   # the apps and Plex may still be starting
    while not stop:
        server.handle_request()
        for p in [p for p in kids if p.poll() is not None]:   # a child that ended
            del kids[p]
        REFUSALS.flush()
        if server.queued or time.monotonic() - last >= TICK:
            server.queued, last = False, time.monotonic()
            if p := runner.ensure_worker(spawn):   # a worker that still ends after its last job holds no lock, and exits when it sees this one
                kids[p] = "worker"
            if "daily" not in kids.values() and daily_due(at):
                kids[spawn("daily")] = "daily"
    server.server_close()
    REFUSALS.flush(now=float("inf"))   # the count since the last summary line
    for p in kids:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
    for p in kids:   # the worker ends its flag edit or re-grab first, see runner.no_stop()
        p.wait()
    print("arr-media-guard: stopped", flush=True)
