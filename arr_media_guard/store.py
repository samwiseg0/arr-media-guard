# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The state store: one SQLite file in STATE_DIR, in WAL mode (docs/design.md, "State"). It holds the job queue, the
records of the kept files, the re-grab counts, the caches, the scan progress and the facts that the readers of the
decision log need. A change runs in one short transaction, which takes the place of a lock file. status.json and
lid.sqlite stay files of their own."""
import contextlib, json, os, sqlite3, threading, time

from . import config

FILE = "state.sqlite"
WAIT = 60              # seconds a change waits for the transaction of another process
HOOK_WAIT = 2          # the wait of hook() and the listener, which the apps wait for. A job then goes into a file, see runner.queue_job().
wait = WAIT            # the wait of one transaction of this process
until = None           # a time.perf_counter() time: no wait of this process goes past it. hook() sets it, so its whole run waits HOOK_WAIT.
KEEP_DECISIONS = 14 * 86400   # seconds a decision row and an editing mark stay, two weekly rotations of the decision log
TABLES = (
    "CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (ns, key))",
    # name is <time in nanoseconds>-<pid>.json. claimed is 1 while a job process of runner.coordinate() runs the job. due
    # is the time in nanoseconds that a job put back for a later try waits for.
    "CREATE TABLE IF NOT EXISTS jobs (name TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0, "
    "at REAL NOT NULL, pid INTEGER, settled INTEGER NOT NULL DEFAULT 0, job TEXT NOT NULL, PRIMARY KEY (name, claimed))",
    "CREATE TABLE IF NOT EXISTS decisions (at REAL NOT NULL, app TEXT, path TEXT, rec TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS decisions_at ON decisions (at)",
    "CREATE INDEX IF NOT EXISTS decisions_path ON decisions (path)",
    "PRAGMA user_version = 1")
LOCAL = threading.local()   # db(): the connection of this thread
INHERITED = []              # connections of a parent process. SQLite asks that a forked child never uses or closes them.


def path():
    return os.path.join(config.CFG.state_dir, FILE)


def db():
    """The connection of this thread to the store in STATE_DIR. A new store gets its tables. A store file that was
    removed or replaced gets a new connection."""
    here, pid = path(), os.getpid()
    got = getattr(LOCAL, "db", None)
    with contextlib.suppress(OSError):
        if got and got[:3] == (pid, here, os.stat(here).st_ino):
            return got[3]
    if got and got[0] != pid:
        INHERITED.append(got[3])
    elif got:
        got[3].close()
    os.makedirs(config.CFG.state_dir, exist_ok=True)
    # A new store file gets mode 0o666, and SQLite gives its WAL files the mode of the store, so both apps' users write.
    # The close of any descriptor of the file drops every POSIX lock of this process on it, SQLite's too. So db() opens
    # the file only to create it.
    with contextlib.suppress(FileExistsError):
        os.close(os.open(here, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666))
    c = sqlite3.connect(here, timeout=wait, isolation_level=None)
    c.execute("PRAGMA journal_size_limit = 4194304")   # bytes the -wal file keeps once a checkpoint resets it. The listener runs for days.
    ino = os.stat(here).st_ino
    if not c.execute("PRAGMA user_version").fetchone()[0]:
        c.execute("PRAGMA auto_vacuum = INCREMENTAL")   # before the first table, see shrink()
        c.execute("PRAGMA journal_mode = WAL")
        for t in TABLES:
            c.execute(t)
    LOCAL.db = (pid, here, ino, c)
    return c


@contextlib.contextmanager
def tx():
    """One write transaction. It takes the write lock at its start, so what it reads stays true until it ends. Its wait
    for another writer ends after wait seconds, at until, or at the job's time limit with OutOfTime. Inside a transaction
    it adds nothing."""
    c = db()
    if c.in_transaction:
        yield c
        return
    secs = config.DEADLINE.bound(wait) if until is None else max(0.0, min(config.DEADLINE.bound(wait), until - time.perf_counter()))
    c.execute(f"PRAGMA busy_timeout = {int(secs * 1000)}")
    try:
        c.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        config.DEADLINE.check()   # the wait took the time the job had left
        raise
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def read(sql, *args):
    return db().execute(sql, args).fetchall()


def write(sql, *args):
    """One change in a transaction of its own, or of the caller's. Returns its cursor."""
    with tx() as c:
        return c.execute(sql, args)


def get(ns, key, default=None, newer=None):
    """The value under key in ns, or default. With newer, only a value stored after that time counts."""
    row = db().execute("SELECT value, at FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone()
    return default if row is None or (newer is not None and row[1] <= newer) else json.loads(row[0])


def put(ns, key, value, at=None):
    """Store value under key in ns, with the time at, now by default. A key keeps its place in items()."""
    write("INSERT INTO kv VALUES (?, ?, ?, ?) ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value, at = excluded.at",
          ns, key, json.dumps(value), time.time() if at is None else at)


def drop(ns, key=None, older=None):
    """Remove key from ns. With no key, every key of ns goes, or with older each one stored before that time."""
    where = "ns = ?" + (" AND key = ?" if key is not None else "") + (" AND at < ?" if older is not None else "")
    write(f"DELETE FROM kv WHERE {where}", *(x for x in (ns, key, older) if x is not None))


def items(ns):
    """{key: value} of ns, in the order the keys came in."""
    return {k: json.loads(v) for k, v in db().execute("SELECT key, value FROM kv WHERE ns = ? ORDER BY rowid", (ns,))}


def add(ns, key):
    """Store key in ns with no value. False when it is there already, so of two processes one alone adds it."""
    return write("INSERT OR IGNORE INTO kv VALUES (?, ?, 'null', ?)", ns, key, time.time()).rowcount == 1


def decided(at, app, path, rec):
    """Keep one decision line for the audit and --force-convert, rec as JSON text, see logs.decision(). The rows and the
    editing marks older than KEEP_DECISIONS go."""
    with tx() as c:
        c.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)", (at, app, path, rec))
        c.execute("DELETE FROM decisions WHERE at < ?", (at - KEEP_DECISIONS,))
        c.execute("DELETE FROM kv WHERE ns = 'editing' AND at < ?", (at - KEEP_DECISIONS,))


def shrink():
    """Give the free pages of the store back to the disk, once a night in the audit. A row that decided() deletes
    leaves a free page, and SQLite never shrinks the file by itself. db() creates the store with auto_vacuum
    INCREMENTAL for this. The pragma frees one page per row it returns, so fetchall() runs it to its end."""
    db().execute("PRAGMA incremental_vacuum").fetchall()

