# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The second read of the video after a full read with video errors (docs/design.md, "Whole-file check"). Made-up
media: 30-second clips with H.264 video and E-AC-3 audio. A clip whose H.264 packets hold a NAL length of 0 fails the
read of a subtitle remux, as a real download did. A clip with random bytes in its slice data prints decoder errors,
and the read of a subtitle remux still passes."""
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import types

import pytest

import amg
import test_arr_media_guard as main_tests
from test_arr_media_guard import (REAL_MKVMERGE, REAL_WHOLE_PLAN, backfill_job, check_listed, checked, decided, enqueue, env, flash_film,  # noqa: F401
                                  flash_srt, grabbed, hook, log_lines, noise, one_alert_film, posted, sync_mkv, two_lines, whole_import)

DAMAGE = {"read": "ffmpeg", "fault": "parts of the file cannot be read", "line": "Invalid data found when processing input"}
DAMAGE_TEXT = ("A full read of the file found 108 errors.\nA second read of the video showed that it's damaged, because "
               "**parts of the file cannot be read**.\nInvalid data found when processing input.")   # one sentence a line, see markdown()


def video_packets(path):
    """(byte, size) of each video packet of path, by ffprobe, in file order."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pos,size", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return [(int(p["pos"]), int(p["size"])) for p in json.loads(r.stdout)["packets"] if "pos" in p]


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """clean.mkv, nal0.mkv with a NAL length of 0 in video packets 300 to 305, slices.mkv with random bytes from byte
    40 of video packets 1 to 5, and audio.mkv with random bytes over every audio packet."""
    d = tmp_path_factory.mktemp("damage")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=30", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=30:sample_rate=48000", "-map", "0", "-map", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-g", "48", "-c:a", "eac3", "-b:a", "192k", "-ac", "2", "-shortest", str(d / "clean.mkv")], check=True)
    data, got = (d / "clean.mkv").read_bytes(), video_packets(d / "clean.mkv")
    nal0, slices = bytearray(data), bytearray(data)
    for pos, _ in got[300:306]:   # each packet holds 4 bytes of block header, then the 4-byte length of its first NAL unit
        nal0[pos + 4:pos + 8] = bytes(4)
    for pos, size in got[1:6]:
        slices[pos + 40:pos + size] = noise(size - 40, pos)
    (d / "nal0.mkv").write_bytes(nal0)
    (d / "slices.mkv").write_bytes(slices)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pos,size", "-of", "json", str(d / "clean.mkv")],
                       capture_output=True, text=True, check=True)
    audio = bytearray(data)
    for p in json.loads(r.stdout)["packets"]:
        pos, size = int(p["pos"]), int(p["size"])
        audio[pos + 12:pos + size] = noise(size - 12, pos)
    (d / "audio.mkv").write_bytes(audio)
    return d


def check(path, apply=False):
    """whole_act() of path, as a job of a backfill runs it: check in grab, so it never re-grabs."""
    return hook.whole_act(None, {"app": "radarr", "path": str(path), "grab": {"check": True}}, {"id": "x", "apply": apply}, [], post=False)


def test_a_second_read_that_fails_makes_the_file_damaged(clips, tmp_path, settings):
    """Tester 2026-11-20: a full read found 108 H.264 errors and AMG alerted "may be broken". Hours later the subtitle
    remux failed on the same data. The second read now runs the read of that remux at once. It fails, so the file is
    damaged, and one Damaged file alert takes the place of the doubt. A backfill's job never re-grabs."""
    settings(state_dir=str(tmp_path), regrab=frozenset({"audio", "video", "damage"}))
    rec = check(clips / "nal0.mkv")
    (f,) = rec["findings"]
    n = rec["whole"]["ffmpeg"]["lines"]
    assert (rec["outcome"], rec["regrab"], f["kind"], f["action"]["code"]) == ("damaged_source", "check", "damage", "check"), rec
    assert rec["whole"]["ffmpeg"]["kinds"] == {"video": n} and f["doubts"] == [f"a full read of the file found {n} errors"], rec["whole"]
    assert rec["whole"]["reread"]["damage"] == {"read": "ffmpeg", "fault": "parts of the file cannot be read", "line": f["line"]}
    assert re.fullmatch(r"(Invalid data found when processing input|NAL unit size \(0 > \d+\)\.).*", f["line"]), f
    assert rec["result"] == "damaged source: parts of the file cannot be read, re-grab check" and rec["alert_kinds"] == ["damage"]
    assert hook.title(f) == ("Damaged file, would re-grab", hook.title(f)[1])
    text = hook.unmarked(hook.FINDINGS["damage"][1](f, "done"))
    assert text == (f"A full read of the file found {n} errors. A second read of the video showed that it's damaged, because parts of the file "
                    f"cannot be read. {f['line'].rstrip('.')}."), text


