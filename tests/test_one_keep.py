# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""One kept original per library file, see vault.keep_original() and docs/design.md, "Kept originals". A write of a
file that holds the hook's own output, with its original still kept, keeps nothing new. It names that keep, and the
keep's days start again. A new grab, a hand edit or an expired keep gives a new keep. The flag edit of process.edit()
carries the keep, the cached words, the walk and the mark of a burn-in job only to the file it edited. The files are
made up."""
import contextlib
import importlib
import json
import os
import shutil
import sqlite3
import time
import types

import amg
from test_arr_media_guard import (hook, REAL_MKVMERGE, REAL_RUN, convertible, kept_files, real_convert, refused_link, stamp,  # noqa: F401
                                  state_dir_in_tmp, sync_mkv)

VAULT = next(m for m in hook._mods if m.__name__.endswith(".vault"))
MATROSKA = hook.matroska
FIX = {2: {"rate": "1/1", "offset": 1.0}}   # the SubRip track of sync_mkv, 1 s later


def clock(monkeypatch, days, at=None):
    """The clock of vault.py, days ahead: the stamp of a new keep, the renewal, and the prune. With at, the clock stands
    still at at plus days."""
    now = time.time if at is None else lambda: at
    monkeypatch.setattr(VAULT, "time", types.SimpleNamespace(gmtime=time.gmtime, strptime=time.strptime, strftime=time.strftime,
                                                             time=lambda: now() + days * 86400))


def film(sync_mkv, tmp_path, monkeypatch, settings):
    """A copy of sync_mkv in a library folder, with the keep folder at the top of the test's mount. Returns its path
    and its stat."""
    path = tmp_path / "lib" / "Film B (1980)" / "Film B (1980).mkv"
    path.parent.mkdir(parents=True)
    shutil.copy(sync_mkv / "f.mkv", path)
    settings(keep_days=7, log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "lib"))
    return str(path), os.stat(path)


def retime(path):
    """One subtitle remux of path, a write of the hook."""
    return hook.resub(path, REAL_MKVMERGE(path), os.stat(path), True, FIX)


def keeps(path):
    """The kept media files of path's keep folder."""
    root = hook.originals_root(path)
    return sorted(os.path.join(d, n) for d, _, ns in os.walk(root) for n in ns if n.endswith(".mkv"))


def test_two_writes_of_one_run_keep_one_original_and_both_posts_name_it(tmp_path, settings, monkeypatch):
    """An import whose header cut runs before the checks, and whose subtitle retime runs at the write stage. The run
    keeps the file as imported once. Both posts name that keep."""
    dur = 360   # over SUB_MIN_SECONDS, so the subtitle checks run
    lib = tmp_path / "lib" / "Film A (1979)"
    lib.mkdir(parents=True)
    d = tmp_path / "src"
    d.mkdir()
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=24:duration={dur}", "-f", "lavfi", "-i",
                f"sine=frequency=440:duration={dur}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", str(d / "av.mkv")], check=True)

    def srt(name, events):
        (d / name).write_text("\n\n".join(f"{k + 1}\n{stamp(a)} --> {stamp(b)}\n{t}" for k, (a, b, t) in enumerate(events)) + "\n")
    srt("eng.srt", [(10 + 10 * k, 12 + 10 * k, f"English line {k}") for k in range(30)])
    srt("spa.srt", [(10 + 10 * k, 12 + 10 * k, f"Linea {k}") for k in range(30)] + [(340, 3540, "Una linea que no acaba")])   # past the end
    srt("late.srt", [(12 + 10 * k, 14 + 10 * k, f"Late line {k}") for k in range(30)])   # 2 s late
    path = lib / "Film A (1979).mkv"
    REAL_RUN(["mkvmerge", "-q", "-o", str(path), "--language", "1:eng", str(d / "av.mkv"), "--language", "0:eng", "--default-track-flag", "0:0",
              str(d / "eng.srt"), "--language", "0:spa", "--default-track-flag", "0:0", str(d / "spa.srt"), "--language", "0:eng", "--track-name",
              "0:SDH", "--default-track-flag", "0:0", str(d / "late.srt")], check=True)
    settings(log=str(tmp_path / "log.jsonl"), keep_days=7, subtitles="fix", header_repair=True, discord_posts="all")
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "lib"))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "hear", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(hook, "subtitle_read", lambda *a, **k: {})
    monkeypatch.setattr(hook, "sub_reference", lambda *a, **k: ({}, {}))
    in_time = {"verdict": "match", "why": "faked", "windows": [], "timing": {"fix": None, "why": "in time"}, "audio": 0, "starts": []}
    late = dict(in_time, timing={"fix": {"offset": 2.0, "rate": "1/1"}, "why": "faked 2 s late"})
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: {p: (late if p == "s3" else in_time) for p in items})
    imported = os.stat(path)
    hook.DEADLINE.start(hook.BUDGET)
    try:
        rec = hook.process(hook.Ctx(None, str(path), "Film A (1979)", "English", dur // 60, mode="import", apply=True, post=False,
                                    job={"check": "outside"}, lock=open(tmp_path / "lock", "w"), shared=False))
    finally:
        hook.DEADLINE.stop()
    (kept,) = keeps(str(path))
    assert rec["header_repair"]["code"] == "subtitle_trimmed" and rec["subremux"]["done"], json.dumps(rec, default=str)[:2000]
    assert os.stat(kept).st_ino == imported.st_ino and rec["header_repair"]["kept"] == rec["subremux"]["kept"] == kept
    assert rec["subremux"]["kept_how"] == hook.KEPT_BEFORE and "kept_how" not in rec["header_repair"]
    posts = dict(hook.changes(rec))
    assert sorted(posts) == ["File repaired", "Subtitles retimed"] and all(f"The original file is kept at {kept}." in t for t in posts.values()), posts


