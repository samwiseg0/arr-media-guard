# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The file marks of the store, see runner.mark_key(): the damage mark, the mark of a burn-in job and the keep row of
a write. Each holds the inode, the size and the mtime of its file. ext4 and XFS give a freed inode number to the next
new file at once, so a new file of the same size can have the inode number of a marked one. os.stat fakes that here.
The writes of AMG's own carry each mark, and a file the app imports after AMG's write gets none. A chmod, a chown and
the removal of a hard link change only the ctime, and each mark stays. The files are made up."""
import contextlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import types

import pytest

from test_arr_media_guard import (hook, REAL_MKVMERGE, keep_on, burn, burn_lines, env, japanese_burn, media, state_dir_in_tmp,  # noqa: F401
                                  sync_mkv, verdict)
from test_one_keep import film, flag_edit, heard, retime
from test_remux_edits import header
from test_whole_damage import DAMAGE, clips  # noqa: F401

MARKS = {"damaged": (lambda p: hook.damage_mark(p, DAMAGE, hook.file_key(os.stat(p))), lambda p: hook.damaged(p) == DAMAGE),
         "muted": (lambda p: hook.mute_mark(p, hook.file_key(os.stat(p))), hook.muted),
         "wrote": (retime, lambda p: hook.kept_of(p) is not None)}   # a subtitle remux keeps the original and writes the row


def reuse(monkeypatch, path, **changed):
    """Make os.stat of path give a new file that took the inode number and the size of the file there now. Its mtime
    and its ctime are 1 ms later, and changed sets other fields."""
    real, st = os.stat, os.stat(path)
    new = types.SimpleNamespace(**dict({f: getattr(st, f) for f in dir(st) if f.startswith("st_")}, st_mtime_ns=st.st_mtime_ns + 10**6,
                                       st_ctime_ns=st.st_ctime_ns + 10**6, **changed))
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: new if p == path and not a and not k else real(p, *a, **k))


def tick():
    """Wait past one tick of the clock that stamps file times, 1 to 10 ms on Linux. A change in the same tick as the
    last one leaves the ctime as it was."""
    time.sleep(0.05)


def imported(path):
    """An app import over path: a copy one byte longer under a new inode."""
    shutil.copy(path, path + ".new")
    with open(path + ".new", "ab") as f:
        f.write(b"i")
    os.replace(path + ".new", path)


@pytest.mark.parametrize("mark", sorted(MARKS))
def test_a_new_file_with_the_inode_and_size_of_a_marked_file_has_no_mark(sync_mkv, tmp_path, monkeypatch, settings, mark):
    """The new file has the inode number and the size of the marked one. Its mtime differs, so it has no mark. A damage
    mark and a burn-in mark of the old file go."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    put, held = MARKS[mark]
    put(path)
    assert held(path)
    reuse(monkeypatch, path)
    assert not held(path)
    assert mark == "wrote" or hook.store.get({"damaged": hook.DAMAGED, "muted": hook.MUTED}[mark], path) is None