def test_a_second_read_that_passes_keeps_the_doubt(clips, tmp_path, settings):
    """Random bytes in the slice data print decoder errors at the start. The read of a subtitle remux parses no slice
    data, so it passes, and the doubt and its alert stay as they were."""
    settings(state_dir=str(tmp_path))
    rec = check(clips / "slices.mkv")
    (f,) = rec["findings"]
    assert (rec["outcome"], f["kind"], hook.title(f)[0]) == ("video_doubt", "video", "Video may be broken"), rec
    assert set(rec["whole"]["reread"]) == {"took"} and "regrab" not in rec, rec["whole"]
    n = rec["whole"]["ffmpeg"]["loose"]
    assert hook.unmarked(hook.FINDINGS["video"][1](f, "done")) == f"A full read of the file found {n} errors. AMG kept the file and did not re-grab it."


def test_audio_errors_alone_read_nothing_again(clips, tmp_path, settings):
    """The second read is for video errors. The audio doubt keeps its own check, one sample of each track."""
    settings(state_dir=str(tmp_path))
    rec = check(clips / "audio.mkv")
    assert rec["outcome"] == "audio_doubt" and "reread" not in rec["whole"] and "video" not in rec["whole"]["ffmpeg"]["kinds"], rec


@pytest.mark.parametrize("reused", [False, True])
def test_a_damaged_file_is_marked_on_an_apply(clips, tmp_path, settings, monkeypatch, reused):
    """The job of a backfill applies, so it marks the file it kept. A dry check marks nothing. A new file at the path
    clears the mark. clean.mkv has the size of nal0.mkv, and with reused the new file also gets the old inode number,
    as ext4 and XFS give a freed one to the next new file at once. Its new mtime clears the mark."""
    settings(state_dir=str(tmp_path))
    path = tmp_path / "film.mkv"
    path.write_bytes((clips / "nal0.mkv").read_bytes())
    check(path)
    assert hook.damaged(str(path)) is None
    rec = check(path, apply=True)
    assert hook.damaged(str(path)) == rec["whole"]["reread"]["damage"]
    ino = os.stat(path).st_ino
    path.unlink()
    path.write_bytes((clips / "clean.mkv").read_bytes())
    if reused:
        real = os.stat
        monkeypatch.setattr(os, "stat", lambda p, *a, **k: types.SimpleNamespace(**dict(
            {f: getattr(real(p), f) for f in dir(real(p)) if f.startswith("st_")}, st_ino=ino)) if p == path or p == str(path) else real(p, *a, **k))
        assert os.stat(path).st_ino == ino and os.stat(path).st_size == os.path.getsize(clips / "nal0.mkv")
    assert hook.damaged(str(path)) is None and hook.store.get(hook.DAMAGED, str(path)) is None
    check(clips / "slices.mkv", apply=True)
    assert hook.damaged(str(clips / "slices.mkv")) is None


def test_the_second_read_maps_the_video_streams_alone(clips, monkeypatch, settings, tmp_path):
    """The second read is the read of a subtitle remux on the video streams only, with the filter of PROOF_BSF."""
    settings(state_dir=str(tmp_path))
    seen = []
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, **k: seen.append((maps, bsf, texts)) or ({}, {}))
    assert hook.source_read({"read": "ffmpeg", "kind": "video"}, str(clips / "clean.mkv"), None) is None
    assert seen == [([0], {0: "filter_units=remove_types=9|12"}, [])], seen


@pytest.mark.parametrize("zero, more, kinds", [
    ([], ["a full read of the file found 3 errors"], {"container": 3}),            # demuxer lines alone
    (["the file has a blank gap at 50% of the file size"], [], {"video": 3})])     # video lines that belong to the gap
def test_only_video_errors_of_a_doubt_get_a_second_read(env, monkeypatch, zero, more, kinds):
    """The second read is for video errors that the read pass doubts. Demuxer lines alone, and video lines that the
    one gap holds, read nothing more, and the doubt stays."""
    fields = {"zeros": {"runs": [[500, 600]] if zero else [], "at": [], "read": 1000, "took": 0.1, "count": [[500, 600]] if zero else []},
              "size": 1000, "pcm": False, "fault": None, "ffmpeg": {"ran": True, "lines": 3, "pos": [], "at": [], "loose": 3, "kinds": kinds, "audio": []}}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, zero, more, json.loads(json.dumps(fields))))
    reads = []
    monkeypatch.setattr(hook, "source_read", lambda damage, path, j: reads.append(path) or dict(DAMAGE))
    rec = whole_import(env, monkeypatch, ())
    assert reads == [] and rec["outcome"] == "video_doubt" and "reread" not in rec["whole"], rec


