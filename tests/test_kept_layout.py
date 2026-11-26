# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The layout of the kept folders, see vault.kept_path() and docs/design.md, "Kept originals". A keep sits at
<kind folder>/<folders of the path>/<UTC time>/<file name>. The prune finds the time folders at any depth, removes the
folders it leaves empty up to the kind folder, and clears the old layout's top-level time folders. The files are made
up."""
import os
import time

from test_arr_media_guard import hook, decided, env, keep_on, kept_upgrade, refused_link, silent, sync_mkv, state_dir_in_tmp  # noqa: F401
from test_one_keep import clock

OLD = "20000101T000000Z"   # a time folder far older than keep_days
EP = os.path.join("TV Shows", "Show (1998)", "Season 4", "Show (1998) - S04E07.mp4")


def library(tmp_path, monkeypatch, settings, rel=EP, data=b"original"):
    """A library file at tmp_path/media/rel, with the top of the mount at tmp_path/media. Returns its path and the
    kept originals folder."""
    path = tmp_path / "media" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    settings(keep_days=7, log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "media"))
    return str(path), str(tmp_path / "media" / hook.CFG.keep_dir)


def tree(root):
    """Every folder and file below root, as paths from root, folders with a trailing slash."""
    return sorted([os.path.relpath(os.path.join(d, n), root) + "/" for d, ds, _ in os.walk(root) for n in ds] +
                  [os.path.relpath(os.path.join(d, n), root) for d, _, ns in os.walk(root) for n in ns])


def test_a_keep_puts_the_folders_of_the_path_first_and_the_time_folder_beside_the_file(tmp_path, monkeypatch, settings):
    """Two keeps of one file sit side by side as two time folders. The prune of the older one leaves the newer one and
    its folders. The prune of both removes the empty show folders and keeps the kind folder."""
    path, root = library(tmp_path, monkeypatch, settings)
    clock(monkeypatch, 0, at=1_795_000_000)
    first = hook.keep_original(path)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_795_000_000))
    assert first == os.path.join(root, "TV Shows", "Show (1998)", "Season 4", stamp, "Show (1998) - S04E07.mp4")
    assert os.stat(first).st_ino == os.stat(path).st_ino
    os.remove(path)
    open(path, "wb").write(b"a new grab")
    clock(monkeypatch, 3, at=1_795_000_000)
    second = hook.keep_original(path)
    assert os.path.dirname(os.path.dirname(second)) == os.path.dirname(os.path.dirname(first)) and second != first
    clock(monkeypatch, 8, at=1_795_000_000)
    assert hook.prune_originals(root) == [os.path.relpath(os.path.dirname(first), root)]
    assert not os.path.exists(first) and os.path.exists(second)
    clock(monkeypatch, 11, at=1_795_000_000)
    assert hook.prune_originals(root) == [os.path.relpath(os.path.dirname(second), root)]
    assert tree(root) == [".plexignore"]


def test_the_prune_removes_old_layout_time_folders_and_keeps_every_other_folder(tmp_path, settings):
    """An old-layout time folder goes whole. An expired time folder deep in the tree goes, with the folders above it
    that it leaves empty. A folder whose name is no time stays while it holds a file, and so does an empty one that held
    no time folder. A young time folder stays whole, and the walk never enters it. A symlink named like a time is
    never followed. The kind folder stays when the prune empties it."""
    settings(keep_days=7)
    root = tmp_path / ".kept"
    young = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - 3600))
    for rel in (f"{OLD}/TV Shows/Show A/Season 1/a.mkv", f"TV Shows/Show B/Season 1/{OLD}/b.mkv", f"TV Shows/Show C/notes/{OLD}/c.mkv",
                "TV Shows/Show C/notes/hand.txt", f"Films/Film D (1990)/{young}/d.mkv", f"Films/Film D (1990)/{young}/{OLD}/e.mkv"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    (root / "Films" / "empty").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "f.mkv").write_text("f")
    os.symlink(tmp_path / "outside", root / "Films" / "20000102T000000Z")
    assert hook.prune_originals(str(root)) == [OLD, f"TV Shows/Show B/Season 1/{OLD}", f"TV Shows/Show C/notes/{OLD}"]
    assert tree(root) == sorted(["Films/", "Films/20000102T000000Z/", "Films/Film D (1990)/", f"Films/Film D (1990)/{young}/",
                          f"Films/Film D (1990)/{young}/{OLD}/", f"Films/Film D (1990)/{young}/{OLD}/e.mkv", f"Films/Film D (1990)/{young}/d.mkv",
                          "Films/empty/", "TV Shows/", "TV Shows/Show C/", "TV Shows/Show C/notes/", "TV Shows/Show C/notes/hand.txt"])
    assert os.path.exists(tmp_path / "outside" / "f.mkv")
    empty = tmp_path / ".empty"
    (empty / "Show E" / "Season 2" / OLD).mkdir(parents=True)
    assert hook.prune_originals(str(empty)) == [f"Show E/Season 2/{OLD}"] and os.listdir(empty) == []


def test_a_folder_named_like_a_short_time_is_no_time_folder(tmp_path, settings):
    """strptime() reads "202011T111Z" as a time in 2020. The prune takes a time folder only with every digit of the
    time, so a show folder with such a name stays with its files."""
    settings(keep_days=7)
    root = tmp_path / ".kept"
    for rel in ("TV Shows/202011T111Z/Season 1/a.mkv", f"TV Shows/Show B/{OLD}/b.mkv"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    assert hook.prune_originals(str(root)) == [f"TV Shows/Show B/{OLD}"]
    assert tree(root) == ["TV Shows/", "TV Shows/202011T111Z/", "TV Shows/202011T111Z/Season 1/", "TV Shows/202011T111Z/Season 1/a.mkv"]
    assert hook.is_stamp(OLD) and not hook.is_stamp("202011T111Z") and not hook.is_stamp(OLD + "x")


def test_drop_empty_stops_at_a_folder_that_holds_a_file_and_never_leaves_root(tmp_path):
    (tmp_path / "root" / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "root" / "a" / "keep.txt").write_text("x")
    hook.drop_empty(str(tmp_path / "root" / "a" / "b" / "c"), str(tmp_path / "root"))
    assert tree(tmp_path / "root") == ["a/", "a/keep.txt"]
    (tmp_path / "other" / "d").mkdir(parents=True)
    hook.drop_empty(str(tmp_path / "other" / "d"), str(tmp_path / "root"))
    assert os.path.isdir(tmp_path / "other" / "d")


def test_a_later_write_reuses_a_keep_in_either_layout(tmp_path, monkeypatch, settings):
    """kept_of() finds the time folder of a new-layout keep and of an old-layout keep from 2.22.0 or older. The reuse
    renews that time folder. A keep outside the kept folder is never reused."""
    path, root = library(tmp_path, monkeypatch, settings)
    young = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - 86400))
    for keep, folder in ((os.path.join(root, "TV Shows", "Show (1998)", "Season 4", young, "Show (1998) - S04E07.mp4"), None),
                         (os.path.join(root, young, EP), os.path.join(root, young))):
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        open(keep, "wb").write(b"original")
        hook.kept_write(path, keep, hook.file_key(os.stat(path)))
        note = {}
        assert hook.keep_original(path, note) == keep and note["kept_how"] == hook.KEPT_BEFORE
        assert os.path.exists(os.path.join(folder or os.path.dirname(keep), hook.RENEWED))
    outside = str(tmp_path / "elsewhere" / young / "Show (1998) - S04E07.mp4")
    os.makedirs(os.path.dirname(outside))
    open(outside, "wb").write(b"original")
    hook.kept_write(path, outside, hook.file_key(os.stat(path)))
    assert hook.kept_of(path) is None


def test_a_sidecar_kept_later_goes_in_the_time_folder_of_its_original(tmp_path, monkeypatch, settings):
    """stamp_of names a keep in either layout. A stamp_of outside the kept folder gives a keep of its own."""
    path, root = library(tmp_path, monkeypatch, settings)
    srt = path[:-4] + ".en.srt"
    open(srt, "w").write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    clock(monkeypatch, 0, at=1_795_000_000)
    video = hook.keep_original(path)
    assert hook.keep_original(srt, stamp_of=video) == os.path.join(os.path.dirname(video), os.path.basename(srt))
    old = os.path.join(root, OLD, EP)
    assert hook.keep_original(srt, stamp_of=old) == os.path.join(root, OLD, os.path.basename(srt))
    clock(monkeypatch, 1, at=1_795_000_000)   # a new stamp, so the keep takes no name the first sidecar took
    own = hook.keep_original(srt, stamp_of=str(tmp_path / "elsewhere" / OLD / "x.mp4"))
    assert own.startswith(os.path.join(root, "TV Shows", "Show (1998)", "Season 4") + os.sep) and own.endswith(os.sep + os.path.basename(srt))


def test_a_refused_hard_link_copies_the_original_into_the_new_layout(sync_mkv, tmp_path, monkeypatch):
    flat, data, st = refused_link(sync_mkv, tmp_path, monkeypatch, err=hook.errno.EXDEV)
    path = tmp_path / "Films" / "Film A (1979)" / "f.mkv"
    path.parent.mkdir(parents=True)
    os.replace(flat, path)
    note = {}
    keep = hook.keep_original(str(path), note)
    assert os.path.dirname(os.path.dirname(keep)) == str(tmp_path / ".kept" / "Films" / "Film A (1979)") and os.path.basename(keep) == "f.mkv"
    assert hook.is_stamp(os.path.basename(os.path.dirname(keep))) and open(keep, "rb").read() == data
    assert note["kept_how"] == "copied, because the hard link failed: Invalid cross-device link"


def test_a_season_grab_links_each_season_into_its_own_time_folder(env, monkeypatch, tmp_path, capsys):
    """An anime batch that crosses a season. Each season folder gets one time folder for the grab, with the extras of
    each video beside it. A file of two episodes is linked once. A refused link leaves no empty folder behind, and a
    season folder that held a keep before stays."""
    keep_on(monkeypatch, tmp_path)
    show, root = tmp_path / "media" / "Anime" / "Show", tmp_path / "media" / hook.CFG.recycle_dir
    paths = [show / "Season 1" / "Show - S01E12-E13.mkv", show / "Season 2" / "Show - S02E01.mkv", show / "Season 2" / "Show - S02E01.en.srt"]
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.name)
    eps = [str(paths[0]), str(paths[0]), str(paths[1])]
    monkeypatch.setattr(hook.ARR["sonarr"], "grab_paths", lambda owner, ids: eps)
    clock(monkeypatch, 0, at=1_795_000_000)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_795_000_000))
    rec = hook.keep_grab("sonarr", 5, "pack1", eps=(1, 2, 3))
    assert rec["result"] == "kept_replaced" and tree(root) == [
        ".plexignore", "Anime/", "Anime/Show/", "Anime/Show/Season 1/", f"Anime/Show/Season 1/{stamp}/", f"Anime/Show/Season 1/{stamp}/Show - S01E12-E13.mkv",
        "Anime/Show/Season 2/", f"Anime/Show/Season 2/{stamp}/", f"Anime/Show/Season 2/{stamp}/Show - S02E01.en.srt", f"Anime/Show/Season 2/{stamp}/Show - S02E01.mkv"]
    assert sorted(r["kept"] for r in hook.kept_read()) == sorted(rec["kept"]) and len(rec["kept"]) == 3
    new = show / "Season 3" / "Show - S03E01.mkv"
    new.parent.mkdir()
    new.write_text(new.name)
    eps.append(str(new))
    real = os.link
    monkeypatch.setattr(hook.os, "link", lambda a, b: real(a, b) if "Season 1" in a else (_ for _ in ()).throw(OSError(hook.errno.EXDEV, "Invalid cross-device link")))
    clock(monkeypatch, 1, at=1_795_000_000)
    assert hook.keep_grab("sonarr", 5, "pack2", eps=(1, 2, 3, 4))["result"] == "kept_replaced"
    later = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_795_000_000 + 86400))
    assert [f for f in tree(root) if later in f or "Season 3" in f] == [f"Anime/Show/Season 1/{later}/", f"Anime/Show/Season 1/{later}/Show - S01E12-E13.mkv"]
    assert os.listdir(root / "Anime" / "Show" / "Season 2") == [stamp]


def test_the_hooks_own_copy_comes_back_from_the_new_layout(env, monkeypatch, tmp_path):
    """The grab kept the old file and its subtitle in one time folder below the movie's folder. The restore takes both
    from there by their records."""
    old, srt, rb, k = kept_upgrade(env, monkeypatch, tmp_path)
    root = tmp_path / "media" / hook.CFG.recycle_dir
    rel = os.path.relpath(os.path.dirname(old), tmp_path / "media")
    assert os.path.dirname(os.path.dirname(k)) == str(root / rel) and hook.is_stamp(os.path.basename(os.path.dirname(k)))
    assert sorted(os.listdir(os.path.dirname(k))) == sorted([os.path.basename(old), os.path.basename(srt)])
    os.remove(old)
    os.remove(srt)
    monkeypatch.setenv("radarr_deletedrecyclebinpaths", "")
    silent(env, monkeypatch, env["path"])
    hook.main([])
    (r,) = decided(env)["restore"]
    assert (r["result"], r["recycle"]) == ("restored", k) and [x["result"] for x in r["extras"]] == ["restored"]
    assert open(old, "rb").read() == b"old" * 300 and os.path.exists(srt) and os.listdir(os.path.dirname(k)) == []