@pytest.mark.parametrize("mark", sorted(MARKS))
def test_an_edit_and_a_remux_of_amg_keep_each_mark(sync_mkv, tmp_path, monkeypatch, settings, mark):
    """A flag edit with mkvpropedit writes in place, and a subtitle remux makes a new file, whose swap gives it the
    owner and mode of the old one with chown and chmod. Each mark follows the file, see runner.carry_marks()."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    put, held = MARKS[mark]
    put(path)
    assert flag_edit(path, audio_off=True)["outcome"] == "edited" and held(path)
    before, owned = os.stat(path), []
    real = hook.own_like
    monkeypatch.setattr(hook, "own_like", lambda p, st: owned.append(os.stat(p).st_ctime_ns) or real(p, st))
    code, _, info = retime(path)
    hook.swap_carry(path, before, info)   # as process.write() does after its remux
    now = os.stat(path)
    assert code == "subtitles_remuxed" and now.st_ino != before.st_ino and owned and now.st_ctime_ns != owned[0], info
    assert held(path) and info["file_key"] == list(hook.file_key(now))


def test_an_import_after_the_swap_gets_no_mark_no_keep_row_and_no_words(sync_mkv, tmp_path, monkeypatch, settings):
    """The app imports a new file in the moment between a swap's rename and the carry. The import is no file of AMG's
    own, so the carry and the keep row of the write skip it, and the words heard stay with the stat they were heard at."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    hook.damage_mark(path, DAMAGE, hook.file_key(os.stat(path)))
    hook.mute_mark(path, hook.file_key(os.stat(path)))
    keys, before = heard(path), os.stat(path)
    real = hook.kept_write

    def late(p, keep, proven):   # the import lands right after the rename
        imported(p)
        real(p, keep, proven)
    monkeypatch.setattr(hook, "kept_write", late)
    code, _, info = retime(path)
    hook.swap_carry(path, before, info)
    now = os.stat(path)
    assert code == "subtitles_remuxed" and info.get("kept") and list(hook.file_key(now)) != info["file_key"], info
    assert not hook.damaged(path) and not hook.muted(path) and hook.kept_of(path) is None and hook.store.get(hook.WROTE, path) is None
    assert keys() == [(before.st_size, before.st_mtime_ns)]


def test_an_import_during_a_repair_inside_the_flag_edit_gets_no_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """The repair inside process.edit() swaps the file, and the app imports another file before the carry."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=True)
    hook.mute_mark(path, hook.file_key(os.stat(path)))

    def repack(p, j, st, apply, hp=None, edits=()):   # the swap proves its file, then the import replaces it
        shutil.copy(p, p + ".new")
        os.replace(p + ".new", p)
        info = {"file_key": list(hook.file_key(os.stat(p)))}
        imported(p)
        return "header_repaired", "header repaired", info
    monkeypatch.setattr(hook, "header_of", lambda p, j: {"edit_tail": 10, "issue": ["10 bytes past the Segment end"], "repairable": True})
    monkeypatch.setattr(hook, "repack", repack)
    rec = flag_edit(path, audio_off=True)
    assert rec["header_repair"]["code"] == "header_repaired" and not hook.muted(path), rec


def test_a_swap_that_fails_after_its_keep_leaves_the_marks(sync_mkv, tmp_path, monkeypatch, settings):
    """The swap links the original into the keep, and the rename fails. The link and its removal set a new ctime on the
    original and keep its mtime, so its marks stay with no carry."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    hook.damage_mark(path, DAMAGE, hook.file_key(os.stat(path)))
    hook.mute_mark(path, hook.file_key(os.stat(path)))
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b, **k: (_ for _ in ()).throw(OSError("refused")) if b == path else real(a, b, **k))
    before = os.stat(path)
    code, result, info = retime(path)
    assert code == "subtitle_remux_failed" and "kept" not in info and os.stat(path).st_ino == before.st_ino, result
    assert os.stat(path).st_ctime_ns != before.st_ctime_ns and hook.damaged(path) == DAMAGE and hook.muted(path)


def test_the_link_of_a_grab_leaves_the_marks(env, monkeypatch, tmp_path):
    """A Grab event hard-links the file the import may replace. The link sets a new ctime and keeps the mtime, so the
    file keeps its marks until the import replaces it."""
    keep_on(monkeypatch, tmp_path)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    hook.mute_mark(env["path"], hook.file_key(os.stat(env["path"])))
    before = os.stat(env["path"])
    tick()
    hook.keep_grab("radarr", 7, "d1")
    assert os.stat(env["path"]).st_ctime_ns != before.st_ctime_ns and hook.damaged(env["path"]) == DAMAGE and hook.muted(env["path"])