def damaged_import(env, monkeypatch, again=True, reads=None):
    """An import whose whole-file check finds 108 video errors, and whose second read fails, see whole_import(). A read
    of the video after that gives DAMAGE again, or nothing when again is False. reads takes each read."""
    fields = {"zeros": {"runs": [], "at": [], "read": 1000, "took": 0.1, "count": []}, "size": 1000, "pcm": False, "fault": None,
              "ffmpeg": {"ran": True, "lines": 108, "pos": [], "at": [], "loose": 108, "kinds": {"video": 108}, "audio": []}}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], ["a full read of the file found 108 errors"], json.loads(json.dumps(fields))))
    reads = [] if reads is None else reads
    monkeypatch.setattr(hook, "source_read", lambda damage, path, j: reads.append((damage, path)) or (dict(DAMAGE) if again or len(reads) == 1 else None))
    return whole_import(env, monkeypatch, ()), reads


def test_an_import_regrabs_a_damaged_file(env, monkeypatch, settings):
    """REGRAB=damage re-grabs the file, as a damaged conversion does. The re-grab reads the video once more first."""
    settings(regrab=frozenset({"audio", "video", "damage"}))
    rec, reads = damaged_import(env, monkeypatch)
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    assert (rec["outcome"], rec["regrab"], rec["alert_result"]) == ("damaged_source", "regrabbed", ["sent"]), rec
    assert reads == [({"read": "ffmpeg", "kind": "video"}, env["path"])] * 2
    assert posted(env) == [("Damaged file, re-grabbed", DAMAGE_TEXT + "\nDeleted the broken file and marked the grab as failed, so Radarr is "
                                                                      "searching for another copy.")], posted(env)
    assert hook.store.get(hook.DAMAGED, env["path"]) is None


@pytest.mark.parametrize("regrab", [{"audio", "video", "damage"}, {"audio", "video"}])
def test_the_regrab_reads_the_video_again_before_it_takes_the_lock(env, monkeypatch, settings, regrab):
    """Reviewer 2026-11-25: the re-grab's second check read the whole video under the one global lock, so every import
    waited for it, 8 to 12 s for a 1.6 GB file. That read now runs before the lock, with the default REGRAB too. Each
    read of the video tries the lock from another open file, so it sees the lock free."""
    settings(regrab=frozenset(regrab))
    free = []

    def read(damage, path, j):
        with open(os.path.join(hook.CFG.state_dir, "lock"), "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
            except BlockingIOError:
                free.append(False)
        return dict(DAMAGE)
    fields = {"zeros": {"runs": [], "at": [], "read": 1000, "took": 0.1, "count": []}, "size": 1000, "pcm": False, "fault": None,
              "ffmpeg": {"ran": True, "lines": 108, "pos": [], "at": [], "loose": 108, "kinds": {"video": 108}, "audio": []}}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], ["a full read of the file found 108 errors"], json.loads(json.dumps(fields))))
    monkeypatch.setattr(hook, "source_read", read)
    rec = whole_import(env, monkeypatch, ())
    assert free == [True, True] and rec["regrab"] == ("regrabbed" if "damage" in regrab else "would_regrab"), (free, rec)


def test_an_import_keeps_a_damaged_file_when_regrab_leaves_damage_out(env, monkeypatch, settings):
    """The default REGRAB leaves damage out. The file stays, the alert says so, and the mark keeps its subtitles as
    they are."""
    rec, _ = damaged_import(env, monkeypatch)
    assert env["writes"] == [] and (rec["outcome"], rec["regrab"]) == ("damaged_source", "would_regrab"), rec
    assert posted(env) == [("Damaged file, re-grab is off", DAMAGE_TEXT + "\nRe-grabs for damaged files are off, so the file was kept.")], posted(env)
    assert hook.damaged(env["path"]) == DAMAGE


def kept_original(monkeypatch, tmp_path):
    """A kept original of an import's header repair, and a read of it that finds 108 video errors and then DAMAGE.
    Returns its path and the list of the files each read read."""
    kept = tmp_path / "kept.mkv"
    kept.write_bytes(b"x" * 1000)
    fields = {"zeros": {"runs": [], "at": [], "read": 1000, "took": 0.1, "count": []}, "size": 1000, "pcm": False, "fault": None,
              "ffmpeg": {"ran": True, "lines": 108, "pos": [], "at": [], "loose": 108, "kinds": {"video": 108}, "audio": []}}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], ["a full read of the file found 108 errors"], json.loads(json.dumps(fields))))
    reads = []
    monkeypatch.setattr(hook, "source_read", lambda damage, path, j: reads.append(path) or dict(DAMAGE))
    return str(kept), reads