def test_a_later_write_within_keep_days_reuses_the_keep_and_its_days_start_again(sync_mkv, tmp_path, monkeypatch, settings):
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    _, result, info = retime(path)
    kept, root = info["kept"], hook.originals_root(path)
    folder = os.path.relpath(kept, root).split(os.sep)[0]
    assert result == "subtitles remuxed" and os.stat(kept).st_ino == imported.st_ino and "kept_how" not in info, result
    clock(monkeypatch, 6)   # a later run, 6 days after the first write
    hook.store.put("wrote", "/gone.mkv", {"keep": "/gone.mkv", "file": [1, 2, 3]}, at=time.time() - 8 * 86400)
    _, result, info = retime(path)
    assert hook.store.get("wrote", "/gone.mkv") is None and hook.store.get("wrote", path)["keep"] == kept   # a write drops the old rows
    assert result == "subtitles remuxed" and info["kept"] == kept and info["kept_how"] == hook.KEPT_BEFORE and keeps(path) == [kept], info
    assert os.stat(kept).st_ino == imported.st_ino and os.stat(os.path.join(root, folder, hook.RENEWED)).st_mtime > time.time() + 5 * 86400
    clock(monkeypatch, 8)   # past keep_days from the first write, 2 days after the second
    assert hook.prune_originals(root) == [] and os.path.exists(kept)
    clock(monkeypatch, 6 + 7 + 0.1)
    assert hook.prune_originals(root) == [folder] and not os.path.exists(kept)


def test_a_write_after_the_keep_expired_keeps_a_new_original(sync_mkv, tmp_path, monkeypatch, settings):
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    first = retime(path)[2]["kept"]
    written = os.stat(path)
    clock(monkeypatch, 7.1)
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] != first and "kept_how" not in info, info
    assert os.stat(info["kept"]).st_ino == written.st_ino and keeps(path) == [info["kept"]]   # the prune after the write removed the first


def test_a_new_grab_or_a_hand_edit_between_runs_is_a_new_original(sync_mkv, tmp_path, monkeypatch, settings):
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    first = retime(path)[2]["kept"]
    REAL_RUN(["mkvpropedit", path, "--edit", "track:3", "--set", "name=Edited by hand"], check=True, capture_output=True)   # the same inode, a new mtime
    edited = os.stat(path)
    clock(monkeypatch, 1 / 86400)   # 1 s later, so the new keep gets its own time folder
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] != first and os.stat(info["kept"]).st_ino == edited.st_ino and "kept_how" not in info, info
    shutil.copy(sync_mkv / "f.mkv", path + ".part")
    os.replace(path + ".part", path)   # the app imports a new grab
    grab = os.stat(path)
    clock(monkeypatch, 2 / 86400)
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and os.stat(info["kept"]).st_ino == grab.st_ino and "kept_how" not in info, info
    assert len(keeps(path)) == 3