def test_an_old_mark_reads_as_no_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """A damage mark or a burn-in mark that 2.23.0 or older wrote holds the inode and the size alone. The keep row of
    2.22.0 holds the three values of file_key() in "file". None of them matches, so the file is checked again and its
    next write keeps a new original, see docs/design.md, "File marks"."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    assert retime(path)[0] == "subtitles_remuxed" and hook.kept_of(path)
    row, st = hook.store.get(hook.WROTE, path), os.stat(path)
    hook.store.put(hook.WROTE, path, {"keep": row["keep"], "file": list(hook.file_key(st))})
    hook.store.put(hook.DAMAGED, path, {"ino": st.st_ino, "size": st.st_size, "damage": DAMAGE})
    hook.store.put(hook.MUTED, path, {"ino": st.st_ino, "size": st.st_size})
    assert hook.damaged(path) is None and not hook.muted(path) and hook.kept_of(path) is None


def test_a_mark_of_another_file_stays_out_of_the_carry(sync_mkv, tmp_path, monkeypatch, settings):
    """A mark that did not match the file before AMG's change is of another file. The carry leaves it, so the file
    AMG left has no mark."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    hook.store.put(hook.MUTED, path, {"key": [1, 2, 3]})
    hook.store.put(hook.DAMAGED, path, {"key": [1, 2, 3], "damage": DAMAGE})
    before = os.stat(path)
    code, _, info = retime(path)
    hook.swap_carry(path, before, info)
    assert code == "subtitles_remuxed" and not hook.muted(path) and hook.damaged(path) is None


def test_the_words_move_to_the_stat_the_carry_proved(sync_mkv, tmp_path, monkeypatch, settings):
    """The app imports a new file right after runner.carry_marks() proved AMG's own. The words heard move to the stat
    that the carry proved, so the import has none."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    keys, before = heard(path), os.stat(path)
    code, _, info = retime(path)
    ours, real = os.stat(path), hook.carry_marks
    monkeypatch.setattr(hook, "carry_marks", lambda *a: [real(*a), imported(path)][0])
    hook.swap_carry(path, before, info)
    assert code == "subtitles_remuxed" and keys() == [(ours.st_size, ours.st_mtime_ns)] != [(os.stat(path).st_size, os.stat(path).st_mtime_ns)]



def marked(sync_mkv, tmp_path, monkeypatch, settings):
    """A file with all three marks and a second hard link, as a download client or a Grab event holds one. A subtitle
    remux of AMG's own writes the keep row, then come a damage mark and a burn-in mark. Returns the path, its stat and
    the keep of the original."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    code, _, info = retime(path)
    os.link(path, tmp_path / "seeding.mkv")
    hook.damage_mark(path, DAMAGE, hook.file_key(os.stat(path)))
    hook.mute_mark(path, hook.file_key(os.stat(path)))
    assert code == "subtitles_remuxed" and all(held(path) for _, held in MARKS.values()), info
    return path, os.stat(path), info["kept"]


@pytest.mark.parametrize("change", ["unlink", "chmod", "chown"])
def test_a_link_removal_a_chmod_and_a_chown_keep_each_mark(sync_mkv, tmp_path, monkeypatch, settings, change):
    """A download client removes its hard link when seeding ends, the app or a permissions job sets the mode, and a
    permissions job sets the owner. Each sets a new ctime and keeps the mtime, so every mark stays."""
    path, before, _ = marked(sync_mkv, tmp_path, monkeypatch, settings)
    tick()
    if change == "unlink":
        os.remove(tmp_path / "seeding.mkv")
    elif change == "chmod":
        os.chmod(path, 0o664)
    else:
        os.chown(path, os.getuid(), os.getgid())
    now = os.stat(path)
    assert now.st_ctime_ns != before.st_ctime_ns and now.st_mtime_ns == before.st_mtime_ns
    assert all(held(path) for _, held in MARKS.values())