@pytest.mark.parametrize("keyed", [True, False])
def test_every_read_of_the_video_reads_the_file_the_full_read_read(env, monkeypatch, settings, tmp_path, keyed):
    """The import's header repair kept the original, so the full read reads it. The second read and the re-grab's own
    check read that kept original too, as the zero runs are measured again in it. The mark goes on the file the repair
    made, by the job's key. A job with no key names no file, so it marks nothing."""
    settings(regrab=frozenset({"audio", "video", "damage"}))
    kept, reads = kept_original(monkeypatch, tmp_path)
    job = {"app": "radarr", "path": env["path"], "original": kept, "remux": "header repair", "grab": {"check": True},
           **({"key": list(hook.file_key(os.stat(env["path"])))} if keyed else {})}
    rec = hook.whole_act(None, job, {"id": "x", "apply": True}, [], post=False)
    assert (rec["outcome"], rec["regrab"], reads) == ("damaged_source", "check", [kept] * 2), (rec, reads)
    assert hook.damaged(env["path"]) == (DAMAGE if keyed else None)


def test_the_whole_file_check_of_check_marks_the_file_the_import_left(env, monkeypatch, settings, tmp_path):
    """--check --apply runs the whole-file check at once, with the key of the file its import pass left, as an import
    queues the job. So a read of the kept original marks that file."""
    kept, _ = kept_original(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "whole_plan", lambda rec: {"path": rec["path"], "remux": "header repair", "original": kept})
    _, rec = hook.whole_later("radarr", {}, (), types.SimpleNamespace(apply=True), {"check": True}, {"path": env["path"], "label": "Film"},
                              types.SimpleNamespace(post=False))
    assert rec["outcome"] == "damaged_source" and hook.damaged(env["path"]) == DAMAGE, rec


def test_a_new_file_during_the_regrab_of_a_damaged_file_gets_no_mark(env, monkeypatch, settings):
    """The re-grab runs under the lock, and its own second check reads the video for minutes. The app holds no lock and
    can import a new file meanwhile. The mark holds the key of the file the reads judged, so the new file has none."""
    rec, _ = damaged_import(env, monkeypatch)   # the default REGRAB keeps the file, so it gets the mark
    assert hook.damaged(env["path"]) == DAMAGE
    hook.store.drop(hook.DAMAGED, env["path"])
    real = hook.regrab

    def regrab(*a, **k):
        out = real(*a, **k)
        with open(env["path"] + ".new", "wb") as f:
            f.write(b"n" * 1000)
        os.replace(env["path"] + ".new", env["path"])
        return out
    monkeypatch.setattr(hook, "regrab", regrab)
    job = {"app": "radarr", "path": env["path"], "grab": {}, "key": list(hook.file_key(os.stat(env["path"])))}
    rec = hook.whole_act(None, job, {"id": "y", "apply": True}, [], post=False)   # a job with no grab fields finds no grab
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "no_grab") and hook.damaged(env["path"]) is None, rec


def test_a_damage_the_regrab_does_not_find_again_marks_nothing(env, monkeypatch, settings):
    """The re-grab's own second check reads the video once more. When it finds no damage, the file stays unmarked."""
    settings(regrab=frozenset({"audio", "video", "damage"}))
    rec, reads = damaged_import(env, monkeypatch, again=False)
    assert env["writes"] == [] and (rec["outcome"], rec["regrab"], len(reads)) == ("damaged_source", "unconfirmed", 2), rec
    assert [t for t, _ in posted(env)] == ["Damaged file, not confirmed"] and hook.damaged(env["path"]) is None, posted(env)


def test_a_stopped_regrab_of_a_damaged_file_marks_nothing(env, monkeypatch, settings):
    """A stop before the re-grab's second check changes nothing, and the next run of the job checks again."""
    settings(regrab=frozenset({"audio", "video", "damage"}))
    reads = []
    monkeypatch.setattr(hook, "stop_asked", lambda: bool(reads))   # a stop asked during the second read
    rec, _ = damaged_import(env, monkeypatch, reads=reads)
    assert env["writes"] == [] and rec["regrab"] == "failed" and len(reads) == 1 and hook.damaged(env["path"]) is None, rec


def test_a_backfill_job_of_a_damaged_file_still_runs_its_checks(env, monkeypatch, settings):
    """The mark holds back only the subtitle work. The import checks of a backfill job still run on the file."""
    check_listed(env, monkeypatch, "")
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    name = backfill_job(env, monkeypatch)
    hook.background(name, [])
    (rec,) = checked(env, "backfill_job")
    assert rec["outcome"] != "file_damaged" and "tracks" in rec, rec


def test_the_deep_analysis_of_a_damaged_file_drops_with_its_held_alert(env, monkeypatch, settings):
    """The tester's order: the whole-file check runs before the deep analysis of its group. The deep analysis of a
    damaged file hears nothing and rewrites nothing. The subtitle alert the import held drops with it, because the
    Damaged file alert went out."""
    one_alert_film(env, monkeypatch, settings, "fixed")
    hook.run_job(enqueue(env, 0, env["path"]), [])
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    (name,) = hook.deep_analysis_queued()
    hook.background(name, [])
    first, (deep,) = two_lines(env)
    assert first["alert_result"] == [hook.HOLD_RESULT] and deep["outcome"] == "file_damaged", deep
    assert deep["result"] == "dropped, the file is damaged: parts of the file cannot be read"
    assert deep["held_result"] == ["dropped, the file is damaged"] and posted(env) == [] and "subremux" not in deep, deep