def test_the_hook_s_own_flag_edit_keeps_the_file_its_own_output(sync_mkv, tmp_path, monkeypatch, settings):
    """A remux, then a flag edit of the same run, then a write of a later run: one keep."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    kept = retime(path)[2]["kept"]
    j = REAL_MKVMERGE(path)
    before = os.stat(path)
    rec = hook.edit({"path": path}, j, [[f"track:={j['tracks'][2]['properties']['uid']}", 1, 0, hook.arr_decide.FORCED_FLAG]], True)
    assert rec["outcome"] == "edited" and os.stat(path).st_mtime_ns != before.st_mtime_ns, rec
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] == kept and info["kept_how"] == hook.KEPT_BEFORE and keeps(path) == [kept], info


def test_a_reused_keep_still_marks_the_grab_links_of_the_file_stale(sync_mkv, tmp_path, monkeypatch, settings):
    """KEEP_REPLACED: a grab linked the file the hook wrote. A later write replaces that file, so the link goes stale,
    as at a new keep."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(keep_replaced=True)
    kept = retime(path)[2]["kept"]
    with hook.kept_records() as recs:
        recs.append(dict(app="radarr", old=path, kept=str(tmp_path / "link.mkv"), download_id="d1", time=time.time(), ino=0))
    assert retime(path)[2]["kept"] == kept
    assert [r.get("stale") for r in hook.kept_read()] == ["arr-media-guard changed the file after the grab"]


def test_a_failed_write_never_removes_a_keep_an_earlier_write_made(sync_mkv, tmp_path, monkeypatch, settings):
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    original = open(path, "rb").read()
    kept = retime(path)[2]["kept"]
    written = open(path, "rb").read()
    monkeypatch.setattr(hook, "broken_plan", lambda *a: "the test stops the write")   # after the keep, before the rename
    _, result, info = retime(path)
    assert result == "subtitle remux failed: the test stops the write" and "kept" not in info, result
    assert open(kept, "rb").read() == original and open(path, "rb").read() == written and keeps(path) == [kept]


