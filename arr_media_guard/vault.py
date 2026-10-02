# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The files the hook keeps: the original of a remux, the grab links, and their prune."""
import calendar, contextlib, errno, hashlib, os, shutil, sqlite3, time

from . import apps, config, logs, regrab, store


def kept_read():
    """The records of the links keep_grab() made, from the store, see kept_records()."""
    return store.get("kept", "replaced", [])


@contextlib.contextmanager
def kept_records():
    """The records of kept_read() as a list to change in place, in one store transaction, written back after the
    block. A record stays while it is younger than keep_days or its link exists, so links() knows every link of the
    hook's own until the nightly prune removes it. Each record holds the app, the old path, the kept path, the grab's
    download id, the time and the inode of the link. An extra also holds "of", its video's old path.
    "import" names the download id of the import that replaced the old path, and "stale" says why the link may never
    come back, see kept_replaced()."""
    old = {r["kept"] for r in kept_read() if time.time() - r["time"] > config.CFG.keep_days * 86400}
    gone = {k for k in old if not os.path.lexists(k)}   # before the transaction, so a slow NAS never holds the store
    with store.tx():   # a conversion's check keeps a sidecar under the job's time limit, and the wait ends at it
        recs = [r for r in kept_read() if r["kept"] not in gone]
        yield recs
        store.put("kept", "replaced", recs)


def kept_replaced(path, download_id=None, app=None):
    """The file at path changed after the grabs that kept it. download_id is the import's when an import of the instance
    app replaced path, None when the hook replaced, moved or restored it. An import claims each unclaimed record of its
    own instance's grab, and of such a grab with no download id, so its restore may use that link. Every other record of
    path goes stale, a claimed one and another instance's too, because the import replaced the file its link kept. A
    change by the hook leaves a claimed record, which holds the file its import replaced. So a restore never brings back
    an older version. An extra follows its video. Never raises. An error logs a line."""
    if not config.CFG.keep_replaced:
        return
    try:
        with kept_records() as recs:
            for r in recs:
                if path not in (r["old"], r.get("of")) or "stale" in r or (download_id is None and "import" in r):
                    continue
                if download_id is not None and "import" not in r and r["download_id"] in ("", download_id) and r["app"] == app:
                    r["import"] = download_id
                else:
                    r["stale"] = "arr-media-guard changed the file after the grab" if download_id is None else "another import replaced the file after the grab"
    except Exception as ex:
        with contextlib.suppress(OSError):
            logs.log(dict(source="hook", path=path, result="warning", note=config.mask(f"the hook could not mark the kept copies of the file: {type(ex).__name__}: {ex}")[:300]))


def kept_copy(app, old, download_id):
    """(the record of the hook's own copy of old that the import download_id may bring back, or None, why the copy may
    not come back or None). The import must have claimed it, see kept_replaced(). The link must still be the file the
    grab linked. A copy within KEEP_MARGIN of keep_days stays out, because the nightly prune may remove it."""
    recs = [r for r in kept_read() if r["app"] == app and r["old"] == old and "of" not in r]
    mine = [r for r in recs if r.get("import") == (download_id or "") and "stale" not in r]
    if mine:
        r = max(mine, key=lambda r: r["time"])
        if time.time() - r["time"] > config.CFG.keep_days * 86400 - regrab.KEEP_MARGIN:
            return None, "The kept copy is too close to its removal at KEEP_ORIGINALS_DAYS"
        try:
            same = os.stat(r["kept"]).st_ino == r["ino"]
        except OSError:
            return None, "The kept copy is gone"
        return (r, None) if same else (None, "The kept copy changed after the grab")
    stale = [r["stale"] for r in recs if "stale" in r and r["download_id"] in ("", download_id or "")]
    return None, f"The kept copy is older, because {stale[-1]}" if stale else None