def test_a_deep_analysis_marked_damaged_during_the_run_drops_at_its_remux(env, monkeypatch, settings):
    """A deep analysis that started before the mark rewrites nothing. Its remux step reads the mark too, and the job
    drops there as at its start. It posts no subtitle alert, and the alert the import held drops with it."""
    one_alert_film(env, monkeypatch, settings, "fixed")
    hook.run_job(enqueue(env, 0, env["path"]), [])
    real, asked = hook.damaged, []
    monkeypatch.setattr(hook, "damaged", lambda path, st=None: (asked.append(path), real(path, st) if len(asked) > 1 else None)[1])
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    hook.main([])
    _, (deep,) = two_lines(env)
    assert len(asked) == 2 and deep["outcome"] == "file_damaged" and "subremux" not in deep, deep
    assert deep["result"] == "dropped, the file is damaged: parts of the file cannot be read"
    assert deep["held_result"] == ["dropped, the file is damaged"] and env["repacks"] == [] and posted(env) == [], posted(env)


# The read of the original that a subtitle remux runs, as it failed on a real download (docs/design.md, "Whole-file check")
BAD_READ = ("NAL unit size (0 > 49766).\n[filter_units @ 0x55e3a1c07d40] Failed to read packet.\n[vost#0:0/copy @ 0x55e3a1c08e80] "
            "Error applying bitstream filters to a packet: Invalid data found when processing input")
REMUX_DAMAGE = {"read": "ffmpeg", "fault": "parts of the file cannot be read", "line": "NAL unit size (0 > 49766)."}
REMUX_TEXT = ("Rewriting the file to fix its subtitles showed that it's damaged, because **parts of the file cannot be read**.\n"
              "NAL unit size (0 > 49766).")


@pytest.mark.parametrize("on, message, damaged", [("original", BAD_READ, True), ("temp", BAD_READ, False),
                                                  ("original", "Error while decoding stream #0:0", False)])
def test_a_remux_that_cannot_read_the_original_names_the_damage(sync_mkv, tmp_path, monkeypatch, settings, on, message, damaged):
    """The proof of a subtitle remux reads the original with the filter of PROOF_BSF. A DAMAGE message of that read
    names the damage, as in a conversion. A failed read of the temp file, or another message, names none. The file
    stays as it was each time."""
    settings(state_dir=str(tmp_path))
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    before = open(path, "rb").read()

    def bad_read(src, tmp, *a, **k):
        p = src if on == "original" else tmp
        raise hook.ReadFailed(p, f"ffmpeg did not read {os.path.basename(p)} cleanly: {message}")
    monkeypatch.setattr(hook, "prove", bad_read)
    code, result, info = hook.resub(path, REAL_MKVMERGE(path), os.stat(path), True, {2: {"rate": "1/1", "offset": 1.0}})
    assert code == "subtitle_remux_failed" and result.startswith("subtitle remux failed: ffmpeg did not read "), result
    assert info.get("damage") == (REMUX_DAMAGE if damaged else None), info
    assert open(path, "rb").read() == before


def nal0_film(clips, tmp_path):
    """nal0.mkv with a SubRip track muxed in. mkvmerge keeps its broken H.264 packets. Returns its path and probe."""
    (tmp_path / "s.srt").write_text("".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},000\nLine {i}\n\n" for i in range(1, 12)))
    path = str(tmp_path / "f.mkv")
    subprocess.run(["mkvmerge", "-q", "-o", path, str(clips / "nal0.mkv"), "--language", "0:eng", str(tmp_path / "s.srt")], check=True)
    return path, REAL_MKVMERGE(path)


@pytest.mark.parametrize("first", ["any", "temp", "temp other"])
def test_a_remux_of_a_clip_with_a_nal_length_of_0_names_the_damage(clips, tmp_path, monkeypatch, settings, first):
    """The real read. The proof reads the original and the new file at once, and the new file holds the broken packets
    too, so either read can fail first. With the read of the new file first, the original is read again from scratch,
    so the damage is named each time. A read of the new file that failed with no DAMAGE message reads nothing more."""
    settings(state_dir=str(tmp_path))
    path, j = nal0_film(clips, tmp_path)
    if first != "any":
        tmp, text = os.path.join(os.path.dirname(path), ".f.mkv.repack-tmp"), BAD_READ if first == "temp" else "Error while decoding stream #0:0"
        monkeypatch.setattr(hook, "prove", lambda *a, **k: (_ for _ in ()).throw(hook.ReadFailed(tmp, f"ffmpeg did not read .f.mkv.repack-tmp cleanly: {text}")))
    sub = next(t["id"] for t in j["tracks"] if t["type"] == "subtitles")
    code, result, info = hook.resub(path, j, os.stat(path), True, {sub: {"rate": "1/1", "offset": 1.0}})
    assert code == "subtitle_remux_failed", result
    if first == "temp other":
        assert "damage" not in info, info
        return
    assert (info.get("damage") or {}).get("fault") == "parts of the file cannot be read", (result, info)
    assert re.fullmatch(r"(Invalid data found when processing input|NAL unit size \(0 > \d+\)\.).*", info["damage"]["line"]), info