def test_a_kept_copy_is_reused_as_a_kept_link_is(sync_mkv, tmp_path, monkeypatch):
    """A file system that refuses a hard link keeps a copy. A second write reuses the copy, and copies nothing."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, FIX)
    kept = info["kept"]
    assert result == "subtitles remuxed" and info["kept_how"].startswith("copied"), result
    _, result, info = retime(path)
    folder = os.path.relpath(kept, tmp_path / ".kept").split(os.sep)[0]
    assert result == "subtitles remuxed" and info["kept"] == kept and info["kept_how"] == hook.KEPT_BEFORE, result
    assert kept_files(tmp_path) == sorted([os.path.relpath(kept, tmp_path / ".kept"), os.path.join(folder, hook.RENEWED)])
    assert open(kept, "rb").read() == before


def test_a_kept_sidecar_is_reused_by_a_later_retime(tmp_path, settings, monkeypatch):
    video, side = tmp_path / "Film (2001).mkv", tmp_path / "Film (2001).en.srt"
    text = "1\n00:00:10,000 --> 00:00:11,000\na\n\n2\n00:00:12,000 --> 00:00:13,000\nb\n"
    side.write_text(text)
    settings(keep_days=7, log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    fix = {"rate": "1/1", "offset": 1.0}
    sync = {side.name: {"verdict": "match", "why": "", "timing": {"fix": fix, "why": "a fix of +1.00 s"}}}
    sides = lambda: {s["name"]: s for s in hook.sidecar_subs(str(video))}
    (e,) = hook.sidecar_fix(sides(), sync, True, "radarr", "sub_time")
    assert e["result"] == "retimed" and "kept_how" not in e, e
    (again,) = hook.sidecar_fix(sides(), sync, True, "radarr", "sub_time")
    assert again["result"] == "retimed" and again["kept"] == e["kept"] and again["kept_how"] == hook.KEPT_BEFORE, again
    twice = "1\n00:00:08,000 --> 00:00:09,000\na\n\n2\n00:00:10,000 --> 00:00:11,000\nb\n"   # each retime moved it 1 s
    assert open(e["kept"]).read() == text and side.read_text() == twice
    assert [f for f in kept_files(tmp_path) if f.endswith(".srt")] == [os.path.relpath(e["kept"], tmp_path / ".kept")]


def test_a_write_after_a_kept_conversion_reuses_the_kept_original(convertible, tmp_path, monkeypatch, settings):
    """A forced conversion keeps the MP4. A later subtitle remux of the new MKV names the MP4 as its original."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    fault = "stream video 0 (h264) holds 1006 packets in the new file, 1000 in the original"
    real_prove = hook.prove
    monkeypatch.setattr(hook, "prove", lambda *a, **k: ((0, fault), {}))
    monkeypatch.setattr(hook, "relink", lambda app, owner, old, items, target, imports=None: (True, {"import": "completed", "file_id": 12, "listed": [target],
                                                                                                   "remonitored": []}))
    st = os.stat(mp4)
    _, result, info, now = hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), st, True, {"app_id": 7, "file_id": 11}, force=f"repack failed: {fault}")
    assert result == "repacked" and now.endswith(".mkv") and os.stat(info["kept"]).st_ino == st.st_ino, result
    monkeypatch.setattr(hook, "prove", real_prove)
    _, result, again = hook.resub(now, REAL_MKVMERGE(now), os.stat(now), True, FIX)
    assert result == "subtitles remuxed" and again["kept"] == info["kept"] and again["kept_how"] == hook.KEPT_BEFORE, result


def flag_edit(path, audio_off=False):
    """The hook's own flag edit of path: the SubRip track becomes forced. With audio_off, the audio track loses its
    default flag, an edit that a burn-in job's mark allows."""
    j = REAL_MKVMERGE(path)
    uid = lambda k: f"track:={j['tracks'][k]['properties']['uid']}"
    return hook.edit({"path": path}, j, [[uid(1), 0, 1]] if audio_off else [[uid(2), 1, 0, hook.arr_decide.FORCED_FLAG]], True)


def heard(path):
    """Put a row of spans of speech for the file at path in lid.sqlite, as a hearing does. Returns a function that
    gives the (size, mtime) keys of path's rows there."""
    lid, cache, st = importlib.import_module(hook._name + ".lid"), os.path.join(hook.CFG.state_dir, "lid.sqlite"), os.stat(path)
    with contextlib.closing(lid._db(cache)) as db, db:
        db.execute("INSERT INTO speech VALUES (?, ?, ?, ?, ?, ?, ?)", (path, st.st_size, st.st_mtime_ns, 0, lid.speech_how(), "[[1.0, 2.5]]", time.time()))

    def keys():
        with contextlib.closing(sqlite3.connect(cache)) as db:
            return db.execute("SELECT size, mtime_ns FROM speech WHERE path = ?", (path,)).fetchall()
    return keys


def import_meanwhile(sync_mkv):
    """A header_of() that imports a new grab at the path, as the app can do during the hook's edit."""
    def header_of(p, j):
        shutil.copy(sync_mkv / "f.mkv", p + ".part")
        os.replace(p + ".part", p)
        return {}
    return header_of