@pytest.mark.parametrize("how", ["move", "copy"])
def test_a_restore_of_the_kept_original_drops_each_mark(sync_mkv, tmp_path, monkeypatch, settings, how):
    """A person undoes AMG's write with the kept original, whose bytes differ from the marked file. A move gives the
    path the inode of the original. A copy that keeps the times writes into the marked file's inode, and gives it the
    original's size and mtime. AMG's write kept the second of that mtime, so the two differ in the sub-second part
    alone. Every mark drops either way."""
    path, before, kept = marked(sync_mkv, tmp_path, monkeypatch, settings)
    if how == "move":   # docs/design.md, "Kept originals": move the kept file back
        os.replace(kept, path)
    else:
        shutil.copyfile(kept, path)
        shutil.copystat(kept, path)
    now = os.stat(path)
    assert (now.st_ino == before.st_ino) == (how == "copy") and now.st_mtime_ns != before.st_mtime_ns
    assert not any(held(path) for _, held in MARKS.values())


def test_an_outside_write_that_fails_the_swap_moves_no_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """A tool writes the original in place during the subtitle remux. The swap sees the change and stops, see
    remux.Swap.check(). The file keeps its inode, but its bytes are new, so its marks drop."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    hook.damage_mark(path, DAMAGE, hook.file_key(os.stat(path)))
    hook.mute_mark(path, hook.file_key(os.stat(path)))
    real = hook.own_like

    def own_like(p, st):   # runs after the remux's checks and before the swap's check
        with open(path, "ab") as f:
            f.write(b"w")
        return real(p, st)
    monkeypatch.setattr(hook, "own_like", own_like)
    before = os.stat(path)
    code, result, _ = retime(path)
    assert code == "subtitle_remux_failed" and "the original changed" in result and os.stat(path).st_ino == before.st_ino, result
    assert hook.damaged(path) is None and not hook.muted(path)


def new_on_the_inode(monkeypatch, path, data):
    """The app deletes the file at path and writes data there as a new file. os.stat gives the new file the inode
    number of the old one, as ext4 and XFS reuse it, and an mtime 1 ms after the old one."""
    real, old = os.stat, os.stat(path)
    os.remove(path)
    with open(path, "wb") as f:
        f.write(data)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 10**6))
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: types.SimpleNamespace(**dict(
        {f: getattr(real(p), f) for f in dir(real(p)) if f.startswith("st_")}, st_ino=old.st_ino)) if str(p) == path else real(p, *a, **k))


def whole_job(path):
    """The whole-file check job of path, as an import queues it."""
    return {"app": "radarr", "path": path, "grab": {}, "key": list(hook.file_key(os.stat(path)))}


def land_in_lock_wait(monkeypatch, path, data):
    """hook.locked() with the import of data at path while the job waits for the lock, see new_on_the_inode()."""
    real = hook.locked

    def locked(*a, **k):
        new_on_the_inode(monkeypatch, path, data)
        monkeypatch.setattr(hook, "locked", real)
        return real(*a, **k)
    monkeypatch.setattr(hook, "locked", locked)


def test_a_new_file_on_the_reused_inode_gets_no_damage_mark_of_the_old_one(clips, tmp_path, settings, monkeypatch):
    """Reviewer 2026-11-25: the whole-file job read a damaged file. During its wait for the lock, the app replaced it with
    a clean file that took the old inode number. The job compares the inode alone, as every release did, so it goes on.
    2.23.0 marked the clean file damaged. The mark now holds the key of the file the reads judged, whose mtime differs,
    so the clean file has no mark."""
    settings(state_dir=str(tmp_path))
    path = str(tmp_path / "film.mkv")
    shutil.copy(clips / "nal0.mkv", path)
    job = whole_job(path)
    land_in_lock_wait(monkeypatch, path, (clips / "clean.mkv").read_bytes())
    rec = hook.whole_act(None, job, {"id": "x", "apply": True}, [], post=False)
    assert os.stat(path).st_ino == job["key"][0] and rec["outcome"] == "damaged_source", rec
    assert hook.store.get(hook.DAMAGED, path)["key"] == job["key"] and hook.damaged(path) is None


def test_an_identical_broken_repost_on_the_reused_inode_gets_its_own_alert(clips, tmp_path, settings, monkeypatch):
    """As above, with the same broken bytes reposted. In 2.23.0 the old job marked the repost, so the repost's own job
    posted no Damaged file alert and asked for no re-grab. The old job's mark now misses the repost, so the repost's own
    job finds the damage."""
    settings(state_dir=str(tmp_path))
    path = str(tmp_path / "film.mkv")
    shutil.copy(clips / "nal0.mkv", path)
    land_in_lock_wait(monkeypatch, path, (clips / "nal0.mkv").read_bytes())
    first = hook.whole_act(None, whole_job(path), {"id": "x", "apply": True}, [], post=False)
    second = hook.whole_act(None, whole_job(path), {"id": "y", "apply": True}, [], post=False)   # the job the repost's import queued
    assert first["outcome"] == second["outcome"] == "damaged_source", (first, second)
    assert [f["kind"] for f in second["findings"]] == ["damage"] and hook.damaged(path) == second["whole"]["reread"]["damage"], second


def test_a_burn_in_job_mutes_no_new_file_on_the_reused_inode(env, burn, monkeypatch, settings):
    """The burn-in job read the passes of one file. During its wait for the lock, the app replaced it with a new file
    that took the old inode number and its size. The check under the lock compares the inode alone and passes. The
    check before the mute compares the key of the file the passes read, so the job drops, and the new file keeps its
    English subtitles on and gets no mark."""
    name = japanese_burn(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng")]
    land_in_lock_wait(monkeypatch, env["path"], open(env["path"], "rb").read())
    hook.background(name, [])
    (rec,) = burn_lines(env)
    assert rec["outcome"] == "file_replaced" and env["mkvpropedit"] == [] and not hook.muted(env["path"]), rec


def test_a_burn_in_job_with_no_key_acts_only_on_the_file_its_passes_read(env, burn, monkeypatch, settings):
    """A burn-in job of --check holds no key, so it takes the stat its passes start from. A new file on the reused inode
    during the passes drops the job, and the new file gets no edit and no mark."""
    name = japanese_burn(env, ("eng", True, {}))
    job = {k: v for k, v in hook.job_of(name).items() if k != "key"}
    burn["full"] = [lambda: new_on_the_inode(monkeypatch, env["path"], open(env["path"], "rb").read()) or verdict("full", True, "eng")]
    rec = hook.burn_act(None, job, {"id": "x", "app": "radarr", "source": "check", "path": env["path"], "apply": True, "label": "Film"}, [], post=False)
    assert rec["outcome"] == "file_replaced" and env["mkvpropedit"] == [] and not hook.muted(env["path"]), rec


def test_a_new_file_during_the_probe_under_the_lock_gets_no_mute_mark(env, burn, monkeypatch, settings):
    """The app holds no lock, so a new file can land after the job's check under the lock, during its probe of the
    tracks. The check before the mute compares the key of the file the passes read, so the new file gets no edit and
    no mark. The safety self-checks are off here, as in a live run."""
    monkeypatch.setattr(hook.arr_subsync, "INVARIANTS", False)
    name = japanese_burn(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng")]
    real, calls = hook.mkvmerge, []

    def mkvmerge(path, *a, **k):
        calls.append(path)
        if len(calls) == 2:   # the probe under the lock
            new_on_the_inode(monkeypatch, env["path"], open(env["path"], "rb").read())
        return real(path, *a, **k)
    monkeypatch.setattr(hook, "mkvmerge", mkvmerge)
    hook.background(name, [])
    (rec,) = burn_lines(env)
    assert len(calls) >= 2 and not hook.muted(env["path"]), calls
    assert rec["outcome"] == "file_replaced" and env["mkvpropedit"] == [], rec


def test_a_new_file_during_the_mute_mark_write_gets_no_edit(env, burn, monkeypatch, settings):
    """Reviewer 2026-11-25: a new file lands during mute_mark(), a store write after the check before the mute. The
    check runs again after the write, so the new file gets no edit and the job drops."""
    monkeypatch.setattr(hook.arr_subsync, "INVARIANTS", False)
    name = japanese_burn(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng")]
    real = hook.mute_mark

    def mute_mark(path, key):   # the app writes a new file and renames it over the old one
        real(path, key)
        shutil.copy(path, path + ".new")
        os.replace(path + ".new", path)
    monkeypatch.setattr(hook, "mute_mark", mute_mark)
    hook.background(name, [])
    (rec,) = burn_lines(env)
    assert rec["outcome"] == "file_replaced" and env["mkvpropedit"] == [] and not hook.muted(env["path"]), rec


def test_an_error_after_the_burn_in_mute_stays_an_error(env, burn, monkeypatch, settings):
    """mkvpropedit gives the file a new mtime and keeps its inode, so an error after the edit logs as an error, never as
    a file the app replaced."""
    name = japanese_burn(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng")]
    real = hook.subprocess.run

    def run(argv, **kw):   # mkvpropedit writes the file in place
        out = real(argv, **kw)
        if argv[0] == "mkvpropedit":
            os.utime(argv[1], ns=(time.time_ns(), os.stat(argv[1]).st_mtime_ns + 10**6))
        return out
    monkeypatch.setattr(hook.subprocess, "run", run)
    monkeypatch.setattr(hook, "alert_findings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the post failed")))
    hook.background(name, [])
    (rec,) = burn_lines(env)
    assert len(env["mkvpropedit"]) == 1 and hook.muted(env["path"]), rec
    assert rec["outcome"] == "error" and "the post failed" in rec["result"], rec


def test_a_job_goes_on_after_another_process_of_amg_edits_its_file_in_place(clips, tmp_path, settings, monkeypatch):
    """The worker runs a claimed whole-file job. During its wait for the lock, another process of AMG, as a person's
    --sub-check --apply, edits the file in place as process.edit() does: mkvpropedit, keep_second() and carry_marks().
    The edit keeps the inode, so the running job goes on, as in 2.22.0, and its stored row takes the new key."""
    settings(state_dir=str(tmp_path))
    path = str(tmp_path / "film.mkv")
    shutil.copy(clips / "nal0.mkv", path)
    name = hook.whole_name(path)
    hook.store.write("INSERT INTO jobs (name, claimed, at, job) VALUES (?, 1, ?, ?)", name, time.time(), json.dumps(whole_job(path)))
    job, real = hook.job_of(name, True), hook.locked

    def locked(*a, **k):   # the other process
        monkeypatch.setattr(hook, "locked", real)
        pid = hook.store.fork()   # the test process holds a connection to the store, see store.fork()
        if pid == 0:
            code = 1
            try:
                before = os.stat(path)
                subprocess.run(["mkvpropedit", path, "--edit", "info", "--set", "title=Film"], check=True, capture_output=True)
                hook.keep_second(path, before)
                code = 0 if hook.carry_marks(path, before) else 2
            finally:
                os._exit(code)
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
        return real(*a, **k)
    monkeypatch.setattr(hook, "locked", locked)
    rec = hook.whole_act(name, job, {"id": "x", "apply": True}, [], post=False)
    now = list(hook.file_key(os.stat(path)))
    assert now != job["key"] and hook.job_of(name, True)["key"] == now and rec["outcome"] == "damaged_source", rec


NS, DAY = 10**9, 86400 * 10**9
AIR = 1_550_000_000 * NS + 250_000_000   # an air date, with the sub-second part of the import that Sonarr keeps


def sonarr_date(path, air=AIR):
    """Sonarr's Change File Date on a rescan, UpdateEpisodeFileService: a file whose whole second differs from the air
    date takes the second of the air date and keeps its own sub-second part. Returns whether it changed the file."""
    m = os.stat(path).st_mtime_ns
    if m // NS == air // NS:
        return False
    os.utime(path, ns=(time.time_ns(), air // NS * NS + m % NS))
    return True


def radarr_date(path, air=AIR):
    """Radarr's Change File Date on a rescan, UpdateMovieFileService: a file whose day differs from the release day takes
    that day at midnight. Returns whether it changed the file."""
    if os.stat(path).st_mtime_ns // DAY == air // DAY:
        return False
    os.utime(path, ns=(time.time_ns(), air // DAY * DAY))
    return True


@pytest.mark.parametrize("app", [sonarr_date, radarr_date])
@pytest.mark.parametrize("write", ["edit", "remux"])
def test_the_app_s_file_date_after_a_write_of_amg_keeps_every_mark_and_job(sync_mkv, tmp_path, monkeypatch, settings, app, write):
    """The app set the file date at the import, and a write of AMG's own keeps the keep row. The file has a damage mark,
    a burn-in mark, and a queued deep analysis, whole-file check and burn-in job. A flag edit or a remux keeps the
    second of the original, so the app's rescan after it changes nothing, and each mark and job stays with the file."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    assert app(path) and retime(path)[0] == "subtitles_remuxed" and not app(path)
    key = list(hook.file_key(os.stat(path)))
    hook.damage_mark(path, DAMAGE, key)
    hook.mute_mark(path, key)
    jobs = (hook.deep_name(path), hook.whole_name(path), hook.burn_name(path))
    for name in jobs:
        hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", name, 0.0, json.dumps({"path": path, "key": key}))
    before = os.stat(path)
    if write == "edit":
        assert flag_edit(path, audio_off=True)["outcome"] == "edited"
    else:
        code, _, info = retime(path)
        hook.swap_carry(path, before, info)
        assert code == "subtitles_remuxed", info
    ours = os.stat(path)
    assert not app(path) and os.stat(path).st_mtime_ns == ours.st_mtime_ns != before.st_mtime_ns
    assert all(held(path) for _, held in MARKS.values())
    assert all(hook.job_of(n)["key"] == list(hook.file_key(ours)) and hook.deep_replaced(hook.job_of(n)) is None for n in jobs)