def test_a_conversion_whose_new_file_fails_first_reads_the_original_again(env, monkeypatch):
    """The same race in a conversion: the read of the new MKV failed first. The original is read again from scratch
    and shows the damage, so the import re-grabs, after the second check of regrab()."""
    mp4, reads = main_tests.damaged_import(env, monkeypatch, "ffmpeg", message=main_tests.BAD_READ)
    tmp = hook.repack_tmp(mp4)
    monkeypatch.setattr(hook, "prove", lambda *a, **k: (_ for _ in ()).throw(hook.ReadFailed(tmp, f"ffmpeg did not read x cleanly: {main_tests.BAD_READ}")))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"], rec["repack"]["damage"]["line"]) == ("damaged_source", "regrabbed", "NAL unit size (0 > 4817)."), rec
    assert reads == [(mp4, {0: main_tests.AUD})] * 2   # the read of the original again, then the second check


def remux_film(env, monkeypatch, settings, tmp_path, info):
    """An import of an English film whose French track and French sidecar flash, see flash_film(). Its subtitle remux
    fails with the facts info. The second check of a re-grab reads REMUX_DAMAGE again, and reads takes each path it
    reads. Returns (the sidecar, reads)."""
    flash_film(env, monkeypatch)
    side = env["path"][:-4] + ".fr.forced.srt"
    with open(side, "w", newline="") as f:
        f.write(flash_srt())
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    failed = f"subtitle remux failed: ffmpeg did not read {os.path.basename(env['path'])} cleanly: {BAD_READ}"
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: ("subtitle_remux_failed", failed[:500], dict(info)))
    reads = []
    monkeypatch.setattr(hook, "source_read", lambda damage, path, j: reads.append(path) or dict(REMUX_DAMAGE))
    return side, reads


def test_an_import_whose_subtitle_remux_cannot_read_the_original_posts_one_damaged_file_alert(env, monkeypatch, settings, tmp_path):
    """Owner 2026-11-25: the import's own subtitle remux failed on the damage, and the file got a "Rewrite failed" post,
    then a Damaged file post from the whole-file check. Now the failed read makes the file damaged at once. One
    Damaged file alert goes out. The sidecar keeps its times, and no flag edit runs. The mark makes the whole-file check
    post nothing more, and the deep analysis drops."""
    side, reads = remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None, "damage": REMUX_DAMAGE})
    grabbed(env, monkeypatch)
    fields = {"zeros": {"runs": [], "at": [], "read": 1000, "took": 0.1, "count": []}, "size": 1000, "pcm": False, "fault": None,
              "ffmpeg": {"ran": True, "lines": 108, "pos": [], "at": [], "loose": 108, "kinds": {"video": 108}, "audio": []}}
    monkeypatch.setattr(hook, "whole_plan", REAL_WHOLE_PLAN)
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], ["a full read of the file found 108 errors"], json.loads(json.dumps(fields))))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"], rec["alert_kinds"]) == ("damaged_source", "would_regrab", ["damage"]), rec
    assert rec["result"] == "damaged source: parts of the file cannot be read" and rec["subremux"]["damage"] == REMUX_DAMAGE, rec
    assert posted(env) == [("Damaged file, re-grab is off", REMUX_TEXT + "\nRe-grabs for damaged files are off, so the file was kept.")], posted(env)
    assert "sidecars" not in rec and open(side, newline="").read() == flash_srt() and env["mkvpropedit"] == [], rec
    assert hook.damaged(env["path"]) == REMUX_DAMAGE
    (whole,) = [r for r in checked(env, "whole_file")]
    assert (whole["outcome"], "findings" in whole, "regrab" in whole) == ("damaged_source", False, False), whole
    assert whole["result"] == "damaged source: parts of the file cannot be read, marked damaged before: parts of the file cannot be read"
    settings(subtitles="deep")   # the deep analysis of the file then drops at its start, see the test above
    hook.queue_deep_analysis({"app": "radarr", "path": env["path"]}, rec, {"label": rec["label"]})
    hook.main([])
    (deep,) = checked(env, "deep_analysis")
    assert deep["outcome"] == "file_damaged" and len(posted(env)) == 1, (deep, posted(env))