def test_an_import_during_the_hook_s_flag_edit_gets_a_new_keep(sync_mkv, tmp_path, monkeypatch, settings):
    """process.edit() stats the file, reads its header, then runs mkvpropedit. Here the app imports a new grab between,
    so mkvpropedit edits the grab. kept_carry() sees another inode and leaves the row, so the next write keeps the grab."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=True)
    first = retime(path)[2]["kept"]
    with monkeypatch.context() as m:
        m.setattr(hook, "header_of", import_meanwhile(sync_mkv))
        rec = flag_edit(path)
    grab = os.stat(path)
    clock(monkeypatch, 1 / 86400)   # 1 s later, so the new keep gets its own time folder
    _, result, info = retime(path)
    assert rec["outcome"] == "edited" and result == "subtitles remuxed", (rec, result)
    assert info["kept"] != first and "kept_how" not in info and os.stat(info["kept"]).st_ino == grab.st_ino, info


def test_a_failed_flag_edit_carries_no_row(sync_mkv, tmp_path, monkeypatch, settings):
    """mkvpropedit can exit 2 and still change the file. kept_carry() and lid_carry() run only after a successful edit,
    so the next write keeps the file as found, and the next check hears it again."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    first = retime(path)[2]["kept"]
    keys, written = heard(path), os.stat(path)
    real = hook.subprocess.run

    def exits_2(argv, **kw):
        r = real(argv, **kw)   # the real mkvpropedit sets the flag
        return types.SimpleNamespace(returncode=2, stdout="Updating the 'Tracks' element failed.", stderr="") if argv[0] == "mkvpropedit" else r
    with monkeypatch.context() as m:
        m.setattr(hook.subprocess, "run", exits_2)
        rec = flag_edit(path)
    edited = os.stat(path)
    clock(monkeypatch, 1 / 86400)
    _, result, info = retime(path)
    assert rec["outcome"] == "edit_failed" and result == "subtitles remuxed", (rec, result)
    assert info["kept"] != first and "kept_how" not in info and os.stat(info["kept"]).st_ino == edited.st_ino, info
    assert keys() == [(written.st_size, written.st_mtime_ns)] != [(edited.st_size, edited.st_mtime_ns)]


def test_an_import_during_the_hook_s_flag_edit_gets_no_words_and_no_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """The cached words of the file and the mark of a burn-in job stay with the file the hook edits. A new grab that the
    app imports during the edit gets neither, so the next check hears it, and its English subtitles follow the policy."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=True)
    keys = heard(path)
    hook.mute_mark(path)
    with monkeypatch.context() as m:
        m.setattr(hook, "header_of", import_meanwhile(sync_mkv))
        rec = flag_edit(path, audio_off=True)
    grab = os.stat(path)
    assert rec["outcome"] == "edited" and grab.st_ino != imported.st_ino, rec
    assert keys() == [(imported.st_size, imported.st_mtime_ns)] and not hook.muted(path)


def test_the_hook_s_flag_edit_carries_the_words_and_the_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """An edit of the same file moves its cached words and its mark to the new mtime and size."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    keys = heard(path)
    hook.mute_mark(path)
    rec = flag_edit(path, audio_off=True)
    edited = os.stat(path)
    assert rec["outcome"] == "edited" and edited.st_mtime_ns != imported.st_mtime_ns, rec
    assert keys() == [(edited.st_size, edited.st_mtime_ns)] and hook.muted(path)


def test_a_hand_edit_then_the_hook_s_flag_edit_is_a_new_original(sync_mkv, tmp_path, monkeypatch, settings):
    """The hand edit changes the file after the hook's write. The hook's flag edit then finds a row of another file and
    carries nothing, so the next write keeps the hand-edited file."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    first = retime(path)[2]["kept"]
    REAL_RUN(["mkvpropedit", path, "--edit", "track:3", "--set", "name=Edited by hand"], check=True, capture_output=True)
    rec = flag_edit(path)
    edited = os.stat(path)
    clock(monkeypatch, 1 / 86400)
    _, result, info = retime(path)
    assert rec["outcome"] == "edited" and result == "subtitles remuxed", (rec, result)
    assert info["kept"] != first and "kept_how" not in info and os.stat(info["kept"]).st_ino == edited.st_ino, info


def test_a_keep_moved_out_of_its_folder_gives_a_new_keep(sync_mkv, tmp_path, monkeypatch, settings):
    """docs/regrabs.md tells a user to move a kept original out of its folder to keep it longer. The next write keeps
    the file as found."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    first = retime(path)[2]["kept"]
    written = os.stat(path)
    os.replace(first, tmp_path / "saved.mkv")
    clock(monkeypatch, 1 / 86400)
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] != first and "kept_how" not in info, info
    assert os.stat(info["kept"]).st_ino == written.st_ino