def test_amg_s_own_checks_still_see_its_flag_edit(sync_mkv, tmp_path, monkeypatch, settings):
    """The flag edit keeps the inode, the size and the second of the mtime, and gives the mtime a new sub-second part.
    A job whose checks read the file before the edit replans, a dry run's plan refuses, and the saved subtitle result
    of the file before the edit no longer counts."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    sonarr_date(path)
    lid, cache, st = importlib.import_module(hook._name + ".lid"), os.path.join(hook.CFG.state_dir, "lid.sqlite"), os.stat(path)
    lid.verdict_put(cache, path, {"mode": "sub_check"}, False)
    assert lid.verdict_get(cache, path) and flag_edit(path)["outcome"] == "edited"
    now = os.stat(path)
    assert (now.st_ino, now.st_size, now.st_mtime_ns // NS) == (st.st_ino, st.st_size, AIR // NS) and now.st_mtime_ns != st.st_mtime_ns
    with pytest.raises(hook.Replan, match="the file changed since the checks"):
        hook.same_file(path, st)
    assert hook.changed_since({"path": path, "key": list(hook.file_key(st))}) == "the file changed since the dry run"
    assert lid.verdict_get(cache, path) is None


@pytest.mark.parametrize("write", ["repair", "conversion"])
def test_a_header_repair_and_a_conversion_keep_the_second_of_the_original(sync_mkv, media, tmp_path, monkeypatch, settings, write):
    """A header repair swaps a remux of the file in, and a conversion swaps an MKV in for an MP4. The new file has the
    second of the original's mtime and a new sub-second part."""
    if write == "repair":
        path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    else:
        path = str(tmp_path / "Film D (1982).mkv")   # MP4 under a .mkv name, so it converts in place
        shutil.copy(media / "good.mp4", path)
    os.utime(path, ns=(AIR, AIR))
    st, j = os.stat(path), REAL_MKVMERGE(path)
    got = hook.repack(path, j, st, True, header(path, j))[1] if write == "repair" else hook.convert("radarr", path, j, st, True, {})[1]
    now = os.stat(path)
    assert got == {"repair": "header repaired", "conversion": "repacked"}[write], got
    assert now.st_ino != st.st_ino and now.st_mtime_ns // NS == AIR // NS and now.st_mtime_ns != AIR