def test_an_import_regrabs_a_file_its_subtitle_remux_found_damaged(env, monkeypatch, settings, tmp_path):
    """REGRAB=damage re-grabs the file, after a second read of the original from scratch finds the damage again. A
    deleted file gets no mark."""
    _, reads = remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None, "damage": REMUX_DAMAGE})
    grabbed(env, monkeypatch)
    settings(regrab=frozenset({"audio", "video", "damage"}))
    hook.main([])
    rec = decided(env)
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    assert (rec["outcome"], rec["regrab"], reads) == ("damaged_source", "regrabbed", [env["path"]]), rec
    assert posted(env) == [("Damaged file, re-grabbed", REMUX_TEXT + "\nDeleted the broken file and marked the grab as failed, so Radarr is "
                                                                    "searching for another copy.")], posted(env)
    assert hook.store.get(hook.DAMAGED, env["path"]) is None


def test_the_mark_of_an_import_s_remux_names_the_file_the_remux_read(env, monkeypatch, settings, tmp_path):
    """The import's subtitle remux could not read the original. The app imports a new file during the re-grab. The mark
    holds the key of the file the remux read, so the new file has none."""
    remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None, "damage": REMUX_DAMAGE})
    grabbed(env, monkeypatch)
    real = hook.regrab

    def regrab(*a, **k):
        out = real(*a, **k)
        with open(env["path"] + ".new", "wb") as f:
            f.write(b"n" * 1000)
        os.replace(env["path"] + ".new", env["path"])
        return out
    monkeypatch.setattr(hook, "regrab", regrab)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "would_regrab") and hook.damaged(env["path"]) is None, rec


def test_a_backfill_job_marks_a_file_its_subtitle_remux_found_damaged(env, monkeypatch, settings, tmp_path):
    """A job a backfill queued never re-grabs. It says what an import would do, and it marks the file it kept."""
    remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None, "damage": REMUX_DAMAGE})
    settings(regrab=frozenset({"audio", "video", "damage"}))
    check_listed(env, monkeypatch, "")
    hook.background(backfill_job(env, monkeypatch), [])
    (rec,) = checked(env, "backfill_job")
    assert (rec["outcome"], rec["regrab"], rec["alert_kinds"]) == ("damaged_source", "check", ["damage"]), rec
    assert posted(env) == [("Damaged file, would re-grab", REMUX_TEXT + "\nAn import would re-grab the file.\nThis check never re-grabs, so it "
                                                                       "was kept.")], posted(env)
    assert hook.damaged(env["path"]) == REMUX_DAMAGE


@pytest.mark.parametrize("run", ["backfill job", "backfill job, SUBTITLES check", "sub-check", "sub-time"])
def test_a_marked_file_keeps_its_sidecar_times_and_posts_no_subtitle_alert(env, monkeypatch, settings, tmp_path, run):
    """Reviewer 2026-11-25: the sidecar step and the subtitle alerts read only the damage of the run's own remux. A
    later run of a marked file retimed its sidecar, and at SUBTITLES check a subtitle alert followed the Damaged file
    alert. The mark now holds both back in every run, as it holds back the remux."""
    side, _ = remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None})
    if "check" in run:
        settings(subtitles="check")
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    check_listed(env, monkeypatch, "")
    if run.startswith("backfill job"):
        hook.background(backfill_job(env, monkeypatch), [])
        (rec,) = checked(env, "backfill_job")
    else:
        with pytest.raises(SystemExit) if run == "sub-time" else contextlib.nullcontext():   # --sub-time names the remux it could not make
            hook.main(["--backfill", "radarr", "--sub-check", "--apply"] if run == "sub-check" else ["--sub-time", env["path"], "--apply"])
        rec = [r for r in log_lines(env) if "schema" in r][-1]
    assert "sidecars" not in rec and open(side, newline="").read() == flash_srt(), rec
    assert "subtiming" not in [f["kind"] for f in rec.get("findings") or []] and posted(env) == [], (rec.get("findings"), posted(env))
    assert (rec.get("subremux") or {}).get("result", "") in ("", f"subtitle remux skipped, the file is damaged: {DAMAGE['fault']}"), rec