def test_a_keep_in_its_last_hour_is_not_reused(sync_mkv, tmp_path, monkeypatch, settings):
    """A prune that decided before the renewal can remove a keep in its last hour during the write. So that write keeps
    a new original. The vault clock stands 1 s either side of KEEP_MARGIN, counted from the stamp, then the renewal."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    first = retime(path)[2]["kept"]
    root = hook.originals_root(path)
    folder = os.path.join(root, os.path.relpath(first, root).split(os.sep)[0])
    clock(monkeypatch, 7, at=hook.stamp_time(folder) - hook.KEEP_MARGIN - 1)
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] == first and info["kept_how"] == hook.KEPT_BEFORE, info
    written = os.stat(path)
    clock(monkeypatch, 7, at=hook.stamp_time(folder) - hook.KEEP_MARGIN + 1)
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] != first and "kept_how" not in info, info
    assert os.stat(info["kept"]).st_ino == written.st_ino and os.path.exists(first)


def test_a_write_of_another_file_leaves_the_row_of_the_first(sync_mkv, tmp_path, monkeypatch, settings):
    """kept_write() of one file drops only the rows older than keep_days. So the next write of the other file still
    reuses its keep."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    other = tmp_path / "lib" / "Film C (1981)" / "Film C (1981).mkv"
    other.parent.mkdir(parents=True)
    shutil.copy(sync_mkv / "f.mkv", other)
    first = retime(path)[2]["kept"]
    assert retime(str(other))[2]["kept"] != first
    _, result, info = retime(path)
    assert result == "subtitles remuxed" and info["kept"] == first and info["kept_how"] == hook.KEPT_BEFORE, info


def test_a_failed_write_removes_the_copy_it_kept(sync_mkv, tmp_path, monkeypatch):
    """A file system that refuses a hard link keeps a copy. A write that fails after that keep removes the copy, as it
    removes a link. Only a keep of an earlier write stays, see Swap.unkeep()."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "broken_plan", lambda *a: "the test stops the write")   # after the keep, before the rename
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, FIX)
    assert result == "subtitle remux failed: the test stops the write" and "kept" not in info, result
    assert kept_files(tmp_path) == [] and open(path, "rb").read() == before


def test_a_failed_flag_edit_that_wrote_keeps_the_burn_in_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """mkvpropedit can exit 2 after it wrote, here with a new size. The flags it set are those of the burn-in job, so
    the mark moves to the file as it is, see runner.muted()."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=False)
    hook.mute_mark(path)
    real = hook.subprocess.run

    def grows_and_exits_2(argv, **kw):
        if argv[0] != "mkvpropedit":
            return real(argv, **kw)
        real(argv + ["--edit", "info", "--set", "title=" + "A long title " * 400], **kw)   # the header outgrows its room
        return types.SimpleNamespace(returncode=2, stdout="Updating the 'Tracks' element failed. The file has been modified.", stderr="")
    with monkeypatch.context() as m:
        m.setattr(hook.subprocess, "run", grows_and_exits_2)
        rec = flag_edit(path, audio_off=True)
    assert rec["outcome"] == "edit_failed" and os.stat(path).st_size != imported.st_size and hook.muted(path), rec


def test_a_remux_inside_the_hook_s_flag_edit_carries_the_words_and_the_mark(sync_mkv, tmp_path, monkeypatch, settings):
    """A file with bytes past its Segment end takes the flag edit in one remux, see process.edit(). The remux gives the
    file a new inode, and its cached words and its mark follow it."""
    path, imported = film(sync_mkv, tmp_path, monkeypatch, settings)
    settings(header_repair=True)
    keys = heard(path)
    hook.mute_mark(path)

    def repack(p, j, st, apply, hp=None, edits=()):   # as Swap's rename leaves it: a new inode with the edits
        shutil.copy(p, p + ".new")
        REAL_RUN(["mkvpropedit", p + ".new"] + hook.propedit_args(edits, 1), check=True, capture_output=True)
        os.replace(p + ".new", p)
        return "header_repaired", "header repaired", {}
    monkeypatch.setattr(hook, "header_of", lambda p, j: {"edit_tail": 10, "issue": ["10 bytes past the Segment end"], "repairable": True})
    monkeypatch.setattr(hook, "repack", repack)
    rec = flag_edit(path, audio_off=True)
    now = os.stat(path)
    assert rec["outcome"] == "edited" and rec["header_repair"]["code"] == "header_repaired" and now.st_ino != imported.st_ino, rec
    assert keys() == [(now.st_size, now.st_mtime_ns)] and hook.muted(path)