def links(st):
    """The hard links of the file st describes, less the ones keep_grab() made. edit(), repack_skip() and convert_skip()
    refuse a file with more than one, the download client's copy. They leave out a link of the hook's own. An edit
    reaches the kept copy too, and a rename over the file makes the kept copy stale, see kept_replaced(). A record matches
    by inode, so a file the app renamed or moved after the grab still finds its link."""
    n = st.st_nlink
    for r in kept_read() if n > 1 else ():
        if r.get("ino") == st.st_ino:
            with contextlib.suppress(OSError):
                k = os.stat(r["kept"])
                n -= (k.st_ino, k.st_dev) == (st.st_ino, st.st_dev)
    return n


def kept_extras(rec):
    """[(kept extra, its old path, its signature)] for the extras the grab of rec kept with its video. A stale one stays."""
    return sorted((r["kept"], r["old"], regrab.bin_sig(r["kept"])) for r in kept_read()
                  if r.get("of") == rec["old"] and r["time"] == rec["time"] and "stale" not in r)


def extra_of(stem, name):
    """Whether name is an extra of the video stem: the app names it <stem>.en.srt, <stem>.nfo or <stem>-thumb.jpg."""
    return name.startswith(stem) and name[len(stem):len(stem) + 1] in (".", "-") and os.path.splitext(name)[1].lower() not in regrab.VIDEO_EXT


def keep_grab(app, owner, download_id, eps=()):
    """Hard-link each library file a grab may replace into replaced_root(), with its extras, the kinds old_extras()
    restores. The app deletes the old file before the import calls the hook, so the Grab event is the last moment the
    file is there. eps are the episode ids of a Sonarr grab. A file that holds several of them is linked once. A file
    the app does not list gives nothing. A file the app lists and that is not on disk gives a note, because the import
    won the race. The links of one grab share one UTC stamp folder, with the path from the mount below, as
    keep_original() lays them out. When a link fails, the hook keeps nothing of that file, because a copy of a large
    file can lose the race to the import. An error after the first link removes every link of the grab, so no link
    stays without its record. The nightly audit prunes the folder, so the app never waits for it here. Returns the
    decision log line it wrote, or None when it kept and noted nothing. With KEEP_ORIGINALS_DAYS 0 it keeps nothing."""
    if not (config.CFG.keep_replaced and config.CFG.keep_days):
        return None
    paths = apps.ARR[app].grab_paths(owner, eps)
    now, kept, made, roots, stamps = time.time(), [], [], set(), set()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    missed = [f"{p}: the file is not on disk" for p in sorted({p for p in paths if p and not os.path.isfile(p)})]
    try:
        for p in sorted({p for p in paths if p and os.path.isfile(p)}):
            root, (stem, folder) = replaced_root(p), (os.path.splitext(os.path.basename(p))[0], os.path.dirname(p))
            extras = [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if extra_of(stem, n) and os.path.isfile(os.path.join(folder, n))]
            for x in [p] + extras:
                k = os.path.join(root, stamp, os.path.relpath(x, os.path.dirname(root)))
                if not os.path.lexists(os.path.join(root, stamp)):
                    stamps.add(os.path.join(root, stamp))
                try:
                    os.makedirs(os.path.dirname(k), exist_ok=True)
                    os.link(x, k)
                except OSError as ex:   # the folder or the link
                    missed.append(f"{x}: {ex.strerror or ex}")
                    if x == p:   # no extra without its video
                        break
                    continue
                made.append(k)
                roots.add(root)
                kept.append(dict(app=app, old=x, kept=k, download_id=download_id or "", time=now, ino=os.stat(k).st_ino, **({"of": p} if x != p else {})))
        if kept:
            with kept_records() as recs:
                recs += kept
    except BaseException:
        for k in made:
            with contextlib.suppress(OSError):
                os.remove(k)
        raise
    finally:   # a stamp folder this call made and left empty, after a failed link or the removal above
        for d in stamps:
            for sub, _, _ in os.walk(d, topdown=False):
                with contextlib.suppress(OSError):
                    os.rmdir(sub)
    for root in sorted(roots):
        remember_folder(root)
    if not kept and not missed:
        return None
    rec = dict(source="hook", app=app, result="kept_replaced" if kept else "not_kept_replaced", download_id=download_id or "", kept=[r["kept"] for r in kept],
               **({"note": config.mask("not kept: " + "; ".join(missed))[:500]} if missed else {}))
    logs.log(rec)
    return rec