@pytest.mark.parametrize("during", ["checks", "keep"])
def test_a_sweep_during_the_swap_leaves_its_temp_file(sync_mkv, tmp_path, monkeypatch, settings, during):
    """The original's mtime is years old. Another run sweeps the folder while the swap checks the temp file or keeps the
    original, see remux.sweep_repack_tmp(). A conversion worker proves its file without the file lock for hours. The
    temp file takes the original's second only after the keep, so the sweep leaves it, and the swap ends."""
    path, _ = film(sync_mkv, tmp_path, monkeypatch, settings)
    os.utime(path, ns=(AIR, AIR))
    swept, name = [], {"checks": "mkvmerge", "keep": "keep_original"}[during]
    real = getattr(hook, name)

    def sweep(p, *a):
        if os.path.exists(hook.repack_tmp(path)):
            swept.append(name)
            hook.sweep_repack_tmp(os.path.dirname(path), "test")
        return real(p, *a)
    monkeypatch.setattr(hook, name, sweep)
    code, _, info = retime(path)
    assert swept and code == "subtitles_remuxed" and os.stat(path).st_mtime_ns // NS == AIR // NS, info

def written(tmp_path, was, now, name="Film C (1981).mkv"):
    """A file whose original had the mtime was, in ns, and whose write left the mtime now. Returns its path and the
    stat of the original."""
    path = tmp_path / name
    path.write_bytes(b"original")
    os.utime(path, ns=(was, was))
    st = os.stat(path)
    path.write_bytes(b"the write")
    os.utime(path, ns=(now, now))
    return str(path), st