def test_an_import_with_a_flag_edit_walks_a_stale_tag_file_once(tmp_path, settings, monkeypatch):
    """A file whose NUMBER_OF_FRAMES tags are stale takes a walk for its subtitle counts, see checks.walk_frames(). The
    flag edit changes no frame, so process.edit() carries the walk to the edited file, and the probe after the edit
    walks no second time. The carried walk is the walk of the edited file."""
    lib, d = tmp_path / "lib" / "Film A (1979)", tmp_path / "src"
    lib.mkdir(parents=True)
    d.mkdir()
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=60", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=60", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(d / "av.mkv")], check=True)
    (d / "s.srt").write_text("\n\n".join(f"{k + 1}\n{stamp(5 + 5 * k)} --> {stamp(7 + 5 * k)}\nLine {k}" for k in range(10)) + "\n")
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "mm.mkv"), "--language", "1:eng", str(d / "av.mkv"), "--language", "0:eng", "--default-track-flag",
              "0:0", str(d / "s.srt"), "--language", "0:fre", "--default-track-flag", "0:1", str(d / "s.srt")], check=True)
    path = str(lib / "Film A (1979).mkv")   # ffmpeg keeps the statistics tags of mkvmerge, so they are stale
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(d / "mm.mkv"), "-map", "0", "-c", "copy", "-default_mode", "passthrough", path], check=True)
    settings(log=str(tmp_path / "log.jsonl"), header_repair=True)
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "lib"))
    walks, real = [], MATROSKA.walk
    monkeypatch.setattr(MATROSKA, "walk", lambda p, enough=None: (enough or walks.append(os.stat(p).st_mtime_ns), real(p, enough))[1])
    hook.DEADLINE.start(hook.BUDGET)
    try:
        rec = hook.process(hook.Ctx("radarr", path, "Film A (1979)", "English", 1, mode="import", apply=True, post=False, job={"check": "outside"},
                                    lock=open(tmp_path / "lock", "w"), shared=False))
    finally:
        hook.DEADLINE.stop()
    j = hook.mkvmerge(path)
    assert rec["outcome"] == "edited" and rec["edits"] and len(walks) == 1, json.dumps(rec, default=str)[:3000]
    assert [t["properties"].get("walk_frames") for t in j["tracks"]] == [None, None, 10, 10] and MATROSKA.frames(path) == real(path), j


def test_the_walk_moves_only_from_a_whole_segment_with_no_error(sync_mkv, tmp_path):
    """matroska.carry() moves the walk of a Segment that ends at the end of the file, and whose walk has no error.
    mkvpropedit puts bytes past the Segment end inside the Segment, so a file with such bytes walks again. A walk that
    names damage walks again too, and so does a file that changed after the edit."""
    path = str(tmp_path / "f.mkv")
    for tail, error in ((bytes(4096), None), (b"", "damage"), (b"", None)):
        with open(path, "wb") as f:
            f.write((sync_mkv / "f.mkv").read_bytes() + tail)
        MATROSKA.WALKS.clear()
        got, before = MATROSKA.frames(path), os.stat(path)
        got["error"] = error
        REAL_RUN(["mkvpropedit", "-q", path, "--edit", "track:2", "--set", "flag-forced=1"], check=True)
        MATROSKA.carry(path, before)
        st = os.stat(path)
        assert list(MATROSKA.WALKS) == ([(path, st.st_size, st.st_mtime_ns)] if not tail and not error else []), (len(tail), error)
    carried = MATROSKA.WALKS[(path, st.st_size, st.st_mtime_ns)]
    os.utime(path, ns=(1, 1))
    assert MATROSKA.frames(path) is not carried and len(MATROSKA.WALKS) == 2