def mount_top(folder):
    """The nearest folder at or above folder that is a mount point. It reads only."""
    m = os.path.abspath(folder)
    while m != "/" and not os.path.ismount(m):
        m = os.path.dirname(m)
    return m


def keep_root(path, name):
    """The folder named name, keep_dir or recycle_dir, where the hook keeps a file of path. It reads only, so a dry run
    may ask. The hook takes the first of these that fits:
    1. name at the top of the mount that holds path, when it exists and is writable.
    2. name at the mount top, to create, when the mount top is writable. With a mount such as /data above every root
       folder of the apps and every Plex section, none of them scans it.
    3. A writable folder name that exists on the path from the mount top down to the file's own folder, the highest
       first. So the choice stays the same from run to run.
    4. name to create in the highest writable folder on that path, as in a Docker volume that only root may write.
    5. name at the mount top. No folder on the path is writable, so the keep fails and names the mount top.
    Each one sits on the file's mount, so a hard link from the file works. A folder whose real path is on another mount,
    as one above a symlink to a share, never comes in. The dot hides it from the apps' disk scans and Plex."""
    folder = os.path.dirname(os.path.abspath(path))
    top, steps = mount_top(folder), [folder]
    while steps[0] != top and steps[0] != "/":   # the folders from the mount top down to the file's own
        steps.insert(0, os.path.dirname(steps[0]))
    own = mount_top(os.path.realpath(folder))   # a folder named through a symlink can sit on another mount
    steps = [f for f in steps if mount_top(os.path.realpath(f)) == own]
    writable = lambda f: os.path.isdir(f) and os.access(f, os.W_OK | os.X_OK)
    here = os.path.join(top, name)
    if top in steps and not os.path.lexists(here) and writable(top):
        return here
    for f in steps:   # the mount top first, so a writable folder there wins
        if writable(os.path.join(f, name)):
            return os.path.join(f, name)
    for f in steps:
        if f != top and writable(f) and not os.path.lexists(os.path.join(f, name)):
            return os.path.join(f, name)
    return here


def originals_root(path):
    """Where a repack keeps the original of path: keep_dir in the folder keep_root() picks."""
    return keep_root(path, config.CFG.keep_dir)


def replaced_root(path):
    """Where a grab keeps the library file of path that an upgrade may replace, see keep_grab(): recycle_dir in the
    folder keep_root() picks."""
    return keep_root(path, config.CFG.recycle_dir)


def kept_folders():
    """The folders a keep wrote in, from the store, see remember_folder()."""
    return set(store.get("kept", "folders", []))


def remember_folder(root):
    """Add root, a keep_dir or recycle_dir the hook wrote in, to kept_folders(), so a prune reaches a folder that
    keep_root() put below a root folder too. A folder stays in the list while it is missing, as on a NAS that is down,
    and a prune of it does nothing. It raises only OutOfTime, the job's time limit. An error logs a line."""
    if root in kept_folders():
        return
    try:
        with store.tx():
            store.put("kept", "folders", sorted(kept_folders() | {root}))
    except (sqlite3.Error, OSError) as ex:
        with contextlib.suppress(OSError):
            logs.log(dict(source="hook", path=root, result="warning", note=config.mask(f"the hook could not record the folder: {type(ex).__name__}: {ex}")[:300]))


def prune_roots(originals):
    """The folders a prune clears: each folder of kept_folders(), and the recycle_dir of each grab link. originals
    False leaves out each keep_dir. A folder of another name never comes in, so a prune never removes another folder."""
    names = (config.CFG.keep_dir, config.CFG.recycle_dir) if originals else (config.CFG.recycle_dir,)
    found = kept_folders()
    for r in kept_read():
        k = r["kept"]
        while k not in ("", "/") and os.path.basename(k) != config.CFG.recycle_dir:
            k = os.path.dirname(k)
        found.add(k)
    return {f for f in found if os.path.basename(f) in names}