def test_a_marked_file_that_its_flag_edit_grows_keeps_its_mark(env, monkeypatch, settings, tmp_path):
    """Reviewer 2026-11-25: mkvpropedit grew a file that ffmpeg made by 284 bytes, with the same inode. The mark held
    the old size, so the run retimed the sidecar and posted "Subtitles out of sync", and a later whole-file check gave
    a second Damaged file alert and re-grabbed. The edit now moves the mark to the new size."""
    side, _ = remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None})
    env["probe"] = main_tests.sub_probe(("fre", True, {}), audio=(("eng", True),))   # the French track plays by default, so the run edits its flag
    settings(subtitles="check")
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    check_listed(env, monkeypatch, "")
    real, grown = hook.subprocess.run, []

    def grows(argv, **kw):   # mkvpropedit moves Tracks to the end of a file with no room after them
        r = real(argv, **kw)
        if argv[0] == "mkvpropedit":
            with open(argv[1], "ab") as f:
                f.write(bytes(284))
            grown.append(os.stat(argv[1]).st_size)
        return r
    monkeypatch.setattr(hook.subprocess, "run", grows)
    ino = os.stat(env["path"]).st_ino
    hook.background(backfill_job(env, monkeypatch), [])
    (rec,) = checked(env, "backfill_job")
    assert len(grown) == 1 and os.stat(env["path"]).st_ino == ino and rec["outcome"] == "edited", rec
    assert "sidecars" not in rec and open(side, newline="").read() == flash_srt(), rec
    assert "subtiming" not in [f["kind"] for f in rec.get("findings") or []] and posted(env) == [], (rec.get("findings"), posted(env))
    assert hook.damaged(env["path"]) == DAMAGE
    settings(regrab=frozenset({"audio", "video", "damage"}))
    fields = {"zeros": {"runs": [], "at": [], "read": 1000, "took": 0.1, "count": []}, "size": 1000, "pcm": False, "fault": None,
              "ffmpeg": {"ran": True, "lines": 108, "pos": [], "at": [], "loose": 108, "kinds": {"video": 108}, "audio": []}}
    monkeypatch.setattr(hook, "whole_check", lambda path, stop: (None, [], ["a full read of the file found 108 errors"], json.loads(json.dumps(fields))))
    whole = hook.whole_act(None, {"app": "radarr", "path": env["path"]}, {"id": "x", "apply": True}, [])
    assert (whole["outcome"], "findings" in whole, "regrab" in whole) == ("damaged_source", False, False), whole
    assert whole["result"] == f"damaged source: {REMUX_DAMAGE['fault']}, marked damaged before: {DAMAGE['fault']}"
    assert posted(env) == [] and env["writes"] == [], posted(env)


def test_the_damage_mark_follows_a_remux_of_amg_itself(env):
    """A remux of AMG's own gives the file a new inode, and it keeps the video, so the mark moves to the new file that
    the swap proved, see process.swap_carry(). A mark of another stat moves nothing and stays as it is."""
    hook.damage_mark(env["path"], DAMAGE, hook.file_key(os.stat(env["path"])))
    before = os.stat(env["path"])
    os.replace(env["path"], env["path"] + ".tmp")
    shutil.copy(env["path"] + ".tmp", env["path"])
    hook.swap_carry(env["path"], before, {"file_key": list(hook.file_key(os.stat(env["path"])))})
    assert hook.damaged(env["path"]) == DAMAGE and hook.store.get(hook.DAMAGED, env["path"])["key"] == hook.mark_key(os.stat(env["path"]))
    stale = hook.store.get(hook.DAMAGED, env["path"])
    hook.carry_marks(env["path"], os.stat(env["path"] + ".tmp"))
    assert hook.store.get(hook.DAMAGED, env["path"]) == stale
    with open(env["path"], "ab") as f:   # the mark holds the old size and mtime now
        f.write(b"x")
    hook.carry_marks(env["path"], os.stat(env["path"] + ".tmp"))
    assert hook.store.get(hook.DAMAGED, env["path"]) == stale and hook.damaged(env["path"]) is None


def test_a_subtitle_remux_that_fails_another_way_posts_as_before(env, monkeypatch, settings, tmp_path):
    """A remux that fails for any other reason keeps its "Rewrite failed" alert, and the sidecar fix still runs."""
    side, reads = remux_film(env, monkeypatch, settings, tmp_path, {"warnings": None})
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] != "damaged_source" and "regrab" not in rec and rec["alert_kinds"] == ["subtiming"], rec
    assert [t for t, _ in posted(env)] == ["Rewrite failed"] and rec["sidecars"][0]["result"] == "retimed", posted(env)
    assert reads == [] and hook.damaged(env["path"]) is None


def test_a_deep_analysis_whose_remux_cannot_read_the_original_posts_as_before(env, monkeypatch, settings):
    """Only an import and the job of a backfill act on the damage their remux finds. A deep analysis of an unmarked
    file keeps its subtitle alert, and it never re-grabs."""
    one_alert_film(env, monkeypatch, settings, "fixed")
    hook.run_job(enqueue(env, 0, env["path"]), [])
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None, timed=None: (
        "subtitle_remux_failed", f"subtitle remux failed: ffmpeg did not read x cleanly: {BAD_READ}"[:500], {"warnings": None, "damage": REMUX_DAMAGE}))
    hook.main([])
    _, (deep,) = two_lines(env)
    assert deep["outcome"] != "damaged_source" and "regrab" not in deep and deep["subremux"]["damage"] == REMUX_DAMAGE, deep
    assert [t for t, _ in posted(env)] == ["Rewrite failed"] and hook.damaged(env["path"]) is None, posted(env)