def test_a_write_takes_the_second_of_the_original_and_keeps_its_own_sub_second_part(tmp_path):
    """In place, and for a swap's file of the key it proved. Another file at the path, as an import meanwhile, and a
    swap's file of another key keep their own times."""
    late = 1_700_000_000 * NS + 123_456_789
    path, st = written(tmp_path, AIR, late)
    assert hook.keep_second(path, st) == hook.file_key(os.stat(path)) and os.stat(path).st_mtime_ns == AIR // NS * NS + 123_456_789
    tmp = tmp_path / "tmp.mkv"
    tmp.write_bytes(b"a remux")
    os.utime(tmp, ns=(late, late))
    assert hook.keep_second(str(tmp), st, (1, 2, 3)) is None and os.stat(tmp).st_mtime_ns == late
    assert hook.keep_second(str(tmp), st, hook.file_key(os.stat(tmp))) and os.stat(tmp).st_mtime_ns == AIR // NS * NS + 123_456_789
    path, st = written(tmp_path, AIR, late, "Film E (1983).mkv")
    os.replace(tmp, path)   # an import over the edited file
    assert hook.keep_second(path, st) is None and os.stat(path).st_mtime_ns == AIR // NS * NS + 123_456_789


@pytest.mark.parametrize("fs", ["whole", "cut", "refused"])
def test_a_file_system_of_whole_seconds_keeps_the_write_s_own_mtime(tmp_path, monkeypatch, fs):
    """whole: the file system keeps whole seconds, so the write's mtime has no sub-second part either. cut: the file
    system drops the sub-second part that utime sets. The mtime would then be the original's, and AMG would not see its
    write. refused: a share refuses the times of a file of another owner. The write's own mtime stays in each case."""
    was, late = AIR // DAY * DAY, 1_700_000_000 * NS + (0 if fs == "whole" else 500_000_000)   # Radarr's midnight
    path, st = written(tmp_path, was, late)
    real = os.utime
    if fs == "cut":
        monkeypatch.setattr(os, "utime", lambda p, ns, **k: real(p, ns=(ns[0], ns[1] // NS * NS), **k))
    elif fs == "refused":
        monkeypatch.setattr(os, "utime", lambda *a, **k: (_ for _ in ()).throw(PermissionError("not the owner")))
    assert hook.keep_second(path, st) == hook.file_key(os.stat(path))
    assert os.stat(path).st_mtime_ns == (late // NS * NS if fs == "cut" else late) != was