LINK_REFUSED = {errno.EPERM, errno.EXDEV, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}   # a copy keeps the original then
KEEP_COPY_FREE = 1 << 30   # bytes a copy of a kept original leaves free, see keep_original()


def keep_folder(path):
    """The folder a keep of path writes in: originals_root() when it exists, else the folder that creates it."""
    root = originals_root(path)
    return root if os.path.isdir(root) else os.path.dirname(root)


def keepable(path, st):
    """Why the original of path cannot be kept, or None. The keep is a hard link, or a copy where the file system
    refuses one, see keep_original(). So a folder on another file system needs room for a copy, and keep_folder() must be
    writable. It writes nothing, so a dry run may ask."""
    where = keep_folder(path)
    try:
        other, free = os.stat(where).st_dev != st.st_dev, shutil.disk_usage(where).free
    except OSError as ex:
        return f"{where} does not read ({ex})"
    if other and free < st.st_size + KEEP_COPY_FREE:
        return f"{where} is on another file system with {free / 1e9:.1f} GB free, too little for a copy of {st.st_size / 1e9:.1f} GB"
    return None if os.access(where, os.W_OK) else f"{where} is not writable"


def keep_original(path, note=None):
    """Keep path at <originals_root>/<UTC stamp>/<path from the mount> and return that path. The rename of the new file
    over path follows, so path is never missing. The keep holds the original for keep_days, see prune_originals().

    The keep is a hard link, which needs no space. Where the file system refuses one (LINK_REFUSED: another file
    system, too many links, or a mount or a container that allows no link), it is a copy with the mode and times of
    path, and note["kept_how"] says so. The copy needs the size of path plus KEEP_COPY_FREE free, else it raises
    OSError and the fix does not run. It is written under a hidden name, compared with path by size and hash, and only
    then renamed to the kept name. So a crash leaves no file that looks kept, and the swap waits for a whole copy.
    Every fix that replaces or moves a library file keeps it here first, so the links of path from a grab go stale."""
    kept_replaced(path)
    root = originals_root(path)
    keep = os.path.join(root, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time())), os.path.relpath(path, os.path.dirname(root)))
    os.makedirs(os.path.dirname(keep), exist_ok=True)
    remember_folder(root)
    try:
        os.link(path, keep)
        return keep
    except OSError as ex:
        if ex.errno not in LINK_REFUSED:
            raise
        refused = ex.strerror or str(ex)
    size, free = os.path.getsize(path), shutil.disk_usage(os.path.dirname(keep)).free
    if free < size + KEEP_COPY_FREE:
        raise OSError(f"the hard link failed ({refused}), and {os.path.dirname(keep)} has {free / 1e9:.1f} GB free, too little for a copy of "
                      f"{size / 1e9:.1f} GB")
    part, digests = os.path.join(os.path.dirname(keep), f".{os.path.basename(keep)}.copying"), []
    try:
        with open(path, "rb") as src, open(part, "wb") as dst:
            h = hashlib.blake2b()
            for block in iter(lambda: src.read(1 << 20), b""):
                h.update(block)
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
            digests.append(h.digest())
        with open(part, "rb") as f:
            h = hashlib.blake2b()
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
            digests.append(h.digest())
        if os.path.getsize(part) != size or digests[0] != digests[1]:
            raise OSError(f"the copy of the original does not match it, so the original stays: {part}")
        shutil.copystat(path, part)
        os.replace(part, keep)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(part)
        raise
    if note is not None:
        note["kept_how"] = f"copied, because the hard link failed: {refused}"
    return keep


def prune_originals(root):
    """Remove the stamp folders under root older than keep_days. Returns their names. An error keeps the folder."""
    gone = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return gone
    for n in names:
        with contextlib.suppress(ValueError, OSError):   # a folder the other host's audit removes at the same time
            if time.time() - calendar.timegm(time.strptime(n, "%Y%m%dT%H%M%SZ")) > config.CFG.keep_days * 86400:
                shutil.rmtree(os.path.join(root, n))
                gone.append(n)
    return gone
